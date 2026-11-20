"""独立轮廓服务：在共同线性时段上精确求解正面积重叠时间集合。

合同（严格）：

- 两辆车的轴对齐矩形在时刻 t 正面积重叠，当且仅当两个方向同时满足
  ``|dx(t)| < half_width_a + half_width_b`` 与
  ``|dy(t)| < half_height_a + half_height_b``；
- 只碰边（一个方向相等）或只碰角（两个方向都相等）不算重叠，因此所有
  根点（等号成立处）一律排除；
- 输入是两个线性动作段，中心距离在段内是 t 的一次函数，不等式
  ``|d0 + v*tau| < S`` 用有理数精确求根，不做采样、不做浮点比较。

服务本身不依赖 SQLite，轮廓与动作段由调用方传入，可独立复核。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Optional

from .intervals import TimeInterval, intersect_intervals
from .models import MotionSegment, Profile

__all__ = ["ProfileOverlapService"]


class ProfileOverlapService:
    """无状态轮廓重叠求解器。"""

    # ---- 单方向严格线性不等式 -----------------------------------------
    @staticmethod
    def _axis_interval(
        d0: Fraction,
        slope: Fraction,
        limit: Fraction,
        lo: Fraction,
        hi: Fraction,
    ) -> Optional[TimeInterval]:
        """求解 lo <= t < hi 上的 ``|d0 + slope*(t-lo)| < limit``。

        返回带开闭标志的时间区间；无解（含恰好接触的孤立点）返回 None。
        """
        if limit <= 0:
            # 正半尺寸之和恒为正；此分支仅作防御。
            return None
        if slope == 0:
            if abs(d0) < limit:
                return TimeInterval(lo, hi, True, False)
            return None
        # |d0 + slope*tau| < limit  <=>  -limit < d0 + slope*tau < limit
        tau_low = (-limit - d0) / slope
        tau_high = (limit - d0) / slope
        if tau_low > tau_high:
            tau_low, tau_high = tau_high, tau_low
        root_low = lo + tau_low
        root_high = lo + tau_high
        low = max(root_low, lo)
        high = min(root_high, hi)
        if high < low:
            return None
        if high == low:
            # 仅剩一个时刻：严格不等式下根点被排除，不是正面积重叠。
            return None
        # 左端点只有在它就是域起点且起点处已严格满足时才闭；
        # 任何根点处等号成立，必须开。
        left_inclusive = low == lo and abs(d0) < limit
        # 右端点：半开 cell 的 hi 本就排除；根点按严格合同也排除，恒为开。
        return TimeInterval(low, high, left_inclusive, False)

    def segment_overlap(
        self,
        profile_a: Profile,
        segment_a: MotionSegment,
        profile_b: Profile,
        segment_b: MotionSegment,
    ) -> Optional[TimeInterval]:
        """两个线性动作段共同时间范围内的正面积重叠区间。

        共同线性时段取两段时间跨度的交集；无共同时间返回 None。
        完全静止、同速（相对速度为 0）由 ``slope == 0`` 分支精确处理；
        只在一个方向接触时，该方向区间为空，整体结论即为不冲突。
        """
        lo = max(segment_a.t0, segment_b.t0)
        hi = min(segment_a.t1, segment_b.t1)
        if lo >= hi:
            return None
        xa0, ya0 = segment_a.position_at(lo)
        xb0, yb0 = segment_b.position_at(lo)
        # 相对速度 = 两端位移差 / 段时长（分段线性 => 段内恒定）。
        vx_a = (segment_a.x1 - segment_a.x0) / segment_a.duration
        vy_a = (segment_a.y1 - segment_a.y0) / segment_a.duration
        vx_b = (segment_b.x1 - segment_b.x0) / segment_b.duration
        vy_b = (segment_b.y1 - segment_b.y0) / segment_b.duration
        dx0, dy0 = xa0 - xb0, ya0 - yb0
        vx, vy = vx_a - vx_b, vy_a - vy_b
        width_sum = profile_a.half_width + profile_b.half_width
        height_sum = profile_a.half_height + profile_b.half_height
        x_interval = self._axis_interval(dx0, vx, width_sum, lo, hi)
        y_interval = self._axis_interval(dy0, vy, height_sum, lo, hi)
        return intersect_intervals(x_interval, y_interval)
