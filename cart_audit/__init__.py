"""独立推车轮廓目录与连续动作核查库。

仅处理合成平面轨迹：轴对齐矩形轮廓（正有理数半宽/半高），姿态固定，
中心随时间分段线性平移。所有数值按精确有理数（:class:`fractions.Fraction`）
处理，不做浮点近似、不控制任何实体设备。
"""

from .rational import fraction, rational_json_loads, qstr
from .models import Profile, Knot, Trajectory, MotionSegment
from .intervals import TimeInterval, merge_union
from .catalog import ProfileCatalog, MotionStore, InputError
from .service import ProfileOverlapService
from .library import AuditLibrary, AuditRejected, AuditInputError
from .verifier import verify_report
from .runner import OperationRunner, load_motions, load_profile_revision

__all__ = [
    "fraction",
    "rational_json_loads",
    "qstr",
    "Profile",
    "Knot",
    "Trajectory",
    "MotionSegment",
    "TimeInterval",
    "merge_union",
    "ProfileCatalog",
    "MotionStore",
    "InputError",
    "ProfileOverlapService",
    "AuditLibrary",
    "AuditRejected",
    "AuditInputError",
    "verify_report",
    "OperationRunner",
    "load_motions",
    "load_profile_revision",
]
