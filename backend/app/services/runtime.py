"""运行实例业务规则：接收唯一重跑区间记录，并跨模块更新执行状态。

口径
----

* 运行实例清单里的 ``重跑区间`` 相关字段不再各自保存，而是从 ``rerun_interval`` 里
  该原始运行的唯一当前记录投影出来，因此与异常重跑侧看到的是同一条数据。
* 运行实例动作（启动作业 / 终止作业 / 标记成功）会驱动重跑任务的对应动作，二者在
  同一把锁内提交；重跑任务侧的终态是唯一事实来源，运行实例只做状态镜像。
* 对重跑任务的取消/完成重复到达时，只有一个终态能落地，另一个会收到可读的拒绝说明。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services.rerun_interval import (
    ACTIVE_STATUSES,
    CURRENT,
    END,
    INTERVAL_ID,
    INTERVAL_NO,
    ORIGINAL_RUN,
    RERUN_ACTION_BY_RUNTIME_ACTION,
    RERUN_STATUS,
    START,
    TASK_NO,
    TERMINAL_STATUSES,
    as_text,
    normalize_values,
    rerun_intervals,
    status_of,
)

MODULE = "runtime"
REQUIRED_FIELDS = ["记录编号", "作业名称", "开始时间"]
STATUS_ORDER = ["草稿", "运行中", "成功", "失败", "已终止"]
ACTION_RULES = {"启动作业": "运行中", "终止作业": "已终止", "标记成功": "成功"}
NEGATIVE_ACTIONS = ["终止作业"]

# 运行实例动作对应的重跑动作：启动 → 启动重跑，终止 → 取消重跑，成功 → 完成重跑。
RERUN_ACTIONS = RERUN_ACTION_BY_RUNTIME_ACTION

RERUN_PROJECTED_FIELDS = [
    ORIGINAL_RUN,
    INTERVAL_NO,
    INTERVAL_ID,
    TASK_NO,
    START,
    END,
    RERUN_STATUS,
    CURRENT,
]


class RuntimeService:
    def __init__(self) -> None:
        self.intervals = rerun_intervals

    # ---- 读 ----------------------------------------------------------------
    def _project_interval(self, row: dict[str, Any]) -> dict[str, Any]:
        """把唯一当前区间记录投影到运行实例；没有重跑时保持原样。"""
        original_run = as_text(row.get(ORIGINAL_RUN)) or as_text(row.get("记录编号"))
        record = self.intervals.current_for_run(original_run)
        if record is None:
            # 兼容历史：运行实例上可能还挂着区间编号，但区间表里没有对应记录。
            interval_no = as_text(row.get(INTERVAL_NO))
            if interval_no:
                record = self.intervals.find_by_no(interval_no)
            if record is None:
                return row
        view = dict(row)
        view[ORIGINAL_RUN] = as_text(record.get(ORIGINAL_RUN))
        view[INTERVAL_NO] = as_text(record.get(INTERVAL_NO))
        view[INTERVAL_ID] = record.get("id")
        view[TASK_NO] = as_text(record.get(TASK_NO))
        view[START] = as_text(record.get(START))
        view[END] = as_text(record.get(END))
        view[RERUN_STATUS] = status_of(record)
        view[CURRENT] = bool(record.get(CURRENT))
        return view

    def _view(self, row: dict[str, Any]) -> dict[str, Any]:
        view = self._project_interval(row)
        view["重跑区间"] = self._interval_label(view)
        return view

    @staticmethod
    def _interval_label(view: dict[str, Any]) -> str:
        start = as_text(view.get(START))
        end = as_text(view.get(END))
        if not start and not end:
            return ""
        if not end or end == start:
            return start
        return f"{start} ~ {end}"

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        original_run: str | None = None,
        interval_no: str | None = None,
        task_no: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with store.lock:
            self.intervals.ensure_all()
            views = [self._view(row) for row in store.rows(MODULE)]

        if keyword:
            needle = keyword.strip()
            views = [
                view
                for view in views
                if needle in str(view.get("记录编号", ""))
                or needle in str(view.get("作业名称", ""))
                or needle in str(view.get(ORIGINAL_RUN, ""))
                or needle in str(view.get(INTERVAL_NO, ""))
                or needle in str(view.get(TASK_NO, ""))
            ]
        if original_run:
            views = [view for view in views if str(view.get(ORIGINAL_RUN, "")) == original_run.strip()]
        if interval_no:
            views = [view for view in views if str(view.get(INTERVAL_NO, "")) == interval_no.strip()]
        if task_no:
            views = [view for view in views if str(view.get(TASK_NO, "")) == task_no.strip()]
        if status:
            wanted = status.strip()
            views = [
                view
                for view in views
                if str(view.get("执行状态")) == wanted or str(view.get(RERUN_STATUS)) == wanted
            ]

        views.sort(key=lambda view: int(view.get("id", 0)))
        total = len(views)
        safe_page = max(int(page or 1), 1)
        safe_size = max(int(size or 20), 1)
        start = (safe_page - 1) * safe_size
        return views[start:start + safe_size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with store.lock:
            self.intervals.ensure_all()
            row = store.find(MODULE, entry_id)
            if row is None:
                return None
            return self._view(row)

    def get_entry_by_record_no(self, record_no: str) -> dict[str, Any] | None:
        with store.lock:
            self.intervals.ensure_all()
            row = self._find_runtime(record_no)
            return self._view(row) if row is not None else None

    def intervals_of(self, record_no: str) -> list[dict[str, Any]] | None:
        """运行实例侧的区间查询：返回该原始运行关联的重跑区间记录。"""
        with store.lock:
            self.intervals.ensure_all()
            row = self._find_runtime(record_no)
            if row is None:
                return None
            records = self.intervals.for_run(as_text(row.get("记录编号")))
        return [dict(record) for record in sorted(records, key=lambda item: int(item.get("id", 0)))]

    # ---- 写 ----------------------------------------------------------------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        normalized = normalize_values(values)
        missing = [
            field
            for field in REQUIRED_FIELDS
            if not as_text(normalized.get(field))
        ]
        if missing:
            return None, missing
        with store.lock:
            rows = store.rows(MODULE)
            record_no = as_text(normalized.get("记录编号"))
            if self._find_runtime(record_no) is not None:
                return None, [f"记录编号 {record_no} 已存在，请更换"]
            entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: normalized.get(field) for field in REQUIRED_FIELDS})
            for field in ("结束时间", "读取行数", "写入行数", "错误信息"):
                if field in normalized:
                    entry[field] = normalized[field]
            entry["执行状态"] = "草稿"
            entry["status"] = "草稿"
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
            return self._view(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            self.intervals.ensure_all()
            row = store.find(MODULE, entry_id)
            if row is None:
                return None, f"运行记录 {entry_id} 不存在或已归档"
            return self._apply_action(row, action)

    def run_action_by_record_no(self, record_no: str, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            self.intervals.ensure_all()
            row = self._find_runtime(record_no)
            if row is None:
                return None, f"运行记录 {record_no} 不存在或已归档"
            return self._apply_action(row, action)

    def _apply_action(
        self,
        row: dict[str, Any],
        action: str,
    ) -> tuple[dict[str, Any] | None, str]:
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于运行实例可执行范围"

        original_run = as_text(row.get("记录编号"))
        record = self.intervals.current_for_run(original_run)
        if record is not None:
            rerun_action = RERUN_ACTIONS.get(action)
            rerun_task = self._rerun_task(record)
            if rerun_action and rerun_task is not None:
                rerun_status = status_of(rerun_task)
                if rerun_status in TERMINAL_STATUSES:
                    # 终态的重跑任务不允许被运行实例重新拉起，必须新建任务。
                    return None, (
                        f"原始运行 {original_run} 的重跑任务已是终态「{rerun_status}」，"
                        "如需再次执行请新建重跑任务"
                    )
                from app.services.rerun import RerunService

                rerun_service = RerunService(self.intervals)
                result, message = rerun_service.run_action_by_task_no(
                    as_text(rerun_task.get(TASK_NO)), rerun_action
                )
                if result is not None:
                    # 重跑侧状态机已经同步了区间记录与运行实例，只需回读最新视图。
                    latest = store.find(MODULE, int(row.get("id", 0))) or row
                    return self._view(latest), message
                # 重跑任务还处在草稿/审批阶段，对应的重跑动作不合法；
                # 此时只更新运行实例自身的执行状态，不改写重跑状态。
                if rerun_status in ACTIVE_STATUSES:
                    return None, message

        target = ACTION_RULES[action]
        row["执行状态"] = target
        row["status"] = target
        row["pending"] = target == "运行中"
        row["abnormal"] = action in NEGATIVE_ACTIONS
        if target in {"成功", "已终止"}:
            row["结束时间"] = row.get("结束时间") or _now_text()
        # 运行实例侧的状态变化同样回写到唯一区间记录，避免两边状态漂移。
        if record is not None:
            record["运行执行状态"] = target
            record[UPDATED_AT] = _now_text()
        return self._view(row), f"运行记录已{action}"

    @staticmethod
    def _rerun_task(record: dict[str, Any]) -> dict[str, Any] | None:
        task_no = as_text(record.get(TASK_NO))
        if not task_no:
            return None
        from app.services.rerun_interval import find_task

        return find_task(task_no)

    # ---- 内部工具 ----------------------------------------------------------
    @staticmethod
    def _find_runtime(value: Any) -> dict[str, Any] | None:
        text = as_text(value)
        if not text:
            return None
        rows = store.rows(MODULE)
        if text.isdigit():
            for row in rows:
                if int(row.get("id", 0)) == int(text):
                    return row
        for row in rows:
            if as_text(row.get("记录编号")) == text:
                return row
        return None


def _now_text() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
