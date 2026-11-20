"""资料加载与操作流运行器（对应 materials 中的 operations.jsonl）。

支持的操作：

- ``audit``：按指定轮廓修订对车对列表批次核查并发布报告；
- ``revise_profiles``：载入轮廓修订文件并冻结到目录（同内容复读、异文拒绝）；
- ``read_report``：读取已发布报告（旧报告保留其冻结尺寸，不随修订改变）。

运行器只编排，不改变严格接触合同；任何核查失败都由核查库抛错，不产生
“可通过”外观的报告。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Tuple

from .catalog import InputError, MotionStore, ProfileCatalog
from .library import AuditLibrary
from .models import Profile, Trajectory
from .rational import fraction, rational_json_loads

__all__ = ["OperationRunner", "load_motions", "load_profile_revision"]


def load_profile_revision(path: str | Path) -> Tuple[str, List[Profile]]:
    data = rational_json_loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "revision" not in data or "profiles" not in data:
        raise InputError(f"轮廓修订文件 {path} 缺少 revision/profiles 字段")
    profiles = [Profile.from_data(item) for item in data["profiles"]]
    return str(data["revision"]), profiles


def load_motions(path: str | Path) -> Tuple[Tuple[object, object], List[Trajectory]]:
    data = rational_json_loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "horizon" not in data or "carts" not in data:
        raise InputError(f"轨迹文件 {path} 缺少 horizon/carts 字段")
    horizon_raw = data["horizon"]
    if not isinstance(horizon_raw, list) or len(horizon_raw) != 2:
        raise InputError("horizon 必须是 [h0, h1] 两元素列表")
    h0 = fraction(horizon_raw[0], field="horizon[0]")
    h1 = fraction(horizon_raw[1], field="horizon[1]")
    if not h1 > h0:
        raise InputError("核查域必须是非空半开区间 [h0, h1)，且 h1 > h0")
    trajectories = [Trajectory.from_data(item, (h0, h1)) for item in data["carts"]]
    ids = [t.cart_id for t in trajectories]
    if len(set(ids)) != len(ids):
        raise InputError(f"cart_id 重复: {ids}")
    return (h0, h1), trajectories


class OperationRunner:
    def __init__(
        self,
        catalog: ProfileCatalog,
        store: MotionStore,
        library: AuditLibrary,
        materials_dir: str | Path,
    ):
        self.catalog = catalog
        self.store = store
        self.library = library
        self.materials_dir = Path(materials_dir)

    def run_file(self, operations_path: str | Path) -> List[dict]:
        results = []
        for lineno, line in enumerate(
            Path(operations_path).read_text(encoding="utf-8").splitlines(), start=1
        ):
            text = line.strip()
            if not text:
                continue
            try:
                op = json.loads(text)
            except json.JSONDecodeError as exc:
                raise InputError(f"操作文件第 {lineno} 行不是合法 JSON: {exc}") from exc
            results.append(self.run_op(op))
        return results

    def run_op(self, op: dict) -> dict:
        kind = op.get("op")
        if kind == "audit":
            report = self.library.audit(
                report_id=str(op["report_id"]),
                pairs=op["pairs"],
                revision=str(op["revision"]) if "revision" in op else self._default_revision(),
            )
            return {"op": "audit", "report_id": report["report_id"], "result": report}
        if kind == "revise_profiles":
            revision, profiles = load_profile_revision(self.materials_dir / op["file"])
            self.catalog.put_revision(revision, profiles)
            return {"op": "revise_profiles", "revision": revision, "profiles": [p.to_dict() for p in profiles]}
        if kind == "read_report":
            report = self.library.read_report(str(op["report_id"]))
            return {"op": "read_report", "report_id": report["report_id"], "result": report}
        raise InputError(f"未知操作类型: {kind!r}")

    def _default_revision(self) -> str:
        revisions = self.catalog.list_revisions()
        if not revisions:
            raise InputError("目录中尚无轮廓修订，audit 必须显式指定 revision")
        # 操作流未显式指定 revision 时使用“当前修订”：最后载入的版本。
        # 已发布报告自带冻结快照，切换当前修订不会影响旧报告。
        return revisions[-1]
