"""连续动作核查库。

职责：

1. 在两车 *不同时间结点* 的共同细分（merged knot grid）上逐段调用
   :class:`~cart_audit.service.ProfileOverlapService`，只做精确求交，
   不使用固定采样步长，也不凭端点分离判无冲突；
2. 合并同一对车重复/相邻的重叠区间，保留开闭端点，并保存每个区间背后
   导致重叠的实际动作段（可独立复核的区间证据）；
3. 在 SQLite 中保存轮廓版本、轨迹快照、冲突区间、每对累计重叠时长与
   可通过结论；同 report_id 同内容复读、异文拒绝；旧报告不受后续轮廓
   修订影响（尺寸随报告冻结）；
4. 核查失败一律抛异常并回滚，绝不发布“可通过”外观的报告。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from bisect import bisect_right
from datetime import datetime, timezone
from fractions import Fraction
from typing import List, Optional, Sequence, Tuple

from .catalog import InputError, MotionStore, ProfileCatalog
from .intervals import TimeInterval, intersect_intervals, merge_union
from .models import MotionSegment, Trajectory
from .rational import qstr
from .service import ProfileOverlapService

__all__ = ["AuditLibrary", "AuditRejected", "AuditInputError"]


class AuditInputError(ValueError):
    """核查输入非法（未知车/版本、参数越界等）。"""


class AuditRejected(Exception):
    """report_id 已存在但输入异文，拒绝覆盖。"""


_REPORT_SCHEMA = """
CREATE TABLE IF NOT EXISTS report (
    report_id        TEXT PRIMARY KEY,
    revision         TEXT NOT NULL,
    horizon_h0       TEXT NOT NULL,
    horizon_h1       TEXT NOT NULL,
    request_json     TEXT NOT NULL,
    snapshot_json    TEXT NOT NULL,
    input_hash       TEXT NOT NULL,
    passed           INTEGER NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS report_pair (
    report_id             TEXT NOT NULL REFERENCES report(report_id),
    pair_index            INTEGER NOT NULL,
    cart_a                TEXT NOT NULL,
    cart_b                TEXT NOT NULL,
    total_duration        TEXT NOT NULL,
    passed                INTEGER NOT NULL,
    merged_intervals_json TEXT NOT NULL,
    PRIMARY KEY (report_id, pair_index)
);
CREATE TABLE IF NOT EXISTS conflict (
    conflict_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id       TEXT NOT NULL REFERENCES report(report_id),
    pair_index      INTEGER NOT NULL,
    interval_index  INTEGER NOT NULL,
    left            TEXT NOT NULL,
    right           TEXT NOT NULL,
    left_inclusive  INTEGER NOT NULL,
    right_inclusive INTEGER NOT NULL,
    duration        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conflict_evidence (
    conflict_id       INTEGER NOT NULL REFERENCES conflict(conflict_id),
    evidence_index    INTEGER NOT NULL,
    cell_left         TEXT NOT NULL,
    cell_right        TEXT NOT NULL,
    seg_a_index       INTEGER NOT NULL,
    seg_b_index       INTEGER NOT NULL,
    raw_interval_json TEXT NOT NULL,
    PRIMARY KEY (conflict_id, evidence_index)
);
CREATE TABLE IF NOT EXISTS audit_attempt (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id  TEXT,
    ok         INTEGER NOT NULL,
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _interval_row(iv: TimeInterval) -> dict:
    return {
        "left": qstr(iv.left),
        "right": qstr(iv.right),
        "left_inclusive": iv.left_inclusive,
        "right_inclusive": iv.right_inclusive,
        "duration": qstr(iv.duration),
    }


class AuditLibrary:
    def __init__(
        self,
        catalog: ProfileCatalog,
        store: MotionStore,
        db_path: Optional[str] = None,
        service: Optional[ProfileOverlapService] = None,
        conn: Optional[sqlite3.Connection] = None,
    ):
        self.catalog = catalog
        self.store = store
        self.service = service or ProfileOverlapService()
        # 允许与目录/轨迹存储共享同一连接（内存库场景必需）；
        # 文件库场景给出 db_path 时独立打开连接。
        self.conn = conn if conn is not None else sqlite3.connect(db_path)
        self._owns_conn = conn is None
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_REPORT_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        if self._owns_conn:
            self.conn.close()

    # ------------------------------------------------------------------ #
    # 内部：车对在共同细分上的精确核查
    # ------------------------------------------------------------------ #
    @staticmethod
    def _subdivision(
        traj_a: Trajectory, traj_b: Trajectory, lo: Fraction, hi: Fraction
    ) -> List[Fraction]:
        """两车结点的共同细分（仅保留落在 [lo, hi] 内的结点并补端点）。"""
        grid = {lo, hi}
        for traj in (traj_a, traj_b):
            for knot in traj.knots:
                if lo <= knot.t <= hi:
                    grid.add(knot.t)
        return sorted(grid)

    @staticmethod
    def _segment_at(segments: Sequence[MotionSegment], starts: List[Fraction], t: Fraction) -> MotionSegment:
        idx = bisect_right(starts, t) - 1
        if idx < 0 or idx >= len(segments):
            raise AuditInputError(f"时刻 {t} 找不到对应动作段（轨迹缺段）")
        return segments[idx]

    def audit_pair(
        self,
        revision: str,
        cart_a: str,
        cart_b: str,
        lo: Optional[Fraction] = None,
        hi: Optional[Fraction] = None,
    ) -> dict:
        """对一对车在 [lo, hi)（默认整个核查域）做完整精确核查。

        返回合并后的冲突区间、累计时长、结论以及逐 cell 的原始证据。
        """
        h0, h1 = self.store.get_horizon()
        lo = h0 if lo is None else fraction_in(lo, h0, h1, "lo")
        hi = h1 if hi is None else fraction_in(hi, h0, h1, "hi")
        if not hi > lo:
            raise AuditInputError("核查时段必须满足 hi > lo")
        traj_a, traj_b = self._load_two(cart_a, cart_b)
        profile_a = self.catalog.get_profile(revision, traj_a.profile_id)
        profile_b = self.catalog.get_profile(revision, traj_b.profile_id)
        segments_a = traj_a.segments()
        segments_b = traj_b.segments()
        starts_a = [s.t0 for s in segments_a]
        starts_b = [s.t0 for s in segments_b]

        grid = self._subdivision(traj_a, traj_b, lo, hi)
        raw: List[Tuple[TimeInterval, dict]] = []
        for u, v in zip(grid, grid[1:]):
            seg_a = self._segment_at(segments_a, starts_a, u)
            seg_b = self._segment_at(segments_b, starts_b, u)
            iv = self.service.segment_overlap(profile_a, seg_a, profile_b, seg_b)
            if iv is None:
                continue
            # 动作段可能比请求 cell 更长（lo/hi 不是轨迹结点）：
            # 与半开 cell [u, v) 求交，保证区间不越出请求时段。
            iv = intersect_intervals(iv, TimeInterval(u, v, True, False))
            if iv is None:
                continue
            raw.append(
                (
                    iv,
                    {
                        "cell": [qstr(u), qstr(v)],
                        "seg_a_index": seg_a.index,
                        "seg_b_index": seg_b.index,
                        "raw_interval": _interval_row(iv),
                    },
                )
            )

        merged = merge_union([iv for iv, _ in raw])
        # 把每条原始证据归并到它所贡献的合并区间。
        evidence_by_interval: List[List[dict]] = [[] for _ in merged]
        for iv, evidence in raw:
            target = None
            for k, miv in enumerate(merged):
                if iv.left >= miv.left and iv.right <= miv.right:
                    target = k
                    break
            if target is None:  # 理论上不可达：合并区间必覆盖所有输入
                raise RuntimeError("合并区间未能覆盖原始重叠区间")
            evidence_by_interval[target].append(evidence)

        total = sum((iv.duration for iv in merged), Fraction(0))
        return {
            "cart_a": cart_a,
            "cart_b": cart_b,
            "revision": revision,
            "range": [qstr(lo), qstr(hi)],
            "passed": len(merged) == 0,
            "total_overlap_duration": qstr(total),
            "merged_intervals": [_interval_row(iv) for iv in merged],
            "evidence": [
                {
                    "interval": _interval_row(miv),
                    "sources": evidence_by_interval[k],
                }
                for k, miv in enumerate(merged)
            ],
        }

    def _load_two(self, cart_a: str, cart_b: str) -> Tuple[Trajectory, Trajectory]:
        if cart_a == cart_b:
            raise AuditInputError("车对必须由两辆不同的车组成")
        traj_a = self.store.get_trajectory(cart_a)
        traj_b = self.store.get_trajectory(cart_b)
        return traj_a, traj_b

    # ------------------------------------------------------------------ #
    # 独立查询：轮廓 / 一对单段重叠 / 整车对
    # ------------------------------------------------------------------ #
    def query_profile(self, revision: str, profile_id: str) -> dict:
        profile = self.catalog.get_profile(revision, profile_id)
        return {"revision": revision, **profile.to_dict()}

    def query_pair_segment(
        self,
        revision: str,
        cart_a: str,
        cart_b: str,
        seg_a_index: int,
        seg_b_index: int,
    ) -> dict:
        """对外独立查询：一对车各取一个动作段，求共同线性时段上的重叠。"""
        traj_a, traj_b = self._load_two(cart_a, cart_b)
        segs_a, segs_b = traj_a.segments(), traj_b.segments()
        if not 0 <= seg_a_index < len(segs_a):
            raise AuditInputError(f"{cart_a} 段下标 {seg_a_index} 越界（共 {len(segs_a)} 段）")
        if not 0 <= seg_b_index < len(segs_b):
            raise AuditInputError(f"{cart_b} 段下标 {seg_b_index} 越界（共 {len(segs_b)} 段）")
        seg_a, seg_b = segs_a[seg_a_index], segs_b[seg_b_index]
        profile_a = self.catalog.get_profile(revision, traj_a.profile_id)
        profile_b = self.catalog.get_profile(revision, traj_b.profile_id)
        lo = max(seg_a.t0, seg_b.t0)
        hi = min(seg_a.t1, seg_b.t1)
        iv = self.service.segment_overlap(profile_a, seg_a, profile_b, seg_b)
        return {
            "revision": revision,
            "cart_a": cart_a,
            "cart_b": cart_b,
            "profiles": {
                cart_a: profile_a.to_dict(),
                cart_b: profile_b.to_dict(),
            },
            "segments": {
                cart_a: _segment_row(seg_a),
                cart_b: _segment_row(seg_b),
            },
            "common_linear_span": (
                None if lo >= hi else {"left": qstr(lo), "right": qstr(hi)}
            ),
            "overlap": None if iv is None else _interval_row(iv),
        }

    # ------------------------------------------------------------------ #
    # 批次核查与报告
    # ------------------------------------------------------------------ #
    def audit(
        self,
        report_id: str,
        pairs: Sequence[Sequence[str]],
        revision: str,
    ) -> dict:
        reason: Optional[str] = None
        try:
            return self._audit(report_id, pairs, revision)
        except (AuditInputError, AuditRejected, InputError) as exc:
            # 仅业务性失败落 audit_attempt 留痕；编程错误（RuntimeError 等）
            # 继续向上抛，避免被静默吞掉。失败路径不写任何 report 行。
            reason = f"{type(exc).__name__}: {exc}"
            with self.conn:
                self.conn.execute(
                    "INSERT INTO audit_attempt(report_id, ok, reason, created_at) "
                    "VALUES (?, 0, ?, ?)",
                    (report_id, reason, _utc_now()),
                )
            raise

    def _normalize_pairs(self, pairs: Sequence[Sequence[str]]) -> List[Tuple[str, str, Tuple[str, str]]]:
        if not isinstance(pairs, (list, tuple)) or not pairs:
            raise AuditInputError("pairs 必须是非空的车对列表")
        normalized: List[Tuple[str, str, Tuple[str, str]]] = []
        seen = set()
        for pair in pairs:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise AuditInputError(f"非法车对: {pair!r}，必须是 [cart_a, cart_b]")
            a, b = str(pair[0]), str(pair[1])
            if not a or not b:
                raise AuditInputError("cart_id 不能为空")
            if a == b:
                raise AuditInputError(f"非法车对 ({a}, {b})：两车必须不同")
            canonical = tuple(sorted((a, b)))
            if canonical in seen:
                raise AuditInputError(f"车对 {canonical} 在同一批次中重复")
            seen.add(canonical)
            normalized.append((canonical[0], canonical[1], (a, b)))
        return normalized

    def _audit(self, report_id: str, pairs: Sequence[Sequence[str]], revision: str) -> dict:
        if not isinstance(report_id, str) or not report_id:
            raise AuditInputError("report_id 必须是非空字符串")
        # 版本存在性与资料合法性先校验，失败则整笔回滚。
        revision_payload = self.catalog.get_revision_payload(revision)
        h0, h1 = self.store.get_horizon()
        normalized = self._normalize_pairs(pairs)
        involved = sorted({c for a, b, _ in normalized for c in (a, b)})
        snapshots = self.store.snapshot(involved)  # 未知 cart_id 在此抛错
        # 每辆车引用的 profile 必须存在于该修订。
        for cart_id in involved:
            pid = snapshots[cart_id]["profile_id"]
            self.catalog.get_profile(revision, pid)

        input_fingerprint = _canonical_json(
            {
                "revision_payload": revision_payload,
                "horizon": [qstr(h0), qstr(h1)],
                "pairs": [[a, b] for a, b, _ in normalized],
                "snapshots": snapshots,
            }
        )
        input_hash = hashlib.sha256(input_fingerprint.encode("utf-8")).hexdigest()

        existing = self.conn.execute(
            "SELECT input_hash FROM report WHERE report_id = ?", (report_id,)
        ).fetchone()
        if existing is not None:
            if existing[0] != input_hash:
                raise AuditRejected(
                    f"report_id {report_id!r} 已存在且输入异文，拒绝覆盖"
                )
            return self.read_report(report_id)  # 同内容复读：返回原报告

        pair_results = [
            self.audit_pair(revision, a, b, h0, h1) for a, b, _ in normalized
        ]
        passed = all(result["passed"] for result in pair_results)
        snapshot_payload = {
            "revision": revision,
            "revision_payload": revision_payload,
            "horizon": [qstr(h0), qstr(h1)],
            "requested_pairs": [list(original) for _, _, original in normalized],
            "trajectories": snapshots,
        }
        created_at = _utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO report(report_id, revision, horizon_h0, horizon_h1, "
                "request_json, snapshot_json, input_hash, passed, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report_id,
                    revision,
                    qstr(h0),
                    qstr(h1),
                    _canonical_json([list(original) for _, _, original in normalized]),
                    _canonical_json(snapshot_payload),
                    input_hash,
                    1 if passed else 0,
                    created_at,
                ),
            )
            for idx, (result, (a, b, _)) in enumerate(zip(pair_results, normalized)):
                total = Fraction(result["total_overlap_duration"])
                self.conn.execute(
                    "INSERT INTO report_pair(report_id, pair_index, cart_a, cart_b, "
                    "total_duration, passed, merged_intervals_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        report_id,
                        idx,
                        a,
                        b,
                        qstr(total),
                        1 if result["passed"] else 0,
                        _canonical_json(result["merged_intervals"]),
                    ),
                )
                for iv_idx, (iv_dict, evidence) in enumerate(
                    zip(result["merged_intervals"], result["evidence"])
                ):
                    cur = self.conn.execute(
                        "INSERT INTO conflict(report_id, pair_index, interval_index, "
                        "left, right, left_inclusive, right_inclusive, duration) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            report_id,
                            idx,
                            iv_idx,
                            iv_dict["left"],
                            iv_dict["right"],
                            1 if iv_dict["left_inclusive"] else 0,
                            1 if iv_dict["right_inclusive"] else 0,
                            iv_dict["duration"],
                        ),
                    )
                    conflict_id = cur.lastrowid
                    self.conn.executemany(
                        "INSERT INTO conflict_evidence(conflict_id, evidence_index, "
                        "cell_left, cell_right, seg_a_index, seg_b_index, raw_interval_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                conflict_id,
                                e_idx,
                                ev["cell"][0],
                                ev["cell"][1],
                                ev["seg_a_index"],
                                ev["seg_b_index"],
                                _canonical_json(ev["raw_interval"]),
                            )
                            for e_idx, ev in enumerate(evidence["sources"])
                        ],
                    )
            self.conn.execute(
                "INSERT INTO audit_attempt(report_id, ok, reason, created_at) "
                "VALUES (?, 1, ?, ?)",
                (report_id, "published", created_at),
            )
        return self.read_report(report_id)

    # ------------------------------------------------------------------ #
    def read_report(self, report_id: str) -> dict:
        row = self.conn.execute(
            "SELECT report_id, revision, horizon_h0, horizon_h1, request_json, "
            "snapshot_json, input_hash, passed, created_at FROM report WHERE report_id = ?",
            (report_id,),
        ).fetchone()
        if row is None:
            raise AuditInputError(f"报告 {report_id!r} 不存在（核查失败不会产生报告）")
        pair_rows = self.conn.execute(
            "SELECT pair_index, cart_a, cart_b, total_duration, passed, merged_intervals_json "
            "FROM report_pair WHERE report_id = ? ORDER BY pair_index",
            (report_id,),
        ).fetchall()
        pairs = []
        for pidx, a, b, total, p_passed, merged_json in pair_rows:
            conflict_rows = self.conn.execute(
                "SELECT conflict_id, interval_index, left, right, left_inclusive, "
                "right_inclusive, duration FROM conflict "
                "WHERE report_id = ? AND pair_index = ? ORDER BY interval_index",
                (report_id, pidx),
            ).fetchall()
            intervals = []
            for cid, iv_idx, left, right, li, ri, duration in conflict_rows:
                evidence_rows = self.conn.execute(
                    "SELECT cell_left, cell_right, seg_a_index, seg_b_index, raw_interval_json "
                    "FROM conflict_evidence WHERE conflict_id = ? ORDER BY evidence_index",
                    (cid,),
                ).fetchall()
                intervals.append(
                    {
                        "interval": {
                            "left": left,
                            "right": right,
                            "left_inclusive": bool(li),
                            "right_inclusive": bool(ri),
                            "duration": duration,
                        },
                        "evidence": [
                            {
                                "cell": [cl, cr],
                                "seg_a_index": sa,
                                "seg_b_index": sb,
                                "raw_interval": json.loads(rj),
                            }
                            for cl, cr, sa, sb, rj in evidence_rows
                        ],
                    }
                )
            pairs.append(
                {
                    "pair_index": pidx,
                    "cart_a": a,
                    "cart_b": b,
                    "passed": bool(p_passed),
                    "total_overlap_duration": total,
                    "merged_intervals": json.loads(merged_json),
                    "conflicts": intervals,
                }
            )
        return {
            "report_id": row[0],
            "revision": row[1],
            "horizon": [row[2], row[3]],
            "requested_pairs": json.loads(row[4]),
            "snapshot": json.loads(row[5]),
            "input_hash": row[6],
            "passed": bool(row[7]),
            "created_at": row[8],
            "pairs": pairs,
        }

    def list_reports(self) -> List[str]:
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT report_id FROM report ORDER BY created_at, rowid"
            ).fetchall()
        ]

    def list_attempts(self) -> List[dict]:
        return [
            {"report_id": r[0], "ok": bool(r[1]), "reason": r[2], "created_at": r[3]}
            for r in self.conn.execute(
                "SELECT report_id, ok, reason, created_at FROM audit_attempt ORDER BY id"
            ).fetchall()
        ]


def fraction_in(value: Fraction, h0: Fraction, h1: Fraction, name: str) -> Fraction:
    from .rational import fraction as _fraction

    value = _fraction(value, field=name)
    if value < h0 or value > h1:
        raise AuditInputError(f"{name}={value} 超出核查域 [{h0}, {h1})")
    return value


def _segment_row(seg: MotionSegment) -> dict:
    return {
        "segment_index": seg.index,
        "t0": qstr(seg.t0),
        "t1": qstr(seg.t1),
        "x0": qstr(seg.x0),
        "y0": qstr(seg.y0),
        "x1": qstr(seg.x1),
        "y1": qstr(seg.y1),
    }
