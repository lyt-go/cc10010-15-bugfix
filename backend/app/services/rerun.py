"""异常重跑业务规则：区间校验、唯一区间记录、状态机与跨模块同步。

口径
----

* 重跑任务的 ``原始运行编号`` 永远指向 ``runtime`` 的记录编号，登记时不会被
  ``任务编号`` 覆盖；``任务编号`` 缺省时自动生成。
* 区间开始不得晚于区间结束；只登记开始时间时结束时间取开始时间（兼容旧入口）。
* 每个原始运行在同一时刻只有一条“当前重跑区间”记录。异常重跑清单、任务详情和运行实例
  清单都从 ``rerun_interval`` 的这条记录投影原始运行、区间与状态，三处天然一致。
* 状态只允许单向推进：草稿 → 重跑审批中 → 执行中 → 已完成 / 已取消。
  已取消、已完成是终态，要再次执行必须新建任务。所有流转都在 ``store.lock`` 内完成，
  并发启动/取消只有一个终态能落地。
* 既存历史重跑记录在首次读取时补建区间记录，缺失结束时间、区间倒挂等脏数据只做最小
  修正，不抛错、不改写原始运行编号。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store
from app.services.rerun_interval import (
    ACTION_MESSAGES,
    ACTION_RULES,
    ACTION_SOURCES,
    APPROVING,
    CANCELLED,
    CURRENT,
    DRAFT,
    END,
    HISTORICAL,
    INTERVAL_ID,
    INTERVAL_NO,
    ORIGINAL_RUN,
    OVERRIDE_POLICY,
    REASON,
    RERUN_STATUS,
    RUNNING,
    START,
    STATUS,
    STATUS_ORDER,
    TAKEOVER,
    TASK_NO,
    TERMINAL_STATUSES,
    RerunIntervalRepository,
    as_text,
    canonical_action,
    find_runtime,
    normalize_values,
    rerun_intervals,
    status_of,
    validate_interval,
)

MODULE = "rerun"
REQUIRED_FIELDS = [ORIGINAL_RUN, START]
STATUS_ORDER = STATUS_ORDER
ACTION_RULES = ACTION_RULES
NEGATIVE_ACTIONS = ["取消重跑"]

PROJECTED_FIELDS = [
    TASK_NO,
    ORIGINAL_RUN,
    INTERVAL_NO,
    INTERVAL_ID,
    START,
    END,
    REASON,
    OVERRIDE_POLICY,
    "依赖级联",
    RERUN_STATUS,
    STATUS,
    CURRENT,
    HISTORICAL,
    TAKEOVER,
]


class RerunService:
    def __init__(self, intervals: RerunIntervalRepository | None = None) -> None:
        self.intervals = intervals or rerun_intervals

    # ---- 读 ----------------------------------------------------------------
    def _view(self, task: dict[str, Any]) -> dict[str, Any]:
        """把任务与其唯一区间记录合成一条对外数据；列表与详情共用。"""
        record = self.intervals.for_task(task)
        owner_is_task = bool(
            record is not None
            and as_text(record.get(TASK_NO)) == as_text(task.get(TASK_NO))
        )
        view: dict[str, Any] = dict(task)

        if owner_is_task and record is not None:
            view[TASK_NO] = as_text(record.get(TASK_NO)) or as_text(task.get(TASK_NO))
            view[ORIGINAL_RUN] = as_text(record.get(ORIGINAL_RUN)) or as_text(task.get(ORIGINAL_RUN))
            view[INTERVAL_NO] = as_text(record.get(INTERVAL_NO))
            view[INTERVAL_ID] = record.get("id")
            view[START] = as_text(record.get(START))
            view[END] = as_text(record.get(END))
            status = status_of(record)
            view[CURRENT] = bool(record.get(CURRENT))
            view[HISTORICAL] = bool(record.get(HISTORICAL))
        else:
            # 历史任务：区间字段取任务自身（已做最小修正），不冒充当前区间。
            status = status_of(task)
            view[CURRENT] = False
            view[HISTORICAL] = bool(task.get(HISTORICAL))
            view[INTERVAL_NO] = as_text(task.get(INTERVAL_NO))
            view[INTERVAL_ID] = task.get(INTERVAL_ID)
            view[START] = as_text(task.get(START))
            view[END] = as_text(task.get(END)) or as_text(task.get(START))

        view[RERUN_STATUS] = status
        view[STATUS] = status
        view["pending"] = status not in TERMINAL_STATUSES
        view["abnormal"] = status == CANCELLED
        view[TAKEOVER] = bool(task.get(TAKEOVER))

        runtime = find_runtime(view.get(ORIGINAL_RUN))
        view["作业名称"] = as_text(runtime.get("作业名称")) if runtime else ""
        view["运行执行状态"] = as_text(runtime.get("执行状态")) if runtime else ""
        return view

    def _all_views(self) -> list[dict[str, Any]]:
        return [self._view(task) for task in store.rows(MODULE)]

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        original_run: str | None = None,
        interval_no: str | None = None,
        task_no: str | None = None,
        include_history: bool = False,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with store.lock:
            self.intervals.ensure_all()
            views = self._all_views()

        history_query = status is not None and status.strip() in {"历史", "历史数据"}
        if history_query:
            include_history = True
        if not include_history:
            # 同一原始运行只保留当前重跑区间；被新任务接管的旧任务不再出现在清单里。
            views = [view for view in views if view[CURRENT] and not view[TAKEOVER]]

        if keyword:
            needle = keyword.strip()
            views = [
                view
                for view in views
                if needle in str(view.get(TASK_NO, ""))
                or needle in str(view.get(ORIGINAL_RUN, ""))
                or needle in str(view.get(INTERVAL_NO, ""))
                or needle in str(view.get("作业名称", ""))
                or needle in str(view.get(REASON, ""))
            ]
        if original_run:
            views = [view for view in views if str(view.get(ORIGINAL_RUN, "")) == original_run.strip()]
        if interval_no:
            views = [view for view in views if str(view.get(INTERVAL_NO, "")) == interval_no.strip()]
        if task_no:
            views = [view for view in views if str(view.get(TASK_NO, "")) == task_no.strip()]
        if status:
            wanted = status.strip()
            if wanted in {"历史", "历史数据"}:
                views = [view for view in views if view.get(HISTORICAL)]
            else:
                views = [view for view in views if view.get(RERUN_STATUS) == wanted]

        views.sort(key=lambda view: (int(view.get("id", 0)), str(view.get(TASK_NO, ""))))
        total = len(views)
        safe_page = max(int(page or 1), 1)
        safe_size = max(int(size or 20), 1)
        start = (safe_page - 1) * safe_size
        return views[start:start + safe_size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with store.lock:
            self.intervals.ensure_all()
            task = store.find(MODULE, entry_id)
            if task is None:
                return None
            return self._view(task)

    def get_entry_by_task_no(self, task_no: str) -> dict[str, Any] | None:
        with store.lock:
            self.intervals.ensure_all()
            task = self._find_task(task_no)
            return self._view(task) if task is not None else None

    # ---- 登记 --------------------------------------------------------------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        normalized = normalize_values(values)
        original_run = as_text(normalized.get(ORIGINAL_RUN))
        start_value = normalized.get(START)
        end_value = normalized.get(END)

        errors: list[str] = []
        if not original_run:
            errors.append(ORIGINAL_RUN)
        if not as_text(start_value):
            errors.append(START)
        if errors:
            return None, errors

        ok, message, start, end = validate_interval(start_value, end_value)
        if not ok:
            return None, [message]

        with store.lock:
            self.intervals.ensure_all()
            runtime = find_runtime(original_run)
            if runtime is None:
                return None, [f"原始运行编号 {original_run} 在运行实例中不存在，无法关联重跑区间"]

            tasks = store.rows(MODULE)
            task_no = as_text(normalized.get(TASK_NO))
            if task_no:
                if self._find_task(task_no) is not None:
                    return None, [f"任务编号 {task_no} 已存在，请更换"]
            else:
                task_no = self._next_task_no(tasks)

            # 同一区间已有进行中的申请时不再重复建任务；不同区间允许多个申请并存，
            # 由“当前区间”规则决定清单里展示哪一段。
            duplicate = self._active_interval_task(original_run, start, end)
            if duplicate is not None:
                return None, [
                    f"原始运行 {original_run} 的区间 {start} ~ {end} 已有进行中的重跑任务 "
                    f"{as_text(duplicate.get(TASK_NO))}"
                ]

            entry: dict[str, Any] = {"id": self._next_id(tasks)}
            entry[TASK_NO] = task_no
            entry[ORIGINAL_RUN] = original_run
            entry[START] = start
            entry[END] = end
            for field in (REASON, OVERRIDE_POLICY, "依赖级联"):
                if field in normalized and normalized[field] is not None:
                    entry[field] = normalized[field]
            entry[RERUN_STATUS] = DRAFT
            entry[STATUS] = DRAFT
            entry["pending"] = True
            entry["abnormal"] = False
            entry[HISTORICAL] = False
            entry["创建时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            tasks.append(entry)

            record = self.intervals.ensure_for_task(entry, historical=False)
            self.intervals._refresh_record_status(record, entry)
            if as_text(record.get(TASK_NO)) == task_no and not entry.get(TAKEOVER):
                # 只有真正持有该区间记录的新任务才能把它设为当前区间。
                self.intervals.select_current(record)
            return self._view(entry), []

    # ---- 动作 --------------------------------------------------------------
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        canonical = canonical_action(action)
        with store.lock:
            self.intervals.ensure_all()
            task = store.find(MODULE, entry_id)
            if task is None:
                return None, f"重跑任务 {entry_id} 不存在或已归档"
            return self._apply_action(task, canonical)

    def run_action_by_task_no(self, task_no: str, action: str) -> tuple[dict[str, Any] | None, str]:
        canonical = canonical_action(action)
        with store.lock:
            self.intervals.ensure_all()
            task = self._find_task(task_no)
            if task is None:
                return None, f"重跑任务 {task_no} 不存在或已归档"
            return self._apply_action(task, canonical)

    def _apply_action(
        self,
        task: dict[str, Any],
        action: str,
        *,
        allow_running_cancel: bool = True,
    ) -> tuple[dict[str, Any] | None, str]:
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于异常重跑可执行范围"
        if task.get(TAKEOVER):
            return None, f"重跑任务 {as_text(task.get(TASK_NO))} 已被新的重跑任务接管，请在新任务上操作"

        target = ACTION_RULES[action]
        current_status = status_of(task)
        sources = set(ACTION_SOURCES[action])
        if action == "取消重跑" and allow_running_cancel:
            sources.add(RUNNING)
        if current_status not in sources:
            if current_status in TERMINAL_STATUSES:
                return None, (
                    f"重跑任务已是终态「{current_status}」，如需再次执行请新建重跑任务"
                )
            return None, f"当前状态「{current_status}」不允许执行「{action}」"

        original_run = as_text(task.get(ORIGINAL_RUN))
        record = self.intervals.ensure_for_task(task)
        owner_no = as_text(record.get(TASK_NO))
        if owner_no and owner_no != as_text(task.get(TASK_NO)):
            owner_status = status_of(self._find_task(owner_no) or {})
            if owner_status not in TERMINAL_STATUSES:
                return None, f"重跑区间 {as_text(record.get(INTERVAL_NO))} 正被任务 {owner_no} 占用"

        if action == "提交重跑审批":
            active = self._active_task_for_run(original_run, exclude=task)
            if active is not None:
                return None, (
                    f"原始运行 {original_run} 已有进行中的重跑任务 "
                    f"{as_text(active.get(TASK_NO))}，请先取消或完成该任务"
                )
        if action == "启动重跑":
            active = self._active_task_for_run(original_run, exclude=task)
            if active is not None:
                return None, (
                    f"原始运行 {original_run} 已有进行中的重跑任务 "
                    f"{as_text(active.get(TASK_NO))}，不能同时启动两个重跑"
                )

        # 真正的状态提交点：锁内只有一个请求能把状态从当前值推进到目标值。
        task[RERUN_STATUS] = target
        task[STATUS] = target
        task["pending"] = target not in TERMINAL_STATUSES
        task["abnormal"] = target == CANCELLED
        task["更新时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if target in TERMINAL_STATUSES:
            task["结束时间"] = task["更新时间"]
        for other in self._tasks_for_run(original_run):
            if other is not task and status_of(other) in TERMINAL_STATUSES:
                # 新任务生效后，旧的终态任务让出“当前”位置。
                other[CURRENT] = False

        self.intervals._refresh_record_status(record, task)
        if target in TERMINAL_STATUSES:
            # 终态任务不再是当前区间；由仍进行中的任务（或最新任务）接管当前区间。
            self.intervals.refresh_current(original_run)
        else:
            self.intervals.select_current(record)
        return self._view(task), ACTION_MESSAGES.get(action, f"重跑任务已{action}")

    # ---- 区间记录查询 ------------------------------------------------------
    def list_intervals(
        self,
        *,
        original_run: str | None = None,
        keyword: str | None = None,
        current_only: bool = False,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with store.lock:
            self.intervals.ensure_all()
            records = [dict(record) for record in self.intervals.rows]
        if original_run:
            records = [row for row in records if as_text(row.get(ORIGINAL_RUN)) == original_run.strip()]
        if current_only:
            records = [row for row in records if row.get(CURRENT)]
        if keyword:
            needle = keyword.strip()
            records = [
                row
                for row in records
                if needle in as_text(row.get(INTERVAL_NO))
                or needle in as_text(row.get(ORIGINAL_RUN))
                or needle in as_text(row.get(TASK_NO))
            ]
        for row in records:
            runtime = find_runtime(row.get(ORIGINAL_RUN))
            row["作业名称"] = as_text(runtime.get("作业名称")) if runtime else ""
        records.sort(key=lambda row: int(row.get("id", 0)))
        total = len(records)
        safe_page = max(int(page or 1), 1)
        safe_size = max(int(size or 20), 1)
        start = (safe_page - 1) * safe_size
        return records[start:start + safe_size], total

    def get_interval(self, interval_no: str) -> dict[str, Any] | None:
        with store.lock:
            self.intervals.ensure_all()
            record = self.intervals.find_by_no(interval_no)
            if record is None:
                try:
                    record = self.intervals.find_by_id(interval_no)
                except (TypeError, ValueError):
                    record = None
            if record is None:
                return None
            row = dict(record)
            runtime = find_runtime(row.get(ORIGINAL_RUN))
            row["作业名称"] = as_text(runtime.get("作业名称")) if runtime else ""
            return row

    def current_interval(self, original_run: str) -> dict[str, Any] | None:
        with store.lock:
            self.intervals.ensure_all()
            record = self.intervals.current_for_run(original_run)
            return dict(record) if record is not None else None

    # ---- 内部工具 ----------------------------------------------------------
    @staticmethod
    def _next_id(rows: list[dict[str, Any]]) -> int:
        return max((int(row.get("id", 0)) for row in rows), default=0) + 1

    def _next_task_no(self, tasks: list[dict[str, Any]]) -> str:
        max_index = 0
        for task in tasks:
            text = as_text(task.get(TASK_NO))
            suffix = text.rsplit("-", 1)[-1]
            if suffix.isdigit():
                max_index = max(max_index, int(suffix))
        max_index = max(max_index, len(tasks))
        while True:
            max_index += 1
            candidate = f"RERU-{max_index:04d}"
            if self._find_task(candidate) is None:
                return candidate

    def _find_task(self, value: Any) -> dict[str, Any] | None:
        text = as_text(value)
        if not text:
            return None
        tasks = store.rows(MODULE)
        # 业务编号优先：数字结尾的编号不能被误当成数据库 id。
        for task in tasks:
            if as_text(task.get(TASK_NO)) == text:
                return task
        if text.isdigit():
            for task in tasks:
                if int(task.get("id", 0)) == int(text):
                    return task
        return None

    def _tasks_for_run(self, original_run: str) -> list[dict[str, Any]]:
        wanted = as_text(original_run)
        if not wanted:
            return []
        return [task for task in store.rows(MODULE) if as_text(task.get(ORIGINAL_RUN)) == wanted]

    def _active_task_for_run(
        self,
        original_run: str,
        *,
        exclude: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        for task in self._tasks_for_run(original_run):
            if task is exclude or task.get(TAKEOVER):
                continue
            if status_of(task) in {APPROVING, RUNNING}:
                return task
        return None

    def _active_interval_task(
        self,
        original_run: str,
        start: str,
        end: str,
    ) -> dict[str, Any] | None:
        """同一原始运行、同一区间是否已有未终结的重跑申请。"""
        for task in self._tasks_for_run(original_run):
            if task.get(TAKEOVER):
                continue
            if status_of(task) in TERMINAL_STATUSES:
                continue
            if as_text(task.get(START)) == start and as_text(task.get(END)) == end:
                return task
        return None
