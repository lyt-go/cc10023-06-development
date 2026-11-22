"""项目训练数据集领域服务。

建集流程：从租户训练字段目录选择字段 → 映射并校验准入/权限 → 只保留有权限的列 →
检测同名异义列的口径冲突 → 冲突解决后才能交给训练流水线。
"""
from __future__ import annotations

import json
from typing import Any

from app.services.base import BaseModuleService
from app.services.catalog_identity import entry_lineage
from app.services.tenant_field_catalog import TenantFieldCatalogService
from app.store import store


class ProjectDatasetService(BaseModuleService):
    MODULE = "project_dataset"
    ENTITY = "项目训练数据集"
    OPERATION_TERM = "校验"
    LIST_FIELDS = [
        "数据集编号", "数据集名称", "归属项目", "字段映射",
        "冲突列", "共享来源说明", "准入校验结果", "数据集版本",
    ]
    REQUIRED_FIELDS = ["数据集编号", "数据集名称", "归属项目"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用数据集": "已停用"}
    NEGATIVE_ACTIONS = ["停用数据集"]

    def __init__(self) -> None:
        self.catalog_service = TenantFieldCatalogService()

    # ── 建集：目录映射 + 准入校验 ──
    def create_project_dataset(
        self, project: str, dataset_name: str, catalog_ids: list[int]
    ) -> dict[str, Any]:
        """从租户字段目录映射字段并校验准入，只把有权限的列写入训练数据集。"""
        if not project or not dataset_name:
            return {"ok": False, "message": "归属项目和数据集名称为必填"}
        check = self.catalog_service.check_admission(catalog_ids, project)
        mappings: list[dict[str, Any]] = []
        shared_from: set[str] = set()
        for item in check["通过"]:
            entry = store.find("tenant_field_catalog", item["catalog_id"])
            mapping = entry_lineage(entry, project)
            mappings.append(mapping)
            if mapping["来源类型"] != "本项目私有":
                shared_from.add(mapping["归属项目"])

        conflicts = self._detect_conflicts(mappings)
        rows = store.rows(self.MODULE)
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        shared_note = (
            "跨项目共享列来源：" + "、".join(sorted(shared_from))
            if shared_from else "跨项目共享列：无"
        )
        entry = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": bool(check["拒绝数"]) or bool(conflicts),
            "数据集编号": f"DS-{next_id:04d}",
            "数据集名称": dataset_name,
            "归属项目": project,
            "字段映射": json.dumps(mappings, ensure_ascii=False),
            "冲突列": json.dumps(conflicts, ensure_ascii=False),
            "共享来源说明": shared_note,
            "准入校验结果": json.dumps(
                {"通过": check["通过数"], "拒绝": check["拒绝"]}, ensure_ascii=False),
            "数据集版本": "v1.0.0",
        }
        rows.append(entry)
        return {
            "ok": check["拒绝数"] == 0,
            "dataset_id": next_id,
            "数据集编号": entry["数据集编号"],
            "通过数": check["通过数"],
            "拒绝数": check["拒绝数"],
            "拒绝明细": check["拒绝"],
            "冲突列": conflicts,
            "message": "字段映射完成，存在未准入/无权限字段被剔除" if check["拒绝数"] else "字段映射完成",
        }

    @staticmethod
    def _detect_conflicts(mappings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """同名指标出现多个计算口径即构成口径冲突，候选列各自带列标识与归属项目。"""
        by_name: dict[str, list[dict[str, Any]]] = {}
        for mapping in mappings:
            by_name.setdefault(mapping["字段名称"], []).append(mapping)
        conflicts: list[dict[str, Any]] = []
        for name, group in by_name.items():
            calibers = {item["计算口径"] for item in group}
            if len(calibers) > 1:
                conflicts.append({
                    "字段名称": name,
                    "候选列": [
                        {
                            "catalog_id": item["catalog_id"],
                            "列标识": item["列标识"],
                            "归属项目": item["归属项目"],
                            "计算口径": item["计算口径"],
                            "定义版本": item["定义版本"],
                            "来源类型": item["来源类型"],
                        }
                        for item in group
                    ],
                    "口径集合": sorted(calibers),
                })
        return conflicts

    def get_mappings(self, dataset_id: int) -> list[dict[str, Any]]:
        """训练流水线读取的字段映射（含列标识、口径、版本与来源类型）。"""
        entry = store.find(self.MODULE, dataset_id)
        if entry is None:
            return []
        return json.loads(entry.get("字段映射") or "[]")

    def conflict_report(self, dataset_id: int) -> dict[str, Any]:
        """返回数据集当前的口径冲突报告，供项目负责人判断统计范围。"""
        entry = store.find(self.MODULE, dataset_id)
        if entry is None:
            return {"ok": False, "message": f"数据集 {dataset_id} 不存在"}
        conflicts = json.loads(entry.get("冲突列") or "[]")
        return {
            "ok": not conflicts,
            "dataset_id": dataset_id,
            "数据集名称": entry.get("数据集名称"),
            "冲突数": len(conflicts),
            "冲突列": conflicts,
        }

    def resolve_conflict(
        self, dataset_id: int, field_name: str, chosen_catalog_id: int,
        compare_scope_note: str = "",
    ) -> dict[str, Any]:
        """解决同名指标口径冲突：选定实际采用的列并记录统计范围与可比性说明。"""
        entry = store.find(self.MODULE, dataset_id)
        if entry is None:
            return {"ok": False, "message": f"数据集 {dataset_id} 不存在"}
        mappings = json.loads(entry.get("字段映射") or "[]")
        conflicts = json.loads(entry.get("冲突列") or "[]")
        target = next((c for c in conflicts if c["字段名称"] == field_name), None)
        if target is None:
            return {"ok": False, "message": f"字段 {field_name} 不存在未解决冲突"}
        candidate_ids = [c["catalog_id"] for c in target["候选列"]]
        if chosen_catalog_id not in candidate_ids:
            return {"ok": False, "message": "选定列不在冲突候选范围内"}
        chosen = next(m for m in mappings if m["catalog_id"] == chosen_catalog_id)
        removed = [m for m in mappings if m["字段名称"] == field_name and m["catalog_id"] != chosen_catalog_id]
        mappings = [m for m in mappings if m not in removed]
        chosen["口径选择"] = {
            "冲突字段": field_name,
            "采用列标识": chosen["列标识"],
            "采用统计范围": chosen["计算口径"],
            "未采用候选": [m["列标识"] for m in removed],
            "可比性说明": compare_scope_note or "未填写",
        }
        remaining = self._detect_conflicts(mappings)
        entry["字段映射"] = json.dumps(mappings, ensure_ascii=False)
        entry["冲突列"] = json.dumps(remaining, ensure_ascii=False)
        entry["abnormal"] = bool(remaining)
        return {
            "ok": True,
            "dataset_id": dataset_id,
            "字段名称": field_name,
            "采用列标识": chosen["列标识"],
            "采用统计范围": chosen["计算口径"],
            "剩余冲突数": len(remaining),
        }

    def unselected_conflicts(self, dataset_id: int) -> list[dict[str, Any]]:
        """训练前检查：仍有未解决冲突时不允许进入训练。"""
        return json.loads((store.find(self.MODULE, dataset_id) or {}).get("冲突列") or "[]")
