"""格面受墨量：轴对齐矩形格与平移印模的精确积分凭单。

本模块只做离线数据复核：给定一张试版（若干互不重叠的轴对齐矩形格）、
若干印模（沿用旧档 ``cart_id`` 的分段线性平移路径与对应轮廓半宽/半高）
以及每只印模对每格的恒定非负流墨率，计算

    ink(cell) = ∫_horizon Σ_stamp flow_rate · area(stamp(t) ∩ cell) dt

面积按严格正面积解释：只碰边、只碰角都没有面积贡献。所有数值均为
:class:`fractions.Fraction`，不使用浮点，也不采样。

每个动作段内，印模中心线性平移；某一方向上的交叠长度是分段线性函数，
其折点出现在印模左/右边缘越过格的边界处。两个方向同时考虑后，交叠
面积在折点之间是二次多项式，可按有理数精确积分。因此斜向平移导致面积
在同一动作段内改变时也能给出精确凭单。

凭单冻结当时的轮廓版本、轨迹快照、格网与流墨率；后续修订轮廓或更换
格网不会改变旧凭单，旧凭单可独立重算比对。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .catalog import InputError, MotionStore, ProfileCatalog
from .models import Knot, MotionSegment, Profile, Trajectory
from .rational import fraction, qstr

__all__ = [
    "Stamp",
    "Cell",
    "ProofPlate",
    "InkLedger",
    "InkInputError",
    "InkRejected",
    "verify_voucher",
    "area_integral",
]

Q = Fraction


class InkInputError(ValueError):
    """受墨量计算的输入非法（格网、印模、版本或轨迹）。"""


class InkRejected(Exception):
    """voucher_id 已存在但输入异文，拒绝覆盖。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# 领域模型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Stamp:
    """一只印模及其在某一格上的恒定流墨率（非负有理数）。"""

    cart_id: str
    flow_rate: Fraction

    def __post_init__(self) -> None:
        if not isinstance(self.cart_id, str) or not self.cart_id:
            raise InkInputError("stamp.cart_id 必须是非空字符串")
        rate = fraction(self.flow_rate, field=f"stamp {self.cart_id!r} flow_rate")
        if rate < 0:
            raise InkInputError(f"stamp {self.cart_id!r} 流墨率不得为负: {rate}")
        object.__setattr__(self, "flow_rate", rate)

    @classmethod
    def from_data(cls, data: Mapping) -> "Stamp":
        try:
            return cls(str(data["cart_id"]), fraction(data["flow_rate"], field="flow_rate"))
        except KeyError as exc:
            raise InkInputError(f"印模缺少字段 {exc.args[0]!r}") from exc

    def to_dict(self) -> dict:
        return {"cart_id": self.cart_id, "flow_rate": qstr(self.flow_rate)}


@dataclass(frozen=True)
class Cell:
    """一个轴对齐矩形格，坐标严格满足 x0 < x1、y0 < y1（非零面积）。"""

    cell_id: str
    x0: Fraction
    y0: Fraction
    x1: Fraction
    y1: Fraction

    def __post_init__(self) -> None:
        if not isinstance(self.cell_id, str) or not self.cell_id:
            raise InkInputError("cell_id 必须是非空字符串")
        x0 = fraction(self.x0, field="cell.x0")
        y0 = fraction(self.y0, field="cell.y0")
        x1 = fraction(self.x1, field="cell.x1")
        y1 = fraction(self.y1, field="cell.y1")
        if not x1 > x0:
            raise InkInputError(f"cell {self.cell_id!r} 需要 x1 > x0: {x0} .. {x1}")
        if not y1 > y0:
            raise InkInputError(f"cell {self.cell_id!r} 需要 y1 > y0: {y0} .. {y1}")
        object.__setattr__(self, "x0", x0)
        object.__setattr__(self, "y0", y0)
        object.__setattr__(self, "x1", x1)
        object.__setattr__(self, "y1", y1)

    @classmethod
    def from_data(cls, data: Mapping) -> "Cell":
        try:
            return cls(
                str(data["cell_id"]),
                fraction(data["x0"], field="cell.x0"),
                fraction(data["y0"], field="cell.y0"),
                fraction(data["x1"], field="cell.x1"),
                fraction(data["y1"], field="cell.y1"),
            )
        except KeyError as exc:
            raise InkInputError(f"格缺少字段 {exc.args[0]!r}") from exc

    def to_dict(self) -> dict:
        return {
            "cell_id": self.cell_id,
            "x0": qstr(self.x0),
            "y0": qstr(self.y0),
            "x1": qstr(self.x1),
            "y1": qstr(self.y1),
        }

    def overlaps(self, other: "Cell") -> bool:
        """正面积相交（只碰边/角不算）。"""
        return (
            self.x0 < other.x1
            and other.x0 < self.x1
            and self.y0 < other.y1
            and other.y0 < self.y1
        )


@dataclass(frozen=True)
class ProofPlate:
    """一张试版：互不重叠的矩形格 + 每格各自指定的印模与流墨率。

    ``ink_rates`` 是 ``{cell_id: [Stamp, ...]}``；同一格可挂多只印模，贡献相加。
    一张格若未出现在 ``ink_rates`` 中，视为没有印模（墨量为 0）。
    """

    plate_id: str
    cells: Sequence[Cell]
    ink_rates: Mapping[str, Sequence[Stamp]]

    def __post_init__(self) -> None:
        if not isinstance(self.plate_id, str) or not self.plate_id:
            raise InkInputError("plate_id 必须是非空字符串")
        cells = list(self.cells)
        if not cells:
            raise InkInputError(f"试版 {self.plate_id!r} 至少要有一格")
        ids = [c.cell_id for c in cells]
        if len(set(ids)) != len(ids):
            raise InkInputError(f"试版 {self.plate_id!r} 内 cell_id 重复: {ids}")
        # 格必须两两不相交（正面积意义下）。
        for i in range(len(cells)):
            for j in range(i + 1, len(cells)):
                if cells[i].overlaps(cells[j]):
                    raise InkInputError(
                        f"试版 {self.plate_id!r} 的格 {cells[i].cell_id!r} 与 "
                        f"{cells[j].cell_id!r} 正面积重叠"
                    )
        rates: Dict[str, List[Stamp]] = {}
        for cell_id, stamps in self.ink_rates.items():
            if cell_id not in ids:
                raise InkInputError(f"流墨率指向未知格 {cell_id!r}")
            listed = list(stamps)
            for stamp in listed:
                if not isinstance(stamp, Stamp):
                    raise InkInputError("ink_rates 中的项必须是 Stamp")
            # 同一格同一印模重复指定视为异文错误，避免暗中相加。
            if len({s.cart_id for s in listed}) != len(listed):
                raise InkInputError(f"格 {cell_id!r} 重复指定同一印模")
            rates[cell_id] = listed
        object.__setattr__(self, "cells", tuple(cells))
        object.__setattr__(self, "ink_rates", {k: tuple(v) for k, v in rates.items()})

    @classmethod
    def from_data(cls, data: Mapping) -> "ProofPlate":
        try:
            plate_id = str(data["plate_id"])
            cells = [Cell.from_data(item) for item in data["cells"]]
            raw_rates = data.get("ink_rates", {})
        except KeyError as exc:
            raise InkInputError(f"试版缺少字段 {exc.args[0]!r}") from exc
        if not isinstance(raw_rates, Mapping):
            raise InkInputError("ink_rates 必须是 {cell_id: [stamp, ...]} 映射")
        rates = {
            str(cell_id): [Stamp.from_data(item) for item in listed]
            for cell_id, listed in raw_rates.items()
        }
        return cls(plate_id, cells, rates)

    def to_dict(self) -> dict:
        return {
            "plate_id": self.plate_id,
            "cells": [c.to_dict() for c in self.cells],
            "ink_rates": {
                cell_id: [s.to_dict() for s in stamps]
                for cell_id, stamps in sorted(self.ink_rates.items())
            },
        }

    def stamps_for(self, cell_id: str) -> Tuple[Stamp, ...]:
        return self.ink_rates.get(cell_id, ())


# --------------------------------------------------------------------------- #
# 精确面积积分
# --------------------------------------------------------------------------- #
def _clip_length(lo: Fraction, hi: Fraction, a: Fraction, b: Fraction) -> Fraction:
    """一维区间 [a, b] 与 [lo, hi] 的交叠长度（退化处为 0）。"""
    left = max(lo, a)
    right = min(hi, b)
    return right - left if right > left else Q(0)


def _interval_length_value(l_center: Fraction, half: Fraction, cell_lo: Fraction, cell_hi: Fraction) -> Fraction:
    return _clip_length(cell_lo, cell_hi, l_center - half, l_center + half)


def area_integral(
    profile: Profile,
    segment: MotionSegment,
    cell: Cell,
    lo: Fraction,
    hi: Fraction,
) -> Fraction:
    """精确计算 ∫_lo^hi area(stamp(t) ∩ cell) dt。

    在动作段内中心线性移动。把 ``[lo, hi]`` 按两个方向上每一个边缘越过
    格边界的时刻切成子区间；每个子区间内两个方向的交叠长度都是线性的，
    交叠面积是两线性函数的乘积（二次），用有理数精确积分（Simpson 对
    次数 ≤ 2 的多项式精确）。

    严格正面积：任何时刻若某方向交叠长度为 0（碰边），该时刻面积贡献为 0。
    """
    if hi <= lo:
        return Q(0)
    span = segment.t1 - segment.t0
    if span <= 0:  # 防御：合法动作段恒为正时长
        return Q(0)
    vx = (segment.x1 - segment.x0) / span
    vy = (segment.y1 - segment.y0) / span
    hw, hh = profile.half_width, profile.half_height
    # 边缘函数：中心 ± 半尺寸在 t 时刻的位置 = 边缘在 lo 处的位置 + 速度*(t-lo)
    x_at_lo = segment.x0 + vx * (lo - segment.t0)
    y_at_lo = segment.y0 + vy * (lo - segment.t0)

    # 候选折点：中心 x - hw / x + hw 越过 cell.x0 / cell.x1，y 向同理。
    breaks = {lo, hi}
    for edge0, vel, boundaries in (
        (x_at_lo - hw, vx, (cell.x0, cell.x1)),
        (x_at_lo + hw, vx, (cell.x0, cell.x1)),
        (y_at_lo - hh, vy, (cell.y0, cell.y1)),
        (y_at_lo + hh, vy, (cell.y0, cell.y1)),
    ):
        if vel != 0:
            for boundary in boundaries:
                t = lo + (boundary - edge0) / vel
                if lo < t < hi:
                    breaks.add(t)
    points = sorted(breaks)

    total = Q(0)

    def x_len(t: Fraction) -> Fraction:
        cx = segment.x0 + vx * (t - segment.t0)
        return _interval_length_value(cx, hw, cell.x0, cell.x1)

    def y_len(t: Fraction) -> Fraction:
        cy = segment.y0 + vy * (t - segment.t0)
        return _interval_length_value(cy, hh, cell.y0, cell.y1)

    for u, v in zip(points, points[1:]):
        if v <= u:
            continue
        # 子区间 [u, v] 内两个方向的交叠长度都是线性的；面积 = 乘积。
        # 用 Simpson 三点精确积分二次多项式。
        mid = (u + v) / 2
        a = x_len(u) * y_len(u)
        b = x_len(mid) * y_len(mid)
        c = x_len(v) * y_len(v)
        total += (v - u) * (a + 4 * b + c) / 6
    return total


# --------------------------------------------------------------------------- #
# 凭单存储与复核
# --------------------------------------------------------------------------- #
_VOUCHER_SCHEMA = """
CREATE TABLE IF NOT EXISTS ink_voucher (
    voucher_id      TEXT PRIMARY KEY,
    plate_id        TEXT NOT NULL,
    revision        TEXT NOT NULL,
    plate_json      TEXT NOT NULL,
    revision_json   TEXT NOT NULL,
    snapshot_json   TEXT NOT NULL,
    horizon_h0      TEXT NOT NULL,
    horizon_h1      TEXT NOT NULL,
    input_hash      TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ink_cell (
    voucher_id  TEXT NOT NULL REFERENCES ink_voucher(voucher_id),
    cell_id     TEXT NOT NULL,
    x0 TEXT NOT NULL, y0 TEXT NOT NULL, x1 TEXT NOT NULL, y1 TEXT NOT NULL,
    total       TEXT NOT NULL,
    PRIMARY KEY (voucher_id, cell_id)
);
CREATE TABLE IF NOT EXISTS ink_stamp_cell (
    voucher_id  TEXT NOT NULL REFERENCES ink_voucher(voucher_id),
    cell_id     TEXT NOT NULL,
    cart_id     TEXT NOT NULL,
    flow_rate   TEXT NOT NULL,
    total       TEXT NOT NULL,
    PRIMARY KEY (voucher_id, cell_id, cart_id)
);
CREATE TABLE IF NOT EXISTS ink_segment_contrib (
    voucher_id    TEXT NOT NULL REFERENCES ink_voucher(voucher_id),
    cell_id       TEXT NOT NULL,
    cart_id       TEXT NOT NULL,
    segment_index INTEGER NOT NULL,
    seg_t0        TEXT NOT NULL,
    seg_t1        TEXT NOT NULL,
    ink           TEXT NOT NULL,
    PRIMARY KEY (voucher_id, cell_id, cart_id, segment_index)
);
CREATE TABLE IF NOT EXISTS ink_attempt (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    voucher_id  TEXT,
    ok          INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def _segment_rows(traj: Trajectory) -> List[MotionSegment]:
    return traj.segments()


class InkLedger:
    """受墨量凭单库：按格 / 印模 / 动作段计算并冻结精确有理数凭单。"""

    def __init__(
        self,
        catalog: ProfileCatalog,
        store: MotionStore,
        db_path: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ):
        self.catalog = catalog
        self.store = store
        self.conn = conn if conn is not None else sqlite3.connect(db_path)
        self._owns_conn = conn is None
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_VOUCHER_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        if self._owns_conn:
            self.conn.close()

    # ---- 计算 ----------------------------------------------------------
    def compute_plate(self, plate: ProofPlate, revision: str) -> dict:
        """不做持久化，直接按 revision 当前尺寸计算整张试版的受墨明细。"""
        h0, h1 = self.store.get_horizon()
        revision_payload = self.catalog.get_revision_payload(revision)
        involved = sorted(
            {s.cart_id for stamps in plate.ink_rates.values() for s in stamps}
        )
        # 印模必须存在且其 profile 在该修订中可查。
        trajectories: Dict[str, Trajectory] = {}
        profiles: Dict[str, Profile] = {}
        for cart_id in involved:
            traj = self.store.get_trajectory(cart_id)
            trajectories[cart_id] = traj
            profiles[cart_id] = self.catalog.get_profile(revision, traj.profile_id)
        snapshots = self.store.snapshot(involved)

        cells_out = []
        for cell in plate.cells:
            stamps_out = []
            cell_total = Q(0)
            for stamp in plate.stamps_for(cell.cell_id):
                traj = trajectories[stamp.cart_id]
                profile = profiles[stamp.cart_id]
                seg_rows = []
                stamp_total = Q(0)
                for seg in _segment_rows(traj):
                    lo = max(seg.t0, h0)
                    hi = min(seg.t1, h1)
                    if hi <= lo:
                        continue
                    ink = stamp.flow_rate * area_integral(profile, seg, cell, lo, hi)
                    if ink == 0:
                        continue
                    stamp_total += ink
                    seg_rows.append(
                        {
                            "segment_index": seg.index,
                            "seg_t0": qstr(seg.t0),
                            "seg_t1": qstr(seg.t1),
                            "ink": qstr(ink),
                        }
                    )
                cell_total += stamp_total
                stamps_out.append(
                    {
                        "cart_id": stamp.cart_id,
                        "flow_rate": qstr(stamp.flow_rate),
                        "total": qstr(stamp_total),
                        "segments": seg_rows,
                    }
                )
            cells_out.append(
                {
                    "cell": cell.to_dict(),
                    "total": qstr(cell_total),
                    "stamps": stamps_out,
                }
            )
        return {
            "plate": plate,
            "plate_id": plate.plate_id,
            "revision": revision,
            "revision_payload": revision_payload,
            "horizon": [qstr(h0), qstr(h1)],
            "snapshots": snapshots,
            "cells": cells_out,
        }

    # ---- 持久化 --------------------------------------------------------
    def put_voucher(self, voucher_id: str, plate: ProofPlate, revision: str) -> dict:
        """计算并冻结一张凭单；同 id 同内容幂等，异文拒绝。"""
        if not isinstance(voucher_id, str) or not voucher_id:
            raise InkInputError("voucher_id 必须是非空字符串")
        try:
            return self._put_voucher(voucher_id, plate, revision)
        except (InkInputError, InkRejected, InputError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
            with self.conn:
                self.conn.execute(
                    "INSERT INTO ink_attempt(voucher_id, ok, reason, created_at) VALUES (?, 0, ?, ?)",
                    (voucher_id, reason, _utc_now()),
                )
            raise

    def _put_voucher(self, voucher_id: str, plate: ProofPlate, revision: str) -> dict:
        computed = self.compute_plate(plate, revision)
        fingerprint = _canonical_json(
            {
                "plate": plate.to_dict(),
                "revision_payload": computed["revision_payload"],
                "horizon": computed["horizon"],
                "snapshots": computed["snapshots"],
            }
        )
        input_hash = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        existing = self.conn.execute(
            "SELECT input_hash FROM ink_voucher WHERE voucher_id = ?", (voucher_id,)
        ).fetchone()
        if existing is not None:
            if existing[0] != input_hash:
                raise InkRejected(f"voucher_id {voucher_id!r} 已存在且输入异文，拒绝覆盖")
            return self.read_voucher(voucher_id)

        created_at = _utc_now()
        h0, h1 = computed["horizon"]
        with self.conn:
            self.conn.execute(
                "INSERT INTO ink_voucher(voucher_id, plate_id, revision, plate_json, "
                "revision_json, snapshot_json, horizon_h0, horizon_h1, input_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    voucher_id,
                    plate.plate_id,
                    revision,
                    _canonical_json(plate.to_dict()),
                    _canonical_json(computed["revision_payload"]),
                    _canonical_json(computed["snapshots"]),
                    h0,
                    h1,
                    input_hash,
                    created_at,
                ),
            )
            for cell_out in computed["cells"]:
                cell = cell_out["cell"]
                self.conn.execute(
                    "INSERT INTO ink_cell(voucher_id, cell_id, x0, y0, x1, y1, total) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        voucher_id,
                        cell["cell_id"],
                        cell["x0"],
                        cell["y0"],
                        cell["x1"],
                        cell["y1"],
                        cell_out["total"],
                    ),
                )
                for stamp_out in cell_out["stamps"]:
                    self.conn.execute(
                        "INSERT INTO ink_stamp_cell(voucher_id, cell_id, cart_id, flow_rate, total) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            voucher_id,
                            cell["cell_id"],
                            stamp_out["cart_id"],
                            stamp_out["flow_rate"],
                            stamp_out["total"],
                        ),
                    )
                    for seg_out in stamp_out["segments"]:
                        self.conn.execute(
                            "INSERT INTO ink_segment_contrib(voucher_id, cell_id, cart_id, "
                            "segment_index, seg_t0, seg_t1, ink) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                voucher_id,
                                cell["cell_id"],
                                stamp_out["cart_id"],
                                seg_out["segment_index"],
                                seg_out["seg_t0"],
                                seg_out["seg_t1"],
                                seg_out["ink"],
                            ),
                        )
            self.conn.execute(
                "INSERT INTO ink_attempt(voucher_id, ok, reason, created_at) VALUES (?, 1, ?, ?)",
                (voucher_id, "published", created_at),
            )
        return self.read_voucher(voucher_id)

    # ---- 读取 ----------------------------------------------------------
    def read_voucher(self, voucher_id: str) -> dict:
        row = self.conn.execute(
            "SELECT voucher_id, plate_id, revision, plate_json, revision_json, snapshot_json, "
            "horizon_h0, horizon_h1, input_hash, created_at FROM ink_voucher WHERE voucher_id = ?",
            (voucher_id,),
        ).fetchone()
        if row is None:
            raise InkInputError(f"凭单 {voucher_id!r} 不存在")
        (
            vid, plate_id, revision, plate_json, revision_json, snapshot_json,
            h0, h1, input_hash, created_at,
        ) = row
        cells = []
        for cell_id, x0, y0, x1, y1, total in self.conn.execute(
            "SELECT cell_id, x0, y0, x1, y1, total FROM ink_cell "
            "WHERE voucher_id = ? ORDER BY rowid",
            (voucher_id,),
        ).fetchall():
            stamps = []
            for cart_id, flow_rate, stamp_total in self.conn.execute(
                "SELECT cart_id, flow_rate, total FROM ink_stamp_cell "
                "WHERE voucher_id = ? AND cell_id = ? ORDER BY cart_id",
                (voucher_id, cell_id),
            ).fetchall():
                segs = [
                    {
                        "segment_index": seg_idx,
                        "seg_t0": t0,
                        "seg_t1": t1,
                        "ink": ink,
                    }
                    for seg_idx, t0, t1, ink in self.conn.execute(
                        "SELECT segment_index, seg_t0, seg_t1, ink FROM ink_segment_contrib "
                        "WHERE voucher_id = ? AND cell_id = ? AND cart_id = ? "
                        "ORDER BY segment_index",
                        (voucher_id, cell_id, cart_id),
                    ).fetchall()
                ]
                stamps.append(
                    {
                        "cart_id": cart_id,
                        "flow_rate": flow_rate,
                        "total": stamp_total,
                        "segments": segs,
                    }
                )
            cells.append(
                {
                    "cell": {"cell_id": cell_id, "x0": x0, "y0": y0, "x1": x1, "y1": y1},
                    "total": total,
                    "stamps": stamps,
                }
            )
        return {
            "voucher_id": vid,
            "plate_id": plate_id,
            "revision": revision,
            "plate": json.loads(plate_json),
            "revision_payload": json.loads(revision_json),
            "snapshot": json.loads(snapshot_json),
            "horizon": [h0, h1],
            "input_hash": input_hash,
            "created_at": created_at,
            "cells": cells,
        }

    def cell_totals(self, voucher_id: str) -> Dict[str, Fraction]:
        return {
            cell_id: Fraction(total)
            for cell_id, total in self.conn.execute(
                "SELECT cell_id, total FROM ink_cell WHERE voucher_id = ?",
                (voucher_id,),
            ).fetchall()
        }

    def list_vouchers(self) -> List[str]:
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT voucher_id FROM ink_voucher ORDER BY created_at, rowid"
            ).fetchall()
        ]

    def list_attempts(self) -> List[dict]:
        return [
            {"voucher_id": r[0], "ok": bool(r[1]), "reason": r[2], "created_at": r[3]}
            for r in self.conn.execute(
                "SELECT voucher_id, ok, reason, created_at FROM ink_attempt ORDER BY id"
            ).fetchall()
        ]


# --------------------------------------------------------------------------- #
# 独立复核
# --------------------------------------------------------------------------- #
def verify_voucher(ledger: InkLedger, voucher_id: str) -> dict:
    """用凭单冻结的轮廓与轨迹快照重算，逐格/逐印模/逐段比对。

    复核不读当前目录或当前轨迹，只依赖凭单内容；发现任何篡改都返回
    ``verified=False``。
    """
    voucher = ledger.read_voucher(voucher_id)
    profiles = {
        item["profile_id"]: Profile(
            item["profile_id"],
            fraction(item["half_width"]),
            fraction(item["half_height"]),
        )
        for item in voucher["revision_payload"]["profiles"]
    }
    h0, h1 = fraction(voucher["horizon"][0]), fraction(voucher["horizon"][1])
    trajectories: Dict[str, Trajectory] = {}
    for cart_id, item in voucher["snapshot"].items():
        trajectories[cart_id] = Trajectory(
            cart_id=item["cart_id"],
            profile_id=item["profile_id"],
            knots=[
                Knot(fraction(k["t"]), fraction(k["x"]), fraction(k["y"]))
                for k in item["knots"]
            ],
            horizon=(h0, h1),
        )
    plate = ProofPlate.from_data(voucher["plate"])

    checks = []
    all_ok = True
    for cell_out in voucher["cells"]:
        cell_dict = cell_out["cell"]
        cell = Cell(
            cell_dict["cell_id"],
            fraction(cell_dict["x0"]),
            fraction(cell_dict["y0"]),
            fraction(cell_dict["x1"]),
            fraction(cell_dict["y1"]),
        )
        recomputed_total = Q(0)
        stamp_checks = []
        for stamp_out in cell_out["stamps"]:
            cart_id = stamp_out["cart_id"]
            rate = fraction(stamp_out["flow_rate"])
            traj = trajectories[cart_id]
            profile = profiles[traj.profile_id]
            recomputed_stamp = Q(0)
            recomputed_segs: Dict[int, Fraction] = {}
            for seg in traj.segments():
                lo = max(seg.t0, h0)
                hi = min(seg.t1, h1)
                if hi <= lo:
                    continue
                ink = rate * area_integral(profile, seg, cell, lo, hi)
                if ink != 0:
                    recomputed_segs[seg.index] = ink
                    recomputed_stamp += ink
            stored_segs = {
                int(s["segment_index"]): fraction(s["ink"]) for s in stamp_out["segments"]
            }
            segs_match = stored_segs == recomputed_segs
            stamp_match = Fraction(stamp_out["total"]) == recomputed_stamp
            recomputed_total += recomputed_stamp
            all_ok &= segs_match and stamp_match
            stamp_checks.append(
                {
                    "cart_id": cart_id,
                    "stamp_match": stamp_match,
                    "segment_match": segs_match,
                    "recomputed_total": qstr(recomputed_stamp),
                    "recomputed_segments": {
                        str(k): qstr(v) for k, v in sorted(recomputed_segs.items())
                    },
                }
            )
        cell_match = Fraction(cell_out["total"]) == recomputed_total
        # 各印模贡献之和必须等于格总量（防止印模行被删漏）。
        sum_stamps = sum(
            (Fraction(s["total"]) for s in cell_out["stamps"]), Q(0)
        )
        sum_match = sum_stamps == Fraction(cell_out["total"])
        all_ok &= cell_match and sum_match
        checks.append(
            {
                "cell_id": cell.cell_id,
                "cell_match": cell_match,
                "stamp_sum_match": sum_match,
                "recomputed_total": qstr(recomputed_total),
                "stamps": stamp_checks,
            }
        )
    # 凭单声明的印模集合必须与格网里挂载的一致（不能凭空多出/缺少印模行）。
    expected = {
        (cell.cell_id, s.cart_id) for cell in plate.cells for s in plate.stamps_for(cell.cell_id)
    }
    stored = {
        (c["cell"]["cell_id"], s["cart_id"]) for c in voucher["cells"] for s in c["stamps"]
    }
    all_ok &= expected == stored
    return {
        "voucher_id": voucher_id,
        "revision": voucher["revision"],
        "verified": bool(all_ok),
        "stamp_set_match": expected == stored,
        "checks": checks,
    }
