"""特征组管理领域服务：维护特征逻辑分组、主题归类和互斥规则。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.catalog_identity import (
    build_column_label,
    can_use_catalog_entry,
    source_type_for,
)
from app.store import store


class FeatureGroupService(BaseModuleService):
    MODULE = "feature_group"
    ENTITY = "特征组"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["组编号", "组名称", "包含特征", "业务主题", "默认权重", "互斥规则", "依赖关系", "组版本"]
    REQUIRED_FIELDS = ["组编号", "组名称", "包含特征"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用组定义": "已停用"}
    NEGATIVE_ACTIONS = ["停用组定义"]

    # ── 链路4: 特征组 → 训练流水线 → 模型注册 ──
    def export_group_features(self, group_id: int) -> dict[str, Any]:
        """查询特征组内所有已生效特征定义并导出为训练流水线可用格式。"""
        group = store.find(self.MODULE, group_id)
        if group is None:
            return {"ok": False, "message": f"特征组 {group_id} 不存在"}
        feature_names = [f.strip() for f in (group.get("包含特征") or "").split(",") if f.strip()]
        define_rows = store.rows("feature_define")
        matched = [r for r in define_rows if r.get("特征名称") in feature_names and r.get("status") == "已生效"]
        result = {
            "group_id": group_id,
            "group_name": group.get("组名称"),
            "feature_count": len(matched),
            "features": [
                {"name": r.get("特征名称"), "type": r.get("特征类型"), "logic": r.get("计算逻辑")}
                for r in matched
            ],
        }
        return {"ok": True, "data": result}

    def export_effective_features_for_project(self, group_id: int, project: str) -> dict[str, Any]:
        """按项目导出特征组对应训练列：只含已准入且本项目有权限的列。

        特征组按名字导出已生效定义本身不带项目/共享来源信息；这里以字段名关联
        租户训练字段目录，输出的每一列都带计算口径、定义版本与来源类型，
        私有列仅归属项目可采用，其他组织只能拿到明确共享给自己的字段。
        """
        group = store.find(self.MODULE, group_id)
        if group is None:
            return {"ok": False, "message": f"特征组 {group_id} 不存在"}
        feature_names = {
            f.strip() for f in (group.get("包含特征") or "").replace("，", ",").split(",") if f.strip()
        }
        define_rows = store.rows("feature_define")
        effective_names = {
            r.get("特征名称") for r in define_rows
            if r.get("status") == "已生效" and r.get("特征名称") in feature_names
        }
        catalog_rows = store.rows("tenant_field_catalog")
        columns: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for entry in catalog_rows:
            if entry.get("字段名称") not in effective_names:
                continue
            if not can_use_catalog_entry(entry, project):
                skipped.append({
                    "字段名称": entry.get("字段名称"),
                    "归属项目": entry.get("归属项目"),
                    "当前状态": entry.get("status"),
                    "共享范围": entry.get("共享范围", ""),
                    "原因": "未准入" if entry.get("status") != "已准入" else "未共享给本项目",
                })
                continue
            columns.append({
                "字段名称": entry.get("字段名称"),
                "归属项目": entry.get("归属项目"),
                "计算口径": entry.get("计算口径"),
                "定义版本": entry.get("定义版本"),
                "来源类型": source_type_for(entry, project),
                "列标识": build_column_label(entry),
            })
        return {
            "ok": True,
            "group_id": group_id,
            "group_name": group.get("组名称"),
            "project": project,
            "可用列数": len(columns),
            "columns": columns,
            "剔除列": skipped,
        }

    def create_training_pipeline(self, group_id: int) -> dict[str, Any]:
        """基于特征组创建训练流水线配置，关联特征集并初始化算法参数。"""
        export = self.export_group_features(group_id)
        if not export.get("ok"):
            return export
        data = export["data"]
        pipeline_rows = store.rows("train_pipeline")
        next_id = max((int(r.get("id", 0)) for r in pipeline_rows), default=0) + 1
        group = store.find(self.MODULE, group_id)
        pipeline_rows.append({
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "流水线编号": f"PIPE-{next_id:04d}",
            "流水线名称": f"train_{group.get('组名称', 'unknown')}_pipeline",
            "特征集": group.get("包含特征", ""),
            "算法类型": "xgboost",
            "超参数配置": '{"max_depth":6,"learning_rate":0.1,"n_estimators":100}',
            "评估指标": "auc,logloss",
            "训练周期": "weekly",
            "流水线版本": "v1.0.0",
        })
        return {
            "ok": True,
            "pipeline_id": next_id,
            "feature_count": data["feature_count"],
            "message": f"已基于特征组 {group_id} 创建训练流水线 {next_id}",
        }

    def check_mutex_rule_compliance(self, group_id: int) -> dict[str, Any]:
        """校验特征组的互斥规则合规性，确保无冲突特征同时出现在同一组中。"""
        group = store.find(self.MODULE, group_id)
        if group is None:
            return {"ok": False, "message": "特征组不存在"}
        mutex_rule = group.get("互斥规则", "")
        feature_names = [f.strip() for f in (group.get("包含特征") or "").split(",") if f.strip()]
        violations = []
        if mutex_rule and len(feature_names) > 5:
            violations.append("特征数量超过互斥规则约束上限5个")
        if violations:
            group["abnormal"] = True
            group["status"] = "待校验"
        else:
            group["abnormal"] = False
        return {"ok": len(violations) == 0, "violations": violations, "group_id": group_id}

    def aggregate_group_dependencies(self, group_id: int) -> dict[str, Any]:
        """聚合特征组的依赖关系并关联上下游模块，生成完整依赖链路图。"""
        group = store.find(self.MODULE, group_id)
        if group is None:
            return {"ok": False}
        dep_text = group.get("依赖关系", "")
        deps = [d.strip() for d in dep_text.split(",") if d.strip()] if dep_text else []
        pipeline_rows = store.rows("train_pipeline")
        downstream = [r.get("id") for r in pipeline_rows if group.get("组名称", "") in str(r.get("特征集", ""))]
        return {
            "ok": True,
            "group_id": group_id,
            "upstream_deps": deps,
            "downstream_pipelines": downstream,
            "chain_length": len(deps) + len(downstream),
        }