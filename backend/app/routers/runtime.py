"""运行实例接口：维护运行记录，并从唯一重跑区间记录跨模块更新执行状态。

* ``GET /api/runtime`` 的每条记录都投影当前重跑区间（区间编号、开始/结束、重跑状态），
  与异常重跑侧命中的是同一条 ``rerun_interval`` 数据。
* ``GET /api/runtime/{记录编号}/intervals`` 返回该原始运行关联的重跑区间，供两边对照。
* 启动作业 / 终止作业 / 标记成功会先驱动重跑任务状态机，再镜像执行状态。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.runtime import RuntimeService

router = APIRouter(prefix="/api/runtime", tags=["运行实例"])

service = RuntimeService()

LIST_FIELDS = [
    "记录编号",
    "作业名称",
    "开始时间",
    "结束时间",
    "执行状态",
    "重跑区间编号",
    "重跑区间开始",
    "重跑区间结束",
    "重跑状态",
    "读取行数",
    "写入行数",
    "错误信息",
]
STATUSES = ["草稿", "运行中", "成功", "失败", "已终止"]


def _resolve_entry_id(value: str) -> int | None:
    text = str(value or "").strip()
    if text.isdigit():
        return int(text)
    row = service.get_entry_by_record_no(text)
    if row is None:
        return None
    return int(row.get("id", 0))


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出运行实例清单：包含当前重跑区间投影。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "runtime", "total": total, "items": items}


@router.get("/{record_no}/intervals", response_model=dict)
def list_intervals(record_no: str) -> dict[str, Any]:
    """运行实例侧查询重跑区间：返回该原始运行关联的唯一区间记录。"""
    records = service.intervals_of(record_no)
    if records is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {record_no} 不存在或已归档")
    return {"module": "runtime", "original_run": record_no, "total": len(records), "items": records}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按记录编号、作业名称、原始运行、区间或任务编号检索"),
    status: str | None = Query(default=None, description="执行状态或重跑状态"),
    original_run: str | None = Query(default=None, description="按原始运行编号过滤"),
    interval_no: str | None = Query(default=None, description="按重跑区间编号过滤"),
    task_no: str | None = Query(default=None, description="按重跑任务编号过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按记录编号、重跑区间与状态过滤运行实例列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword,
        status=status,
        original_run=original_run,
        interval_no=interval_no,
        task_no=task_no,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: str) -> dict:
    """读取单条运行记录明细；支持 id 或记录编号。"""
    resolved = _resolve_entry_id(entry_id)
    entry = service.get_entry(resolved) if resolved is not None else None
    if entry is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条运行记录，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.merged_values())
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="运行记录已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: str, payload: EntryPayload) -> ActionResult:
    """启动作业、终止作业、标记成功；重跑中的记录会同步更新重跑任务状态。"""
    values = payload.merged_values()
    action = str(values.get("action") or "").strip()
    resolved = _resolve_entry_id(entry_id)
    if resolved is None:
        return ActionResult(ok=False, message=f"运行记录 {entry_id} 不存在或已归档")
    entry, message = service.run_action(resolved, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
