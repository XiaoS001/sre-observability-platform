"""RMB03 数据访问：只读打开 RMB03 自己的业务库。

移植自 XM05 aiops/db_access.py（复制不引用，原文件只读未改），两处改动：

1. DB_PATH 指向 data/rmb03.db（XM05 指向的是 XM01 的副本 xm01_copy.db）
2. 所有 SQL 的表名换成 RMB03 的 schema：
   check_result → check_record · monitor_target → target

🔴 只读是**由 sqlite 连接强制**的，不靠自觉：
   `file:...?mode=ro` 让 SQLite 在引擎层拒绝任何写操作。
   这是「AI 工具只能读库」这条约束的实现方式 —— 不用 SQLAlchemy session，
   因为 session 天然带写能力，约束就该落在连接层而不是纪律上。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import config

DATA_DIR = Path(config.DATA_DIR)
DB_PATH = DATA_DIR / "rmb03.db"

# 失败四分类：与 RMB03 metrics._classify_failure 同源。
# ⚠️ 这里保留一份副本而不是 import metrics —— 原因是 aiops 要能独立 import
#    （测试里会单独测工具，不该连带拉起 SQLAlchemy + Flask 那一套）。
#    代价是两处逻辑必须同步，tests/test_tools.py 里有对拍测试锁住一致性。
TIMEOUT_PREFIX = "请求超时:"
CONNECT_PREFIX = "连接失败:"
EXCEPTION_PREFIX = "请求异常:"


def _classify_failure(status_code: int | None, error_msg: str | None) -> str:
    """失败四分类：timeout / connect / exception / bad_status。"""
    if error_msg is not None and error_msg.startswith(TIMEOUT_PREFIX):
        return "timeout"
    if error_msg is not None and error_msg.startswith(CONNECT_PREFIX):
        return "connect"
    if error_msg is not None and error_msg.startswith(EXCEPTION_PREFIX):
        return "exception"
    if status_code is not None:
        return "bad_status"
    return "exception"


class DbUnavailable(RuntimeError):
    """库打不开/查询失败。

    🔴 单独一个异常类型，是为了让 tools 层能把它**转成结构化错误对象**
    而不是让它冒到 Flask 变成 500（约束：接口任何情况不 500）。
    """


@contextmanager
def open_ro() -> Iterator[sqlite3.Connection]:
    """只读打开业务库（uri mode=ro 防写）。

    失败时抛 DbUnavailable —— 调用方负责转成错误对象。
    """

    try:
        conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise DbUnavailable(f"无法只读打开 {DB_PATH.name}: {exc}") from exc
    try:
        yield conn
    except sqlite3.Error as exc:
        raise DbUnavailable(f"查询失败: {exc}") from exc
    finally:
        conn.close()


@dataclass(frozen=True)
class CheckRecord:
    """一条巡检记录（从业务库读出）。"""

    id: int
    target_id: int
    target_name: str
    url: str
    status_code: int | None
    duration_ms: float
    is_success: bool
    error_msg: str | None
    checked_at: str
    failure_reason: str  # 失败分类；成功则为 ""


def list_failures(limit: int = 50) -> list[CheckRecord]:
    """列出巡检失败记录（带失败分类）。"""
    with open_ro() as conn:
        rows = conn.execute(
            """
            SELECT cr.id, cr.target_id, t.name, t.url,
                   cr.status_code, cr.duration_ms, cr.is_success,
                   cr.error_msg, cr.checked_at
            FROM check_record cr
            JOIN target t ON t.id = cr.target_id
            WHERE cr.is_success = 0
            ORDER BY cr.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    records = []
    for rid, tid, name, url, sc, dur, succ, emsg, checked_at in rows:
        reason = _classify_failure(sc, emsg) if not succ else ""
        records.append(CheckRecord(
            id=rid, target_id=tid, target_name=name, url=url,
            status_code=sc, duration_ms=dur, is_success=bool(succ),
            error_msg=emsg, checked_at=str(checked_at), failure_reason=reason,
        ))
    return records


def summary() -> dict[str, Any]:
    """巡检总览：总数 / 成功 / 失败 / 成功率 / 失败分类计数。"""
    with open_ro() as conn:
        total, succ = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(is_success),0) FROM check_record"
        ).fetchone()
        rows = conn.execute(
            "SELECT status_code, error_msg FROM check_record WHERE is_success=0"
        ).fetchall()

    from collections import Counter

    reasons = Counter(_classify_failure(sc, emsg) for sc, emsg in rows)
    return {
        "total": total,
        "success": succ,
        "fail": total - succ,
        "success_rate": round(succ / total * 100, 1) if total else 0.0,
        "failure_reasons": dict(reasons),
    }


def list_targets() -> list[dict[str, Any]]:
    """列出监控目标。"""
    with open_ro() as conn:
        rows = conn.execute(
            "SELECT id, name, url, timeout, interval_seconds, enabled FROM target"
        ).fetchall()
    return [
        {"id": r[0], "name": r[1], "url": r[2],
         "timeout": r[3], "interval_seconds": r[4], "enabled": r[5]}
        for r in rows
    ]