"""SQLite 持久化：版本化轮廓目录与当前轨迹快照存储。

设计要点：

- 轮廓按修订版本（revision）整体保存，版本一经写入即冻结；同版本同内容
  复读幂等，异文拒绝。核查报告通过 ``report_profile_snapshot`` 留存当时
  使用的尺寸，因此后续修订不会改变旧报告。
- 轨迹按车保存稳定的 cart_id / profile_id 与严格递增结点；核查域作为
  单例行保存，所有轨迹必须在构造时即完整覆盖该半开区间。
- 所有有理数以规范分数字符串（``"p/q"``）落库，读出时还原为
  :class:`fractions.Fraction`，全程无浮点。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from fractions import Fraction
from typing import Dict, List

from .models import Knot, Profile, Trajectory
from .rational import qstr

__all__ = ["ProfileCatalog", "MotionStore", "InputError"]


class InputError(ValueError):
    """输入资料非法（轮廓、轨迹、版本冲突等）。"""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS profile_revision (
    revision   TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profile (
    revision    TEXT NOT NULL REFERENCES profile_revision(revision),
    profile_id  TEXT NOT NULL,
    half_width  TEXT NOT NULL,
    half_height TEXT NOT NULL,
    PRIMARY KEY (revision, profile_id)
);
CREATE TABLE IF NOT EXISTS horizon (
    id  INTEGER PRIMARY KEY CHECK (id = 1),
    h0  TEXT NOT NULL,
    h1  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cart (
    cart_id    TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trajectory_knot (
    cart_id     TEXT NOT NULL REFERENCES cart(cart_id),
    knot_index  INTEGER NOT NULL,
    t           TEXT NOT NULL,
    x           TEXT NOT NULL,
    y           TEXT NOT NULL,
    PRIMARY KEY (cart_id, knot_index)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProfileCatalog:
    """版本化轮廓目录（独立服务，可被核查库或外部直接查询）。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ---- 写入 ----------------------------------------------------------
    def put_revision(self, revision: str, profiles: List[Profile]) -> None:
        if not isinstance(revision, str) or not revision:
            raise InputError("revision 必须是非空字符串")
        if not profiles:
            raise InputError(f"修订 {revision!r} 至少要包含一个轮廓")
        ids = [p.profile_id for p in profiles]
        if len(set(ids)) != len(ids):
            raise InputError(f"修订 {revision!r} 内 profile_id 重复: {ids}")
        payload = json.dumps(
            {"revision": revision, "profiles": [p.to_dict() for p in profiles]},
            sort_keys=True,
            ensure_ascii=False,
        )
        with self.conn:
            row = self.conn.execute(
                "SELECT payload FROM profile_revision WHERE revision = ?", (revision,)
            ).fetchone()
            if row is not None:
                if row[0] != payload:
                    raise InputError(
                        f"轮廓修订 {revision!r} 已存在且内容不同，版本不可篡改"
                    )
                return  # 同内容复读：幂等
            self.conn.execute(
                "INSERT INTO profile_revision(revision, payload, created_at) VALUES (?, ?, ?)",
                (revision, payload, _now()),
            )
            self.conn.executemany(
                "INSERT INTO profile(revision, profile_id, half_width, half_height) "
                "VALUES (?, ?, ?, ?)",
                [
                    (revision, p.profile_id, qstr(p.half_width), qstr(p.half_height))
                    for p in profiles
                ],
            )

    # ---- 读取 ----------------------------------------------------------
    def list_revisions(self) -> List[str]:
        rows = self.conn.execute(
            "SELECT revision FROM profile_revision ORDER BY rowid"
        ).fetchall()
        return [r[0] for r in rows]

    def get_revision_payload(self, revision: str) -> dict:
        row = self.conn.execute(
            "SELECT payload FROM profile_revision WHERE revision = ?", (revision,)
        ).fetchone()
        if row is None:
            raise InputError(f"未知轮廓修订 {revision!r}")
        return json.loads(row[0])

    def get_profile(self, revision: str, profile_id: str) -> Profile:
        row = self.conn.execute(
            "SELECT profile_id, half_width, half_height FROM profile "
            "WHERE revision = ? AND profile_id = ?",
            (revision, profile_id),
        ).fetchone()
        if row is None:
            raise InputError(f"修订 {revision!r} 中不存在轮廓 {profile_id!r}")
        return Profile(row[0], Fraction(row[1]), Fraction(row[2]))

    def revision_fingerprint(self, revision: str) -> str:
        """版本内容指纹，供报告幂等比对。"""
        return self.get_revision_payload(revision)  # 调用方再做哈希；此处保证版本存在


class MotionStore:
    """当前轨迹存储（核查时以不可变快照形式进入报告）。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def set_horizon(self, h0: Fraction, h1: Fraction) -> None:
        if not h1 > h0:
            raise InputError("核查域必须是非空半开区间 [h0, h1)，且 h1 > h0")
        with self.conn:
            row = self.conn.execute("SELECT h0, h1 FROM horizon WHERE id = 1").fetchone()
            if row is not None:
                if (Fraction(row[0]), Fraction(row[1])) != (h0, h1):
                    raise InputError(
                        f"核查域已固定为 [{row[0]}, {row[1]})，不能改为 [{qstr(h0)}, {qstr(h1)})"
                    )
                return
            self.conn.execute(
                "INSERT INTO horizon(id, h0, h1) VALUES (1, ?, ?)", (qstr(h0), qstr(h1))
            )

    def get_horizon(self) -> tuple[Fraction, Fraction]:
        row = self.conn.execute("SELECT h0, h1 FROM horizon WHERE id = 1").fetchone()
        if row is None:
            raise InputError("尚未载入核查域 horizon")
        return Fraction(row[0]), Fraction(row[1])

    def upsert_trajectory(self, trajectory: Trajectory) -> None:
        horizon = self.get_horizon()
        if trajectory.horizon != horizon:
            raise InputError(
                f"cart {trajectory.cart_id!r} 的核查域 {trajectory.horizon} 与存储域 {horizon} 不一致"
            )
        # 构造器已保证严格递增、完整覆盖；这里再持久化。
        with self.conn:
            self.conn.execute(
                "INSERT INTO cart(cart_id, profile_id) VALUES (?, ?) "
                "ON CONFLICT(cart_id) DO UPDATE SET profile_id = excluded.profile_id",
                (trajectory.cart_id, trajectory.profile_id),
            )
            self.conn.execute(
                "DELETE FROM trajectory_knot WHERE cart_id = ?", (trajectory.cart_id,)
            )
            self.conn.executemany(
                "INSERT INTO trajectory_knot(cart_id, knot_index, t, x, y) VALUES (?, ?, ?, ?, ?)",
                [
                    (trajectory.cart_id, i, qstr(k.t), qstr(k.x), qstr(k.y))
                    for i, k in enumerate(trajectory.knots)],
            )

    def list_cart_ids(self) -> List[str]:
        rows = self.conn.execute("SELECT cart_id FROM cart ORDER BY cart_id").fetchall()
        return [r[0] for r in rows]

    def get_trajectory(self, cart_id: str) -> Trajectory:
        cart_row = self.conn.execute(
            "SELECT profile_id FROM cart WHERE cart_id = ?", (cart_id,)
        ).fetchone()
        if cart_row is None:
            raise InputError(f"未知 cart_id {cart_id!r}")
        knot_rows = self.conn.execute(
            "SELECT t, x, y FROM trajectory_knot WHERE cart_id = ? ORDER BY knot_index",
            (cart_id,),
        ).fetchall()
        knots = [Knot(Fraction(t), Fraction(x), Fraction(y)) for t, x, y in knot_rows]
        return Trajectory(
            cart_id=cart_id,
            profile_id=cart_row[0],
            knots=knots,
            horizon=self.get_horizon(),
        )

    def snapshot(self, cart_ids: List[str]) -> Dict[str, dict]:
        """供报告留存与指纹计算的不可变轨迹快照（规范字典）。"""
        result: Dict[str, dict] = {}
        for cart_id in sorted(cart_ids):
            traj = self.get_trajectory(cart_id)
            result[cart_id] = {
                "cart_id": traj.cart_id,
                "profile_id": traj.profile_id,
                "knots": [k.to_dict() for k in traj.knots],
            }
        return result
