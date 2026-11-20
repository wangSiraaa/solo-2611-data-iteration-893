"""领域模型：轮廓、时间结点、轨迹与动作段。

全部坐标/时间均为 :class:`fractions.Fraction`。轨迹是时间严格递增的
分段线性函数，在公共核查域（半开区间）上完整覆盖，不允许外推、缺段或瞬移。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import List

from .rational import fraction

__all__ = ["Profile", "Knot", "Trajectory", "MotionSegment"]


class ValidationError(ValueError):
    """非法轮廓或轨迹。"""


@dataclass(frozen=True)
class Profile:
    """轴对齐矩形轮廓，姿态固定；半宽/半高必须为正有理数。"""

    profile_id: str
    half_width: Fraction
    half_height: Fraction

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not self.profile_id:
            raise ValidationError("profile_id 必须是非空字符串")
        width = fraction(self.half_width, field=f"profile {self.profile_id!r} half_width")
        height = fraction(self.half_height, field=f"profile {self.profile_id!r} half_height")
        if width <= 0:
            raise ValidationError(f"profile {self.profile_id!r} half_width 必须为正有理数")
        if height <= 0:
            raise ValidationError(f"profile {self.profile_id!r} half_height 必须为正有理数")
        object.__setattr__(self, "half_width", width)
        object.__setattr__(self, "half_height", height)

    @classmethod
    def from_data(cls, data: dict) -> "Profile":
        try:
            return cls(
                profile_id=str(data["profile_id"]),
                half_width=fraction(data["half_width"], field="half_width"),
                half_height=fraction(data["half_height"], field="half_height"),
            )
        except KeyError as exc:
            raise ValidationError(f"轮廓缺少字段 {exc.args[0]!r}") from exc

    def to_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "half_width": str(self.half_width),
            "half_height": str(self.half_height),
        }


@dataclass(frozen=True)
class Knot:
    """一个时间结点上的中心位置。"""

    t: Fraction
    x: Fraction
    y: Fraction

    @classmethod
    def from_data(cls, data: dict) -> "Knot":
        try:
            return cls(
                t=fraction(data["t"], field="knot.t"),
                x=fraction(data["x"], field="knot.x"),
                y=fraction(data["y"], field="knot.y"),
            )
        except KeyError as exc:
            raise ValidationError(f"时间结点缺少字段 {exc.args[0]!r}") from exc

    def to_dict(self) -> dict:
        return {"t": str(self.t), "x": str(self.x), "y": str(self.y)}


@dataclass(frozen=True)
class MotionSegment:
    """两个相邻结点之间的线性动作段，半开时间区间 [t0, t1)。"""

    index: int
    t0: Fraction
    t1: Fraction
    x0: Fraction
    y0: Fraction
    x1: Fraction
    y1: Fraction

    @property
    def duration(self) -> Fraction:
        return self.t1 - self.t0

    def position_at(self, t: Fraction) -> tuple[Fraction, Fraction]:
        """段内（含端点）线性插值。"""
        span = self.t1 - self.t0
        ratio = (t - self.t0) / span
        return (
            self.x0 + (self.x1 - self.x0) * ratio,
            self.y0 + (self.y1 - self.y0) * ratio,
        )


@dataclass(frozen=True)
class Trajectory:
    """一辆推车在核查域上完整覆盖的分段线性轨迹。"""

    cart_id: str
    profile_id: str
    knots: List[Knot]
    horizon: tuple[Fraction, Fraction]

    def __post_init__(self) -> None:
        if not isinstance(self.cart_id, str) or not self.cart_id:
            raise ValidationError("cart_id 必须是非空字符串")
        if not isinstance(self.profile_id, str) or not self.profile_id:
            raise ValidationError(f"cart {self.cart_id!r} 的 profile_id 必须是非空字符串")
        if len(self.knots) < 2:
            raise ValidationError(f"cart {self.cart_id!r} 至少需要两个时间结点")
        prev = None
        for knot in self.knots:
            if not isinstance(knot, Knot):
                raise ValidationError(f"cart {self.cart_id!r} 的结点类型非法")
            if prev is not None and knot.t <= prev.t:
                raise ValidationError(
                    f"cart {self.cart_id!r} 时间结点必须严格递增: {prev.t} 后出现 {knot.t}"
                )
            prev = knot
        h0, h1 = self.horizon
        if not h1 > h0:
            raise ValidationError("核查域必须是非空半开区间 [h0, h1)，且 h1 > h0")
        first_t, last_t = self.knots[0].t, self.knots[-1].t
        # 完整覆盖且不外推：轨迹结点必须恰好落在核查域两端。
        if first_t != h0 or last_t != h1:
            raise ValidationError(
                f"cart {self.cart_id!r} 必须完整覆盖核查域 [{h0}, {h1}) 且不得外推："
                f"轨迹起止为 [{first_t}, {last_t}]"
            )

    @classmethod
    def from_data(cls, data: dict, horizon: tuple[Fraction, Fraction]) -> "Trajectory":
        try:
            cart_id = str(data["cart_id"])
            profile_id = str(data["profile_id"])
            raw_knots = data["knots"]
        except KeyError as exc:
            raise ValidationError(f"轨迹缺少字段 {exc.args[0]!r}") from exc
        if not isinstance(raw_knots, list) or not raw_knots:
            raise ValidationError(f"cart {cart_id!r} 的 knots 必须是非空列表")
        knots = [Knot.from_data(item) for item in raw_knots]
        return cls(cart_id=cart_id, profile_id=profile_id, knots=knots, horizon=horizon)

    def segments(self) -> List[MotionSegment]:
        result: List[MotionSegment] = []
        for i in range(len(self.knots) - 1):
            a, b = self.knots[i], self.knots[i + 1]
            result.append(
                MotionSegment(
                    index=i,
                    t0=a.t,
                    t1=b.t,
                    x0=a.x,
                    y0=a.y,
                    x1=b.x,
                    y1=b.y,
                )
            )
        return result

    def position_at(self, t: Fraction) -> tuple[Fraction, Fraction]:
        """在核查域内取中心位置（域内连续，端点可取值）。"""
        h0, h1 = self.horizon
        if t < h0 or t > h1:
            raise ValidationError(f"cart {self.cart_id!r} 在 t={t} 处外推（核查域 [{h0}, {h1})）")
        for segment in self.segments():
            if segment.t0 <= t <= segment.t1:
                return segment.position_at(t)
        raise ValidationError(f"cart {self.cart_id!r} 在 t={t} 处缺段")  # 理论上不可达
