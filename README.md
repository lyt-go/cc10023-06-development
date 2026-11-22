# 特征工程与数据挖掘工作台

面向特征构建、特征选择、数据切片、模型训练与挖掘结果管理的数据分析特征工程平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 特征源管理 | `feature_source` | 特征源 | 源编号、源名称、数据表名 |
| 特征定义 | `feature_define` | 特征定义 | 特征编号、特征名称、特征类型 |
| 特征组管理 | `feature_group` | 特征组 | 组编号、组名称、包含特征 |
| 数据切片配置 | `data_slice` | 切片配置 | 切片编号、切片名称、数据来源 |
| 特征计算任务 | `feature_compute` | 计算任务 | 任务编号、关联特征、数据切片 |
| 特征筛选策略 | `feature_selection` | 筛选策略 | 策略编号、特征集、筛选方法 |
| 特征重要性分析 | `feature_iv` | 重要性评估 | 评估编号、关联特征、信息值IV |
| 特征快照管理 | `feature_snapshot` | 特征快照 | 快照编号、特征集、快照日期 |
| 训练流水线 | `train_pipeline` | 流水线配置 | 流水线编号、流水线名称、特征集 |
| 模型注册中心 | `model_registry` | 模型注册 | 模型编号、模型名称、算法类型 |
| 批量预测任务 | `predict_batch` | 预测任务 | 任务编号、模型编号、数据切片 |
| 挖掘结果管理 | `mining_result` | 挖掘结果 | 结果编号、分析主题、算法类型 |
| 聚类分析配置 | `cluster_config` | 聚类配置 | 配置编号、特征集、算法类型 |
| 特征相关性矩阵 | `feature_corr` | 相关性矩阵 | 矩阵编号、特征集、计算方法 |
| 特征漂移监控 | `drift_monitor` | 漂移监控 | 监控编号、关联特征、基准分布 |
| 数据导出管理 | `data_export` | 导出任务 | 任务编号、数据来源、导出范围 |
| 流水线调度 | `pipeline_schedule` | 调度配置 | 调度编号、关联流水线、Cron表达式 |
| 业务场景标注 | `scenario_label` | 场景标注 | 标注编号、场景名称、关联特征 |
| 租户训练字段目录 | `tenant_field_catalog` | 租户训练字段 | 目录编号、字段名称、归属项目、计算口径、定义版本、共享范围 |
| 项目训练数据集 | `project_dataset` | 项目训练数据集 | 数据集编号、归属项目、字段映射、冲突列、准入校验结果 |

### 项目训练数据集与字段权限

- `tenant_field_catalog` 是各项目训练字段的唯一登记处：字段先准入（`已准入`）才能映射，
  私有列仅归属项目可采用，其他项目只能读取「共享范围」中明确授权给自己的字段；
  共享可通过动作 `授权共享/撤销共享`（remark 传目标项目）或专用接口变更，撤销只影响后续训练。
- `project_dataset` 建集时从目录映射并批量校验准入与权限，只保留有权限的列；
  同名指标出现多个计算口径会记入「冲突列」，须经 `resolve-conflict` 选定统计范围后才能训练。
- 所有训练侧输出的列标识统一为 `字段名称|计算口径|定义版本|归属项目`
  （见 `app/services/catalog_identity.py`），同名定义靠口径与版本区分。
- 训练流水线绑定数据集后，训练开始时按目录现状重验权限，并把实际训练列的归属项目、
  定义版本与统计口径快照写入模型登记；`model_registry/{id}/lineage` 可据此对账，
  旧模型快照不随后续撤销共享/下线/升版而回改。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用
