"""重跑区间唯一数据记录：异常重跑与运行实例共同读取的唯一口径。

设计要点
--------

1. **唯一记录**：重跑区间按 ``(原始运行编号, 重跑区间开始, 重跑区间结束)`` 去重。
   同一个原始运行、同一段区间反复登记时复用同一条记录（``重跑区间编号`` 稳定不变），
   不会产生两条互相矛盾的区间数据。
2. **唯一当前**：每个原始运行最多有一条 ``是否当前重跑区间`` 的记录。异常重跑清单、
   任务详情和运行实例清单都从这一条记录投影区间字段与状态，因此三处看到的原始运行、
   区间和状态天然一致。
3. **状态同源**：任务状态与区间记录的 ``重跑状态`` 在同一个锁内一起更新；运行实例
   的 ``执行状态`` / ``重跑状态`` 由这条记录派生。并发启动/取消时只有第一个真正落到
   新状态的请求能改变记录，后到的请求会被状态机拦下。
4. **兼容历史**：历史重跑记录可能没有 ``重跑区间编号``、缺少结束时间、甚至开始时间晚于
   结束时间。读取时按需补建区间记录并做最小修正（结束时间缺失取开始时间、倒挂时把结束
   时间收敛到开始时间），绝不因为脏数据让列表报错，也不会把 ``原始运行编号`` 改写成
   ``任务编号``。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from app.store import store

MODULE = "rerun_interval"
TASK_MODULE = "rerun"
RUNTIME_MODULE = "runtime"

# ---- 重跑任务 / 区间记录字段 -------------------------------------------------
TASK_NO = "任务编号"
ORIGINAL_RUN = "原始运行编号"
INTERVAL_NO = "重跑区间编号"
INTERVAL_ID = "重跑区间ID"
START = "重跑区间开始"
END = "重跑区间结束"
REASON = "重跑原因"
OVERRIDE_POLICY = "覆盖策略"
CASCADE = "依赖级联"
RERUN_STATUS = "重跑状态"
STATUS = "status"
CURRENT = "是否当前重跑区间"
TAKEOVER = "已被新任务接管"
HISTORICAL = "历史数据"
SOURCE = "数据来源"
UPDATED_AT = "更新时间"
SEQUENCE = "序号"

# ---- 状态机 -----------------------------------------------------------------
DRAFT = "草稿"
APPROVING = "重跑审批中"
RUNNING = "执行中"
COMPLETED = "已完成"
CANCELLED = "已取消"

STATUS_ORDER = [DRAFT, APPROVING, RUNNING, COMPLETED, CANCELLED]
STATUS_SET = frozenset(STATUS_ORDER)
ACTIVE_STATUSES = frozenset({APPROVING, RUNNING})
TERMINAL_STATUSES = frozenset({COMPLETED, CANCELLED})

# 动作的入口状态：只能单向推进，终态不可再变。
ACTION_RULES = {
    "提交重跑审批": APPROVING,
    "启动重跑": RUNNING,
    "完成重跑": COMPLETED,
    "取消重跑": CANCELLED,
}
# 状态只能沿 草稿 → 重跑审批中 → 执行中 → 已完成 单向推进，任意非终态可取消。
ACTION_SOURCES = {
    "提交重跑审批": frozenset({DRAFT}),
    "启动重跑": frozenset({APPROVING}),
    "完成重跑": frozenset({RUNNING}),
    "取消重跑": frozenset({DRAFT, APPROVING, RUNNING}),
}
ACTION_ALIASES = {
    "提交审批": "提交重跑审批",
    "重跑审批": "提交重跑审批",
    "审批通过": "启动重跑",
    "启动": "启动重跑",
    "开始重跑": "启动重跑",
    "重跑完成": "完成重跑",
    "标记完成": "完成重跑",
    "完成": "完成重跑",
    "终止重跑": "取消重跑",
    "取消": "取消重跑",
}
ACTION_MESSAGES = {
    "提交重跑审批": "重跑审批已提交",
    "启动重跑": "重跑已启动",
    "完成重跑": "重跑已完成",
    "取消重跑": "重跑已取消",
}

# 运行实例侧的映射：只有真正驱动执行的重跑状态才改写执行状态。
# 草稿/审批中只同步区间与重跑状态，不改写原始运行的执行结果。
RUNTIME_STATUS_BY_RERUN = {
    RUNNING: "运行中",
    COMPLETED: "成功",
    CANCELLED: "已终止",
}

# 运行实例动作 → 重跑动作（跨模块同步时使用）。
RERUN_ACTION_BY_RUNTIME_ACTION = {
    "启动作业": "启动重跑",
    "终止作业": "取消重跑",
    "标记成功": "完成重跑",
}

# 字段别名：兼容其它调用方（或历史表单）可能使用的英文字段名。
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    TASK_NO: (TASK_NO, "重跑任务编号", "task_no", "rerun_no", "rerun_task_no"),
    ORIGINAL_RUN: (
        ORIGINAL_RUN,
        "原运行编号",
        "原始运行",
        "original_run",
        "original_run_no",
        "run_no",
        "runtime_no",
    ),
    START: (START, "区间开始", "start", "interval_start", "rerun_start"),
    END: (END, "区间结束", "end", "interval_end", "rerun_end"),
    REASON: (REASON, "reason"),
    OVERRIDE_POLICY: (OVERRIDE_POLICY, "覆盖方式", "overwrite_policy", "override_policy"),
    CASCADE: (CASCADE, "级联标记", "cascade"),
    RERUN_STATUS: (RERUN_STATUS, "状态", STATUS),
}

# 不允许调用方直接写入的内部字段。
RESERVED_FIELDS = frozenset(
    {
        "id",
        "values",
        "remark",
        "action",
        STATUS,
        RERUN_STATUS,
        CURRENT,
        TAKEOVER,
        HISTORICAL,
        SOURCE,
        UPDATED_AT,
        SEQUENCE,
        INTERVAL_ID,
        INTERVAL_NO,
        "pending",
        "abnormal",
    }
)


def as_text(value: Any) -> str:
    """把任意值收敛成字符串；None 变成空串。"""
    if value is None:
        return ""
    return str(value).strip()


def pick_value(values: dict[str, Any], field: str) -> Any:
    """按别名顺序读取字段，返回第一个非空值。"""
    for alias in FIELD_ALIASES.get(field, (field,)):
        if alias in values and values[alias] is not None:
            value = values[alias]
            if isinstance(value, str) and not value.strip():
                continue
            return value
    return None


def normalize_values(values: dict[str, Any]) -> dict[str, Any]:
    """把别名统一成中文字段名，并保留额外业务字段。"""
    normalized: dict[str, Any] = {}
    for field in FIELD_ALIASES:
        value = pick_value(values, field)
        if value is not None:
            normalized[field] = value
    for key, value in values.items():
        if key in RESERVED_FIELDS or value is None:
            continue
        if key in normalized:
            continue
        # 别名键已经被规范字段吸收，不再重复保留。
        if any(key in aliases for aliases in FIELD_ALIASES.values()):
            continue
        normalized[key] = value
    return normalized


def canonical_action(action: Any) -> str:
    """把动作别名收敛成标准动作名。"""
    text = as_text(action)
    return ACTION_ALIASES.get(text, text)


def parse_datetime(value: Any, *, is_end: bool = False) -> datetime | None:
    """宽松解析日期时间；解析失败返回 None，交给调用方决定是否兼容。"""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = as_text(value).replace("T", " ").replace("/", "-")
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = None
        for fmt in (
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%Y%m%d",
            "%Y%m%d%H%M%S",
        ):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                return None
    # 统一去掉时区，避免带时区和不带时区的值比较时报错。
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    # 只有日期没有时间的结束边界，按当天结束处理，避免同一天区间被误判为倒挂。
    raw = as_text(value)
    if is_end and len(raw) == 10 and raw.count("-") == 2:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


def validate_interval(start_value: Any, end_value: Any) -> tuple[bool, str, str, str]:
    """校验新登记的重跑区间，返回 (是否合法, 说明, 开始, 结束)。"""
    start = as_text(start_value)
    end = as_text(end_value)
    if not start:
        return False, "缺少必填字段：重跑区间开始", start, end
    if not end:
        # 兼容只登记开始时间的旧入口：结束时间取开始时间。
        end = start
    start_at = parse_datetime(start)
    end_at = parse_datetime(end, is_end=True)
    if start_at is None:
        return False, "重跑区间开始格式不正确，请使用 YYYY-MM-DD HH:MM:SS", start, end
    if end_at is None:
        return False, "重跑区间结束格式不正确，请使用 YYYY-MM-DD HH:MM:SS", start, end
    if start_at > end_at:
        return False, "重跑区间开始不能晚于重跑区间结束", start, end
    return True, "", start, end


def sanitize_history_interval(start_value: Any, end_value: Any) -> tuple[str, str]:
    """历史数据的最小修正：不抛错，只把明显矛盾的区间收敛掉。"""
    start = as_text(start_value) or "未登记"
    end = as_text(end_value) or start
    start_at = parse_datetime(start)
    end_at = parse_datetime(end, is_end=True)
    if start_at is not None and end_at is not None and start_at > end_at:
        end = start
    return start, end


def status_of(row: dict[str, Any]) -> str:
    """读取记录状态；``重跑状态`` 优先，其次兼容 ``status``。"""
    value = as_text(row.get(RERUN_STATUS)) or as_text(row.get(STATUS))
    return value or DRAFT


def is_current_record(record: dict[str, Any] | None) -> bool:
    return bool(record and record.get(CURRENT))


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _next_sequence(rows: Iterable[dict[str, Any]]) -> int:
    return max((int(row.get(SEQUENCE, 0) or 0) for row in rows), default=0) + 1


def _next_business_no(rows: list[dict[str, Any]], field: str, prefix: str) -> str:
    """生成 ``RERU-0005`` / ``RRIN-0005`` 形式的业务编号。"""
    max_index = 0
    for row in rows:
        text = as_text(row.get(field))
        if not text:
            continue
        suffix = text.rsplit("-", 1)[-1]
        if suffix.isdigit():
            max_index = max(max_index, int(suffix))
    max_index = max(max_index, len(rows))
    while True:
        max_index += 1
        candidate = f"{prefix}-{max_index:04d}"
        if not any(as_text(row.get(field)) == candidate for row in rows):
            return candidate


def find_runtime(record_no: Any) -> dict[str, Any] | None:
    wanted = as_text(record_no)
    if not wanted:
        return None
    for row in store.rows(RUNTIME_MODULE):
        if as_text(row.get("记录编号")) == wanted:
            return row
    return None


def find_task(value: Any) -> dict[str, Any] | None:
    """按任务编号或数据库 id 查找重跑任务。"""
    text = as_text(value)
    if not text:
        return None
    rows = store.rows(TASK_MODULE)
    # 业务编号优先，避免数字结尾的编号被误当成数据库 id。
    for row in rows:
        if as_text(row.get(TASK_NO)) == text:
            return row
    if text.isdigit():
        for row in rows:
            if int(row.get("id", 0)) == int(text):
                return row
    return None


class RerunIntervalRepository:
    """重跑区间唯一记录的读写入口。所有方法都应在 ``store.lock`` 内调用。"""

    @property
    def rows(self) -> list[dict[str, Any]]:
        return store.rows(MODULE)

    # ---- 查询 ---------------------------------------------------------------
    def find_by_no(self, interval_no: Any) -> dict[str, Any] | None:
        wanted = as_text(interval_no)
        if not wanted:
            return None
        for row in self.rows:
            if as_text(row.get(INTERVAL_NO)) == wanted:
                return row
        return None

    def find_by_id(self, interval_id: Any) -> dict[str, Any] | None:
        try:
            wanted = int(interval_id)
        except (TypeError, ValueError):
            return None
        for row in self.rows:
            if int(row.get("id", 0)) == wanted:
                return row
        return None

    def find_by_key(self, original_run: str, start: str, end: str) -> dict[str, Any] | None:
        for row in self.rows:
            if (
                as_text(row.get(ORIGINAL_RUN)) == original_run
                and as_text(row.get(START)) == start
                and as_text(row.get(END)) == end
            ):
                return row
        return None

    def find_unbound(
        self,
        original_run: str,
        start: str,
        end: str,
        *,
        exclude_task: str = "",
    ) -> dict[str, Any] | None:
        """查找同一区间的无主记录（历史补建后未挂任务，或任务已被接管）。

        这样同一原始运行、同一区间只会有一份数据，重新登记时复用旧记录。
        """
        for row in self.rows:
            if as_text(row.get(ORIGINAL_RUN)) != original_run:
                continue
            if as_text(row.get(START)) != start or as_text(row.get(END)) != end:
                continue
            owner_no = as_text(row.get(TASK_NO))
            if owner_no and owner_no == exclude_task:
                continue
            owner = find_task(owner_no) if owner_no else None
            if owner is None or owner.get(TAKEOVER) or owner.get(HISTORICAL):
                return row
        return None

    def for_run(self, original_run: Any) -> list[dict[str, Any]]:
        wanted = as_text(original_run)
        if not wanted:
            return []
        return [row for row in self.rows if as_text(row.get(ORIGINAL_RUN)) == wanted]

    def for_task(self, task: dict[str, Any]) -> dict[str, Any] | None:
        interval_no = as_text(task.get(INTERVAL_NO))
        if interval_no:
            record = self.find_by_no(interval_no)
            if record is not None:
                return record
        task_no = as_text(task.get(TASK_NO))
        for row in self.rows:
            if task_no and as_text(row.get(TASK_NO)) == task_no:
                return row
        return None

    def current_for_run(self, original_run: Any) -> dict[str, Any] | None:
        candidates = self.for_run(original_run)
        if not candidates:
            return None
        current = [row for row in candidates if row.get(CURRENT)]
        if current:
            return max(current, key=lambda row: int(row.get(SEQUENCE, 0) or 0))
        # 历史数据没有当前标记时，按最近创建的一条兜底。
        return max(candidates, key=lambda row: int(row.get(SEQUENCE, 0) or 0))

    # ---- 当前区间与跨模块同步 ----------------------------------------------
    def select_current(self, record: dict[str, Any]) -> None:
        original_run = as_text(record.get(ORIGINAL_RUN))
        changed = False
        for row in self.for_run(original_run):
            wanted = row is record
            if bool(row.get(CURRENT)) != wanted:
                row[CURRENT] = wanted
                changed = True
        record[CURRENT] = True
        if changed:
            record[UPDATED_AT] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.sync_runtime(record)

    def sync_runtime(self, record: dict[str, Any]) -> None:
        """把唯一区间记录的原始运行、区间和状态同步到运行实例清单。

        运行实例的 ``执行状态`` 只在重跑真正进入执行/终结时被覆盖；草稿与审批中的重跑
        区间不改写原始运行结果，必要时回落到运行实例自己的基线状态。
        """
        runtime = find_runtime(record.get(ORIGINAL_RUN))
        if runtime is None:
            return
        # 第一次同步前先把运行实例自己的执行结果留作基线，之后只在重跑生效期间覆盖。
        runtime.setdefault("原始执行状态", as_text(runtime.get("执行状态")))
        status = status_of(record)
        runtime[ORIGINAL_RUN] = as_text(record.get(ORIGINAL_RUN))
        runtime[INTERVAL_NO] = as_text(record.get(INTERVAL_NO))
        runtime[INTERVAL_ID] = record.get("id")
        runtime[TASK_NO] = as_text(record.get(TASK_NO))
        runtime[START] = as_text(record.get(START))
        runtime[END] = as_text(record.get(END))
        runtime[RERUN_STATUS] = status
        runtime[CURRENT] = True
        runtime_status = RUNTIME_STATUS_BY_RERUN.get(status)
        if runtime_status is None:
            runtime_status = as_text(runtime.get("原始执行状态")) or as_text(runtime.get("执行状态")) or "草稿"
        runtime["执行状态"] = runtime_status
        runtime[STATUS] = runtime_status
        runtime["pending"] = runtime_status == "运行中"
        runtime["abnormal"] = runtime_status == "已终止"
        if runtime_status == "成功":
            runtime["错误信息"] = ""

    # ---- 建记录 / 补历史 ---------------------------------------------------
    def _new_record(
        self,
        *,
        original_run: str,
        start: str,
        end: str,
        task: dict[str, Any],
        historical: bool = False,
        source: str = "重跑任务登记",
    ) -> dict[str, Any]:
        rows = self.rows
        record = {
            "id": _next_id(rows),
            INTERVAL_NO: _next_business_no(rows, INTERVAL_NO, "RRIN"),
            ORIGINAL_RUN: original_run,
            START: start,
            END: end,
            TASK_NO: as_text(task.get(TASK_NO)),
            RERUN_STATUS: status_of(task),
            STATUS: status_of(task),
            CURRENT: False,
            HISTORICAL: historical,
            SOURCE: source,
            SEQUENCE: _next_sequence(rows),
            UPDATED_AT: datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "pending": status_of(task) not in TERMINAL_STATUSES,
            "abnormal": status_of(task) == CANCELLED,
        }
        rows.append(record)
        return record

    def ensure_for_task(
        self,
        task: dict[str, Any],
        *,
        historical: bool | None = None,
    ) -> dict[str, Any]:
        """确保任务有且只有一条唯一区间记录，并把任务与记录互相关联。

        * 同一 ``(原始运行编号, 开始, 结束)`` 只复用一条记录。
        * 记录已被旧任务占用且旧任务是终态时，交给新任务承接（保留区间编号）。
        * 旧任务仍在进行中时，记录不会被抢占；由调用方决定是否拒绝新任务。
        """
        original_run = as_text(task.get(ORIGINAL_RUN))
        start, end = sanitize_history_interval(task.get(START), task.get(END))
        task[START] = start
        task[END] = end
        if historical is None:
            historical = bool(task.get(HISTORICAL))
        task[HISTORICAL] = historical

        record = self.find_by_key(original_run, start, end)
        if record is not None and as_text(record.get(TASK_NO)) == as_text(task.get(TASK_NO)):
            task[INTERVAL_NO] = record[INTERVAL_NO]
            task[INTERVAL_ID] = record["id"]
            self._refresh_record_status(record, task)
            return record

        if record is not None:
            owner_no = as_text(record.get(TASK_NO))
            owner = find_task(owner_no) if owner_no else None
            if owner is not None and owner is not task and not owner.get(TAKEOVER):
                if status_of(owner) not in TERMINAL_STATUSES:
                    # 同一区间的未终结任务独占记录：新任务只做只读关联，不改记录归属。
                    task[INTERVAL_NO] = record[INTERVAL_NO]
                    task[INTERVAL_ID] = record["id"]
                    task[CURRENT] = False
                    task[HISTORICAL] = True
                    return record
            # 记录已终结/被接管/未挂任务：交给当前任务承接同一条记录。
            task[INTERVAL_NO] = record[INTERVAL_NO]
            task[INTERVAL_ID] = record["id"]
            if owner is not None and owner is not task:
                owner[TAKEOVER] = True
                owner[CURRENT] = False
            record[TASK_NO] = as_text(task.get(TASK_NO))
            record[HISTORICAL] = bool(historical)
            record[SOURCE] = "重跑任务重新登记" if owner is not None else "历史数据补建"
            self._refresh_record_status(record, task)
            return record

        record = self._new_record(
            original_run=original_run,
            start=start,
            end=end,
            task=task,
            historical=historical,
            source="历史数据补建" if historical else "重跑任务登记",
        )
        task[INTERVAL_NO] = record[INTERVAL_NO]
        task[INTERVAL_ID] = record["id"]
        return record

    def _refresh_record_status(self, record: dict[str, Any], task: dict[str, Any]) -> None:
        status = status_of(task)
        record[RERUN_STATUS] = status
        record[STATUS] = status
        record["pending"] = status not in TERMINAL_STATUSES
        record["abnormal"] = status == CANCELLED
        record[TASK_NO] = as_text(task.get(TASK_NO))
        record[HISTORICAL] = bool(task.get(HISTORICAL, record.get(HISTORICAL, False)))

    def refresh_current(self, original_run: Any) -> dict[str, Any] | None:
        """为某个原始运行选出唯一的当前区间记录。

        选择顺序是确定的：进行中的任务优先；没有进行中的任务时以最近一次重跑申请为准
        （包括已取消/已完成的任务，它们仍是该原始运行的最后一段重跑区间）。旧任务只有
        在确实有更新的任务时才会让出“当前”位置，不会被更早的草稿“复活”。
        """
        wanted = as_text(original_run)
        if not wanted:
            return None
        tasks = [
            task
            for task in store.rows(TASK_MODULE)
            if as_text(task.get(ORIGINAL_RUN)) == wanted and not task.get(TAKEOVER)
        ]
        if not tasks:
            return None

        def order(task: dict[str, Any]) -> tuple[int, int, str, int]:
            return (
                1 if status_of(task) in ACTIVE_STATUSES else 0,
                0 if task.get(HISTORICAL) else 1,
                as_text(task.get("更新时间") or task.get("创建时间") or ""),
                int(task.get("id", 0)),
            )

        winner = max(tasks, key=order)
        for task in tasks:
            task[CURRENT] = task is winner
        record = self.for_task(winner)
        if record is None:
            return None
        self.select_current(record)
        return record

    def ensure_all(self) -> None:
        """为所有历史/示例重跑任务补齐唯一区间记录；幂等。"""
        tasks = store.rows(TASK_MODULE)
        runs: list[str] = []
        for task in tasks:
            if not as_text(task.get(TASK_NO)):
                task[TASK_NO] = _next_business_no(tasks, TASK_NO, "RERU")
            remapped = False
            if not as_text(task.get(ORIGINAL_RUN)):
                # 早期历史数据把任务编号填进了原始运行编号；这里只做容错关联，
                # 不改写原值，能对上运行实例时才补一份纯数字后缀映射。
                legacy = as_text(task.get(TASK_NO))
                match = find_runtime(legacy)
                if match is None:
                    suffix = legacy.rsplit("-", 1)[-1]
                    if suffix:
                        match = find_runtime(f"RUNT-{suffix}")
                if match is not None:
                    task[ORIGINAL_RUN] = as_text(match.get("记录编号"))
                    remapped = True
            else:
                # 历史脏数据：原始运行编号被写成了任务编号，但编号本身能对上运行实例。
                if (
                    as_text(task.get(ORIGINAL_RUN)) == as_text(task.get(TASK_NO))
                    and find_runtime(task.get(ORIGINAL_RUN)) is None
                ):
                    remapped = True

            run = as_text(task.get(ORIGINAL_RUN))
            if remapped:
                suffix = as_text(task.get(TASK_NO)).rsplit("-", 1)[-1]
                match = find_runtime(f"RUNT-{suffix}") if suffix else None
                if match is not None:
                    task[ORIGINAL_RUN] = as_text(match.get("记录编号"))
                    run = as_text(task.get(ORIGINAL_RUN))

            linked = find_runtime(run) is not None
            had_link = bool(as_text(task.get(INTERVAL_NO)))
            if not linked or not as_text(task.get(START)):
                # 无法关联到运行实例、或没有区间起点的历史任务：只做承接，不参与当前区间。
                task[HISTORICAL] = True
                task[CURRENT] = False
                if not as_text(task.get(START)):
                    continue
            elif not had_link:
                # 首次读取时还没有区间记录的任务属于既存历史数据，补建时打上标记。
                task[HISTORICAL] = True

            record = self.ensure_for_task(task, historical=bool(task.get(HISTORICAL)))
            if not linked:
                task[HISTORICAL] = True
                record[HISTORICAL] = True
                record[CURRENT] = False
                continue
            if run and run not in runs:
                runs.append(run)
        for run in runs:
            self.refresh_current(run)


# 单例：两个模块共用同一份区间数据。
rerun_intervals = RerunIntervalRepository()
