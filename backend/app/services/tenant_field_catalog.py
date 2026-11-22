"""租户训练字段目录领域服务。

目录是各项目训练字段的唯一登记处：
- 字段必须先准入（状态=已准入）才能被项目训练数据集映射；
- 私有列只有归属项目能采用，其他项目只能读取明确共享给自己的字段；
- 共享可授权也可撤销，撤销只影响后续训练，已登记模型保留训练时的列归属快照。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.catalog_identity import (
    build_column_label,
    can_use_catalog_entry,
    parse_projects,
    source_type_for,
)
from app.store import store


class TenantFieldCatalogService(BaseModuleService):
    MODULE = "tenant_field_catalog"
    ENTITY = "租户训练字段"
    OPERATION_TERM = "准入"
    LIST_FIELDS = [
        "目录编号", "字段名称", "归属项目", "计算口径", "定义版本",
        "字段类型", "数据来源", "共享范围",
    ]
    REQUIRED_FIELDS = ["目录编号", "字段名称", "归属项目", "计算口径", "定义版本"]
    STATUS_ORDER = ["待准入", "已准入", "已驳回", "已下线"]
    ACTION_RULES = {"提交准入": "待准入", "批准准入": "已准入", "驳回准入": "已驳回", "下线字段": "已下线"}
    NEGATIVE_ACTIONS = ["驳回准入", "下线字段"]

    def run_action(self, entry_id: int, action: str, remark: str | None = None):
        """通用动作之外承接「授权共享 / 撤销共享」，remark 传目标项目名。"""
        if action == "授权共享":
            result = self.grant_share(entry_id, (remark or "").strip())
            if not result.get("ok"):
                return None, result.get("message", "授权共享失败")
            return store.find(self.MODULE, entry_id), f"已共享给{remark.strip()}"
        if action == "撤销共享":
            result = self.revoke_share(entry_id, (remark or "").strip())
            if not result.get("ok"):
                return None, result.get("message", "撤销共享失败")
            return store.find(self.MODULE, entry_id), f"已撤销对{remark.strip()}的共享"
        return super().run_action(entry_id, action, remark)

    # ── 准入校验 ──
    def admit_field(self, catalog_id: int) -> dict[str, Any]:
        """批准字段准入：校验计算口径与定义版本齐备后才能进入可映射状态。"""
        entry = store.find(self.MODULE, catalog_id)
        if entry is None:
            return {"ok": False, "message": f"字段 {catalog_id} 不存在"}
        missing = [name for name in ("计算口径", "定义版本", "归属项目") if not entry.get(name)]
        if missing:
            entry["status"] = "已驳回"
            entry["abnormal"] = True
            entry["pending"] = False
            return {"ok": False, "catalog_id": catalog_id, "reason": f"缺少{ '、'.join(missing) }，无法准入"}
        entry["status"] = "已准入"
        entry["pending"] = False
        entry["abnormal"] = False
        return {"ok": True, "catalog_id": catalog_id, "列标识": build_column_label(entry)}

    # ── 共享管理 ──
    def grant_share(self, catalog_id: int, target_project: str) -> dict[str, Any]:
        """把字段共享给指定项目；私有列共享范围显式记录每个被授权项目。"""
        if not target_project:
            return {"ok": False, "message": "授权共享必须指定目标项目"}
        entry = store.find(self.MODULE, catalog_id)
        if entry is None:
            return {"ok": False, "message": f"字段 {catalog_id} 不存在"}
        if entry.get("status") != "已准入":
            return {"ok": False, "message": "只有已准入字段可以授权共享"}
        if entry.get("归属项目") == target_project:
            return {"ok": False, "message": "归属项目天然拥有私有列，无需共享"}
        scopes = parse_projects(entry.get("共享范围"))
        if target_project in scopes:
            return {"ok": True, "catalog_id": catalog_id, "message": "共享已存在，无需重复授权"}
        scopes.append(target_project)
        entry["共享范围"] = ",".join(scopes)
        entry.setdefault("共享历史", []).append(
            {"动作": "授权共享", "目标项目": target_project, "结果": "生效"})
        return {
            "ok": True,
            "catalog_id": catalog_id,
            "字段名称": entry.get("字段名称"),
            "共享范围": entry["共享范围"],
            "列标识": build_column_label(entry),
        }

    def revoke_share(self, catalog_id: int, target_project: str) -> dict[str, Any]:
        """撤销对指定项目的共享；历史授权留痕，旧模型记录不受影响。"""
        if not target_project:
            return {"ok": False, "message": "撤销共享必须指定目标项目"}
        entry = store.find(self.MODULE, catalog_id)
        if entry is None:
            return {"ok": False, "message": f"字段 {catalog_id} 不存在"}
        scopes = parse_projects(entry.get("共享范围"))
        if target_project not in scopes:
            return {"ok": False, "message": f"字段未共享给{target_project}，无可撤销授权"}
        scopes.remove(target_project)
        entry["共享范围"] = ",".join(scopes)
        entry.setdefault("共享历史", []).append(
            {"动作": "撤销共享", "目标项目": target_project, "结果": "生效"})
        return {
            "ok": True,
            "catalog_id": catalog_id,
            "字段名称": entry.get("字段名称"),
            "共享范围": entry["共享范围"],
            "列标识": build_column_label(entry),
        }

    # ── 读取与权限 ──
    def visible_fields(self, project: str) -> list[dict[str, Any]]:
        """项目可见的已准入字段：本项目私有列 + 明确共享给本项目的列。"""
        rows = store.rows(self.MODULE)
        return [
            {
                **entry,
                "catalog_id": entry.get("id"),
                "来源类型": source_type_for(entry, project),
                "列标识": build_column_label(entry),
            }
            for entry in rows
            if can_use_catalog_entry(entry, project)
        ]

    def check_admission(self, catalog_ids: list[int], project: str) -> dict[str, Any]:
        """项目训练数据集建集前的准入与权限批量校验，只放行有权限的列。"""
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for catalog_id in catalog_ids:
            entry = store.find(self.MODULE, catalog_id)
            if entry is None:
                rejected.append({"catalog_id": catalog_id, "reason": "字段目录中不存在"})
                continue
            if entry.get("status") != "已准入":
                rejected.append({
                    "catalog_id": catalog_id, "字段名称": entry.get("字段名称"),
                    "当前状态": entry.get("status"), "reason": "字段未准入，禁止用于训练",
                })
                continue
            if not can_use_catalog_entry(entry, project):
                rejected.append({
                    "catalog_id": catalog_id, "字段名称": entry.get("字段名称"),
                    "归属项目": entry.get("归属项目"), "reason": "未共享给本项目，无权使用",
                })
                continue
            accepted.append({
                "catalog_id": catalog_id,
                "字段名称": entry.get("字段名称"),
                "归属项目": entry.get("归属项目"),
                "计算口径": entry.get("计算口径"),
                "定义版本": entry.get("定义版本"),
                "来源类型": source_type_for(entry, project),
                "列标识": build_column_label(entry),
            })
        return {
            "ok": len(rejected) == 0,
            "project": project,
            "通过": accepted,
            "拒绝": rejected,
            "通过数": len(accepted),
            "拒绝数": len(rejected),
        }

    def recheck_at_training(self, catalog_ids: list[int], project: str) -> dict[str, Any]:
        """训练开始时按目录现状重验权限：撤销共享/下线在这一步被拦截。"""
        result = self.check_admission(catalog_ids, project)
        result["ok"] = result["拒绝数"] == 0
        return result
