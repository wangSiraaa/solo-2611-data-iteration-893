"""格面受墨量与纸张配给端到端演示（虚构印模，离线复核，不控制实体设备）。

复现备忘录里的手算：
  - 单格 [0,1]×[0,1]，静止、半尺寸 1/2、流墨率 1、时限 4：
    中心 (5/4,1/2) -> 墨量 1；中心 (3/4,1/2) -> 墨量 3；两版都单印模、无重叠。
  - 斜向平移让交叠面积在同一动作段内改变：精确凭单 1/3。
  - 薄纸仅收 [0,2]、厚纸仅收 [2,4]：后一版（墨量 3）只能用厚纸；
    厚纸被更早的确认领完则该版无法交付。
  - 陈旧库存上确认 -> 失败方看到短缺纸种，补货后用新库存重算、再确认。
  - 并发确认最后一张只有一方成功。
  - 旧凭单在轮廓修订、轨迹改写、格网更换后仍可独立重算。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
from fractions import Fraction
from pathlib import Path

from cart_audit.catalog import MotionStore, ProfileCatalog
from cart_audit.ink import Cell, InkLedger, ProofPlate, Stamp, verify_voucher
from cart_audit.library import AuditLibrary
from cart_audit.paper import (
    PaperShortage,
    PaperStore,
    PlateSpec,
    allocate,
)
from cart_audit.runner import (
    OperationRunner,
    load_motions,
    load_plate,
    load_plate_specs,
    load_profile_revision,
)

MATERIALS = Path(__file__).parent / "materials" / "ink"
Q = Fraction


def line(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> None:
    line("一、受墨量凭单（materials/ink）")
    db_path = tempfile.mktemp(suffix=".sqlite3")
    conn = sqlite3.connect(db_path)
    catalog = ProfileCatalog(conn)
    store = MotionStore(conn)
    revision, profiles = load_profile_revision(MATERIALS / "profiles.json")
    catalog.put_revision(revision, profiles)
    (h0, h1), trajectories = load_motions(MATERIALS / "motions.json")
    store.set_horizon(h0, h1)
    for traj in trajectories:
        store.upsert_trajectory(traj)
    library = AuditLibrary(catalog, store, db_path)
    ledger = InkLedger(catalog, store, conn=conn)

    # 三张试版分别只有一只印模；early/late 是备忘录里的两个静止摆放。
    for voucher_id, plate_file in (
        ("V-early", "plate_early.json"),
        ("V-late", "plate_late.json"),
        ("V-diag", "plate_diag.json"),
    ):
        plate = load_plate(MATERIALS / plate_file)
        voucher = ledger.put_voucher(voucher_id, plate, revision)
        cell = voucher["cells"][0]
        detail = ", ".join(
            f"{s['cart_id']} 段{s['segments'][0]['segment_index']} "
            f"[{s['segments'][0]['seg_t0']},{s['segments'][0]['seg_t1']}) = {s['segments'][0]['ink']}"
            for s in cell["stamps"]
        )
        print(f"  {voucher_id}: plate={voucher['plate_id']} 格 {cell['cell']['cell_id']} "
              f"总量 {cell['total']}  ({detail})")
        assert verify_voucher(ledger, voucher_id)["verified"]

    early_total = ledger.read_voucher("V-early")["cells"][0]["total"]
    late_total = ledger.read_voucher("V-late")["cells"][0]["total"]
    diag_total = ledger.read_voucher("V-diag")["cells"][0]["total"]
    assert (early_total, late_total, diag_total) == ("1", "3", "1/3")
    print("  备忘录核对：中心 (5/4,1/2) -> 1；中心 (3/4,1/2) -> 3；斜向 -> 1/3")

    line("二、轮廓修订 / 轨迹改写 / 换格网不追溯旧凭单")
    rev2, profs2 = load_profile_revision(MATERIALS / "profiles_revised.json")
    catalog.put_revision(rev2, profs2)
    assert ledger.read_voucher("V-early")["cells"][0]["total"] == "1"
    assert verify_voucher(ledger, "V-early")["verified"]
    # 换一个更大的格、同一印模，墨量随之改变（相同长短的格可有不同墨量）。
    big_plate = ProofPlate(
        "big", [Cell("big", Q(0), Q(0), Q(3), Q(1))], {"big": [Stamp("S", Q(1))]}
    )
    big = ledger.put_voucher("V-big", big_plate, revision)
    assert big["cells"][0]["total"] == "4"
    print(f"  修订 {rev2} 后旧凭单 V-early 仍为 {early_total}，复核通过；换格网另算 = {big['cells'][0]['total']}")

    line("三、纸张配给（闭区间门槛 + 权重优先 + 原子确认）")
    paper_db = tempfile.mktemp(suffix=".sqlite3")
    papers = PaperStore(paper_db)
    papers.register_paper("thin", 1)
    papers.register_paper("thick", 1)
    specs = load_plate_specs(MATERIALS / "plates.json")
    feasible = {p.plate_id: p.feasible_papers() for p in specs}
    print("  可用纸种：", feasible)
    result = papers.allocate_batch(specs)
    print("  配给：", result.as_dict())
    assert dict(result.assignments)["late"] == "thick"  # 墨量 3 只能厚纸
    assert dict(result.assignments)["early"] == "thin"
    papers.confirm("CONFIRM-1", result)
    assert papers.stocks() == {"thick": 0, "thin": 0}
    print("  确认 CONFIRM-1：厚纸/薄纸余量 ->", papers.stocks())
    # 同一确认重复提交不重复扣减。
    papers.confirm("CONFIRM-1", result)
    print("  重复提交 CONFIRM-1 不重复扣减，余量仍为", papers.stocks())

    line("四、厚纸已被更早确认领完 -> 后一版无法交付；补货后用新库存重算")
    stale = allocate(specs, {"thin": 1, "thick": 1})  # 基于陈旧假设的旧计划
    try:
        papers.confirm("CONFIRM-2", stale)
    except PaperShortage as exc:
        print("  失败方看到的短缺：", exc.shortages)
        assert {"thick", "thin"} <= set(exc.shortages)
    reallocated = papers.allocate_batch(specs)  # 用真实库存重算
    print("  用新库存重算：", reallocated.as_dict())
    assert reallocated.allocated_count == 0
    papers.add_stock("thick", 1)
    papers.add_stock("thin", 1)
    recomputed = papers.allocate_batch(specs)
    print("  补货后重算：", recomputed.as_dict())
    assert dict(recomputed.assignments)["late"] == "thick"
    papers.confirm("CONFIRM-3", recomputed)
    print("  确认 CONFIRM-3 后余量：", papers.stocks())

    line("五、并发确认最后一张：只有一方成功")
    papers.add_stock("thick", 1)
    papers.add_stock("thin", 1)
    plan = papers.allocate_batch(specs)
    contenders = [PaperStore(paper_db) for _ in range(2)]
    outcomes = []
    barrier = threading.Barrier(2)

    def contender(store, cid):
        barrier.wait()
        try:
            store.confirm(cid, plan)
            outcomes.append(("ok", cid))
        except PaperShortage as exc:
            outcomes.append(("short", cid, exc.shortages))

    threads = [
        threading.Thread(target=contender, args=(contenders[i], f"RACE-{i}"))
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for store in contenders:
        store.close()
    print("  并发结果：", outcomes)
    assert len([o for o in outcomes if o[0] == "ok"]) == 1
    assert papers.stocks() == {"thick": 0, "thin": 0}

    line("六、旧印样固定原用纸与墨量凭单")
    sample = papers.record_sample("SAMPLE-early", "early", "thin", "V-early")
    print("  旧印样：", sample)
    assert sample["paper_type_id"] == "thin" and sample["voucher_id"] == "V-early"

    line("七、操作流（operations.jsonl）贯穿同一库")
    runner = OperationRunner(catalog, store, library, MATERIALS, ink_ledger=ledger)
    for item in runner.run_file(MATERIALS / "operations.jsonl"):
        if item["op"] == "read_voucher":
            cell = item["result"]["cells"][0]
            print(f"  read_voucher {item['voucher_id']} -> 总量 {cell['total']}")

    library.close()
    conn.close()
    papers.close()
    for path in (db_path, paper_db):
        if os.path.exists(path):
            os.remove(path)
    line("全部演示完成（临时库已清理）")


if __name__ == "__main__":
    main()
