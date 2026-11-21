"""纸张配给：按墨量凭单为同时到达的一批试版分配有限纸种。

纸种与余张数由纸张管理员独立登记。每张试版有一份加急权重；每个*指定格*
在某纸种上容许的墨量闭区间可以不同。某纸种要能印一张试版，必须让该试版
**全部指定格**的墨量都落在各自闭区间内。

为同时到达的一批试版分配现有纸张时，目标按字典序：

1. 获配试版的权重总和最大；
2. 并列时获配张数最多；
3. 仍并列时“按试版号排序的纸种序列”字典序最小（未配排在所有纸种之后）。

本模块用精确 `Fraction` 权重与整数容量求解一个带容量二分匹配（最小费用流），
再逐版贪心固定字典序最小的可行纸种。确认整批在一个 ``BEGIN IMMEDIATE``
事务里一次性扣减：要么全扣、要么全不扣；并发确认同一个最后一张时只有一方
成功，失败方拿到 ``PaperShortage.shortages`` 里逐纸种的短缺量，可在补货后
用新库存重算。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from math import lcm
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .catalog import InputError
from .rational import fraction, qstr

__all__ = [
    "PlateSpec",
    "PaperShortage",
    "ConfirmationRejected",
    "PaperStore",
    "allocate",
    "AllocationResult",
]

Q = Fraction


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class PaperShortage(Exception):
    """确认时库存不足。``shortages`` 逐纸种给出请求量、可用量与缺口。"""

    def __init__(self, shortages: Dict[str, dict]):
        self.shortages = shortages
        detail = ", ".join(
            f"{pid}: 需要 {item['requested']} 张，仅有 {item['available']} 张"
            for pid, item in sorted(shortages.items())
        )
        super().__init__(f"纸张不足（{detail}）")


class ConfirmationRejected(Exception):
    """confirmation_id 已存在但计划异文，拒绝重复扣减。"""


class AllocationInputError(ValueError):
    """配给输入非法。"""


# --------------------------------------------------------------------------- #
# 输入模型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PlateSpec:
    """一张试版的配给需求。

    - ``weight``：加急权重，非负有理数；
    - ``ink_by_cell``：来自墨量凭单的各格墨量；
    - ``limits``：``{cell_id: {paper_type_id: [lo, hi]}}``，闭区间，指定格
      在某纸种上容许的墨量范围。未列出的纸种对该格不可用。
    """

    plate_id: str
    weight: Fraction
    ink_by_cell: Mapping[str, Fraction]
    limits: Mapping[str, Mapping[str, Tuple[Fraction, Fraction]]]

    def __post_init__(self) -> None:
        if not isinstance(self.plate_id, str) or not self.plate_id:
            raise AllocationInputError("plate_id 必须是非空字符串")
        weight = fraction(self.weight, field=f"plate {self.plate_id!r} weight")
        if weight < 0:
            raise AllocationInputError(f"plate {self.plate_id!r} 权重不得为负: {weight}")
        ink = {
            str(cid): fraction(val, field=f"ink[{cid}]")
            for cid, val in self.ink_by_cell.items()
        }
        limits: Dict[str, Dict[str, Tuple[Fraction, Fraction]]] = {}
        for cid, per_paper in self.limits.items():
            cid = str(cid)
            if cid not in ink:
                raise AllocationInputError(f"plate {self.plate_id!r} 指定格 {cid!r} 缺少墨量")
            entry: Dict[str, Tuple[Fraction, Fraction]] = {}
            for pid, interval in per_paper.items():
                pid = str(pid)
                if not isinstance(interval, (list, tuple)) or len(interval) != 2:
                    raise AllocationInputError(
                        f"plate {self.plate_id!r} 格 {cid!r} 纸种 {pid!r} 的区间必须是 [lo, hi]"
                    )
                lo = fraction(interval[0], field=f"limits[{cid}][{pid}].lo")
                hi = fraction(interval[1], field=f"limits[{cid}][{pid}].hi")
                if hi < lo:
                    raise AllocationInputError(
                        f"plate {self.plate_id!r} 格 {cid!r} 纸种 {pid!r} 区间 hi < lo"
                    )
                entry[pid] = (lo, hi)
            limits[cid] = entry
        object.__setattr__(self, "weight", weight)
        object.__setattr__(self, "ink_by_cell", ink)
        object.__setattr__(self, "limits", limits)

    @classmethod
    def from_data(cls, data: Mapping) -> "PlateSpec":
        try:
            return cls(
                str(data["plate_id"]),
                fraction(data["weight"], field="weight"),
                {
                    str(cid): fraction(val, field=f"ink[{cid}]")
                    for cid, val in data.get("ink_by_cell", {}).items()
                },
                {
                    str(cid): {
                        str(pid): (fraction(iv[0], field="lo"), fraction(iv[1], field="hi"))
                        for pid, iv in per_paper.items()
                    }
                    for cid, per_paper in data.get("limits", {}).items()
                },
            )
        except KeyError as exc:
            raise AllocationInputError(f"试版缺少字段 {exc.args[0]!r}") from exc

    def to_dict(self) -> dict:
        return {
            "plate_id": self.plate_id,
            "weight": qstr(self.weight),
            "ink_by_cell": {c: qstr(v) for c, v in sorted(self.ink_by_cell.items())},
            "limits": {
                c: {
                    p: [qstr(lo), qstr(hi)]
                    for p, (lo, hi) in sorted(per_paper.items())
                }
                for c, per_paper in sorted(self.limits.items())
            },
        }

    def feasible_papers(self) -> List[str]:
        """该试版可用的纸种（全部指定格均落在闭区间内）。"""
        if not self.limits:
            return []  # 没有指定格时无可判定纸种；调用方应显式给出
        candidates: Optional[set] = None
        for cid, per_paper in self.limits.items():
            ink = self.ink_by_cell.get(cid, Q(0))
            ok = {
                pid
                for pid, (lo, hi) in per_paper.items()
                if lo <= ink <= hi
            }
            candidates = ok if candidates is None else (candidates & ok)
        return sorted(candidates or set())


# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AllocationResult:
    """一次配给的结果（不含库存扣减，纯计算）。"""

    assignments: Tuple[Tuple[str, Optional[str]], ...]  # (plate_id, paper_type_id|None)
    total_weight: Fraction
    allocated_count: int
    usage: Mapping[str, int]
    remaining: Mapping[str, int]

    def as_dict(self) -> dict:
        return {
            "assignments": [
                {"plate_id": pid, "paper_type_id": ptid}
                for pid, ptid in self.assignments
            ],
            "total_weight": qstr(self.total_weight),
            "allocated_count": self.allocated_count,
            "usage": dict(sorted(self.usage.items())),
            "remaining": dict(sorted(self.remaining.items())),
        }


# --------------------------------------------------------------------------- #
# 最小费用流：带容量二分匹配，权重与张数二级字典序
# --------------------------------------------------------------------------- #
class _Edge:
    __slots__ = ("to", "rev", "cap", "cost")

    def __init__(self, to: int, rev: int, cap: int, cost: int):
        self.to = to
        self.rev = rev
        self.cap = cap
        self.cost = cost


class _MinCostFlow:
    def __init__(self, n: int):
        self.graph: List[List[_Edge]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: int, cost: int) -> _Edge:
        fwd = _Edge(v, len(self.graph[v]), cap, cost)
        rev = _Edge(u, len(self.graph[u]), 0, -cost)
        self.graph[u].append(fwd)
        self.graph[v].append(rev)
        return fwd

    def min_cost(self, source: int, sink: int) -> int:
        """在不产生正费用增广的前提下推流，返回最小总费用。"""
        from collections import deque

        n = len(self.graph)
        total = 0
        while True:
            dist: List[Optional[int]] = [None] * n
            pv = [-1] * n
            pe = [-1] * n
            inq = [False] * n
            dist[source] = 0
            dq = deque([source])
            inq[source] = True
            while dq:
                u = dq.popleft()
                inq[u] = False
                du = dist[u]
                for idx, edge in enumerate(self.graph[u]):
                    if edge.cap <= 0:
                        continue
                    nd = du + edge.cost
                    if dist[edge.to] is None or nd < dist[edge.to]:
                        dist[edge.to] = nd
                        pv[edge.to] = u
                        pe[edge.to] = idx
                        if not inq[edge.to]:
                            dq.append(edge.to)
                            inq[edge.to] = True
            if dist[sink] is None or dist[sink] >= 0:
                break
            # 增广一条最短路径（源->试版边容量为 1，故瓶颈为 1）。
            add = None
            v = sink
            while v != source:
                u = pv[v]
                edge = self.graph[u][pe[v]]
                add = edge.cap if add is None else min(add, edge.cap)
                v = u
            v = sink
            while v != source:
                u = pv[v]
                edge = self.graph[u][pe[v]]
                edge.cap -= add
                self.graph[v][edge.rev].cap += add
                v = u
            total += add * dist[sink]
        return total


def _optimal(plates: Sequence[PlateSpec], caps: Mapping[str, int]) -> Tuple[Fraction, int, Dict[str, Optional[str]]]:
    """返回剩余试版在剩余容量下的最优 (权重和, 张数, 分配)。"""
    if not plates:
        return Q(0), 0, {}
    # 把有理权重统一放大为整数，使权重与张数可用一个整数费用编码。
    denom = 1
    for plate in plates:
        denom = lcm(denom, plate.weight.denominator)
    weights_int = {p.plate_id: int(p.weight * denom) for p in plates}
    n = len(plates)
    big = n + 1  # 严格大于任何可能的张数差，保证先比权重、再比张数

    paper_ids = sorted({pid for p in plates for pid in p.feasible_papers() if caps.get(pid, 0) > 0})
    source = 0
    plate_base = 1
    paper_base = plate_base + n
    sink = paper_base + len(paper_ids)
    paper_index = {pid: paper_base + i for i, pid in enumerate(paper_ids)}
    flow = _MinCostFlow(sink + 1)

    edges: Dict[Tuple[str, str], _Edge] = {}
    p_index = {p.plate_id: plate_base + i for i, p in enumerate(plates)}
    for plate in plates:
        flow.add_edge(source, p_index[plate.plate_id], 1, 0)
        benefit = big * weights_int[plate.plate_id] + 1
        for pid in plate.feasible_papers():
            if pid not in paper_index:
                continue
            edges[(plate.plate_id, pid)] = flow.add_edge(
                p_index[plate.plate_id], paper_index[pid], 1, -benefit
            )
    for pid in paper_ids:
        flow.add_edge(paper_index[pid], sink, int(caps[pid]), 0)
    flow.min_cost(source, sink)

    assignment: Dict[str, Optional[str]] = {}
    total_weight = Q(0)
    count = 0
    for plate in plates:
        chosen = None
        for pid in plate.feasible_papers():
            edge = edges.get((plate.plate_id, pid))
            if edge is not None and edge.cap == 0:
                chosen = pid
                break
        assignment[plate.plate_id] = chosen
        if chosen is not None:
            total_weight += plate.weight
            count += 1
    return total_weight, count, assignment


def allocate(plates: Sequence[PlateSpec], stocks: Mapping[str, int]) -> AllocationResult:
    """按字典序目标为一批试版分配纸张，不修改库存。"""
    if not plates:
        raise AllocationInputError("试版批次不能为空")
    ids = [p.plate_id for p in plates]
    if len(set(ids)) != len(ids):
        raise AllocationInputError(f"试版号重复: {ids}")
    caps = {str(pid): _valid_stock(val, pid) for pid, val in stocks.items()}
    paper_ids = sorted(caps)

    ordered = sorted(plates, key=lambda p: p.plate_id)
    global_weight, global_count, _ = _optimal(ordered, caps)

    caps_work = dict(caps)
    chosen: Dict[str, Optional[str]] = {}
    fixed_weight = Q(0)
    fixed_count = 0
    for i, plate in enumerate(ordered):
        remaining = ordered[i + 1:]
        feasible = set(plate.feasible_papers())
        selected: Optional[str] = None
        for pid in paper_ids:
            if pid not in feasible:
                continue
            if caps_work[pid] <= 0:
                continue
            caps_work[pid] -= 1
            w_rem, c_rem, _ = _optimal(remaining, caps_work)
            if fixed_weight + plate.weight + w_rem == global_weight and fixed_count + 1 + c_rem == global_count:
                selected = pid
                break
            caps_work[pid] += 1
        if selected is not None:
            chosen[plate.plate_id] = selected
            fixed_weight += plate.weight
            fixed_count += 1
            continue
        # 未配：必须仍能达成全局最优。
        w_rem, c_rem, _ = _optimal(remaining, caps_work)
        if fixed_weight + w_rem == global_weight and fixed_count + c_rem == global_count:
            chosen[plate.plate_id] = None
        else:  # 理论上不可达：此前每一步都保持了可达性
            raise RuntimeError(f"试版 {plate.plate_id!r} 无法在保持最优的前提下定值")

    usage: Dict[str, int] = {}
    for pid, ptid in chosen.items():
        if ptid is not None:
            usage[ptid] = usage.get(ptid, 0) + 1
    remaining = {pid: caps[pid] - usage.get(pid, 0) for pid in paper_ids}
    assignments = tuple((p.plate_id, chosen[p.plate_id]) for p in ordered)
    return AllocationResult(
        assignments=assignments,
        total_weight=fixed_weight,
        allocated_count=fixed_count,
        usage=usage,
        remaining=remaining,
    )


def _valid_stock(value: object, name: str = "stock") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AllocationInputError(f"纸种 {name!r} 的余张数必须是非负整数: {value!r}")
    if value < 0:
        raise AllocationInputError(f"纸种 {name!r} 的余张数不得为负: {value}")
    return value


# --------------------------------------------------------------------------- #
# 持久化：纸种库存、确认与旧印样
# --------------------------------------------------------------------------- #
_PAPER_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_type (
    paper_type_id TEXT PRIMARY KEY,
    stock         INTEGER NOT NULL CHECK (stock >= 0)
);
CREATE TABLE IF NOT EXISTS paper_confirmation (
    confirmation_id TEXT PRIMARY KEY,
    plan_json       TEXT NOT NULL,
    result_json     TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sample_record (
    record_id     TEXT PRIMARY KEY,
    plate_id      TEXT NOT NULL,
    paper_type_id TEXT NOT NULL,
    voucher_id    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
"""


class PaperStore:
    """纸张管理员：逐纸种登记余张数，原子确认整批配给，保存旧印样。"""

    def __init__(self, db_path: str, busy_timeout_ms: int = 5000):
        self.conn = sqlite3.connect(
            db_path, timeout=busy_timeout_ms / 1000.0, check_same_thread=False
        )
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        self.conn.executescript(_PAPER_SCHEMA)
        self.conn.commit()
        # 同一连接被多线程共享时序列化访问；跨进程并发由 BEGIN IMMEDIATE 串行化。
        self._lock = threading.RLock()

    def close(self) -> None:
        self.conn.close()

    # ---- 纸种登记 ------------------------------------------------------
    def register_paper(self, paper_type_id: str, stock: int) -> None:
        paper_type_id = str(paper_type_id)
        if not paper_type_id:
            raise AllocationInputError("paper_type_id 必须是非空字符串")
        stock = _valid_stock(stock, paper_type_id)
        with self.conn:
            row = self.conn.execute(
                "SELECT stock FROM paper_type WHERE paper_type_id = ?", (paper_type_id,)
            ).fetchone()
            if row is not None:
                if row[0] != stock:
                    raise InputError(
                        f"纸种 {paper_type_id!r} 已登记 {row[0]} 张，登记异文 {stock} 张"
                    )
                return
            self.conn.execute(
                "INSERT INTO paper_type(paper_type_id, stock) VALUES (?, ?)",
                (paper_type_id, stock),
            )

    def add_stock(self, paper_type_id: str, delta: int) -> int:
        paper_type_id = str(paper_type_id)
        if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0:
            raise AllocationInputError("补货量必须是非零整数")
        with self.conn:
            row = self.conn.execute(
                "SELECT stock FROM paper_type WHERE paper_type_id = ?", (paper_type_id,)
            ).fetchone()
            if row is None:
                raise InputError(f"未知纸种 {paper_type_id!r}")
            new_stock = row[0] + delta
            if new_stock < 0:
                raise AllocationInputError(
                    f"纸种 {paper_type_id!r} 余张数不能变为负: {row[0]} + ({delta})"
                )
            self.conn.execute(
                "UPDATE paper_type SET stock = ? WHERE paper_type_id = ?",
                (new_stock, paper_type_id),
            )
            return new_stock

    def stocks(self) -> Dict[str, int]:
        return {
            pid: stock
            for pid, stock in self.conn.execute(
                "SELECT paper_type_id, stock FROM paper_type ORDER BY paper_type_id"
            ).fetchall()
        }

    def stock(self, paper_type_id: str) -> int:
        row = self.conn.execute(
            "SELECT stock FROM paper_type WHERE paper_type_id = ?", (str(paper_type_id),)
        ).fetchone()
        if row is None:
            raise InputError(f"未知纸种 {paper_type_id!r}")
        return row[0]

    # ---- 配给 ----------------------------------------------------------
    def allocate_batch(self, plates: Sequence[PlateSpec]) -> AllocationResult:
        return allocate(plates, self.stocks())

    # ---- 原子确认 ------------------------------------------------------
    def confirm(self, confirmation_id: str, result: AllocationResult) -> dict:
        """整批扣减。要么全扣、要么全不扣；同一 id 同计划幂等，异文拒绝。

        并发确认争抢最后一张时，``BEGIN IMMEDIATE`` 使写事务串行化；
        后到者看到库存不足，抛 :class:`PaperShortage`（含逐纸种缺口）。
        """
        confirmation_id = str(confirmation_id)
        if not confirmation_id:
            raise AllocationInputError("confirmation_id 必须是非空字符串")
        usage = {pid: int(cnt) for pid, cnt in result.usage.items()}
        if any(cnt <= 0 for cnt in usage.values()):
            raise AllocationInputError("确认计划含非正用量")
        plan_json = _canonical_json(
            {
                "assignments": [
                    {"plate_id": pid, "paper_type_id": ptid}
                    for pid, ptid in result.assignments
                ],
                "usage": dict(sorted(usage.items())),
            }
        )
        conn = self.conn
        with self._lock:
            return self._confirm_locked(confirmation_id, plan_json, result)

    def _confirm_locked(self, confirmation_id: str, plan_json: str, result: AllocationResult) -> dict:
        conn = self.conn
        usage = {pid: int(cnt) for pid, cnt in result.usage.items()}
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT plan_json, result_json FROM paper_confirmation WHERE confirmation_id = ?",
                (confirmation_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != plan_json:
                    conn.rollback()
                    raise ConfirmationRejected(
                        f"confirmation_id {confirmation_id!r} 已存在且计划异文，拒绝重复扣减"
                    )
                conn.rollback()
                return json.loads(existing[1])
            # 在同一写锁内读取全部相关库存，一次性收集所有短缺纸种。
            shortages: Dict[str, dict] = {}
            available: Dict[str, int] = {}
            for pid, need in usage.items():
                row = conn.execute(
                    "SELECT stock FROM paper_type WHERE paper_type_id = ?", (pid,)
                ).fetchone()
                have = 0 if row is None else row[0]
                available[pid] = have
                if have < need:
                    shortages[pid] = {
                        "requested": need,
                        "available": have,
                        "missing": need - have,
                    }
            if shortages:
                conn.rollback()
                raise PaperShortage(shortages)
            for pid, need in usage.items():
                cur = conn.execute(
                    "UPDATE paper_type SET stock = stock - ? "
                    "WHERE paper_type_id = ? AND stock >= ?",
                    (need, pid, need),
                )
                if cur.rowcount != 1:
                    # 写锁下不应发生；保守回滚以防任何不一致。
                    conn.rollback()
                    raise PaperShortage(
                        {pid: {"requested": need, "available": available[pid], "missing": need - available[pid]}}
                    )
            result_payload = {
                "confirmation_id": confirmation_id,
                "assignments": [
                    {"plate_id": pid, "paper_type_id": ptid}
                    for pid, ptid in result.assignments
                ],
                "usage": dict(sorted(usage.items())),
                "stocks_after": self._stocks_locked(),
                "created_at": _utc_now(),
            }
            conn.execute(
                "INSERT INTO paper_confirmation(confirmation_id, plan_json, result_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (confirmation_id, plan_json, _canonical_json(result_payload), result_payload["created_at"]),
            )
            conn.commit()
        except (PaperShortage, ConfirmationRejected, InputError):
            raise
        except Exception:
            conn.rollback()
            raise
        return result_payload

    def _stocks_locked(self) -> Dict[str, int]:
        return {
            pid: stock
            for pid, stock in self.conn.execute(
                "SELECT paper_type_id, stock FROM paper_type ORDER BY paper_type_id"
            ).fetchall()
        }

    def read_confirmation(self, confirmation_id: str) -> dict:
        row = self.conn.execute(
            "SELECT result_json FROM paper_confirmation WHERE confirmation_id = ?",
            (str(confirmation_id),),
        ).fetchone()
        if row is None:
            raise InputError(f"确认记录 {confirmation_id!r} 不存在")
        return json.loads(row[0])

    # ---- 旧印样 --------------------------------------------------------
    def record_sample(
        self, record_id: str, plate_id: str, paper_type_id: str, voucher_id: str
    ) -> dict:
        """冻结一张旧印样的原用纸与墨量凭单；同 id 同内容幂等，异文拒绝。"""
        record_id = str(record_id)
        payload = {
            "record_id": record_id,
            "plate_id": str(plate_id),
            "paper_type_id": str(paper_type_id),
            "voucher_id": str(voucher_id),
        }
        payload_json = _canonical_json(payload)
        with self.conn:
            row = self.conn.execute(
                "SELECT payload_json FROM sample_record WHERE record_id = ?", (record_id,)
            ).fetchone()
            if row is not None:
                if row[0] != payload_json:
                    raise InputError(f"旧印样 {record_id!r} 已存在且内容异文，拒绝覆盖")
                return json.loads(row[0])
            self.conn.execute(
                "INSERT INTO sample_record(record_id, plate_id, paper_type_id, voucher_id, "
                "payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record_id,
                    payload["plate_id"],
                    payload["paper_type_id"],
                    payload["voucher_id"],
                    payload_json,
                    _utc_now(),
                ),
            )
        return payload

    def read_sample(self, record_id: str) -> dict:
        row = self.conn.execute(
            "SELECT payload_json FROM sample_record WHERE record_id = ?", (str(record_id),)
        ).fetchone()
        if row is None:
            raise InputError(f"旧印样 {record_id!r} 不存在")
        return json.loads(row[0])

    def list_samples(self) -> List[dict]:
        return [
            json.loads(payload)
            for (payload,) in self.conn.execute(
                "SELECT payload_json FROM sample_record ORDER BY created_at, rowid"
            ).fetchall()
        ]
