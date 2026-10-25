"""定期整理：隔一段时间、且攒够了新记忆时，自动让模型复盘一个作用域。

判断顺序（都过了才调用模型）：
    1. 同一作用域至多每 SCAN_INTERVAL 秒检查一次，检查本身只看本地文件；
    2. 距上次整理不足设定的小时数，跳过；
    3. 上次整理之后新增或修改的记忆不足 MIN_CHANGES 条，跳过——没什么新东西，
       复盘一遍也只会得出"不用改"；
    4. 这个作用域正在整理（管理页手动整理或另一轮定期整理），跳过。
模型认为不需要整理就什么都不改，这一次也算整理过，计时重新开始。调用失败
不算，过 FAIL_BACKOFF 后再试。

有方案时按设置处理：「整理前问我」把方案留着，等管理员在管理页确认；
「直接整理」立即应用。每次应用（含管理页的手动整理）都记下改动前的原文，
最近 KEEP_RUNS 次可以撤销。

磁盘布局（插件数据目录下 tidy/）：
    settings.json          管理页是否已经问过要不要开启
    <作用域>.json           上次整理的时间与结论、待确认的方案
    <作用域>.runs.json      最近几次应用的改动记录（含改动前原文，供撤销）
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from . import consolidate
from .memstore import (
    CONFLICT_OVERWRITE,
    CONFLICT_SKIP,
    ImportEntry,
    MemoryHeader,
    ScopeStore,
    Usage,
    is_person_file,
)

MODE_OFF = "off"
MODE_CONFIRM = "confirm"
MODE_AUTO = "auto"
MODES = (MODE_OFF, MODE_CONFIRM, MODE_AUTO)

DEFAULT_HOURS = 24
MIN_HOURS = 1
MAX_HOURS = 24 * 30
SCAN_INTERVAL = 600
MIN_CHANGES = 3
FAIL_BACKOFF = 3600
KEEP_RUNS = 5

TRIGGER_MANUAL = "manual"
TRIGGER_AUTO = "auto"
TRIGGER_CONFIRMED = "confirmed"

OUTCOME_NOTHING = "nothing"
OUTCOME_PENDING = "pending"
OUTCOME_APPLIED = "applied"
OUTCOME_FAILED = "failed"

PEOPLE_ONLY = "人物记忆只收 user-<数字ID>.md"
# 撤销时判断"之后有没有再改过"的写入时间误差
MTIME_TOLERANCE = consolidate.MTIME_TOLERANCE


class TidyError(Exception):
    """可以直接展示给管理员的错误。"""


def scope_id(scope: str, key: str | None) -> str:
    return "global" if scope == "global" else f"{scope}-{key or ''}"


def scope_label(scope: str, key: str | None, record=None) -> str:
    """整理提示词里的作用域名称。"""
    if scope == "global":
        return "机器人自我记忆（全局）"
    if scope == "people":
        return f"人物记忆（平台 {key}，一人一个文件）"
    if record is not None and (record.remark or record.name):
        return record.remark or record.name
    return key or ""


def clamp_hours(raw: object) -> int:
    try:
        hours = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_HOURS
    return min(max(hours, MIN_HOURS), MAX_HOURS)


def normalize_mode(raw: object) -> str:
    mode = str(raw or "").strip().lower()
    return mode if mode in MODES else MODE_OFF


# ---------------------------------------------------------------- 记录


@dataclass
class TidyMeta:
    """一个作用域的定期整理记录。"""

    last_run: float = 0.0
    last_attempt: float = 0.0
    failures: int = 0
    outcome: str = ""
    message: str = ""
    # 待确认的方案：{"createdAt", "operations", "skipped", "files"}
    pending: dict | None = None

    @classmethod
    def from_raw(cls, raw: object) -> "TidyMeta":
        if not isinstance(raw, dict):
            return cls()
        pending = raw.get("pending")
        meta = cls(
            outcome=str(raw.get("outcome") or ""),
            message=str(raw.get("message") or ""),
            pending=pending if isinstance(pending, dict) else None,
        )
        for name, key in (("last_run", "lastRun"), ("last_attempt", "lastAttempt")):
            try:
                setattr(meta, name, float(raw.get(key) or 0.0))
            except (TypeError, ValueError):
                pass
        try:
            meta.failures = max(0, int(raw.get("failures") or 0))
        except (TypeError, ValueError):
            pass
        return meta

    def to_raw(self) -> dict:
        raw: dict = {
            "lastRun": round(self.last_run, 3),
            "lastAttempt": round(self.last_attempt, 3),
            "failures": self.failures,
            "outcome": self.outcome,
            "message": self.message,
        }
        if self.pending:
            raw["pending"] = self.pending
        return raw

    def pending_count(self) -> int:
        ops = (self.pending or {}).get("operations")
        return len(ops) if isinstance(ops, list) else 0


@dataclass
class Proposal:
    """一次复盘的结果：空的 operations 表示模型认为不需要整理。"""

    operations: list[consolidate.Operation]
    skipped: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)


class TidyBook:
    """tidy/ 目录的读写（同步方法，调用方放进线程执行）与进行中标记。"""

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self._lock = threading.Lock()
        # 正在整理的作用域：管理页与定期整理共用，同一处不会同时跑两份
        self.busy: set[str] = set()

    # ---- 进行中标记（只在事件循环里用）

    def claim(self, sid: str) -> bool:
        if sid in self.busy:
            return False
        self.busy.add(sid)
        return True

    def release(self, sid: str) -> None:
        self.busy.discard(sid)

    # ---- 文件

    def _path(self, sid: str, suffix: str = ".json") -> Path:
        return self.base_dir / f"{sid}{suffix}"

    def _read(self, path: Path):
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None

    def _write(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tidy-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def settings(self) -> dict:
        data = self._read(self.base_dir / "settings.json")
        return data if isinstance(data, dict) else {}

    def mark_asked(self, now: float | None = None) -> None:
        with self._lock:
            data = self.settings()
            data["asked"] = True
            data["askedAt"] = round(time.time() if now is None else now, 3)
            self._write(self.base_dir / "settings.json", data)

    def meta(self, sid: str) -> TidyMeta:
        return TidyMeta.from_raw(self._read(self._path(sid)))

    def save_meta(self, sid: str, meta: TidyMeta) -> None:
        with self._lock:
            self._write(self._path(sid), meta.to_raw())

    def update_meta(self, sid: str, change: Callable[[TidyMeta], None]) -> TidyMeta:
        with self._lock:
            meta = self.meta(sid)
            change(meta)
            self._write(self._path(sid), meta.to_raw())
            return meta

    def all_meta(self) -> dict[str, TidyMeta]:
        if not self.base_dir.is_dir():
            return {}
        result = {}
        for path in self.base_dir.glob("*.json"):
            name = path.name
            if name == "settings.json" or name.endswith(".runs.json"):
                continue
            result[name[: -len(".json")]] = TidyMeta.from_raw(self._read(path))
        return result

    def runs(self, sid: str) -> list[dict]:
        data = self._read(self._path(sid, ".runs.json"))
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    def add_run(self, sid: str, run: dict) -> None:
        with self._lock:
            runs = [run] + self.runs(sid)
            self._write(self._path(sid, ".runs.json"), runs[:KEEP_RUNS])

    def mark_undone(self, sid: str, run_id: str, now: float) -> None:
        with self._lock:
            runs = self.runs(sid)
            for run in runs:
                if run.get("id") == run_id:
                    run["undoneAt"] = round(now, 3)
            self._write(self._path(sid, ".runs.json"), runs)

    def remove(self, sid: str) -> None:
        with self._lock:
            for suffix in (".json", ".runs.json"):
                path = self._path(sid, suffix)
                if path.is_file():
                    path.unlink()


# ---------------------------------------------------------------- 判断


def changed_since(headers: list[MemoryHeader], since: float) -> int:
    """上次整理之后新增或修改过的记忆条数。"""
    return sum(1 for h in headers if h.updated > since)


def due_reason(
    meta: TidyMeta, headers: list[MemoryHeader], now: float, hours: int
) -> str:
    """该不该整理：返回空串表示该整理了，否则是跳过的原因。"""
    if meta.pending:
        return "有待确认的方案"
    if meta.failures and now - meta.last_attempt < FAIL_BACKOFF:
        return "上次失败，稍后再试"
    if meta.last_run and now - meta.last_run < hours * 3600:
        return "未到间隔"
    if changed_since(headers, meta.last_run) < MIN_CHANGES:
        return "新增的记忆不多，无需整理"
    return ""


# ---------------------------------------------------------------- 复盘与应用


async def propose(
    store: ScopeStore,
    label: str,
    call_model: Callable[[str], Awaitable[tuple[str | None, list[str]]]],
) -> Proposal:
    """把作用域的全部记忆交给模型复盘；失败抛 TidyError。"""
    headers = await asyncio.to_thread(store.headers)
    if not headers:
        raise TidyError("这里还没有记忆，无需整理")
    files = []
    for header in headers:
        content = await store.read(header.file)
        if content:
            files.append((header, content))
    prompt, skipped = consolidate.build_prompt(label, files)
    text, attempts = await call_model(prompt)
    if not text:
        raise TidyError(
            "整理模型调用失败：" + ("；".join(attempts) or "没有可用的模型提供商")
        )
    existing = {h.file for h in headers}
    ops = consolidate.parse_operations(text, existing)
    if ops is None:
        raise TidyError("模型返回的整理方案无法解析，请重试")
    # 记下复盘时各目标文件的写入时间，应用时据此识别期间被改过的文件
    for op in ops:
        op.base_mtime = store.mtime(op.file) or 0.0
    return Proposal(operations=ops, skipped=skipped, files=sorted(existing))


async def apply(
    store: ScopeStore, ops: list[consolidate.Operation], people: bool = False
) -> list[dict]:
    """先写后删地应用方案；每项结果带改动前原文（before）供撤销。"""
    results = []
    write_failed = False
    for op in consolidate.ordered(ops):
        before = await store.read(op.file)
        # 删除会连使用统计一起清掉，撤销时要原样还回去
        usage = store.usage_of(op.file).to_raw() if op.op == "delete" else None
        if consolidate.is_stale(op, store.mtime(op.file)):
            ok, message = False, "复盘之后这个文件被改过，已跳过；请重新整理"
        elif op.op == "write" and people and not is_person_file(op.file):
            ok, message = False, PEOPLE_ONLY
        elif op.op == "write":
            report = await store.write(op.file, op.render())
            ok, message = report.ok, report.message
        elif write_failed:
            # 合并目标没写成就不能删源文件，否则内容就丢了
            ok, message = False, "有写入未完成，为避免丢内容已跳过删除"
        else:
            report = await store.delete(op.file)
            ok, message = report.ok, report.message
        if op.op == "write" and not ok:
            write_failed = True
        item = {
            "op": op.op,
            "file": op.file,
            "reason": op.reason,
            "ok": ok,
            "message": message,
        }
        if ok:
            item["before"] = before
            item["afterMtime"] = store.mtime(op.file) or 0.0
            if usage:
                item["usage"] = usage
        results.append(item)
    return results


def make_run(trigger: str, results: list[dict], now: float) -> dict | None:
    """把一次应用记成可撤销的记录；一项都没成功就不记。"""
    done = [r for r in results if r.get("ok")]
    if not done:
        return None
    return {
        "id": f"{int(now * 1000)}",
        "at": round(now, 3),
        "trigger": trigger,
        "ops": [
            {
                "op": r["op"],
                "file": r["file"],
                "reason": r.get("reason", ""),
                "before": r.get("before"),
                "afterMtime": r.get("afterMtime", 0.0),
                **({"usage": r["usage"]} if r.get("usage") else {}),
            }
            for r in done
        ],
        "undoneAt": 0,
    }


def public_results(results: list[dict]) -> list[dict]:
    """给页面的结果：去掉改动前原文。"""
    return [
        {k: v for k, v in r.items() if k not in ("before", "afterMtime", "usage")}
        for r in results
    ]


def public_run(run: dict) -> dict:
    return {
        "id": run.get("id", ""),
        "at": run.get("at", 0),
        "trigger": run.get("trigger", ""),
        "undoneAt": run.get("undoneAt", 0),
        "ops": [
            {
                "op": op.get("op", ""),
                "file": op.get("file", ""),
                "reason": op.get("reason", ""),
                "created": op.get("op") == "write" and op.get("before") is None,
            }
            for op in run.get("ops") or []
            if isinstance(op, dict)
        ],
    }


async def undo(store: ScopeStore, run: dict) -> list[dict]:
    """撤销一次整理：被删的文件写回、被改写的恢复原文、新建的删掉。

    之后又被改过的文件不动（以免盖掉新内容），逐项报告。先恢复被删的
    源文件、再回退写入，与应用时的先写后删相反。
    """
    ops = [op for op in run.get("ops") or [] if isinstance(op, dict)]
    ordered = [op for op in ops if op.get("op") == "delete"] + [
        op for op in ops if op.get("op") == "write"
    ]
    results = []
    for op in ordered:
        file = str(op.get("file") or "")
        before = op.get("before")
        current = store.mtime(file)
        if op.get("op") == "delete":
            if current is not None or store.has_forgotten(file):
                ok, message = False, "同名文件已经存在，没有写回"
            elif not isinstance(before, str):
                ok, message = False, "没有留下原文，无法写回"
            else:
                entry = ImportEntry(file, before, usage=Usage.from_raw(op.get("usage")))
                outcome = await store.import_files([entry], CONFLICT_SKIP)
                ok = bool(outcome) and outcome[0].landed
                message = "已写回" if ok else (outcome[0].reason if outcome else "写回失败")
        else:
            after = float(op.get("afterMtime") or 0.0)
            if current is None or abs(current - after) > MTIME_TOLERANCE:
                ok, message = False, "整理之后这个文件又被改过或删掉，没有回退"
            elif before is None:
                report = await store.delete(file)
                ok, message = report.ok, "已删除整理时新建的文件" if report.ok else report.message
            else:
                outcome = await store.import_files(
                    [ImportEntry(file, str(before))], CONFLICT_OVERWRITE
                )
                ok = bool(outcome) and outcome[0].landed
                message = "已恢复原文" if ok else (outcome[0].reason if outcome else "恢复失败")
        results.append({"op": op.get("op", ""), "file": file, "ok": ok, "message": message})
    return results
