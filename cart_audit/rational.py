"""精确有理数处理。

输入只接受整数、:class:`fractions.Fraction` 或字符串（含 ``"p/q"``、
``"1.5"``、``"3e-2"`` 等形式）。二进制 ``float`` 一律拒绝，因为本系统的
合同要求按精确有理数解释资料中的每个数值。
"""

from __future__ import annotations

import json
from fractions import Fraction
from typing import Any

__all__ = ["fraction", "rational_json_loads", "qstr"]


def fraction(value: Any, *, field: str = "value") -> Fraction:
    """把输入值严格转换为 :class:`Fraction`。

    - int / Fraction：直接采用其精确值；
    - str：交给 Fraction 的字符串语法（含小数与科学记数法），非法即拒绝；
    - bool / float / 其他类型：拒绝。bool 即使是 int 子类也拒绝，避免把
      布尔量误当作尺寸或坐标。
    """
    if isinstance(value, bool):
        raise ValueError(f"{field} 不接受布尔值: {value!r}")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, Fraction):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field} 为空字符串，无法解释为有理数")
        try:
            return Fraction(text)
        except (ZeroDivisionError, ValueError) as exc:
            raise ValueError(f"{field} 不是合法有理数: {value!r}") from exc
    raise ValueError(f"{field} 必须是整数或字符串形式的精确有理数，拒绝浮点: {value!r}")


def qstr(value: Fraction) -> str:
    """规范字符串表示，用于 SQLite 存储与幂等哈希。"""
    value = fraction(value)
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def _rationalize(obj: Any) -> Any:
    """JSON 解析后的递归后处理：所有数字叶子转为 Fraction。"""
    if isinstance(obj, dict):
        return {key: _rationalize(val) for key, val in obj.items()}
    if isinstance(obj, list):
        return [_rationalize(val) for val in obj]
    if isinstance(obj, bool):
        # parse_float 不会碰到布尔；bool 只可能来自 JSON true/false，保留语义。
        return obj
    if isinstance(obj, (int, Fraction)):
        return Fraction(obj)
    if isinstance(obj, str):
        # 资料中的数字以 JSON 整数给出；字符串默认保留，需要数值化的位置
        # 由领域构造器调用 fraction() 显式处理。
        return obj
    if obj is None:
        return None
    raise ValueError(f"无法按精确有理数处理 JSON 叶子: {obj!r}")


def _reject_float(text: str) -> Fraction:
    """``json.loads`` 的 parse_float 钩子：拒绝一切浮点字面量。"""
    raise ValueError(
        f"检测到浮点字面量 {text!r}，本系统只接受精确整数/有理数字符串"
    )


def rational_json_loads(text: str) -> Any:
    """解析 JSON，数字一律转为 :class:`Fraction`，浮点字面量直接拒绝。"""
    raw = json.loads(text, parse_float=_reject_float)
    return _rationalize(raw)
