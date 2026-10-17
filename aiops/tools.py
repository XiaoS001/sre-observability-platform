"""RMB03 取证工具：白名单 + 参数校验 + 结构化错误。

移植自 XM05 aiops/tools.py（复制不引用，原文件只读未改）。**5 个工具的函数体全部重写**，
因为 XM05 那边 SQL 硬编码了 `check_result` / `monitor_target`，RMB03 是
`check_record` / `target`，照抄必然跑不通。

三条硬约束（由代码强制，不靠自觉）：

1. 🔴 **只读**：所有查询走 db_access.open_ro()（sqlite `mode=ro`），
   引擎层拒绝写。不用 SQLAlchemy session —— session 天然带写能力。
2. 🔴 **不抛异常**：参数非法、库打不开、表结构异常，一律转成
   `{"ok": False, "error": ..., "code": ...}`。任何异常冒到 Flask 都是 500，
   而约束是「接口任何情况不 500」。
3. 🔴 **不编数字**：工具只直查库，不让模型自己算。链路数据没有就如实说没有。

第 6 个工具 `query_alert_context` 是 RMB03 新增的：XM05 没有告警表，
这里能让模型知道「这条告警的规则阈值是多少」，避免它凭空猜「为什么触发」。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

from aiops.db_access import (
    CONNECT_PREFIX,
    EXCEPTION_PREFIX,
    TIMEOUT_PREFIX,
    DbUnavailable,
    _classify_failure,
    open_ro,
)
from knowledge.retriever import search as kb_search


FAILURE_REASONS = ("timeout", "connect", "exception", "bad_status")
MAX_LIMIT = 500          # 单次查询上限，防止模型要一条结果时拉全表
MIN_CHECK_ID = 1


# ============ 结构化错误 ============
def err_bad_param(msg: str) -> dict[str, Any]:
    return {"ok": False, "error": msg, "code": "bad_param"}


def err_unknown_tool(name: str) -> dict[str, Any]:
    return {"ok": False, "error": f"未注册的工具: {name}", "code": "unknown_tool"}


def err_db(msg: str) -> dict[str, Any]:
    """🔴 库不可用/查询失败 —— 这是最关键的一类：不能抛，只能返回。"""

    return {"ok": False, "error": msg, "code": "db_error"}


# ============ 参数校验小工具 ============
def _check_limit(limit: Any) -> dict[str, Any] | None:
    """校验 limit。bool 是 int 的子类，这里显式排除（True 不该当 1 用）。"""
    if isinstance(limit, bool) or not isinstance(limit, int):
        return err_bad_param(f"limit 必须为整数，收到: {limit!r}")
    if limit < 0:
        return err_bad_param(f"limit 必须为非负整数，收到: {limit!r}")
    if limit > MAX_LIMIT:
        return err_bad_param(f"limit 上限为 {MAX_LIMIT}，收到: {limit!r}")
    return None


def _check_positive_int(value: Any, name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return err_bad_param(f"{name} 必须为正整数，收到: {value!r}")
    if value < MIN_CHECK_ID:
        return err_bad_param(f"{name} 必须为正整数，收到: {value!r}")
    return None


# ============ 工具 1：query_check_records ============
def query_check_records(target_id: int | None = None, limit: int = 20) -> dict[str, Any]:
    """查巡检记录：哪次、什么错、耗时多少。保留失败四分类。"""

    bad = _check_positive_int(target_id, "target_id") or _check_limit(limit)
    if bad is not None:
        return bad

    sql = (
        "SELECT cr.id, cr.target_id, t.name, t.url, cr.status_code, "
        "cr.duration_ms, cr.is_success, cr.error_msg, cr.checked_at "
        "FROM check_record cr JOIN target t ON t.id = cr.target_id WHERE 1=1"
    )
    params: list[Any] = []
    if target_id is not None:
        sql += " AND cr.target_id = ?"
        params.append(target_id)
    sql += " ORDER BY cr.id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    try:
        with open_ro() as conn:
            rows = conn.execute(sql, params).fetchall()
    except DbUnavailable as exc:
        return err_db(str(exc))

    records = []
    for rid, tid, name, url, sc, dur, succ, emsg, checked_at in rows:
        records.append({
            "id": rid, "target_id": tid, "target_name": name, "url": url,
            "status_code": sc, "duration_ms": dur, "is_success": bool(succ),
            "error_msg": emsg, "checked_at": str(checked_at),
            "failure_reason": _classify_failure(sc, emsg) if not succ else "",
        })
    return {"ok": True, "count": len(records), "records": records}


# ============ 工具 2：query_metrics ============
def query_metrics(target_id: int | None = None) -> dict[str, Any]:
    """查指标：可用率 / P95 / 失败四分类计数。

    🔴 从库里直接聚合，**不查 Prometheus** —— 与平台自身的
    `reports.py` 保持同一口径，避免两处算法给出不同的数。
    """

    bad = _check_positive_int(target_id, "target_id")
    if bad is not None:
        return bad

    # 🔴 三条查询共用同一套 WHERE 片段。已固定写法：每条 SQL 自带一个
    #    已有的 WHERE 条件，追加的片段以 AND 开头 —— 这样拼接顺序不会出错。
    #    （之前用 where.replace() 拼出过 "WHERE is_success=0 WHERE target_id=?"，
    #     双WHERE 直接语法错误。）
    scope_and = ""
    params: list[Any] = []
    if target_id is not None:
        scope_and = " AND target_id = ?"
        params.append(target_id)

    try:
        with open_ro() as conn:
            total, succ = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(is_success),0) FROM check_record WHERE 1=1"
                + scope_and,
                params,
            ).fetchone()
            fail_rows = conn.execute(
                "SELECT status_code, error_msg FROM check_record WHERE is_success=0"
                + scope_and,
                params,
            ).fetchall()
            durations = [r[0] for r in conn.execute(
                "SELECT duration_ms FROM check_record WHERE is_success=1"
                + scope_and,
                params,
            ).fetchall()]
    except DbUnavailable as exc:
        return err_db(str(exc))

    p95 = None
    if durations:
        sd = sorted(durations)
        p95 = round(sd[max(int(0.95 * len(sd)) - 1, 0)], 1)
    reasons = Counter(_classify_failure(sc, emsg) for sc, emsg in fail_rows)
    return {
        "ok": True,
        "scope": "target:%s" % target_id if target_id else "all",
        "total_checks": total, "success_count": succ, "failure_count": total - succ,
        "success_rate": round(succ / total * 100, 1) if total else 0.0,
        "p95_duration_ms": p95, "failure_reasons": dict(reasons),
    }


# ============ 工具 3：query_logs ============
def query_logs(reason: str | None = None, target_id: int | None = None,
               limit: int = 20) -> dict[str, Any]:
    """按失败分类检索日志摘要。

    RMB03 没有独立的日志表 —— 这里的「日志」就是**结构化巡检记录里的
    error_msg 字段**（平台把结构化 JSON 日志推到 Loki，但库里的权威来源是
    check_record）。如实标注，不假装有日志系统。
    """

    if reason is not None and reason not in FAILURE_REASONS:
        return err_bad_param(
            f"reason 必须是 {'/'.join(FAILURE_REASONS)}，收到: {reason!r}"
        )
    bad = _check_positive_int(target_id, "target_id") or _check_limit(limit)
    if bad is not None:
        return bad

    sql = (
        "SELECT cr.id, t.name, cr.status_code, cr.error_msg, cr.duration_ms, cr.checked_at "
        "FROM check_record cr JOIN target t ON t.id = cr.target_id WHERE cr.is_success = 0"
    )
    params: list[Any] = []
    if target_id is not None:
        sql += " AND cr.target_id = ?"
        params.append(target_id)

    # 🔴 reason 过滤必须在 LIMIT **之前**下推到 SQL。
    #    之前是「先 LIMIT 再 Python 侧过滤」—— 那样会漏：
    #    limit=10 时若前 10 条里有 8 条是 exception，查 timeout 会返回空，
    #    而库里明明有 7 条 timeout。这是个会误导根因判断的静默错误。
    if reason is not None:
        # 按分类对应的 error_msg 前缀过滤（bad_status 例外：它没有专属前缀）
        if reason == "timeout":
            sql += " AND cr.error_msg LIKE ?"
            params.append(f"{TIMEOUT_PREFIX}%")
        elif reason == "connect":
            sql += " AND cr.error_msg LIKE ?"
            params.append(f"{CONNECT_PREFIX}%")
        elif reason == "exception":
            sql += " AND cr.error_msg LIKE ?"
            params.append(f"{EXCEPTION_PREFIX}%")

    sql += " ORDER BY cr.id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    try:
        with open_ro() as conn:
            rows = conn.execute(sql, params).fetchall()
    except DbUnavailable as exc:
        return err_db(str(exc))

    logs = []
    for rid, name, sc, emsg, dur, checked_at in rows:
        rsn = _classify_failure(sc, emsg)
        # bad_status 是「有状态码但不属于前三类」，SQL 侧表达不了，留在Python 侧兜
        if reason is not None and rsn != reason:
            continue
        logs.append({
            "check_id": rid, "target_name": name, "status_code": sc,
            "error_msg": emsg, "duration_ms": dur, "checked_at": str(checked_at),
            "reason": rsn,
        })
    return {
        "ok": True, "count": len(logs), "logs": logs,
        "data_source": "check_record.error_msg（结构化巡检日志；Loki 为可选外部通道）",
    }


# ============ 工具 4：query_traces ============
def query_traces(check_id: int | None = None) -> dict[str, Any]:
    """查链路。

    🔴 RMB03 **没有存链路数据**（Jaeger 是运行时可选组件，链路不进库）。
    所以这里如实返回 `has_trace_data: False`，只附带该次巡检的耗时近似，
    不假装有trace —— 让模型知道「没有这条信息」比给它一个假的强。
    """

    bad = _check_positive_int(check_id, "check_id")
    if bad is not None:
        return bad

    result: dict[str, Any] = {
        "ok": True, "has_trace_data": False,
        "message": "链路数据未入库（Jaeger 为运行时可选组件，span 不落库），"
                   "仅能提供单次巡检的总耗时作为近似。",
    }

    try:
        with open_ro() as conn:
            if check_id is not None:
                rows = conn.execute(
                    "SELECT t.name, cr.duration_ms FROM check_record cr "
                    "JOIN target t ON t.id = cr.target_id WHERE cr.id = ?",
                    (check_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT t.name, cr.duration_ms FROM check_record cr "
                    "JOIN target t ON t.id = cr.target_id ORDER BY cr.id DESC LIMIT 20"
                ).fetchall()
    except DbUnavailable as exc:
        return err_db(str(exc))

    result["duration_approx_ms"] = [
        {"target_name": n, "duration_ms": d} for n, d in rows
    ]
    return result


# ============ 工具 5：search_knowledge ============
def search_knowledge(query: str, top_k: int = 3) -> dict[str, Any]:
    """RAG 检索运维知识库（jieba + BM25）。"""

    if not isinstance(query, str) or not query.strip():
        return err_bad_param("query 必须是非空字符串")
    bad = _check_positive_int(top_k, "top_k")
    if bad is not None:
        return bad
    if top_k > 20:
        return err_bad_param(f"top_k 上限为 20，收到: {top_k!r}")

    try:
        results = kb_search(query, top_k=top_k)
    except Exception as exc:  # 检索库损坏也不该把接口带崩
        return {"ok": False, "error": f"知识库检索失败: {exc}", "code": "kb_error"}
    return {"ok": True, "count": len(results), "results": results}


# ============ 工具 6：query_alert_context（RMB03 新增）============
def query_alert_context(alert_id: int | None = None,
                        check_id: int | None = None) -> dict[str, Any]:
    """查告警上下文：这条告警的规则类型、阈值、目标，以及它指向的那次巡检。

    XM05 没有 alert 表，所以没有这个工具。加它的原因很实际：
    模型看到「可用率 88%」时不知道这是触发了 `availability(95)`
    还是 `consecutive_failure(3)` —— 阈值不同，根因判断完全不同。
    """

    bad = _check_positive_int(alert_id, "alert_id") or _check_positive_int(check_id, "check_id")
    if bad is not None:
        return bad
    if alert_id is None and check_id is None:
        return err_bad_param("alert_id 与 check_id 至少要给一个")

    try:
        with open_ro() as conn:
            alert = None
            if alert_id is not None:
                row = conn.execute(
                    "SELECT a.id, a.rule_id, a.target_id, a.message, a.observed_value, "
                    "a.threshold, a.reason, a.triggered_at, a.resolved_at, "
                    "r.rule_type, r.name AS rule_name, t.name AS target_name "
                    "FROM alert a "
                    "LEFT JOIN alert_rule r ON r.id = a.rule_id "
                    "LEFT JOIN target t ON t.id = a.target_id "
                    "WHERE a.id = ?",
                    (alert_id,),
                ).fetchone()
                if row is not None:
                    alert = {
                        "id": row[0], "rule_id": row[1], "target_id": row[2],
                        "message": row[3], "observed_value": row[4],
                        "threshold": row[5], "reason": row[6],
                        "triggered_at": str(row[7]),
                        "resolved_at": str(row[8]) if row[8] else None,
                        "rule_type": row[9], "rule_name": row[10],
                        "target_name": row[11],
                    }

            check = None
            if check_id is not None:
                crow = conn.execute(
                    "SELECT cr.id, t.name, cr.status_code, cr.duration_ms, "
                    "cr.is_success, cr.error_msg, cr.checked_at "
                    "FROM check_record cr JOIN target t ON t.id = cr.target_id "
                    "WHERE cr.id = ?",
                    (check_id,),
                ).fetchone()
                if crow is not None:
                    check = {
                        "check_id": crow[0], "target_name": crow[1],
                        "status_code": crow[2], "duration_ms": crow[3],
                        "is_success": bool(crow[4]), "error_msg": crow[5],
                        "checked_at": str(crow[6]),
                        "failure_reason": (
                            _classify_failure(crow[2], crow[5]) if not crow[4] else ""
                        ),
                    }
    except DbUnavailable as exc:
        return err_db(str(exc))

    return {"ok": True, "alert": alert, "check": check}


# ============ 白名单注册表 ============
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    run: Callable[..., dict[str, Any]]


_TOOLS: dict[str, Tool] = {}


def register(name: str, description: str, func: Callable[..., dict[str, Any]]) -> None:
    if name in _TOOLS:
        raise ValueError(f"工具已注册: {name}")
    _TOOLS[name] = Tool(name=name, description=description, run=func)


def list_tools() -> list[str]:
    return sorted(_TOOLS)


def describe_tools() -> str:
    return "\n".join(f"{t.name} - {t.description}" for t in _TOOLS.values())


def call_tool(name: str, **kwargs: Any) -> dict[str, Any]:
    """白名单调用。🔴 兜底三件事：工具不存在 / 参数不合法 / 内部异常。

    这里是最外层的最后一道防线 —— 即使某个工具内部漏了 try/except，
    异常也会在这里被转成 db_error，而不是冒到 Flask 变成 500。
    """

    if name not in _TOOLS:
        return err_unknown_tool(name)
    try:
        return _TOOLS[name].run(**kwargs)
    except DbUnavailable as exc:
        return err_db(str(exc))
    except Exception as exc:
        return {
            "ok": False,
            "error": f"工具执行异常: {type(exc).__name__}: {exc}",
            "code": "tool_error",
        }


register("query_check_records", "查巡检记录：哪次、什么错、耗时多少", query_check_records)
register("query_metrics", "查指标：可用率 / P95 / 失败分类计数", query_metrics)
register("query_logs", "按失败分类检索日志摘要（结构化巡检日志）", query_logs)
register("query_traces", "查链路（未入库时如实返回 has_trace_data=false）", query_traces)
register("search_knowledge", "RAG 检索运维知识库", search_knowledge)
register("query_alert_context", "查告警的规则类型与阈值（为什么触发）", query_alert_context)