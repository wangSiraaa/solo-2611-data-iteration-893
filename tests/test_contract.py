"""契约级单元测试：严格接触、共同细分、版本冻结与报告幂等。"""

import sqlite3
import unittest
from fractions import Fraction

from cart_audit import (
    AuditInputError,
    AuditLibrary,
    AuditRejected,
    InputError,
    MotionStore,
    Profile,
    ProfileCatalog,
    ProfileOverlapService,
    rational_json_loads,
)
from cart_audit.intervals import TimeInterval, intersect_intervals, merge_union
from cart_audit.models import Knot, Trajectory
from cart_audit.rational import fraction, qstr
from cart_audit.verifier import verify_report

Q = Fraction


def profile(pid="P", w=1, h=1) -> Profile:
    return Profile(pid, Q(w), Q(h))


def make_traj(cart_id, points, horizon=(Q(0), Q(10)), pid="P"):
    return Trajectory(
        cart_id=cart_id,
        profile_id=pid,
        knots=[Knot(Q(t), Q(x), Q(y)) for t, x, y in points],
        horizon=horizon,
    )


def new_library(revisions=None, trajectories=None, horizon=(Q(0), Q(10))):
    conn = sqlite3.connect(":memory:")
    catalog = ProfileCatalog(conn)
    store = MotionStore(conn)
    for name, profs in (revisions or [("r1", [profile()])]):
        catalog.put_revision(name, profs)
    store.set_horizon(*horizon)
    for traj in trajectories or []:
        store.upsert_trajectory(traj)
    return AuditLibrary(catalog, store, conn=conn), catalog, store


class RationalTests(unittest.TestCase):
    def test_exact_parsing(self):
        self.assertEqual(fraction(3), Q(3))
        self.assertEqual(fraction("3/2"), Q(3, 2))
        self.assertEqual(fraction("1.25"), Q(5, 4))
        self.assertEqual(fraction(Q(1, 7)), Q(1, 7))
        self.assertEqual(qstr(Q(4, 6)), "2/3")
        self.assertEqual(qstr(Q(5)), "5")

    def test_reject_inexact(self):
        for bad in (1.5, True, False, "", "1/0", "abc", None):
            with self.assertRaises(ValueError):
                fraction(bad)

    def test_json_float_literal_rejected(self):
        with self.assertRaises(ValueError):
            rational_json_loads('{"half_width": 1.5}')
        data = rational_json_loads('{"half_width": 1, "tag": "p/q"}')
        self.assertEqual(data["half_width"], Q(1))
        self.assertEqual(data["tag"], "p/q")


class ModelTests(unittest.TestCase):
    def test_profile_must_be_positive(self):
        with self.assertRaises(ValueError):
            Profile("P", Q(0), Q(1))
        with self.assertRaises(ValueError):
            Profile("P", Q(-2), Q(1))
        with self.assertRaises(ValueError):
            Profile("", Q(1), Q(1))

    def test_knots_strictly_increasing_and_full_coverage(self):
        with self.assertRaises(ValueError):
            make_traj("X", [(0, 0, 0), (5, 1, 0), (5, 2, 0)])
        with self.assertRaises(ValueError):
            make_traj("X", [(1, 0, 0), (10, 9, 0)])  # 起点缺段
        with self.assertRaises(ValueError):
            make_traj("X", [(0, 0, 0), (9, 9, 0)])  # 终点缺段
        traj = make_traj("X", [(0, 0, 0), (10, 10, 0)])
        self.assertEqual(traj.position_at(Q(5)), (Q(5), Q(0)))
        with self.assertRaises(ValueError):
            traj.position_at(Q(11))  # 不外推


class IntervalTests(unittest.TestCase):
    def test_intersection_endpoint_flags(self):
        open_iv = TimeInterval(Q(4), Q(6), False, False)
        cell = TimeInterval(Q(0), Q(10), True, False)
        got = intersect_intervals(open_iv, cell)
        self.assertEqual(got, open_iv)
        touch = intersect_intervals(
            TimeInterval(Q(0), Q(5), True, False),
            TimeInterval(Q(5), Q(9), False, False),
        )
        self.assertIsNone(touch)  # 共享点两侧皆开 => 空
        closed_touch = intersect_intervals(
            TimeInterval(Q(0), Q(5), True, True),
            TimeInterval(Q(5), Q(9), True, False),
        )
        # 孤立闭点会被相交规则折叠为空（对正面积重叠零贡献）
        self.assertIsNone(closed_touch)

    def test_merge_union_keeps_open_gap(self):
        merged = merge_union([
            TimeInterval(Q(1), Q(5), False, False),
            TimeInterval(Q(5), Q(9), False, False),
        ])
        self.assertEqual(len(merged), 2)  # (1,5) 与 (5,9) 在 5 处断开
        merged2 = merge_union([
            TimeInterval(Q(1), Q(5), False, True),
            TimeInterval(Q(5), Q(9), True, False),
        ])
        self.assertEqual(merged2, [TimeInterval(Q(1), Q(9), False, False)])
        # 完全包含
        merged3 = merge_union([
            TimeInterval(Q(0), Q(10)),
            TimeInterval(Q(2), Q(4)),
        ])
        self.assertEqual(merged3, [TimeInterval(Q(0), Q(10))])


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.svc = ProfileOverlapService()

    def overlap(self, ta, tb, pa=None, pb=None):
        pa = pa or profile()
        pb = pb or profile()
        return self.svc.segment_overlap(pa, ta.segments()[0], pb, tb.segments()[0])

    def test_strict_edge_contact_excluded(self):
        # 恰在端点 t=5 触边：无正面积重叠
        a = make_traj("A", [(0, 0, 0), (10, 0, 0)])
        b = make_traj("B", [(0, 7, 0), (5, 2, 0), (10, 7, 0)])
        self.assertIsNone(
            self.svc.segment_overlap(profile(), a.segments()[0], profile(), b.segments()[0])
        )

    def test_crossing_open_interval(self):
        a = make_traj("A", [(0, 0, 0), (10, 10, 0)])
        b = make_traj("B", [(0, 10, 0), (10, 0, 0)])
        iv = self.overlap(a, b)
        self.assertEqual(iv, TimeInterval(Q(4), Q(6), False, False))
        self.assertEqual(iv.duration, Q(2))

    def test_zero_relative_velocity(self):
        a = make_traj("A", [(0, 0, 0), (10, 10, 0)])
        b_close = make_traj("B", [(0, 1, 0), (10, 11, 0)])
        iv = self.overlap(a, b_close)
        self.assertEqual(iv, TimeInterval(Q(0), Q(10), True, False))
        b_far = make_traj("B", [(0, 5, 0), (10, 15, 0)])
        self.assertIsNone(self.overlap(a, b_far))

    def test_both_stationary(self):
        a = make_traj("A", [(0, 0, 0), (10, 0, 0)])
        b = make_traj("B", [(0, 1, 1), (10, 1, 1)])
        iv = self.overlap(a, b)
        self.assertEqual(iv, TimeInterval(Q(0), Q(10), True, False))
        c = make_traj("C", [(0, 0, 3), (10, 0, 3)])
        # y 向间距 3 > 半高和 2：单方向接触失败 => 整体不冲突
        self.assertIsNone(self.overlap(a, c))

    def test_one_axis_contact_only(self):
        # x 向全程重叠，y 向恒距恰为半高和（只碰一条边）
        a = make_traj("A", [(0, 0, 0), (10, 0, 0)])
        b = make_traj("B", [(0, 0, 2), (10, 0, 2)])
        self.assertIsNone(self.overlap(a, b))

    def test_corner_contact_excluded(self):
        # t=5 时两方向中心差都恰为半尺寸和：只共一个角点
        a = make_traj("G1", [(0, 0, 0), (10, 0, 0)])
        b = make_traj("G2", [(0, 4, 4), (5, 2, 2), (10, 4, 4)])
        for seg_b in b.segments():
            self.assertIsNone(
                self.svc.segment_overlap(profile(), a.segments()[0], profile(), seg_b)
            )

    def test_rational_slope_and_roots(self):
        # 半宽 1/2，两车相对运动产生分数根 t=1/6, 5/6
        pa = Profile("P", Q(1, 2), Q(1))
        a = make_traj("A", [(0, 0, 0), (1, 0, 0)], horizon=(Q(0), Q(1)))
        b = make_traj("B", [(0, -1, 0), (1, 2, 0)], horizon=(Q(0), Q(1)))
        iv = self.overlap(a, b, pa=pa, pb=pa)
        # |3t-1| < 1：t=0 处 |d|=1 恰为根点（触边），左端必须开；右端根 2/3 开
        self.assertEqual(iv, TimeInterval(Q(0), Q(2, 3), False, False))


class AuditLibraryTests(unittest.TestCase):
    def _materials_setup(self):
        lib, catalog, store = new_library(
            revisions=[("r1", [profile("P", 1, 1)]), ("r2", [profile("P", 1, 2)])],
            trajectories=[
                make_traj("A", [(0, 0, 0), (10, 10, 0)]),
                make_traj("B", [(0, 10, 0), (10, 0, 0)]),
                make_traj("C", [(0, 0, 3), (10, 10, 3)]),
            ],
        )
        return lib, catalog

    def test_materials_r1_r2_and_frozen_snapshot(self):
        lib, _ = self._materials_setup()
        r1 = lib.audit("R1", [["A", "B"], ["A", "C"]], "r1")
        self.assertFalse(r1["passed"])
        ab, ac = r1["pairs"]
        self.assertEqual(ab["merged_intervals"][0]["left"], "4")
        self.assertEqual(ab["merged_intervals"][0]["right"], "6")
        self.assertFalse(ab["merged_intervals"][0]["left_inclusive"])
        self.assertTrue(ac["passed"])
        r2 = lib.audit("R2", [["A", "C"]], "r2")
        self.assertFalse(r2["passed"])
        self.assertEqual(r2["pairs"][0]["total_overlap_duration"], "10")
        # 旧报告保留 r1 尺寸
        self.assertEqual(
            r1["snapshot"]["revision_payload"]["profiles"][0]["half_height"], "1"
        )
        self.assertEqual(
            r2["snapshot"]["revision_payload"]["profiles"][0]["half_height"], "2"
        )
        self.assertTrue(verify_report(lib, "R1")["verified"])
        self.assertTrue(verify_report(lib, "R2")["verified"])

    def test_idempotent_replay_and_divergent_reject(self):
        lib, _ = self._materials_setup()
        first = lib.audit("R1", [["A", "B"]], "r1")
        again = lib.audit("R1", [["A", "B"]], "r1")
        self.assertEqual(first["input_hash"], again["input_hash"])
        with self.assertRaises(AuditRejected):
            lib.audit("R1", [["A", "B"], ["A", "C"]], "r1")
        with self.assertRaises(AuditRejected):
            lib.audit("R1", [["A", "B"]], "r2")

    def test_subdivision_with_different_knots_and_merge(self):
        lib, _, _ = new_library(trajectories=[
            make_traj("K1", [(0, 0, 0), (5, 5, 0), (10, 10, 0)]),
            make_traj("K2", [(0, 10, 0), (10, 0, 0)]),
        ])
        res = lib.audit_pair("r1", "K1", "K2")
        self.assertEqual(len(res["merged_intervals"]), 1)
        iv = res["merged_intervals"][0]
        # x1-x2 = 2t-10，|.| < 2 => 4 < t < 6；断点 t=5 把证据切成两个 cell
        self.assertEqual((iv["left"], iv["right"], iv["duration"]), ("4", "6", "2"))
        self.assertEqual(len(res["evidence"][0]["sources"]), 2)

    def test_reopen_two_separate_conflicts(self):
        lib, _, _ = new_library(trajectories=[
            make_traj("R1c", [(0, 0, 0), (10, 0, 0)]),
            make_traj("R2c", [(0, 5, 0), (2, 1, 0), (5, 3, 0), (8, 1, 0), (10, 5, 0)]),
        ])
        res = lib.audit_pair("r1", "R1c", "R2c")
        self.assertEqual(len(res["merged_intervals"]), 2)

    def test_subrange_clips_to_requested_window(self):
        lib, _catalog = self._materials_setup()
        # 全域冲突为 (4, 6)；子窗口 [5, 10) 必须裁剪为 [5, 6)
        sub = lib.audit_pair("r1", "A", "B", Q(5), Q(10))
        self.assertEqual(len(sub["merged_intervals"]), 1)
        iv = sub["merged_intervals"][0]
        self.assertEqual((iv["left"], iv["right"], iv["left_inclusive"]), ("5", "6", True))
        # 窗口完全落在冲突之外
        empty = lib.audit_pair("r1", "A", "B", Q(0), Q(4))
        self.assertTrue(empty["passed"])

    def test_waiting_cart_grid_evidence(self):
        lib, _, _ = new_library(horizon=(Q(0), Q(12)), trajectories=[
            make_traj("W", [(0, 0, 0), (4, 0, 0), (8, 0, 0), (12, 0, 0)], (Q(0), Q(12))),
            make_traj("M", [(0, -6, 0), (3, -3, 0), (7, 1, 0), (9, 3, 0), (12, 6, 0)], (Q(0), Q(12))),
        ])
        res = lib.audit_pair("r1", "W", "M")
        iv = res["merged_intervals"][0]
        # M 分段线性：x_M(t)=t-6（0..7），|t-6| < 2 => 4 < t < 8
        self.assertEqual((iv["left"], iv["right"]), ("4", "8"))
        self.assertFalse(iv["left_inclusive"])
        self.assertGreaterEqual(len(res["evidence"][0]["sources"]), 2)

    def test_failure_publishes_nothing(self):
        lib, _, _ = new_library(trajectories=[make_traj("A", [(0, 0, 0), (10, 0, 0)])])
        before = set(lib.list_reports())
        with self.assertRaises((AuditInputError, InputError)):
            lib.audit("RFAIL", [["A", "GHOST"]], "r1")
        self.assertEqual(set(lib.list_reports()), before)
        with self.assertRaises(AuditInputError):
            lib.read_report("RFAIL")
        attempts = [a for a in lib.list_attempts() if not a["ok"]]
        self.assertTrue(attempts)

    def test_pair_dedup_and_segment_query(self):
        lib, _catalog = self._materials_setup()
        with self.assertRaises(AuditInputError):
            lib.audit("DUP", [["A", "B"], ["B", "A"]], "r1")
        single = lib.query_pair_segment("r1", "A", "B", 0, 0)
        self.assertEqual(single["overlap"]["left"], "4")
        self.assertEqual(single["overlap"]["right"], "6")
        # 不同修订独立查询
        single_r2 = lib.query_pair_segment("r2", "A", "C", 0, 0)
        self.assertEqual(single_r2["overlap"]["duration"], "10")

    def test_unknown_revision_and_profile_gap(self):
        lib, catalog = self._materials_setup()
        with self.assertRaises(InputError):
            catalog.put_revision("r1", [profile("P", 2, 2)])  # 异文拒绝
        with self.assertRaises(InputError):
            lib.audit("RX", [["A", "B"]], "nope")

    def test_tampered_report_fails_independent_verification(self):
        lib, _ = self._materials_setup()
        lib.audit("RT", [["A", "B"]], "r1")
        self.assertTrue(verify_report(lib, "RT")["verified"])
        # 外部直接篡改持久化的冲突右端点，复核必须发现
        lib.conn.execute("UPDATE conflict SET right = '7' WHERE report_id = 'RT'")
        lib.conn.commit()
        self.assertFalse(verify_report(lib, "RT")["verified"])
        # 篡改证据同样可被发现
        lib.conn.execute("UPDATE conflict SET right = '6' WHERE report_id = 'RT'")
        lib.conn.execute("DELETE FROM conflict_evidence")
        lib.conn.commit()
        self.assertFalse(verify_report(lib, "RT")["verified"])


class ScaleTests(unittest.TestCase):
    def test_20_carts_100_segments_all_pairs(self):
        # 题目上限：20 车、每车 100 段；全部 190 个车对一次批次核查。
        horizon = (Q(0), Q(100))
        carts = []
        for i in range(20):
            # 所有车 x 同向同速同位置；y 恒距为车道号 i，相邻车道 y 差 1。
            knots = [(Q(t), Q(t), Q(i)) for t in range(101)]
            carts.append(make_traj(f"C{i:02d}", knots, horizon=horizon))
        lib, _, _ = new_library(trajectories=carts, horizon=horizon)
        pairs = [[f"C{i:02d}", f"C{j:02d}"] for i in range(20) for j in range(i + 1, 20)]
        report = lib.audit("RBIG", pairs, "r1")
        self.assertEqual(len(report["pairs"]), 190)
        by_pair = {(p["cart_a"], p["cart_b"]): p for p in report["pairs"]}
        # 相邻车道 y 恒差 1 < 半高和 2 且 x 同速 => 全程冲突
        adjacent = by_pair[("C00", "C01")]
        self.assertFalse(adjacent["passed"])
        self.assertEqual(adjacent["total_overlap_duration"], "100")
        # 隔一车道 y 恒差 2 = 半高和：只碰边，严格合同下可通过
        self.assertTrue(by_pair[("C00", "C02")]["passed"])
        self.assertTrue(verify_report(lib, "RBIG")["verified"])
        # 证据指向 100 个细分 cell（两车结点一致）
        self.assertEqual(len(adjacent["conflicts"][0]["evidence"]), 100)


class CatalogTests(unittest.TestCase):
    def test_revision_idempotent_and_immutable(self):
        conn = sqlite3.connect(":memory:")
        catalog = ProfileCatalog(conn)
        catalog.put_revision("r1", [profile("P", 1, 1)])
        catalog.put_revision("r1", [profile("P", 1, 1)])  # 同内容复读
        with self.assertRaises(InputError):
            catalog.put_revision("r1", [profile("P", 1, 2)])
        got = catalog.get_profile("r1", "P")
        self.assertEqual((got.half_width, got.half_height), (Q(1), Q(1)))

    def test_duplicate_profile_id_rejected(self):
        conn = sqlite3.connect(":memory:")
        catalog = ProfileCatalog(conn)
        with self.assertRaises(InputError):
            catalog.put_revision("r", [profile("P", 1, 1), profile("P", 2, 2)])


if __name__ == "__main__":
    unittest.main()
