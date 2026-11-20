"""端到端演示（合成平面轨迹，不控制任何实体设备）。

第一部分按 materials/728 的资料与 operations.jsonl 复现：
  r1: A/B 途中相遇 (4, 6) 严格开区间；A/C 仅 y 向间距 3，半高和 2，不冲突；
  r2: 半高修订为 2 后 A/C 半高和 4，在整个 [0, 10) 冲突；旧报告 R1 保留 r1 尺寸。

第二部分在独立内存库中补充：等待、不同断点、零相对速度、恰好接触、
相邻冲突合并以及重开（断点把一个冲突切成两段后由库合并回带证据的单区间）。
"""

from __future__ import annotations

import sqlite3
from fractions import Fraction
from pathlib import Path
from typing import List, Sequence, Tuple

from cart_audit import (
    AuditLibrary,
    MotionStore,
    Profile,
    ProfileCatalog,
    OperationRunner,
    load_motions,
    load_profile_revision,
)
from cart_audit.models import Knot, Trajectory
from cart_audit.verifier import verify_report

MATERIALS = Path(__file__).parent / "materials" / "728"
Q = Fraction


def line(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_pair(result: dict) -> None:
    pair = f"{result['cart_a']}/{result['cart_b']}"
    if result["passed"]:
        print(f"  {pair}: 可通过，累计重叠 0")
        return
    print(
        f"  {pair}: 不可通过，累计重叠时长 = {result['total_overlap_duration']}，"
        f"冲突区间 {len(result['merged_intervals'])} 个"
    )
    # audit_pair 直接返回 evidence；read_report 的持久化结构使用 conflicts。
    conflicts = result.get("evidence") or result.get("conflicts") or []
    for conflict in conflicts:
        iv = conflict["interval"]
        edges = (
            f"{'[' if iv['left_inclusive'] else '('}{iv['left']}, "
            f"{iv['right']}{']' if iv['right_inclusive'] else ')'}"
        )
        print(f"    正面积重叠 {edges}  时长 {iv['duration']}")
        for ev in conflict["sources"] if "sources" in conflict else conflict["evidence"]:
            print(
                f"      证据 cell [{ev['cell'][0]}, {ev['cell'][1]}) "
                f"动作段 {result['cart_a']}#{ev['seg_a_index']} x "
                f"{result['cart_b']}#{ev['seg_b_index']}"
            )


# --------------------------------------------------------------------- #
# 小型场景装配器（每个补充场景一个独立内存库，互不污染）
# --------------------------------------------------------------------- #
def build_scenario(
    profiles: List[Profile],
    horizon: Tuple[Fraction, Fraction],
    carts: Sequence[Trajectory],
) -> Tuple[ProfileCatalog, MotionStore, AuditLibrary]:
    conn = sqlite3.connect(":memory:")
    catalog = ProfileCatalog(conn)
    store = MotionStore(conn)
    catalog.put_revision("r", profiles)
    store.set_horizon(*horizon)
    for traj in carts:
        store.upsert_trajectory(traj)
    # 报告表与目录/轨迹共享同一内存连接。
    library = AuditLibrary(catalog, store, conn=conn)
    return catalog, store, library


def traj(cart_id: str, knots: Sequence[tuple], horizon: Tuple[Fraction, Fraction]) -> Trajectory:
    return Trajectory(
        cart_id=cart_id,
        profile_id="P",
        knots=[Knot(Q(t), Q(x), Q(y)) for t, x, y in knots],
        horizon=horizon,
    )


# --------------------------------------------------------------------- #
def main() -> None:
    line("一、materials/728 主场景（r1 -> 修订 r2 -> 旧报告保留）")
    db_path = str(Path(__file__).parent / "cart_audit_demo.sqlite3")
    Path(db_path).unlink(missing_ok=True)
    conn = sqlite3.connect(db_path)
    catalog = ProfileCatalog(conn)
    store = MotionStore(conn)

    revision_r1, profiles_r1 = load_profile_revision(MATERIALS / "profiles.json")
    catalog.put_revision(revision_r1, profiles_r1)
    (h0, h1), trajectories = load_motions(MATERIALS / "motions.json")
    store.set_horizon(h0, h1)
    for t in trajectories:
        store.upsert_trajectory(t)

    library = AuditLibrary(catalog, store, db_path)
    runner = OperationRunner(catalog, store, library, MATERIALS)

    print("\n[操作流 operations.jsonl]")
    results = runner.run_file(MATERIALS / "operations.jsonl")
    for item in results:
        if item["op"] == "revise_profiles":
            print(f"  -> 轮廓修订 {item['revision']} 已冻结: {item['profiles']}")
        elif item["op"] in ("audit", "read_report"):
            report = item["result"]
            verdict = "可通过" if report["passed"] else "不可通过"
            print(f"  -> {item['op']} {report['report_id']}（修订 {report['revision']}）：{verdict}")
            for pair in report["pairs"]:
                show_pair(pair)

    line("主场景断言与独立复核")
    r1 = library.read_report("R1")
    r2 = library.read_report("R2")
    ab = next(p for p in r1["pairs"] if p["cart_a"] == "A" and p["cart_b"] == "B")
    ac = next(p for p in r1["pairs"] if p["cart_a"] == "A" and p["cart_b"] == "C")
    assert r1["revision"] == "r1" and r1["passed"] is False
    assert len(ab["merged_intervals"]) == 1
    iv = ab["merged_intervals"][0]
    assert (iv["left"], iv["right"], iv["left_inclusive"], iv["right_inclusive"]) == (
        "4", "6", False, False
    ), iv
    assert iv["duration"] == "2"
    assert ac["passed"] is True and ac["total_overlap_duration"] == "0"
    ac2 = r2["pairs"][0]
    assert r2["revision"] == "r2" and r2["passed"] is False
    assert ac2["merged_intervals"][0] == {
        "left": "0", "right": "10",
        "left_inclusive": True, "right_inclusive": False, "duration": "10",
    }
    # 旧报告保留 r1 尺寸：快照里的 half_height 仍是 1。
    frozen = r1["snapshot"]["revision_payload"]["profiles"][0]
    assert frozen["half_height"] == "1"
    print("  R1: A/B = (4, 6) 两端开、时长 2；A/C 可通过；R1 冻结 half_height=1")
    print("  R2: A/C = [0, 10) 时长 10，不可通过；R1 复读仍为原结论")
    for rid in ("R1", "R2"):
        verdict = verify_report(library, rid)
        assert verdict["verified"], verdict
        print(f"  独立复核 {rid}: verified={verdict['verified']}")

    # 幂等：同内容复读；异文拒绝
    repeat = library.audit("R1", [["A", "B"], ["A", "C"]], "r1")
    assert repeat["input_hash"] == r1["input_hash"]
    try:
        library.audit("R1", [["A", "B"]], "r1")
    except Exception as exc:
        print(f"  异文复读 R1 被拒绝: {type(exc).__name__}")
    else:
        raise AssertionError("异文复读必须被拒绝")

    # 独立查询：轮廓 + 一对单段重叠
    line("独立查询接口（轮廓 / 单段重叠 / 子时段整车对）")
    print("  query_profile r1/P =", library.query_profile("r1", "P"))
    single = library.query_pair_segment("r1", "A", "B", 0, 0)
    assert single["overlap"] == {
        "left": "4", "right": "6",
        "left_inclusive": False, "right_inclusive": False, "duration": "2",
    }
    print("  A#0 x B#0 单段重叠 =", single["overlap"])
    sub = library.audit_pair("r1", "A", "B", Q(5), Q(10))
    assert sub["merged_intervals"][0]["left"] == "5"
    assert sub["merged_intervals"][0]["right"] == "6"
    print("  A/B 子时段 [5, 10) 冲突 =", sub["merged_intervals"])

    # ------------------------------------------------------------------ #
    line("二、补充场景 1：等待（W 静止等待 M 通过，含不同断点细分）")
    H = (Q(0), Q(12))
    _, _, lib = build_scenario(
        [Profile("P", 1, 1)],
        H,
        [
            traj("W", [(0, 0, 0), (4, 0, 0), (8, 0, 0), (12, 0, 0)], H),
            # M 的断点 3/7/9 与 W 的 4/8 互不重合，强制走共同细分
            traj("M", [(0, -6, 0), (3, -3, 0), (7, 1, 0), (9, 3, 0), (12, 6, 0)], H),
        ],
    )
    res = lib.audit_pair("r", "W", "M")
    show_pair(res)
    # M 分段线性且在共同细分上连续：x_M(t) = t-6（0<=t<=7 段），
    # W 恒在 x=0，|x_M| < 2 => 4 < t < 8；t=4/8 触边排除，故两端开。
    iv = res["merged_intervals"][0]
    assert (iv["left"], iv["right"], iv["left_inclusive"], iv["right_inclusive"]) == (
        "4", "8", False, False
    ) and iv["duration"] == "4"
    assert len(res["evidence"][0]["sources"]) >= 2  # 跨了共同细分的多个 cell

    line("补充场景 2：零相对速度——同速偏移恒定，全程冲突 / 全程不冲突")
    _, _, lib = build_scenario(
        [Profile("P", 1, 1)],
        H,
        [
            traj("Z1", [(0, 0, 0), (12, 12, 0)], H),
            traj("Z2", [(0, 1, 0), (12, 13, 0)], H),  # 恒距 1 < 半宽和 2
        ],
    )
    res = lib.audit_pair("r", "Z1", "Z2")
    show_pair(res)
    assert res["merged_intervals"] == [{
        "left": "0", "right": "12",
        "left_inclusive": True, "right_inclusive": False, "duration": "12",
    }]
    _, _, lib = build_scenario(
        [Profile("P", 1, 1)],
        H,
        [
            traj("O1", [(0, 0, 0), (12, 12, 0)], H),
            traj("O2", [(0, 5, 0), (12, 17, 0)], H),  # 恒距 5 > 2
        ],
    )
    res = lib.audit_pair("r", "O1", "O2")
    show_pair(res)
    assert res["passed"] is True

    line("补充场景 3：恰好接触——只碰边/角，严格合同下零冲突")
    # 角接触：G1 静止于 (0,0)，G2 沿对角线从 (2,2) 退到 (4,4) 再返回，
    # t=5 时中心恰在 (2,2)：x、y 两方向都恰好等于半尺寸和 2，只共一个角点。
    _, _, lib = build_scenario(
        [Profile("P", 1, 1)],
        (Q(0), Q(10)),
        [
            traj("G1", [(0, 0, 0), (10, 0, 0)], (Q(0), Q(10))),
            traj("G2", [(0, 4, 4), (5, 2, 2), (10, 4, 4)], (Q(0), Q(10))),
        ],
    )
    res = lib.audit_pair("r", "G1", "G2")
    show_pair(res)
    assert res["passed"] is True and res["total_overlap_duration"] == "0"
    # 边接触：E1 静止，E2 在 t=5 时 x 差恰为 2（半宽和），y 恒等，只碰一条边
    _, _, lib = build_scenario(
        [Profile("P", 1, 1)],
        (Q(0), Q(10)),
        [
            traj("E1", [(0, 0, 0), (10, 0, 0)], (Q(0), Q(10))),
            traj("E2", [(0, 7, 0), (5, 2, 0), (10, 7, 0)], (Q(0), Q(10))),
        ],
    )
    res = lib.audit_pair("r", "E1", "E2")
    show_pair(res)
    assert res["passed"] is True and res["total_overlap_duration"] == "0"
    single = lib.query_pair_segment("r", "E1", "E2", 0, 0)
    assert single["overlap"] is None  # 单段独立查询同样排除触边
    print("  单段查询 E1#0 x E2#0：overlap = None（t=5 触边不算）")

    line("补充场景 4：不同断点把一个冲突切成两段，库合并并保留全部证据")
    _, _, lib = build_scenario(
        [Profile("P", 1, 1)],
        (Q(0), Q(10)),
        [
            # K1 在 t=5 有断点但速度方向不变；K2 连续。
            # x1-x2 = 2t-10，|.| < 2 => 真实冲突 (4, 6)，断点 t=5 切开证据。
            traj("K1", [(0, 0, 0), (5, 5, 0), (10, 10, 0)], (Q(0), Q(10))),
            traj("K2", [(0, 10, 0), (10, 0, 0)], (Q(0), Q(10))),
        ],
    )
    res = lib.audit_pair("r", "K1", "K2")
    show_pair(res)
    assert len(res["merged_intervals"]) == 1
    iv = res["merged_intervals"][0]
    assert (iv["left"], iv["right"]) == ("4", "6") and iv["duration"] == "2"
    assert len(res["evidence"][0]["sources"]) == 2  # 两个 cell 的证据都在
    # 批次发布后证据同样可独立复核
    report = lib.audit("RMERGE", [["K1", "K2"]], "r")
    assert verify_report(lib, "RMERGE")["verified"]
    print("  批次 RMERGE 已发布且复核通过，证据条数 =",
          len(report["pairs"][0]["conflicts"][0]["evidence"]))

    line("补充场景 5：重开——冲突后分离再相遇，两个区间不合并")
    _, _, lib = build_scenario(
        [Profile("P", "1", "1")],
        (Q(0), Q(10)),
        [
            traj("R1c", [(0, 0, 0), (10, 0, 0)], (Q(0), Q(10))),
            # 两次靠近、中间在 t=5 处退到 x=3（间距 > 2）
            traj("R2c", [(0, 5, 0), (2, 1, 0), (5, 3, 0), (8, 1, 0), (10, 5, 0)], (Q(0), Q(10))),
        ],
    )
    res = lib.audit_pair("r", "R1c", "R2c")
    show_pair(res)
    assert len(res["merged_intervals"]) == 2
    # 手算：R2c 段 0..2 上 x=5-2t，|x|<2 => 3/2 < t < 7/2；
    # 段 5..8 上 x 从 3 线性到 1，再到段 8..10，对称得 13/2 < t < 17/2。
    assert [(iv["left"], iv["right"]) for iv in res["merged_intervals"]] == [
        ("3/2", "7/2"), ("13/2", "17/2"),
    ]
    assert all(not iv["left_inclusive"] and not iv["right_inclusive"]
               for iv in res["merged_intervals"])

    line("补充场景 6：非法输入拒绝与失败不发布")
    try:
        Profile("bad", 0, 1)
    except ValueError as exc:
        print("  非法轮廓被拒绝:", exc)
    try:
        Profile("bad", "1/0", 1)
    except ValueError as exc:
        print("  非法有理数被拒绝:", exc)
    try:
        traj("X", [(0, 0, 0), (5, 1, 0), (5, 2, 0)], (Q(0), Q(10)))
    except ValueError as exc:
        print("  非严格递增结点被拒绝:", exc)
    try:
        traj("X", [(1, 0, 0), (10, 9, 0)], (Q(0), Q(10)))
    except ValueError as exc:
        print("  未完整覆盖核查域被拒绝:", exc)
    report_before = set(lib.list_reports())
    try:
        lib.audit("RFAIL", [["R1c", "GHOST"]], "r")
    except Exception as exc:
        print("  引用未知车的核查被拒绝:", type(exc).__name__, exc)
    assert set(lib.list_reports()) == report_before
    print("  失败后报告列表不变（无“可通过”外观发布）:", lib.list_reports())
    print("  audit_attempt 留痕:", [a["reason"].split(":")[0] for a in lib.list_attempts() if not a["ok"]])

    line("全部演示完成")
    print(f"持久化库: {db_path}（可重复打开独立查询；内存场景随进程结束释放）")
    library.close()
    conn.close()


if __name__ == "__main__":
    main()
