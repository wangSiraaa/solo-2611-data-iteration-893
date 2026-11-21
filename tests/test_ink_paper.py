"""受墨量凭单与纸张配给的契约级测试。

覆盖：备忘录两个手算值（1 与 3）、斜向平移段内面积改变、碰边零面积、
多印模相加、格网更换/轮廓修订后旧凭单可独立重算、纸张闭区间门槛、
权重→张数→纸种序列的字典序最优、原子确认只扣一次、并发争抢最后一张、
失败方看到短缺纸种并能用新库存重算、旧印样冻结原用纸与凭单。
"""

import os
import sqlite3
import tempfile
import threading
import unittest
from fractions import Fraction

from cart_audit.catalog import MotionStore, ProfileCatalog
from cart_audit.ink import (
    Cell,
    InkLedger,
    InkRejected,
    ProofPlate,
    Stamp,
    area_integral,
    verify_voucher,
)
from cart_audit.library import AuditLibrary
from cart_audit.models import Knot, MotionSegment, Profile, Trajectory
from cart_audit.paper import (
    ConfirmationRejected,
    PaperShortage,
    PaperStore,
    PlateSpec,
    allocate,
)

Q = Fraction


def make_traj(cart_id, points, horizon=(Q(0), Q(4)), pid="P"):
    return Trajectory(
        cart_id=cart_id,
        profile_id=pid,
        knots=[Knot(Q(t), Q(x), Q(y)) for t, x, y in points],
        horizon=horizon,
    )


def make_ledger(profiles, trajectories, horizon=(Q(0), Q(4)), revision="r"):
    conn = sqlite3.connect(":memory:")
    catalog = ProfileCatalog(conn)
    store = MotionStore(conn)
    catalog.put_revision(revision, profiles)
    store.set_horizon(*horizon)
    for traj in trajectories:
        store.upsert_trajectory(traj)
    return InkLedger(catalog, store, conn=conn), catalog, store


CELL = Cell("c", Q(0), Q(0), Q(1), Q(1))
PROF = Profile("P", Q(1, 2), Q(1, 2))


class AreaIntegralTests(unittest.TestCase):
    def test_memo_static_values(self):
        # 中心 (5/4,1/2)：印模宽 1，只有左半 [3/4,5/4] 与格 [0,1] 相交宽 1/2。
        seg = MotionSegment(0, Q(0), Q(4), Q(5, 4), Q(1, 2), Q(5, 4), Q(1, 2))
        self.assertEqual(area_integral(PROF, seg, CELL, Q(0), Q(4)), Q(1))
        # 中心 (3/4,1/2)：整宽 1 都在格内。
        seg2 = MotionSegment(0, Q(0), Q(4), Q(3, 4), Q(1, 2), Q(3, 4), Q(1, 2))
        self.assertEqual(area_integral(PROF, seg2, CELL, Q(0), Q(4)), Q(3))

    def test_diagonal_area_changes_within_segment(self):
        # 中心 (1/2,1/2) -> (3/2,3/2)，斜向平移，面积在同一动作段内改变。
        seg = MotionSegment(0, Q(0), Q(1), Q(1, 2), Q(1, 2), Q(3, 2), Q(3, 2))
        self.assertEqual(area_integral(PROF, seg, CELL, Q(0), Q(1)), Q(1, 3))

    def test_edge_touch_has_zero_area(self):
        # 中心 x=3/2：印模左缘正好在 x=1，只碰边，零面积。
        seg = MotionSegment(0, Q(0), Q(4), Q(3, 2), Q(1, 2), Q(3, 2), Q(1, 2))
        self.assertEqual(area_integral(PROF, seg, CELL, Q(0), Q(4)), Q(0))
        # 中心 y=3/2 同理。
        seg_y = MotionSegment(0, Q(0), Q(4), Q(1, 2), Q(3, 2), Q(1, 2), Q(3, 2))
        self.assertEqual(area_integral(PROF, seg_y, CELL, Q(0), Q(4)), Q(0))

    def test_stationary_outside_cell(self):
        seg = MotionSegment(0, Q(0), Q(4), Q(2), Q(2), Q(2), Q(2))
        self.assertEqual(area_integral(PROF, seg, CELL, Q(0), Q(4)), Q(0))


class InkLedgerTests(unittest.TestCase):
    def _ledger(self):
        ledger, cat, store = make_ledger(
            [PROF],
            [
                make_traj("S", [(0, "5/4", "1/2"), (4, "5/4", "1/2")]),
                make_traj("L", [(0, "3/4", "1/2"), (4, "3/4", "1/2")]),
                make_traj("D", [(0, "1/2", "1/2"), (1, "3/2", "3/2"), (4, "3/2", "3/2")]),
            ],
        )
        return ledger, cat, store

    def test_memo_vouchers_and_verify(self):
        ledger, _, _ = self._ledger()
        early = ProofPlate("early", [CELL], {"c": [Stamp("S", Q(1))]})
        late = ProofPlate("late", [CELL], {"c": [Stamp("L", Q(1))]})
        ve = ledger.put_voucher("V-early", early, "r")
        vl = ledger.put_voucher("V-late", late, "r")
        self.assertEqual(ve["cells"][0]["total"], "1")
        self.assertEqual(vl["cells"][0]["total"], "3")
        # 逐动作段可复核：单段 ink 与总量一致。
        self.assertEqual(ve["cells"][0]["stamps"][0]["segments"][0]["ink"], "1")
        self.assertTrue(verify_voucher(ledger, "V-early")["verified"])
        self.assertTrue(verify_voucher(ledger, "V-late")["verified"])

    def test_diagonal_voucher(self):
        ledger, _, _ = self._ledger()
        diag = ProofPlate("diag", [CELL], {"c": [Stamp("D", Q(1))]})
        v = ledger.put_voucher("V-diag", diag, "r")
        self.assertEqual(v["cells"][0]["total"], "1/3")
        self.assertTrue(verify_voucher(ledger, "V-diag")["verified"])

    def test_multiple_stamps_sum_on_one_cell(self):
        ledger, _, _ = self._ledger()
        plate = ProofPlate(
            "multi", [CELL],
            {"c": [Stamp("S", Q(1)), Stamp("L", Q(2))]},
        )
        v = ledger.put_voucher("V-multi", plate, "r")
        # S: 1 (rate 1) ; L: 3 area * rate 2 = 6 -> total 7
        self.assertEqual(v["cells"][0]["total"], "7")
        per = {s["cart_id"]: s["total"] for s in v["cells"][0]["stamps"]}
        self.assertEqual(per, {"S": "1", "L": "6"})
        self.assertTrue(verify_voucher(ledger, "V-multi")["verified"])

    def test_voucher_idempotent_and_divergent_reject(self):
        ledger, _, _ = self._ledger()
        plate = ProofPlate("early", [CELL], {"c": [Stamp("S", Q(1))]})
        a = ledger.put_voucher("V", plate, "r")
        b = ledger.put_voucher("V", plate, "r")
        self.assertEqual(a["input_hash"], b["input_hash"])
        changed = ProofPlate("early", [CELL], {"c": [Stamp("S", Q(2))]})
        with self.assertRaises(InkRejected):
            ledger.put_voucher("V", changed, "r")

    def test_revision_and_grid_change_do_not_touch_old_voucher(self):
        ledger, cat, _ = self._ledger()
        plate = ProofPlate("early", [CELL], {"c": [Stamp("S", Q(1))]})
        ledger.put_voucher("V", plate, "r")
        # 修订轮廓（新增半个尺寸的 Q 并改动 r 版本尺寸）：旧凭单快照冻结。
        cat.put_revision("r2", [Profile("P", Q(1), Q(1)), Profile("Q", Q(1, 4), Q(1, 4))])
        old = ledger.read_voucher("V")
        self.assertEqual(old["cells"][0]["total"], "1")
        self.assertEqual(old["revision"], "r")
        self.assertTrue(verify_voucher(ledger, "V")["verified"])
        # 换格网另算一张：同一印模、更大的格，墨量不同（相同长短的格可有不同墨量）。
        big = Cell("big", Q(0), Q(0), Q(3), Q(1))
        new_plate = ProofPlate("big", [big], {"big": [Stamp("S", Q(1))]})
        v2 = ledger.put_voucher("V2", new_plate, "r")
        # 中心 5/4，宽 1，格 x∈[0,3]：整宽都在格内 -> 面积 1，时限 4 -> 4。
        self.assertEqual(v2["cells"][0]["total"], "4")
        self.assertNotEqual(v2["cells"][0]["total"], old["cells"][0]["total"])

    def test_uncertain_shapes_do_not_change_old_voucher(self):
        # 更正早期尺寸：轨迹（同一 cart_id）被改写不追溯旧凭单，因为快照冻结。
        ledger, cat, store = make_ledger(
            [PROF],
            [make_traj("S", [(0, "5/4", "1/2"), (4, "5/4", "1/2")])],
        )
        plate = ProofPlate("early", [CELL], {"c": [Stamp("S", Q(1))]})
        ledger.put_voucher("V", plate, "r")
        store.upsert_trajectory(
            make_traj("S", [(0, "3/4", "1/2"), (4, "3/4", "1/2")])
        )
        self.assertEqual(ledger.read_voucher("V")["cells"][0]["total"], "1")
        self.assertTrue(verify_voucher(ledger, "V")["verified"])

    def test_same_size_cells_different_ink(self):
        # 两张格尺寸完全相同（1×1），但印模摆放不同 -> 墨量不同。
        # S 中心 (5/4,1/2)，宽 1，即 x∈[3/4,7/4]：
        #   格 [0,1]×[0,1] 内只压 [3/4,1]（宽 1/4）-> 面积 1/4，时限 4 -> 1；
        #   格 [1,2]×[0,1] 内压 [1,7/4]（宽 3/4）  -> 面积 3/4，时限 4 -> 3。
        ledger, _, _ = self._ledger()
        left = Cell("left", Q(0), Q(0), Q(1), Q(1))
        right = Cell("right", Q(1), Q(0), Q(2), Q(1))
        plate = ProofPlate(
            "same-size",
            [left, right],
            {"left": [Stamp("S", Q(1))], "right": [Stamp("S", Q(1))]},
        )
        v = ledger.put_voucher("V-same", plate, "r")
        totals = {c["cell"]["cell_id"]: c["total"] for c in v["cells"]}
        self.assertEqual(totals, {"left": "1", "right": "3"})
        self.assertTrue(verify_voucher(ledger, "V-same")["verified"])

    def test_cell_total_is_sum_of_stamp_totals(self):
        ledger, cat, _ = self._ledger()
        plate = ProofPlate("m", [CELL], {"c": [Stamp("S", Q(1)), Stamp("L", Q(1))]})
        v = ledger.put_voucher("V", plate, "r")
        total = sum((Fraction(s["total"]) for s in v["cells"][0]["stamps"]), Q(0))
        self.assertEqual(total, Fraction(v["cells"][0]["total"]))


class AllocationTests(unittest.TestCase):
    def _plate(self, pid, weight, ink, limits):
        return PlateSpec(
            pid, Q(weight), {"c": Q(ink)},
            {"c": {p: (Q(lo), Q(hi)) for p, (lo, hi) in limits.items()}},
        )

    def test_closed_interval_gate(self):
        early = self._plate("early", 1, 1, {"thin": (0, 2), "thick": (0, 4)})
        late = self._plate("late", 2, 3, {"thin": (0, 2), "thick": (2, 4)})
        self.assertEqual(early.feasible_papers(), ["thick", "thin"])
        self.assertEqual(late.feasible_papers(), ["thick"])
        res = allocate([early, late], {"thin": 1, "thick": 1})
        by = dict(res.assignments)
        self.assertEqual(by["early"], "thin")
        self.assertEqual(by["late"], "thick")
        self.assertEqual(res.total_weight, Q(3))
        self.assertEqual(res.allocated_count, 2)

    def test_limited_thick_makes_late_undeliverable(self):
        early = self._plate("early", 1, 1, {"thin": (0, 2), "thick": (0, 4)})
        late = self._plate("late", 2, 3, {"thin": (0, 2), "thick": (2, 4)})
        # 厚纸已被更早的确认领完。
        res = allocate([early, late], {"thin": 1, "thick": 0})
        by = dict(res.assignments)
        self.assertEqual(by["late"], None)
        self.assertEqual(res.allocated_count, 1)

    def test_closed_interval_boundary_inclusive(self):
        # 墨量恰为 2，薄纸 [0,2] 闭区间内应合格。
        p = self._plate("p", 1, 2, {"thin": (0, 2)})
        self.assertEqual(p.feasible_papers(), ["thin"])

    def test_weight_then_count_then_sequence(self):
        # 权重优先：两张同可用一纸，权重高者获配。
        a = self._plate("A", 2, 1, {"thin": (0, 2)})
        b = self._plate("B", 3, 1, {"thin": (0, 2)})
        res = allocate([a, b], {"thin": 1})
        self.assertEqual(dict(res.assignments), {"A": None, "B": "thin"})
        # 权重、张数并列 -> 纸种序列按试版号字典序最小。
        p1 = self._plate("P1", 1, 0, {"a": (0, 1), "b": (0, 1)})
        p2 = self._plate("P2", 1, 0, {"a": (0, 1), "b": (0, 1)})
        res = allocate([p1, p2], {"a": 1, "b": 2})
        self.assertEqual(dict(res.assignments), {"P1": "a", "P2": "b"})

    def test_count_prefers_more_plates_after_weight(self):
        # 权重同为 0 时优先张数更多：P1、P2 可各占一纸，P3 与 P1 争同一纸。
        p1 = self._plate("P1", 0, 0, {"a": (0, 1)})
        p2 = self._plate("P2", 0, 0, {"b": (0, 1)})
        p3 = self._plate("P3", 0, 0, {"a": (0, 1)})
        res = allocate([p1, p2, p3], {"a": 1, "b": 1})
        self.assertEqual(res.total_weight, Q(0))
        self.assertEqual(res.allocated_count, 2)

    def test_unallocated_sorts_after_all_papers(self):
        # P1 只能用 a，P2 权重更高独占用 a -> P1 未配（None 排在纸种之后）。
        p1 = self._plate("P1", 1, 0, {"a": (0, 1)})
        p2 = self._plate("P2", 5, 0, {"a": (0, 1)})
        res = allocate([p1, p2], {"a": 1})
        self.assertEqual(dict(res.assignments), {"P1": None, "P2": "a"})

    def test_duplicate_plate_ids_rejected(self):
        p = self._plate("P", 1, 0, {"a": (0, 1)})
        with self.assertRaises(ValueError):
            allocate([p, p], {"a": 1})


class PaperStoreTests(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".sqlite3")
        self.store = PaperStore(self.path)
        self.store.register_paper("thin", 1)
        self.store.register_paper("thick", 1)

    def tearDown(self):
        self.store.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def _plates(self):
        early = PlateSpec("early", Q(1), {"c": Q(1)},
                          {"c": {"thin": (Q(0), Q(2)), "thick": (Q(0), Q(4))}})
        late = PlateSpec("late", Q(2), {"c": Q(3)},
                         {"c": {"thin": (Q(0), Q(2)), "thick": (Q(2), Q(4))}})
        return [early, late]

    def test_confirm_decrements_once(self):
        res = self.store.allocate_batch(self._plates())
        self.assertEqual(res.usage, {"thin": 1, "thick": 1})
        out = self.store.confirm("C1", res)
        self.assertEqual(out["stocks_after"], {"thick": 0, "thin": 0})
        self.assertEqual(self.store.stocks(), {"thick": 0, "thin": 0})
        # 同 confirmation 同计划幂等，不再扣一次。
        again = self.store.confirm("C1", res)
        self.assertEqual(again["stocks_after"], {"thick": 0, "thin": 0})
        self.assertEqual(self.store.stocks(), {"thick": 0, "thin": 0})
        # 异文拒绝。
        other = self.store.allocate_batch(self._plates())
        with self.assertRaises(ConfirmationRejected):
            self.store.confirm("C1", other)

    def test_shortage_lists_paper_types_and_recompute(self):
        res = self.store.allocate_batch(self._plates())
        self.store.confirm("C1", res)
        # 纸都用完：一个仍想用厚纸/薄纸的计划应报短缺，逐纸种给出缺口。
        stale = allocate(self._plates(), {"thin": 1, "thick": 1})
        with self.assertRaises(PaperShortage) as ctx:
            self.store.confirm("C2", stale)
        self.assertIn("thick", ctx.exception.shortages)
        self.assertIn("thin", ctx.exception.shortages)
        self.assertEqual(ctx.exception.shortages["thick"]["available"], 0)
        # 库存全空时重算：late 无纸可用，early 也无纸可用。
        empty = self.store.allocate_batch(self._plates())
        self.assertEqual(empty.allocated_count, 0)
        # 补货后用新库存重算，late 可用厚纸交付。
        self.store.add_stock("thick", 1)
        self.store.add_stock("thin", 1)
        recomputed = self.store.allocate_batch(self._plates())
        self.assertEqual(dict(recomputed.assignments)["late"], "thick")
        self.store.confirm("C3", recomputed)
        self.assertEqual(self.store.stock("thick"), 0)
        self.assertEqual(self.store.stock("thin"), 0)

    def test_concurrent_confirm_only_one_wins(self):
        res = self.store.allocate_batch(self._plates())
        stores = [PaperStore(self.path) for _ in range(2)]
        outcomes = []
        barrier = threading.Barrier(2)

        def run(store, cid):
            barrier.wait()
            try:
                store.confirm(cid, res)
                outcomes.append(("ok", cid, None))
            except PaperShortage as exc:
                outcomes.append(("short", cid, exc.shortages))

        threads = [threading.Thread(target=run, args=(stores[i], f"C{i}")) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for s in stores:
            s.close()
        oks = [o for o in outcomes if o[0] == "ok"]
        shorts = [o for o in outcomes if o[0] == "short"]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(shorts), 1)
        # 失败方看到短缺的纸种。
        self.assertTrue(shorts[0][2])
        self.assertEqual(self.store.stocks(), {"thick": 0, "thin": 0})

    def test_sample_record_freezes_paper_and_voucher(self):
        rec = self.store.record_sample("S1", "early", "thin", "V-early")
        self.assertEqual(rec["paper_type_id"], "thin")
        self.assertEqual(rec["voucher_id"], "V-early")
        # 同 id 同内容幂等。
        again = self.store.record_sample("S1", "early", "thin", "V-early")
        self.assertEqual(again, rec)
        # 异文拒绝（原用纸不可改）。
        with self.assertRaises(ValueError):
            self.store.record_sample("S1", "early", "thick", "V-early")
        self.assertEqual(self.store.read_sample("S1")["paper_type_id"], "thin")


if __name__ == "__main__":
    unittest.main()
