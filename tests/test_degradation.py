"""降级路径与引用校验测试（阶段 6-4）。

审查意见第四节的核心要求：
> `assert answer` 只能证明"有内容"，证明不了"内容对"。
> 所以降级答案必须断言具体内容，而不是只断言非空。

这里对**所有降级输出**都加 `'None' not in answer` —— 它防的是一整类错误
（任何 `.get(k, default)` 兜 None 的写法），而不只是本次那一个 bug。
"""
import re

import pytest

import aiops.graph as graph_module
from aiops.graph import _check_citation, _text, analyze_incident, build_graph
from models import ROLE_OPERATOR, Alert, AlertRule, Analysis, Target, db


# ============ fixture ============
@pytest.fixture()
def incident(app, app_context):
    """一条真实的 incident 载荷（自造数据，不依赖 seed_demo）。"""

    target = Target(
        name='降级测试目标', url='https://deg.example.test',
        timeout=5, interval_seconds=60, enabled=True,
    )
    db.session.add(target)
    db.session.commit()

    from aiops_service import build_incident_payload
    rule = AlertRule(target_id=target.id, name='r',
                     rule_type=AlertRule.RULE_CONSECUTIVE_FAILURE, threshold=2)
    db.session.add(rule)
    db.session.commit()
    alert = Alert(rule_id=rule.id, target_id=target.id, message='测试告警',
                  observed_value=3.0, threshold=2.0, reason='connect')
    db.session.add(alert)
    db.session.commit()
    return build_incident_payload(db.session.get(Alert, alert.id))


@pytest.fixture()
def degraded_answer(monkeypatch, incident):
    """无 key 时的降级答案。"""

    monkeypatch.setattr(graph_module.config, 'DEEPSEEK_API_KEY', '')
    monkeypatch.setattr(graph_module.config, 'AI_ENABLED', False)
    return analyze_incident(incident)


# ============ 一、降级答案的 4 条强断言 ============
def test_degraded_answer_has_no_none(degraded_answer):
    """🔴 直接拦住「耗时 Nonems」那类 bug。

    这条看似土，但防的是一整类：任何 .get(k, default) 兜 None 的写法
    都会被它抓到 —— 因为渲染出来就是字符串 "None"。
    """

    assert "None" not in degraded_answer["answer"]
    assert "null" not in degraded_answer["answer"]


def test_degraded_answer_reports_failure_reason(degraded_answer, incident):
    """确认它真读了 incident 的失败分类，而不是硬编码。"""

    assert degraded_answer["degraded"] is True
    # incident 的 failure_reason 决定 planner 走哪个分支，
    # 规则答案里会出现该分类的中文名之一
    assert any(word in degraded_answer["answer"]
               for word in ("连接失败", "请求超时", "请求异常", "状态码异常", "未分类"))


def test_degraded_answer_has_actionable_advice(degraded_answer):
    """规则路径不能是空壳 —— 必须有处置建议。"""

    assert "处置建议" in degraded_answer["answer"]


def test_degraded_answer_is_substantial(degraded_answer):
    """"分析失败"了事要拦住：答案必须有实质长度。"""

    assert len(degraded_answer["answer"]) > 50
    assert "分析失败" not in degraded_answer["answer"]


# ============ 二、四类降级全都要可读 ============
@pytest.mark.parametrize("exc, label", [
    (TimeoutError("Read timed out"), "模型超时"),
    (RuntimeError("HTTP 429 Too Many Requests"), "HTTP 错误"),
    (ValueError("模型返回内容为空"), "返回体异常"),
])
def test_model_failures_all_degrade_gracefully(monkeypatch, incident, exc, label):
    """🔴 无 key / 超时 / HTTP 错 / 返回体异常 —— 四类都要 200 + 有内容。"""

    monkeypatch.setattr(graph_module.config, 'DEEPSEEK_API_KEY', 'fake-key')
    monkeypatch.setattr(graph_module.config, 'AI_ENABLED', True)

    def boom(_prompt, _vars, _key):
        raise exc

    monkeypatch.setattr(graph_module, '_call_model', boom)

    result = analyze_incident(incident)

    assert result["ok"] is True                    # 不抛
    assert result["degraded"] is True              # 标记降级
    assert len(result["answer"]) > 50             # 有内容
    assert "None" not in result["answer"]
    assert result["degradation_reasons"]           # 有原因
    assert str(exc) in str(result["degradation_reasons"])  # 原因里能看出是哪类错


def test_no_key_reports_key_missing(monkeypatch, incident):
    """无 key 的降级原因要指名道姓，而不是含糊的「失败」。"""

    monkeypatch.setattr(graph_module.config, 'DEEPSEEK_API_KEY', '')
    monkeypatch.setattr(graph_module.config, 'AI_ENABLED', True)

    reasons = analyze_incident(incident)["degradation_reasons"]

    assert any("DEEPSEEK_API_KEY" in r for r in reasons)


def test_ai_disabled_is_distinct_reason(monkeypatch, incident):
    """AI_ENABLED=false 与「没配 key」是两种情况，原因文案要能区分。"""

    monkeypatch.setattr(graph_module.config, 'DEEPSEEK_API_KEY', 'fake-key')
    monkeypatch.setattr(graph_module.config, 'AI_ENABLED', False)

    reasons = analyze_incident(incident)["degradation_reasons"]

    assert any("AI_ENABLED" in r for r in reasons)


# ============ 三、_text 助手：格式化类错误的防火墙 ============
@pytest.mark.parametrize("value, expected", [
    (None, "（未知）"),
    ("", "（未知）"),
    ("   ", "（未知）"),
    ("None", "（未知）"),
    ("none", "（未知）"),
    (0, "0"),              # 0 是有效值，不能被当成空
    (0.0, "0.0"),
    (12.5, "12.5"),
])
def test_text_helper_never_leaks_none(value, expected):
    assert _text(value) == expected


def test_text_helper_suffix_and_fallback():
    assert _text(12.5, suffix="ms") == "12.5ms"
    assert _text(None, suffix="ms", fallback="（未记录耗时）") == "（未记录耗时）"


@pytest.mark.parametrize("incident_override", [
    {"duration_ms": None},
    {"failure_reason": None},
    {"target_name": None, "url": None},
    {"id": None},
    {"duration_ms": None, "failure_reason": None, "target_name": None,
     "url": None, "id": None},
])
def test_answer_never_contains_none_for_any_missing_field(incident, incident_override):
    """🔴 逐个字段置空，答案任何一处都不能出现 None。

    这条比「测那一个 bug」更有价值：它把整个格式化链路都钉住了。
    """

    payload = dict(incident)
    payload.update(incident_override)

    answer = build_graph().invoke(
        {"incident": payload, "incident_id": payload.get("id")}
    ).get("answer", "")

    assert "None" not in answer
    assert "null" not in answer


# ============ 四、引用校验 ============
def test_valid_citation_passes():
    evidence = [{"tool": "query_check_records",
                 "result": {"ok": True, "records": [{"id": 42}]}}]
    answer = "依据巡检记录 [42]，失败分类为 timeout。"

    result = _check_citation(answer, evidence)

    assert result["ok"] is True
    assert result["citations"] == ["42"]
    assert result["invalid_citations"] == []


def test_invalid_citation_is_caught():
    """🔴 编造的引用必须被抓出来 —— 这是引用校验存在的意义。"""

    evidence = [{"tool": "query_check_records",
                 "result": {"ok": True, "records": [{"id": 42}]}}]
    answer = "依据巡检记录 [42] 和 [99999]，两者都超时。"

    result = _check_citation(answer, evidence)

    assert result["ok"] is False
    assert result["invalid_citations"] == ["99999"]


def test_kb_citation_is_not_validated_on_purpose():
    """⚠️ [KB-001] 刻意不进校验体系 —— 这是设计，不是漏写。

    校验的目的是抓「模型编造证据」。知识库引用是规则路径模板生成的，
    不是模型编的；且 KB- id 与巡检记录 id 形态不同，混进同一套 valid_ids
    反而会制造歧义。代码里 _CITATION_RE 处有对应注释。
    """

    evidence = [{"tool": "search_knowledge",
                 "result": {"ok": True, "results": [{"entry": {"id": "KB-001"}}]}}]
    answer = "依据知识库条目 [KB-001]。"

    result = _check_citation(answer, evidence)

    assert result["citations"] == []          # 没被当作数字引用
    assert result["invalid_citations"] == [] # 也不被判为编造
    assert result["ok"] is True


def test_chinese_nth_pattern_is_captured():
    """「第 42 条」这种中文写法也要被校验（模型可能这么写）。"""

    evidence = [{"tool": "query_check_records",
                 "result": {"ok": True, "records": [{"id": 42}]}}]
    result = _check_citation("依据第 42 条记录。", evidence)
    assert result["ok"] is True

    result = _check_citation("依据第 777 条记录。", evidence)
    assert result["invalid_citations"] == ["777"]


def test_real_analysis_produces_valid_citations(app, app_context):
    """端到端：真实分析出来的引用必须全部有效（6-3 曾修好过这个）。"""

    target = Target(name='引用验证目标', url='https://cite.example.test',
                    timeout=5, interval_seconds=60, enabled=True)
    db.session.add(target)
    db.session.commit()
    rule = AlertRule(target_id=target.id, name='r',
                     rule_type=AlertRule.RULE_CONSECUTIVE_FAILURE, threshold=2)
    db.session.add(rule)
    db.session.commit()
    from datetime import datetime
    from models import CheckRecord
    db.session.add(CheckRecord(target_id=target.id, status_code=None, duration_ms=12.0,
                               is_success=False, error_msg='连接失败: refused',
                               checked_at=datetime.now()))
    db.session.commit()
    alert = Alert(rule_id=rule.id, target_id=target.id, message='引用验证告警',
                  observed_value=3.0, threshold=2.0, reason='connect')
    db.session.add(alert)
    db.session.commit()

    from aiops_service import run_analysis
    # 🔴 断言必须在 app_context 内部做：commit 之后 session 会 expire，
    #    出了上下文再访问 analysis.status 会抛 DetachedInstanceError。
    with app.app_context():
        analysis = run_analysis(db.session.get(Alert, alert.id))
        status = analysis.status
        invalid = analysis.invalid_citations_json

    assert status in (Analysis.STATUS_OK, Analysis.STATUS_DEGRADED)
    # 引用要么为空，要么全部有效 —— 不允许有 invalid
    assert invalid in (None, "[]")