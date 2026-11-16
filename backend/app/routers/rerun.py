"""异常重跑接口：登记重跑任务、推进状态、查询唯一重跑区间记录。

约定
----

* ``GET /api/rerun`` 只返回每个原始运行的**当前**重跑区间；``include_history=true``
  才把被接管的旧任务一并列出。
* 列表支持按任务编号、原始运行编号、区间编号、状态检索，分页参数原样回显。
* 区间记录统一从 ``/api/rerun/intervals`` 读取，运行实例侧用
  ``/api/runtime/intervals`` 读取，两边命中同一条数据。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.rerun import RerunService

router = APIRouter(prefix="/api/rerun", tags=["异常重跑"])

service = RerunService()

LIST_FIELDS = [
    "任务编号",
    "原始运行编号",
    "重跑区间编号",
    "重跑区间开始",
    "重跑区间结束",
    "重跑原因",
    "覆盖策略",
    "依赖级联",
    "重跑状态",
]
STATUSES = ["草稿", "重跑审批中", "执行中", "已完成", "已取消"]
INTERVAL_FIELDS = [
    "重跑区间编号",
    "原始运行编号",
    "任务编号",
    "重跑区间开始",
    "重跑区间结束",
    "重跑状态",
    "是否当前重跑区间",
    "历史数据",
]


def _resolve_entry_id(value: str) -> int | None:
    """路径参数既接受数据库 id，也接受任务编号，方便按任务编号定位。"""
    text = str(value or "").strip()
    if text.isdigit():
        return int(text)
    task = service.get_entry_by_task_no(text)
    if task is None:
        return None
    return int(task.get("id", 0))


# ---- 固定路径必须排在 /{entry_id} 前面，否则会被当成记录 id。 ----------------
@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出异常重跑清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "rerun", "total": total, "items": items}


@router.get("/intervals", response_model=PageResult[dict])
def list_intervals(
    original_run: str | None = Query(default=None, description="按原始运行编号过滤"),
    keyword: str | None = Query(default=None, description="按区间编号、原始运行编号或任务编号检索"),
    current_only: bool = Query(default=False, description="只返回当前重跑区间"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """读取唯一重跑区间记录；与运行实例侧查询命中同一份数据。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_intervals(
        original_run=original_run,
        keyword=keyword,
        current_only=current_only,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/intervals/{interval_no}", response_model=dict)
def get_interval(interval_no: str) -> dict:
    """按区间编号（或数据库 id）读取唯一重跑区间记录。"""
    record = service.get_interval(interval_no)
    if record is None:
        raise HTTPException(status_code=404, detail=f"重跑区间 {interval_no} 不存在或已归档")
    return record


@router.get("/runs/{original_run}/interval", response_model=dict)
def get_current_interval(original_run: str) -> dict:
    """读取某个原始运行当前生效的唯一重跑区间。"""
    record = service.current_interval(original_run)
    if record is None:
        raise HTTPException(status_code=404, detail=f"原始运行 {original_run} 暂无重跑区间")
    return record


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按任务编号、原始运行编号、区间编号或作业名称检索"),
    status: str | None = Query(default=None, description="草稿、重跑审批中、执行中、已完成、已取消"),
    original_run: str | None = Query(default=None, description="按原始运行编号过滤"),
    interval_no: str | None = Query(default=None, description="按重跑区间编号过滤"),
    task_no: str | None = Query(default=None, description="按任务编号过滤"),
    include_history: bool = Query(default=False, description="是否包含已被新任务接管的历史重跑记录"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按任务编号、原始运行编号、区间与状态过滤异常重跑列表；只显示当前重跑区间。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword,
        status=status,
        original_run=original_run,
        interval_no=interval_no,
        task_no=task_no,
        include_history=include_history,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: str) -> dict:
    """读取单条重跑任务明细；支持 id 或任务编号。"""
    resolved = _resolve_entry_id(entry_id)
    entry = service.get_entry(resolved) if resolved is not None else None
    if entry is None:
        raise HTTPException(status_code=404, detail=f"重跑任务 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条重跑任务：校验区间、关联原始运行并入唯一区间记录。"""
    entry, missing = service.create_entry(payload.merged_values())
    if entry is None:
        return ActionResult(ok=False, message=f"重跑任务未登记：{'；'.join(missing)}")
    return ActionResult(ok=True, message="重跑任务已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: str, payload: EntryPayload) -> ActionResult:
    """推进重跑任务状态；终态不可逆，取消后再次执行需新建任务。"""
    values = payload.merged_values()
    action = str(values.get("action") or "").strip()
    resolved = _resolve_entry_id(entry_id)
    if resolved is None:
        return ActionResult(ok=False, message=f"重跑任务 {entry_id} 不存在或已归档")
    entry, message = service.run_action(resolved, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
