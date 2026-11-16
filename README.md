# 数据仓库ETL任务编排平台

面向数据仓库ETL任务开发、作业编排调度、任务依赖解析、源端数据校验、异常任务重跑、血缘分板与运行全景监控的ETL工程管理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
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
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| ETL作业 | `etljob` | 作业定义 | 作业编号、作业名称、源端数据表 |
| 任务编排 | `dag` | 编排图 | 图编号、图名称、拓扑结构 |
| 依赖解析 | `dependency` | 依赖关系 | 关系编号、上游作业、下游作业 |
| 源端校验 | `sourcecheck` | 校验配置 | 配置编号、源端数据表、行数下限 |
| 运行实例 | `runtime` | 运行记录 | 记录编号、作业名称、开始时间、重跑区间编号、重跑状态 |
| 异常重跑 | `rerun` | 重跑任务 | 任务编号、原始运行编号、重跑区间编号、重跑区间开始、重跑区间结束、重跑状态 |
| 基线管理 | `baseline` | 基线定义 | 基线编号、基线名称、承诺完成时间 |
| 分区检测 | `partitioncheck` | 检测规则 | 规则编号、数据表名、期望分区数 |
| 数据比对 | `comparator` | 比对任务 | 任务编号、源表名、目标表名 |
| 元数据存储 | `metastore` | 元数据表 | 表编号、表名、所属数仓层 |
| 资源配置 | `resource` | 资源池 | 池编号、池名称、最大并发数 |
| SLA监控 | `slavalidate` | SLA规则 | 规则编号、适用作业、完成截止时间 |
| SQL模板 | `sqltpl` | 模板定义 | 模板编号、模板名称、模板类型 |
| 数据备份 | `backuprestore` | 备份策略 | 策略编号、数据表名、备份周期 |
| 日志归档 | `logarchive` | 归档策略 | 策略编号、日志类型、保留天数 |
| 通知渠道 | `notifychan` | 渠道配置 | 渠道编号、渠道类型、接收地址 |
| 发布计划 | `publishplan` | 发布单 | 发布编号、发布内容、变更说明 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 异常重跑与重跑区间

- **唯一区间记录**：`backend/app/services/rerun_interval.py` 维护 `rerun_interval` 表，按
  `(原始运行编号, 重跑区间开始, 重跑区间结束)` 去重。异常重跑清单、任务详情和运行实例清单
  都从同一条“当前重跑区间”记录投影区间与状态，三处不会指向不同区间。
- **登记校验**：`原始运行编号` 必须能在运行实例中找到（对应 `记录编号`），区间开始不得晚于
  结束；只填开始时间时结束时间取开始时间。`任务编号` 缺省自动生成，不会覆盖原始运行编号。
- **状态机**：草稿 → 重跑审批中 → 执行中 → 已完成，任意非终态可取消。已完成/已取消是终态，
  再次执行必须新建任务；同一原始运行同一区间最多一个未终结任务。
- **并发**：状态变更在 `store.lock` 内提交，启动与取消同时到达时只有一个终态生效；取消优先
  于启动落地，后到的请求会收到可读的拒绝说明。
- **跨模块**：运行实例的启动作业 / 终止作业 / 标记成功会先驱动重跑状态机，再镜像执行状态；
  两边的原始运行、重跑区间编号、区间边界和重跑状态始终一致。
- **历史兼容**：旧记录没有区间编号、缺少结束时间、区间倒挂、或把任务编号填进原始运行编号时，
  首次读取会补建区间记录并做最小修正，不会报错；补建的历史任务标记为 `历史数据`，
  `GET /api/rerun?status=历史` 可单独检索。
- **接口**：`GET /api/rerun/intervals` 与 `GET /api/runtime/{记录编号}/intervals` 命中同一批
  区间记录；`GET /api/rerun` 支持 `keyword`、`original_run`、`interval_no`、`task_no`、
  `status`、`include_history` 与分页参数。

领域：数据工程
子方向：数仓与数据湖
