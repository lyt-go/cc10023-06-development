"""训练数据领域接口：字段目录共享、项目训练数据集、口径冲突与模型列对账。

通用资源路由只承接列表/登记/状态动作；涉及权限判定与跨模块领域逻辑的操作
集中在本路由，并固定注册在通用资源路由之前，避免被 /api/{module} 动态路径抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import service_for
from app.services.feature_group import FeatureGroupService
from app.services.model_registry import ModelRegistryService
from app.services.project_dataset import ProjectDatasetService
from app.services.tenant_field_catalog import TenantFieldCatalogService
from app.services.train_pipeline import TrainPipelineService

router = APIRouter(prefix="/api", tags=["训练数据域"])


class IdListPayload(BaseModel):
    catalog_ids: list[int] = Field(default_factory=list)


class SharePayload(BaseModel):
    target_project: str


class DatasetCreatePayload(BaseModel):
    project: str
    dataset_name: str
    catalog_ids: list[int] = Field(default_factory=list)


class ConflictResolvePayload(BaseModel):
    field_name: str
    chosen_catalog_id: int
    compare_scope_note: str = ""


class BindDatasetPayload(BaseModel):
    dataset_id: int


def _catalog_service() -> TenantFieldCatalogService:
    return service_for("tenant_field_catalog")  # type: ignore[return-value]


def _dataset_service() -> ProjectDatasetService:
    return service_for("project_dataset")  # type: ignore[return-value]


def _ensure_ok(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "领域操作失败"))
    return result


# ── 租户训练字段目录 ──
@router.get("/tenant_field_catalog/visible/{project}")
def visible_fields(project: str) -> dict[str, Any]:
    """项目可见字段：本项目私有列 + 明确共享给本项目的已准入列。"""
    items = _catalog_service().visible_fields(project)
    return {"ok": True, "project": project, "count": len(items), "items": items}


@router.post("/tenant_field_catalog/{catalog_id}/admit")
def admit_field(catalog_id: int) -> dict[str, Any]:
    return _ensure_ok(_catalog_service().admit_field(catalog_id))


@router.post("/tenant_field_catalog/{catalog_id}/share")
def grant_share(catalog_id: int, payload: SharePayload) -> dict[str, Any]:
    return _ensure_ok(_catalog_service().grant_share(catalog_id, payload.target_project.strip()))


@router.post("/tenant_field_catalog/{catalog_id}/revoke-share")
def revoke_share(catalog_id: int, payload: SharePayload) -> dict[str, Any]:
    return _ensure_ok(_catalog_service().revoke_share(catalog_id, payload.target_project.strip()))


@router.post("/tenant_field_catalog/check-admission/{project}")
def check_admission(project: str, payload: IdListPayload) -> dict[str, Any]:
    return _catalog_service().check_admission(payload.catalog_ids, project)


# ── 项目训练数据集 ──
@router.post("/project_dataset/create")
def create_project_dataset(payload: DatasetCreatePayload) -> dict[str, Any]:
    result = _dataset_service().create_project_dataset(
        payload.project.strip(), payload.dataset_name.strip(), payload.catalog_ids)
    # 建集允许部分列被剔除（ok=False 表示有拒绝列），数据集仍创建，不抛 4xx
    return result


@router.get("/project_dataset/{dataset_id}/mappings")
def dataset_mappings(dataset_id: int) -> dict[str, Any]:
    return {"ok": True, "items": _dataset_service().get_mappings(dataset_id)}


@router.get("/project_dataset/{dataset_id}/conflicts")
def dataset_conflicts(dataset_id: int) -> dict[str, Any]:
    return _ensure_ok(_dataset_service().conflict_report(dataset_id))


@router.post("/project_dataset/{dataset_id}/resolve-conflict")
def resolve_conflict(dataset_id: int, payload: ConflictResolvePayload) -> dict[str, Any]:
    return _ensure_ok(_dataset_service().resolve_conflict(
        dataset_id, payload.field_name.strip(), payload.chosen_catalog_id,
        payload.compare_scope_note.strip()))


# ── 训练流水线 ──
@router.post("/train_pipeline/{pipeline_id}/bind-dataset")
def bind_dataset(pipeline_id: int, payload: BindDatasetPayload) -> dict[str, Any]:
    service: TrainPipelineService = service_for("train_pipeline")  # type: ignore[assignment]
    return _ensure_ok(service.bind_dataset(pipeline_id, payload.dataset_id))


@router.post("/train_pipeline/{pipeline_id}/train-with-dataset")
def train_with_dataset(pipeline_id: int) -> dict[str, Any]:
    service = service_for("train_pipeline")  # type: ignore[assignment]
    result = service.train_and_register_model(pipeline_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


# ── 模型列对账 ──
@router.get("/model_registry/{model_id}/lineage")
def model_lineage(model_id: int) -> dict[str, Any]:
    registry: ModelRegistryService = service_for("model_registry")  # type: ignore[assignment]
    return _ensure_ok(registry.reconcile_column_lineage(model_id))


# ── 特征组按项目导出训练列 ──
@router.get("/feature_group/{group_id}/project-export/{project}")
def group_project_export(group_id: int, project: str) -> dict[str, Any]:
    group_service: FeatureGroupService = service_for("feature_group")  # type: ignore[assignment]
    return _ensure_ok(group_service.export_effective_features_for_project(group_id, project))
