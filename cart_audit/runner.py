"""资料加载与操作流运行器（对应 materials 中的 operations.jsonl）。

支持的操作：

- ``audit``：按指定轮廓修订对车对列表批次核查并发布报告；
- ``revise_profiles``：载入轮廓修订文件并冻结到目录（同内容复读、异文拒绝）；
- ``read_report``：读取已发布报告（旧报告保留其冻结尺寸，不随修订改变）；
- ``ink_voucher``：按格网文件与指定修订计算并冻结受墨量凭单（需要
  :class:`~cart_audit.ink.InkLedger`）；
- ``read_voucher``：读取已冻结的受墨量凭单（需要 InkLedger）；
- ``allocate_paper`` / ``confirm_paper``：为一批试版配给并原子确认纸张
  （需要 :class:`~cart_audit.paper.PaperStore`）。

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

__all__ = [
    "OperationRunner",
    "load_motions",
    "load_profile_revision",
    "load_plate",
    "load_plate_specs",
]


def load_profile_revision(path: str | Path) -> Tuple[str, List[Profile]]:
    data = rational_json_loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "revision" not in data or "profiles" not in data:
        raise InputError(f"轮廓修订文件 {path} 缺少 revision/profiles 字段")
    profiles = [Profile.from_data(item) for item in data["profiles"]]
    return str(data["revision"]), profiles


def load_plate(path: str | Path):
    """载入一张试版格网（含每格印模与流墨率）。"""
    from .ink import ProofPlate

    data = rational_json_loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise InputError(f"格网文件 {path} 必须是 JSON 对象")
    return ProofPlate.from_data(data)


def load_plate_specs(path: str | Path):
    """载入一批试版的纸张配给需求。"""
    from .paper import PlateSpec

    data = rational_json_loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise InputError(f"配给需求文件 {path} 必须是 JSON 数组")
    return [PlateSpec.from_data(item) for item in data]


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
        ink_ledger=None,
        paper_store=None,
    ):
        self.catalog = catalog
        self.store = store
        self.library = library
        self.materials_dir = Path(materials_dir)
        self.ink_ledger = ink_ledger
        self.paper_store = paper_store

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
        if kind == "ink_voucher":
            if self.ink_ledger is None:
                raise InputError("本运行器未配置 InkLedger，无法计算受墨量凭单")
            from .ink import ProofPlate

            plate = (
                ProofPlate.from_data(op["plate"])
                if "plate" in op
                else load_plate(self.materials_dir / op["plate_file"])
            )
            voucher = self.ink_ledger.put_voucher(
                str(op["voucher_id"]), plate, str(op["revision"])
            )
            return {"op": "ink_voucher", "voucher_id": voucher["voucher_id"], "result": voucher}
        if kind == "read_voucher":
            if self.ink_ledger is None:
                raise InputError("本运行器未配置 InkLedger，无法读取受墨量凭单")
            voucher = self.ink_ledger.read_voucher(str(op["voucher_id"]))
            return {"op": "read_voucher", "voucher_id": voucher["voucher_id"], "result": voucher}
        if kind == "allocate_paper":
            if self.paper_store is None:
                raise InputError("本运行器未配置 PaperStore，无法配给纸张")
            specs = (
                load_plate_specs(self.materials_dir / op["plates_file"])
                if "plates_file" in op
                else load_plate_specs(self.materials_dir / "plates.json")
            )
            result = self.paper_store.allocate_batch(specs)
            return {"op": "allocate_paper", "result": result.as_dict()}
        if kind == "confirm_paper":
            if self.paper_store is None:
                raise InputError("本运行器未配置 PaperStore，无法确认纸张")
            from .paper import AllocationResult

            if "allocation" in op:
                raw = op["allocation"]
                result = AllocationResult(
                    assignments=tuple(
                        (str(item["plate_id"]), item.get("paper_type_id"))
                        for item in raw["assignments"]
                    ),
                    total_weight=fraction(raw.get("total_weight", 0)),
                    allocated_count=int(raw.get("allocated_count", 0)),
                    usage={str(k): int(v) for k, v in raw.get("usage", {}).items()},
                    remaining={},
                )
            else:
                # 未显式给出计划：用当前库存重算后再确认（并发失败方据此重试）。
                specs = (
                    load_plate_specs(self.materials_dir / op["plates_file"])
                    if "plates_file" in op
                    else load_plate_specs(self.materials_dir / "plates.json")
                )
                result = self.paper_store.allocate_batch(specs)
            payload = self.paper_store.confirm(str(op["confirmation_id"]), result)
            return {"op": "confirm_paper", "confirmation_id": payload["confirmation_id"], "result": payload}
        raise InputError(f"未知操作类型: {kind!r}")

    def _default_revision(self) -> str:
        revisions = self.catalog.list_revisions()
        if not revisions:
            raise InputError("目录中尚无轮廓修订，audit 必须显式指定 revision")
        # 操作流未显式指定 revision 时使用“当前修订”：最后载入的版本。
        # 已发布报告自带冻结快照，切换当前修订不会影响旧报告。
        return revisions[-1]
