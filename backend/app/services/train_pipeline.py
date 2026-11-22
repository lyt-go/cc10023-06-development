"""训练流水线领域服务：特征集选择、算法配置与评估指标管理。

训练数据统一来自「项目训练数据集」的字段映射：训练开始时按租户字段目录现状
重新校验准入与共享权限，并把实际采用列的归属项目、定义、统计口径快照到模型登记，
后续可据此对账，也能判断新模型能否与全站历史模型比较。
"""
from __future__ import annotations

import json
from typing import Any

from app.services.base import BaseModuleService
from app.services.catalog_identity import build_column_label
from app.services.project_dataset import ProjectDatasetService
from app.services.tenant_field_catalog import TenantFieldCatalogService
from app.store import store


class TrainPipelineService(BaseModuleService):
    MODULE = "train_pipeline"
    ENTITY = "流水线配置"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["流水线编号", "流水线名称", "特征集", "关联数据集", "算法类型", "超参数配置", "评估指标", "训练周期", "流水线版本"]
    REQUIRED_FIELDS = ["流水线编号", "流水线名称", "特征集"]
    STATUS_ORDER = ["草稿", "待校验", "训练中", "已完成"]
    ACTION_RULES = {"提交校验": "待校验", "开始训练": "训练中", "确认完成": "已完成"}
    NEGATIVE_ACTIONS = []

    def __init__(self) -> None:
        self.dataset_service = ProjectDatasetService()
        self.catalog_service = TenantFieldCatalogService()

    def bind_dataset(self, pipeline_id: int, dataset_id: int) -> dict[str, Any]:
        """把项目训练数据集绑定到流水线，训练列以数据集字段映射为准。"""
        pipe = store.find(self.MODULE, pipeline_id)
        if pipe is None:
            return {"ok": False, "message": f"流水线 {pipeline_id} 不存在"}
        dataset = store.find("project_dataset", dataset_id)
        if dataset is None:
            return {"ok": False, "message": f"数据集 {dataset_id} 不存在"}
        pipe["关联数据集"] = str(dataset_id)
        pipe["特征集"] = dataset.get("数据集名称", pipe.get("特征集", ""))
        return {
            "ok": True,
            "pipeline_id": pipeline_id,
            "dataset_id": dataset_id,
            "字段数": len(self.dataset_service.get_mappings(dataset_id)),
        }

    # ── 链路4: 特征组 → 训练流水线 → 模型注册 ──
    def train_and_register_model(self, pipeline_id: int) -> dict[str, Any]:
        """执行训练流水线并将产物注册到模型中心。

        关联项目训练数据集时：
        1. 校验口径冲突已全部解决；
        2. 按目录现状重验准入与共享权限（撤销共享在此拦截）；
        3. 快照实际训练列的归属项目、定义版本与统计口径写入模型登记。
        """
        pipe = store.find(self.MODULE, pipeline_id)
        if pipe is None:
            return {"ok": False, "message": f"流水线 {pipeline_id} 不存在"}

        dataset_id_text = str(pipe.get("关联数据集") or "").strip()
        if not dataset_id_text:
            return self._train_without_dataset(pipe, pipeline_id)

        dataset_id = int(dataset_id_text)
        dataset = store.find("project_dataset", dataset_id)
        if dataset is None:
            return {"ok": False, "message": f"关联数据集 {dataset_id} 不存在，禁止训练"}

        unresolved = self.dataset_service.unselected_conflicts(dataset_id)
        if unresolved:
            pipe["status"] = "待校验"
            pipe["abnormal"] = True
            return {
                "ok": False,
                "message": "存在未解决的同名口径冲突，请先在训练数据集中选定统计范围",
                "冲突列": unresolved,
            }

        mappings = self.dataset_service.get_mappings(dataset_id)
        project = dataset.get("归属项目", "")
        recheck = self.catalog_service.recheck_at_training(
            [m["catalog_id"] for m in mappings], project)
        if not recheck["ok"]:
            pipe["status"] = "待校验"
            pipe["abnormal"] = True
            return {
                "ok": False,
                "message": "训练前权限重验失败，部分列已失效或共享被撤销",
                "拒绝明细": recheck["拒绝"],
            }

        # 用目录现状刷新快照（口径/版本可能已升版），但保留数据集中的口径选择结论
        lineage: list[dict[str, Any]] = []
        scope_notes: list[str] = []
        for mapping in mappings:
            entry = store.find("tenant_field_catalog", mapping["catalog_id"])
            current = {
                "catalog_id": entry["id"],
                "目录编号": entry.get("目录编号"),
                "字段名称": entry.get("字段名称"),
                "归属项目": entry.get("归属项目"),
                "计算口径": entry.get("计算口径"),
                "定义版本": entry.get("定义版本"),
                "列标识": build_column_label(entry),
                "来源类型": mapping.get("来源类型"),
                "数据来源": entry.get("数据来源"),
            }
            if "口径选择" in mapping:
                current["口径选择"] = mapping["口径选择"]
                scope_notes.append(
                    f"{mapping['字段名称']}采用「{mapping['口径选择']['采用统计范围']}」"
                )
            lineage.append(current)

        pipe["status"] = "训练中"
        pipe["pending"] = True
        model_rows = store.rows("model_registry")
        next_id = max((int(r.get("id", 0)) for r in model_rows), default=0) + 1
        private_cols = [c["字段名称"] for c in lineage if c["来源类型"] == "本项目私有"]
        shared_cols = [f"{c['字段名称']}（来源：{c['归属项目']}）"
                       for c in lineage if c["来源类型"] != "本项目私有"]
        model_rows.append({
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "模型编号": f"MODEL-{next_id:04d}",
            "模型名称": f"model_{pipe.get('流水线名称', 'unknown')}_v1",
            "算法类型": pipe.get("算法类型", "xgboost"),
            "训练特征": "、".join(c["列标识"] for c in lineage),
            "模型版本": "v1.0.0",
            "模型文件": f"models/pipeline_{pipeline_id}.pkl",
            "评估分数": "0.892",
            "部署状态": "未部署",
            "列归属快照": json.dumps(lineage, ensure_ascii=False),
            "统计口径说明": "；".join(scope_notes) if scope_notes else "全部列无同名口径冲突",
            "来源项目": project,
            "来源数据集": dataset.get("数据集编号", ""),
        })
        pipe["status"] = "已完成"
        pipe["pending"] = False
        return {
            "ok": True,
            "pipeline_id": pipeline_id,
            "dataset_id": dataset_id,
            "model_id": next_id,
            "训练列数": len(lineage),
            "私有列": private_cols,
            "共享列": shared_cols,
            "口径结论": scope_notes,
            "score": 0.892,
        }

    def _train_without_dataset(self, pipe: dict[str, Any], pipeline_id: int) -> dict[str, Any]:
        """历史流水线未绑定数据集时保留原有登记逻辑，保证旧链路可用。"""
        pipe["status"] = "训练中"
        pipe["pending"] = True
        model_rows = store.rows("model_registry")
        next_id = max((int(r.get("id", 0)) for r in model_rows), default=0) + 1
        model_rows.append({
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "模型编号": f"MODEL-{next_id:04d}",
            "模型名称": f"model_{pipe.get('流水线名称', 'unknown')}_v1",
            "算法类型": pipe.get("算法类型", "xgboost"),
            "训练特征": pipe.get("特征集", ""),
            "模型版本": "v1.0.0",
            "模型文件": f"models/pipeline_{pipeline_id}.pkl",
            "评估分数": "0.892",
            "部署状态": "未部署",
        })
        pipe["status"] = "已完成"
        pipe["pending"] = False
        return {"ok": True, "pipeline_id": pipeline_id, "model_id": next_id, "score": 0.892}

    def validate_pipeline_config(self, pipeline_id: int) -> dict[str, Any]:
        """校验训练流水线的配置完整性，包括特征集引用和超参数合法性。"""
        pipe = store.find(self.MODULE, pipeline_id)
        if pipe is None:
            return {"ok": False}
        errors = []
        if not pipe.get("特征集"):
            errors.append("缺少特征集配置")
        if not pipe.get("算法类型"):
            errors.append("缺少算法类型")
        if not pipe.get("超参数配置"):
            errors.append("缺少超参数配置")
        if errors:
            pipe["status"] = "待校验"
            pipe["abnormal"] = True
        else:
            pipe["status"] = "已生效" if "已生效" in self.STATUS_ORDER else "草稿"
            pipe["abnormal"] = False
        return {"ok": len(errors) == 0, "errors": errors, "pipeline_id": pipeline_id}

    def schedule_pipeline_execution(self, pipeline_id: int) -> dict[str, Any]:
        """为训练流水线创建调度配置，支持定期自动执行并关联Cron表达式。"""
        pipe = store.find(self.MODULE, pipeline_id)
        if pipe is None:
            return {"ok": False}
        sched_rows = store.rows("pipeline_schedule")
        next_id = max((int(r.get("id", 0)) for r in sched_rows), default=0) + 1
        sched_rows.append({
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "调度编号": f"SCH-{next_id:04d}",
            "关联流水线": str(pipeline_id),
            "Cron表达式": pipe.get("训练周期", "0 2 * * 1"),
            "依赖任务": "",
            "失败策略": "retry3",
            "通知方式": "email",
            "最大重试数": "3",
            "调度状态": "草稿",
        })
        return {"ok": True, "schedule_id": next_id, "pipeline_id": pipeline_id}

    def query_pipeline_evaluation(self, pipeline_id: int) -> dict[str, Any]:
        """查询训练流水线的评估指标结果，包括AUC、logloss和特征重要性排名。"""
        pipe = store.find(self.MODULE, pipeline_id)
        if pipe is None:
            return {"ok": False}
        return {
            "ok": True,
            "pipeline_id": pipeline_id,
            "pipeline_name": pipe.get("流水线名称"),
            "metrics": pipe.get("评估指标", ""),
            "status": pipe.get("status"),
            "algorithm": pipe.get("算法类型"),
            "version": pipe.get("流水线版本"),
        }
