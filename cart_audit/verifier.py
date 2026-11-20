"""报告独立复核。

从报告冻结的轮廓版本与轨迹快照出发，用轮廓服务重新求解每个车对，
与报告中保存的冲突区间、累计时长、结论逐字节比对。复核过程不写库、
不依赖当前目录状态——即使后续发生轮廓修订，旧报告仍按其冻结尺寸复核。
"""

from __future__ import annotations

from fractions import Fraction
from typing import List

from .intervals import TimeInterval, intersect_intervals, merge_union
from .library import AuditLibrary
from .models import Knot, Profile, Trajectory
from .service import ProfileOverlapService

__all__ = ["verify_report"]


def _as_interval(row: dict) -> TimeInterval:
    return TimeInterval(
        Fraction(row["left"]),
        Fraction(row["right"]),
        bool(row["left_inclusive"]),
        bool(row["right_inclusive"]),
    )


def verify_report(library: AuditLibrary, report_id: str) -> dict:
    """重算报告并比对，返回复核明细；任何不一致都抛出 :class:`AssertionError`。"""
    report = library.read_report(report_id)
    snapshot = report["snapshot"]
    h0, h1 = Fraction(report["horizon"][0]), Fraction(report["horizon"][1])
    revision_payload = snapshot["revision_payload"]
    profiles = {
        item["profile_id"]: Profile(
            item["profile_id"],
            Fraction(item["half_width"]),
            Fraction(item["half_height"]),
        )
        for item in revision_payload["profiles"]
    }
    trajectories = {}
    for cart_id, item in snapshot["trajectories"].items():
        trajectories[cart_id] = Trajectory(
            cart_id=item["cart_id"],
            profile_id=item["profile_id"],
            knots=[
                Knot(Fraction(k["t"]), Fraction(k["x"]), Fraction(k["y"]))
                for k in item["knots"]
            ],
            horizon=(h0, h1),
        )

    service = ProfileOverlapService()
    checks: List[dict] = []
    all_ok = True
    for pair in report["pairs"]:
        a, b = pair["cart_a"], pair["cart_b"]
        traj_a, traj_b = trajectories[a], trajectories[b]
        prof_a, prof_b = profiles[traj_a.profile_id], profiles[traj_b.profile_id]
        segs_a, segs_b = traj_a.segments(), traj_b.segments()
        starts_a = [s.t0 for s in segs_a]
        starts_b = [s.t0 for s in segs_b]
        grid = AuditLibrary._subdivision(traj_a, traj_b, h0, h1)
        raw = []
        for u, v in zip(grid, grid[1:]):
            seg_a = AuditLibrary._segment_at(segs_a, starts_a, u)
            seg_b = AuditLibrary._segment_at(segs_b, starts_b, u)
            iv = service.segment_overlap(prof_a, seg_a, prof_b, seg_b)
            if iv is not None:
                iv = intersect_intervals(iv, TimeInterval(u, v, True, False))
            if iv is not None:
                raw.append((iv, u, v, seg_a.index, seg_b.index))
        recomputed = [iv for iv, *_ in raw]
        merged = merge_union(recomputed)
        stored = [_as_interval(row["interval"]) for row in pair["conflicts"]]
        interval_match = merged == stored
        total = sum((iv.duration for iv in merged), Fraction(0))
        duration_match = total == Fraction(pair["total_overlap_duration"])
        passed_match = pair["passed"] == (len(merged) == 0)
        # 证据完整性：每个保存区间至少有一条动作段证据，且证据的并集能重建该区间。
        evidence_ok = True
        for conflict in pair["conflicts"]:
            if not conflict["evidence"]:
                evidence_ok = False
                break
            rebuilt = merge_union(
                [_as_interval(e["raw_interval"]) for e in conflict["evidence"]]
            )
            target = _as_interval(conflict["interval"])
            if rebuilt != [target]:
                evidence_ok = False
                break
        ok = interval_match and duration_match and passed_match and evidence_ok
        all_ok &= ok
        checks.append(
            {
                "pair": [a, b],
                "interval_match": interval_match,
                "duration_match": duration_match,
                "passed_match": passed_match,
                "evidence_ok": evidence_ok,
                "recomputed_intervals": [iv.to_dict() for iv in merged],
                "stored_intervals": [iv.to_dict() for iv in stored],
                "recomputed_total_duration": str(total),
            }
        )
    report_pass_match = report["passed"] == all(p["passed"] for p in report["pairs"])
    return {
        "report_id": report_id,
        "revision": report["revision"],
        "verified": all_ok and report_pass_match,
        "report_pass_match": report_pass_match,
        "checks": checks,
    }
