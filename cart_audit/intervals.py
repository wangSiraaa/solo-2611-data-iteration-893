"""半开/开闭混合时间区间代数。

核查域是半开区间 [h0, h1)。正面积重叠在两端都可能出现开端点：
严格不等式 ``|d| < S`` 使触碰点被排除，因此区间必须同时保存左右开闭标志，
不能退化为 (start, end) 二元组。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import List, Optional

from .rational import fraction

__all__ = ["TimeInterval", "merge_union", "intersect_intervals"]


@dataclass(frozen=True)
class TimeInterval:
    """带开闭端点的一维时间区间，left <= right。"""

    left: Fraction
    right: Fraction
    left_inclusive: bool = True
    right_inclusive: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "left", fraction(self.left, field="interval.left"))
        object.__setattr__(self, "right", fraction(self.right, field="interval.right"))
        if self.right < self.left:
            raise ValueError(f"区间右端不得小于左端: {self.left} > {self.right}")

    @property
    def is_empty(self) -> bool:
        # 零长度闭点（如 [a, a]）对“正面积重叠时长”没有贡献，
        # 仅在代数内部短暂出现；对外输出前统一视为空。
        if self.left == self.right and not (self.left_inclusive and self.right_inclusive):
            return True
        if self.left == self.right:
            return True
        return False

    @property
    def duration(self) -> Fraction:
        if self.left == self.right:
            return Fraction(0)
        return self.right - self.left

    def to_dict(self) -> dict:
        return {
            "left": str(self.left),
            "right": str(self.right),
            "left_inclusive": self.left_inclusive,
            "right_inclusive": self.right_inclusive,
            "duration": str(self.duration),
        }


def intersect_intervals(a: Optional[TimeInterval], b: Optional[TimeInterval]) -> Optional[TimeInterval]:
    """两个带端点标志的区间求交；无交集或仅剩孤立点时返回 None。"""
    if a is None or b is None:
        return None
    if a.right < b.left or b.right < a.left:
        return None
    if a.left > b.left:
        left, left_inc = a.left, a.left_inclusive
    elif b.left > a.left:
        left, left_inc = b.left, b.left_inclusive
    else:
        left = a.left
        left_inc = a.left_inclusive and b.left_inclusive
    if a.right < b.right:
        right, right_inc = a.right, a.right_inclusive
    elif b.right < a.right:
        right, right_inc = b.right, b.right_inclusive
    else:
        right = a.right
        right_inc = a.right_inclusive and b.right_inclusive
    result = TimeInterval(left, right, left_inc, right_inc)
    # 相交仅剩孤立点（零长度）时对正面积重叠无贡献，视为空。
    if result.left == result.right:
        return None
    return result


def _abut(a: TimeInterval, b: TimeInterval) -> bool:
    """相邻且共享端点至少一侧为闭：并集在该点连续，可合并。"""
    if a.right != b.left:
        return False
    return a.right_inclusive or b.left_inclusive


def merge_union(intervals: List[TimeInterval]) -> List[TimeInterval]:
    """把可能重复、相邻的同一对车重叠区间合并为最小并集表示。

    输入按左端点排序；被完全包含的区间直接吸收；仅触碰（两侧端点都开）
    不合并，因为并集中该点本就不属于任何区间。
    """
    ordered = sorted(intervals, key=lambda iv: (iv.left, not iv.left_inclusive, iv.right))
    merged: List[TimeInterval] = []
    for iv in ordered:
        if iv.is_empty:
            continue
        if not merged:
            merged.append(iv)
            continue
        last = merged[-1]
        if iv.left < last.right or (iv.left == last.right and _abut(last, iv)):
            if iv.right < last.right or (iv.right == last.right and last.right_inclusive):
                # iv 被 last 完全包含。
                continue
            if iv.right == last.right:
                right_inc = iv.right_inclusive or last.right_inclusive
                merged[-1] = TimeInterval(last.left, last.right, last.left_inclusive, right_inc)
            else:
                merged[-1] = TimeInterval(last.left, iv.right, last.left_inclusive, iv.right_inclusive)
        elif iv.left == last.right and not _abut(last, iv):
            # 同点但两端皆开：并集在此点断开，保留两个区间。
            merged.append(iv)
        else:
            merged.append(iv)
    return merged
