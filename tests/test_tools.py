"""AI 取证工具测试（阶段 6-2 的工具 + 6-3 补齐）。

按审查意见要求的四组：
  · 6 个工具各一条正例
  · 参数校验反例
  · db_error（monkeypatch DB_PATH，**不真删库**）
  · unknown_tool

🔴 测试不碰 data/rmb03.db —— 用tmp 库里造的最小数据，
   这样测试结果不受演示数据变化影响。
"""
import json

import pytest

from aiops import db_access, tools
from aiops.tools import call_tool


# ============ fixture：只读的最小测试库 ============
@pytest.fixture()
def sample_db(tmp_path, monkeypatch):
    """造一个只含必要表的库，DB_PATH 指过去。

    用 monkeypatch 改 DB_PATH，不动真实 data/rmb03.db。
    """

    import sqlite3

    path = tmp_path / "sample.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE target (
            id INTEGER PRIMARY KEY, name TEXT, url TEXT,
            timeout INTEGER, interval_seconds INTEGER, enabled INTEGER
        );
        CREATE TABLE check_record (
            id INTEGER PRIMARY KEY, target_id INTEGER, status_code INTEGER,
            duration_ms REAL, is_success INTEGER, error_msg TEXT, checked_at TEXT
        );
        CREATE TABLE alert_rule (
            id INTEGER PRIMARY KEY, target_id INTEGER, name TEXT,
            rule_type TEXT, threshold REAL, enabled INTEGER, created_at TEXT
        );
        CREATE TABLE alert (
            id INTEGER PRIMARY KEY, rule_id INTEGER, target_id INTEGER,
            message TEXT, observed_value REAL, threshold REAL, reason TEXT,
            triggered_at TEXT, resolved_at TEXT
        );
        INSERT INTO target VALUES
            (1, '测试目标', 'https://example.test', 5, 60, 1),
            (2, '故障目标', 'https://fault.example.test', 5, 60, 1);
        -- 成功记录
        INSERT INTO check_record VALUES
            (100, 1, 200, 50.0, 1, NULL, '2026-10-16 10:00:00'),
            (101, 1, 200, 60.0, 1, NULL, '2026-10-16 10:01:00');
        -- 四类失败各一条（构造时故意打乱顺序，验证「连续/分类」不被顺序影响）
        INSERT INTO check_record VALUES
            (102, 2, NULL, 30.0, 0, '请求超时: read timeout', '2026-10-16 10:02:00'),
            (103, 2, NULL, 8.0, 0, '连接失败: refused', '2026-10-16 10:03:00'),
            (104, 2, 500, 15.0, 0, '请求异常: boom', '2026-10-16 10:04:00'),
            (105, 2, 503, 20.0, 0, NULL, '2026-10-16 10:05:00');
        INSERT INTO alert_rule VALUES
            (1, 2, '可用率告警', 'availability', 95.0, 1, '2026-10-16 09:00:00');
        INSERT INTO alert VALUES
            (1, 1, 2, '[故障目标] 可用率告警', 88.2, 95.0, 'timeout',
             '2026-10-16 10:06:00', NULL);
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db_access, "DB_PATH", path)
    return path


# ============ 第一组：6 个工具正例 ============
def test_query_check_records_returns_records(sample_db):
    r = tools.query_check_records(limit=10)

    assert r["ok"] is True
    assert r["count"] == 6
    assert set(r["records"][0]) >= {"id", "target_name", "failure_reason", "duration_ms"}


def test_query_check_records_preserves_failure_classification(sample_db):
    """🔴 四分类必须原样保留 —— 这是平台的诊断基础。"""

    r = tools.query_check_records(target_id=2, limit=10)
    reasons = {rec["id"]: rec["failure_reason"] for rec in r["records"]}

    assert reasons[102] == "timeout"
    assert reasons[103] == "connect"
    assert reasons[104] == "exception"
    assert reasons[105] == "bad_status"


def test_query_metrics_aggregates(sample_db):
    r = tools.query_metrics()

    assert r["ok"] is True
    assert r["total_checks"] == 6
    assert r["success_count"] == 2
    assert r["failure_count"] == 4
    assert r["success_rate"] == 33.3
    assert r["p95_duration_ms"] is not None
    assert r["failure_reasons"] == {
        "timeout": 1, "connect": 1, "exception": 1, "bad_status": 1,
    }


def test_query_metrics_scope_isolation(sample_db):
    """target_id 过滤必须真的生效，不能串到全局。"""

    all_r = tools.query_metrics()
    one = tools.query_metrics(target_id=1)

    assert all_r["total_checks"] == 6
    assert one["total_checks"] == 2
    assert one["scope"] == "target:1"


def test_query_logs_filters_by_reason(sample_db):
    r = tools.query_logs(reason="timeout")

    assert r["ok"] is True
    assert r["count"] == 1
    assert r["logs"][0]["check_id"] == 102


@pytest.mark.parametrize("reason, expected_count", [
    ("timeout", 1), ("connect", 1), ("exception", 1), ("bad_status", 1),
])
def test_query_logs_each_reason(sample_db, reason, expected_count):
    """🔴 每一类都要能查出来 —— 「过滤在 LIMIT 之后」的 bug 就是这么漏掉的。"""

    assert tools.query_logs(reason=reason)["count"] == expected_count


def test_query_traces_honestly_reports_no_data(sample_db):
    """🔴 没有链路数据就必须说没有，不许编。"""

    r = tools.query_traces(check_id=102)

    assert r["ok"] is True
    assert r["has_trace_data"] is False
    assert "未入库" in r["message"]


def test_search_knowledge_returns_hits():
    r = tools.search_knowledge("连接失败", top_k=3)

    assert r["ok"] is True
    assert r["count"] >= 1
    assert r["results"][0]["entry"]["id"].startswith("KB-")


def test_query_alert_context_returns_rule_and_check(sample_db):
    """告警上下文要同时给出规则类型与被指向的巡检记录。"""

    r = tools.query_alert_context(alert_id=1, check_id=102)

    assert r["ok"] is True
    assert r["alert"]["rule_type"] == "availability"
    assert r["alert"]["threshold"] == 95.0
    assert r["check"]["check_id"] == 102
    assert r["check"]["failure_reason"] == "timeout"


# ============ 第二组：参数校验反例 ============
def test_bad_param_negative_limit(sample_db):
    assert tools.query_check_records(limit=-1)["code"] == "bad_param"


def test_bad_param_limit_over_max(sample_db):
    assert tools.query_check_records(limit=tools.MAX_LIMIT + 1)["code"] == "bad_param"


def test_bad_param_zero_target_id(sample_db):
    assert tools.query_check_records(target_id=0)["code"] == "bad_param"


def test_bad_param_invalid_reason(sample_db):
    assert tools.query_logs(reason="disk_full")["code"] == "bad_param"


def test_bad_param_empty_query():
    assert tools.search_knowledge("   ")["code"] == "bad_param"


def test_bad_param_alert_context_requires_one_arg(sample_db):
    assert tools.query_alert_context()["code"] == "bad_param"


# ============ 第三组：db_error（monkeypatch，不删真库）============
@pytest.mark.parametrize("name, kwargs", [
    ("query_check_records", {}),
    ("query_metrics", {}),
    ("query_logs", {}),
    ("query_traces", {}),
    ("query_alert_context", {"alert_id": 1}),
])
def test_db_error_when_database_unavailable(monkeypatch, tmp_path, name, kwargs):
    """🔴 库打不开时必须返回 db_error，**绝不抛异常**。

    这条锁的是「接口任何情况不 500」—— 异常冒到 Flask 就是 500。
    """

    monkeypatch.setattr(db_access, "DB_PATH", tmp_path / "does-not-exist.db")

    result = call_tool(name, **kwargs)   # 若抛异常，这���测试就 ERROR

    assert result["ok"] is False
    assert result["code"] == "db_error"


def test_search_knowledge_survives_missing_db(monkeypatch, tmp_path):
    """知识库不依赖 sqlite —— DB 挂了它也该能用。"""

    monkeypatch.setattr(db_access, "DB_PATH", tmp_path / "nope.db")

    assert tools.search_knowledge("超时")["ok"] is True


# ============ 第四组：unknown_tool ============
def test_unknown_tool_rejected():
    r = call_tool("drop_all_tables")

    assert r["ok"] is False
    assert r["code"] == "unknown_tool"


# ============ 白名单完整性 ============
def test_exactly_six_tools_registered():
    """工具数量与名称是契约 —— planner 按名字调，少一个就断链。"""

    assert tools.list_tools() == [
        "query_alert_context", "query_check_records", "query_logs",
        "query_metrics", "query_traces", "search_knowledge",
    ]


def test_call_tool_wraps_unexpected_exception(monkeypatch):
    """即使工具内部漏了 try/except，最外层也要兜住。"""

    def boom(**kwargs):
        raise RuntimeError("工具内部炸了")

    monkeypatch.setitem(tools._TOOLS, "query_metrics",
                        tools.Tool(name="query_metrics", description="x", run=boom))

    r = call_tool("query_metrics")

    assert r["ok"] is False
    assert r["code"] == "tool_error"
    assert "炸了" in r["error"]


# ============ 只读强制（约束 1）============
@pytest.mark.parametrize("sql", [
    "DELETE FROM check_record WHERE id = 100",
    "INSERT INTO target (name,url,timeout,interval_seconds,enabled) VALUES ('x','y',5,60,1)",
    "UPDATE target SET name='hacked'",
])
def test_database_connection_is_read_only(sample_db, sql):
    """🔴 mode=ro 由 SQLite 引擎层强制 —— 工具拿到的连接物理上写不进去。"""

    import sqlite3

    with pytest.raises(db_access.DbUnavailable):
        with db_access.open_ro() as conn:
            conn.execute(sql)

    # 再确认数据没被改
    with db_access.open_ro() as conn:
        count = conn.execute("SELECT COUNT(*) FROM target").fetchone()[0]
        hacked = conn.execute(
            "SELECT COUNT(*) FROM target WHERE name='hacked'"
        ).fetchone()[0]
    assert count == 2
    assert hacked == 0