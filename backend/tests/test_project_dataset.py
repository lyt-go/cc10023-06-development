"""项目训练数据集链路测试。

业务叙述（与需求逐条对应）：
1. 特征组按名字导出已生效定义，本身不带项目/共享来源；
2. 新增项目训练数据集，从租户训练字段目录映射并校验准入字段，只保留有权限的列；
3. 私有列只有归属项目能用，其他组织只能读取明确共享给自己的字段；
4. 输出列标识带计算口径与定义版本，能区分三个项目的同名字段；
5. 同一份训练配置同时命中本项目 7 天活跃量与共享来的全站 30 天活跃量，口径冲突须显式解决；
6. 模型登记保留训练列的归属项目、定义与统计口径快照，撤销共享后旧模型仍能说明原定义；
7. 为三个项目各建一条同名定义，再撤销一处共享，新模型字段归属反映届时权限。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import service_for
from app.store import store

PROJECT_RISK = "风控项目"
PROJECT_MKT = "营销项目"
PROJECT_OPS = "运营项目"
SAME_NAME = "用户活跃量"


def _rows(module: str):
    return store.rows(module)


def _find(module: str, entry_id: int):
    return store.find(module, entry_id)


def _create_catalog_entry(project: str, caliber: str, version: str, name: str = SAME_NAME) -> int:
    rows = _rows("tenant_field_catalog")
    next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    rows.append({
        "id": next_id,
        "status": "待准入",
        "pending": True,
        "abnormal": False,
        "目录编号": f"CAT-{next_id:04d}",
        "字段名称": name,
        "归属项目": project,
        "计算口径": caliber,
        "定义版本": version,
        "字段类型": "数值",
        "数据来源": f"{project}.{next_id}",
        "共享范围": "",
        "共享历史": [],
    })
    return next_id


def test_same_name_definitions_across_three_projects_have_distinct_identities():
    """三个项目各建一条同名定义，列标识必须靠口径+版本+归属项目区分。"""
    catalog = service_for("tenant_field_catalog")
    rid = _create_catalog_entry(PROJECT_RISK, "本项目近7天活跃", "v2.0.0")
    mid = _create_catalog_entry(PROJECT_MKT, "全站近30天活跃", "v2.0.0")
    oid = _create_catalog_entry(PROJECT_OPS, "本项目近7天活跃", "v2.0.0")
    for cid in (rid, mid, oid):
        assert catalog.admit_field(cid)["ok"]

    from app.services.catalog_identity import build_column_label
    labels = {cid: build_column_label(_find("tenant_field_catalog", cid)) for cid in (rid, mid, oid)}
    assert len(set(labels.values())) == 3, "同名定义的列标识必须互不相同"
    assert all(SAME_NAME in label for label in labels.values())
    assert all(("v2.0.0" in label) for label in labels.values())
    return rid, mid, oid


def test_private_columns_invisible_to_other_projects():
    """私有列只有归属项目能采用；未共享字段对其他项目不可见。"""
    catalog = service_for("tenant_field_catalog")
    cid = _create_catalog_entry(PROJECT_OPS, "运营内部口径近7天活跃", "v1.3.0")
    assert catalog.admit_field(cid)["ok"]

    visible_mkt = {item["catalog_id"] for item in catalog.visible_fields(PROJECT_MKT)}
    visible_ops = {item["catalog_id"] for item in catalog.visible_fields(PROJECT_OPS)}
    assert cid not in visible_mkt, "未共享的私有列不得出现在其他项目可见列表"
    assert cid in visible_ops, "归属项目必须能看到自己的私有列"

    check = catalog.check_admission([cid], PROJECT_MKT)
    assert check["通过数"] == 0 and check["拒绝数"] == 1
    assert "无权" in check["拒绝"][0]["reason"]


def test_dataset_mapping_only_keeps_permitted_columns():
    """建集时未准入与无权限字段被剔除，只把有权限的列写入字段映射。"""
    catalog = service_for("tenant_field_catalog")
    dataset_svc = service_for("project_dataset")

    # CAT-0006 待准入；CAT-0003 风控私有未共享给营销；CAT-0004 营销自己的已准入字段
    result = dataset_svc.create_project_dataset(
        PROJECT_MKT, "营销权限校验集", [6, 3, 4])
    assert result["通过数"] == 1
    assert result["拒绝数"] == 2
    reasons = {item["catalog_id"]: item["reason"] for item in result["拒绝明细"]}
    assert "未准入" in reasons[6]
    assert "无权" in reasons[3]

    dataset = _find("project_dataset", result["dataset_id"])
    mappings = json.loads(dataset["字段映射"])
    assert [m["catalog_id"] for m in mappings] == [4]
    assert mappings[0]["来源类型"] == "本项目私有"
    assert mappings[0]["列标识"].startswith("用户活跃量|全站近30天活跃|v1.0.0|营销项目")


def test_share_then_revoke_changes_new_training_permission_but_keeps_old_model():
    """授权共享→训练登记→撤销共享：旧模型快照不变，新训练被拦截。"""
    catalog = service_for("tenant_field_catalog")
    dataset_svc = service_for("project_dataset")
    pipeline_svc = service_for("train_pipeline")
    registry = service_for("model_registry")

    # 风控私有的风险评分（CAT-0003）共享给营销
    share = catalog.grant_share(3, PROJECT_MKT)
    assert share["ok"] and PROJECT_MKT in _find("tenant_field_catalog", 3)["共享范围"]

    created = dataset_svc.create_project_dataset(
        PROJECT_MKT, "营销含共享列训练集", [3, 4])
    assert created["拒绝数"] == 0
    dataset_id = created["dataset_id"]
    mappings = dataset_svc.get_mappings(dataset_id)
    shared = next(m for m in mappings if m["catalog_id"] == 3)
    assert shared["来源类型"] == f"跨项目共享（来源：{PROJECT_RISK}）"

    # 建一条流水线并绑定数据集后训练
    pipe_rows = _rows("train_pipeline")
    pipe_id = max((int(r.get("id", 0)) for r in pipe_rows), default=0) + 1
    pipe_rows.append({
        "id": pipe_id, "status": "草稿", "pending": True, "abnormal": False,
        "流水线编号": f"TRAI-{pipe_id:04d}", "流水线名称": f"共享列训练{pipe_id}",
        "特征集": "", "关联数据集": "", "算法类型": "xgboost",
        "超参数配置": "{}", "评估指标": "auc", "训练周期": "weekly", "流水线版本": "v1.0.0",
    })
    assert pipeline_svc.bind_dataset(pipe_id, dataset_id)["ok"]
    trained = pipeline_svc.train_and_register_model(pipe_id)
    assert trained["ok"], trained
    model_before_id = trained["model_id"]
    model_before = _find("model_registry", model_before_id)
    snapshot_before = json.loads(model_before["列归属快照"])
    assert {c["catalog_id"] for c in snapshot_before} == {3, 4}
    assert model_before["来源项目"] == PROJECT_MKT

    # 撤销风控→营销的共享
    revoked = catalog.revoke_share(3, PROJECT_MKT)
    assert revoked["ok"]
    assert PROJECT_MKT not in _find("tenant_field_catalog", 3)["共享范围"]
    history = _find("tenant_field_catalog", 3)["共享历史"]
    assert {"动作": "撤销共享", "目标项目": PROJECT_MKT, "结果": "生效"} in history

    # 旧模型快照仍记录原本使用的定义，对账时标注共享已撤销
    reconciliation = registry.reconcile_column_lineage(model_before_id)
    diff_catalogs = {d["字段名称"]: d for d in reconciliation["diff"]}
    assert "风险评分" in diff_catalogs
    assert any("共享已撤销" in note for note in diff_catalogs["风险评分"]["差异"])
    snapshot_again = json.loads(_find("model_registry", model_before_id)["列归属快照"])
    assert snapshot_again == snapshot_before, "撤销共享不得回改旧模型登记快照"

    # 新训练按届时权限重验，共享列已不可用 → 拦截
    blocked = pipeline_svc.train_and_register_model(pipe_id)
    assert not blocked["ok"]
    assert any(item["catalog_id"] == 3 for item in blocked["拒绝明细"])


def test_same_name_metric_caliber_conflict_must_be_resolved_before_training():
    """7天与30天同名活跃量口径冲突：解决后模型登记写明实际统计范围与可比性。"""
    catalog = service_for("tenant_field_catalog")
    dataset_svc = service_for("project_dataset")
    pipeline_svc = service_for("train_pipeline")

    rid, mid, oid = test_same_name_definitions_across_three_projects_have_distinct_identities()
    # 风控项目能拿到：自己的 7 天活跃量（rid）+ 营销共享来的全站 30 天活跃量（mid）
    assert catalog.grant_share(mid, PROJECT_RISK)["ok"]

    created = dataset_svc.create_project_dataset(
        PROJECT_RISK, "风控同名冲突训练集", [rid, mid])
    dataset_id = created["dataset_id"]
    report = dataset_svc.conflict_report(dataset_id)
    assert report["冲突数"] == 1
    conflict = report["冲突列"][0]
    assert conflict["字段名称"] == SAME_NAME
    assert set(conflict["口径集合"]) == {"本项目近7天活跃", "全站近30天活跃"}

    # 未解决冲突前训练必须被拦截
    pipe_rows = _rows("train_pipeline")
    pipe_id = max((int(r.get("id", 0)) for r in pipe_rows), default=0) + 1
    pipe_rows.append({
        "id": pipe_id, "status": "草稿", "pending": True, "abnormal": False,
        "流水线编号": f"TRAI-{pipe_id:04d}", "流水线名称": f"口径冲突训练{pipe_id}",
        "特征集": "", "关联数据集": "", "算法类型": "xgboost",
        "超参数配置": "{}", "评估指标": "auc", "训练周期": "weekly", "流水线版本": "v1.0.0",
    })
    pipeline_svc.bind_dataset(pipe_id, dataset_id)
    blocked = pipeline_svc.train_and_register_model(pipe_id)
    assert not blocked["ok"] and "口径冲突" in blocked["message"]

    # 项目负责人选定全站 30 天口径，并记录与全站历史模型的可比性结论
    resolved = dataset_svc.resolve_conflict(
        dataset_id, SAME_NAME, mid,
        compare_scope_note="采用全站近30天活跃，可与全站历史模型直接比较；7天口径不可比")
    assert resolved["ok"] and resolved["剩余冲突数"] == 0
    assert resolved["采用统计范围"] == "全站近30天活跃"

    trained = pipeline_svc.train_and_register_model(pipe_id)
    assert trained["ok"], trained
    model = _find("model_registry", trained["model_id"])
    snapshot = json.loads(model["列归属快照"])
    assert len(snapshot) == 1
    assert snapshot[0]["catalog_id"] == mid
    assert snapshot[0]["归属项目"] == PROJECT_MKT
    assert "全站近30天活跃" in model["统计口径说明"]
    assert "可与全站历史模型直接比较" in json.dumps(snapshot, ensure_ascii=False)
    assert trained["口径结论"] and "全站近30天活跃" in trained["口径结论"][0]


def test_feature_group_project_export_respects_sharing_and_identity():
    """特征组按项目导出：同名已生效定义经目录过滤后只输出有权限且带列标识的列。"""
    group_svc = service_for("feature_group")
    catalog = service_for("tenant_field_catalog")

    # 准备一条特征组，包含特征「用户活跃量」，并让特征定义里存在同名已生效定义
    define_rows = _rows("feature_define")
    define_id = max((int(r.get("id", 0)) for r in define_rows), default=0) + 1
    define_rows.append({
        "id": define_id, "status": "已生效", "pending": False, "abnormal": False,
        "特征编号": f"FEAT-{define_id:04d}", "特征名称": SAME_NAME, "特征类型": "数值",
        "计算逻辑": "活跃量", "数据来源": "catalog", "缺失填充": "0",
        "取值范围": ">=0", "特征版本": "v1.0.0",
    })
    group_rows = _rows("feature_group")
    group_id = max((int(r.get("id", 0)) for r in group_rows), default=0) + 1
    group_rows.append({
        "id": group_id, "status": "已生效", "pending": False, "abnormal": False,
        "组编号": f"GRP-{group_id:04d}", "组名称": "活跃量特征组",
        "包含特征": SAME_NAME, "业务主题": "活跃", "默认权重": "1",
        "互斥规则": "", "依赖关系": "", "组版本": "v1.0.0",
    })

    risk_export = group_svc.export_effective_features_for_project(group_id, PROJECT_RISK)
    assert risk_export["ok"]
    risk_names = {c["字段名称"]: c for c in risk_export["columns"]}
    assert SAME_NAME in risk_names
    # 风控可见的同名列：自己的私有列 + 共享给自己的列，且每列带口径/版本标识
    assert all("|" in c["列标识"] for c in risk_export["columns"])
    assert all(c["计算口径"] and c["定义版本"] for c in risk_export["columns"])

    # 运营项目未被任何同名字段额外共享时，只看得到自己的私有同名定义
    ops_export = group_svc.export_effective_features_for_project(group_id, PROJECT_OPS)
    assert all(c["归属项目"] == PROJECT_OPS or c["来源类型"] != "本项目私有"
               for c in ops_export["columns"])
    # 营销私有且未共享给运营的列必须出现在剔除清单
    skipped_owners = {item["归属项目"] for item in ops_export["剔除列"]}
    assert PROJECT_MKT in skipped_owners


def _run_all():
    tests = [
        test_same_name_definitions_across_three_projects_have_distinct_identities,
        test_private_columns_invisible_to_other_projects,
        test_dataset_mapping_only_keeps_permitted_columns,
        test_share_then_revoke_changes_new_training_permission_but_keeps_old_model,
        test_same_name_metric_caliber_conflict_must_be_resolved_before_training,
        test_feature_group_project_export_respects_sharing_and_identity,
    ]
    passed = failed = 0
    for test in tests:
        try:
            test()
            print(f"[PASS] {test.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {test.__name__}: {exc}")
            failed += 1
    print(f"\n总测试: {len(tests)}, 通过: {passed}, 失败: {failed}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
