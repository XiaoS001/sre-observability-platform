"""AI 根因分析服务层：把 graph 调用与落库隔开。

为什么单独一层：路由里直接调 analyze_incident 再落库，测试就没法
mock 掉模型调用（约束：pytest 零 API 消耗）。拆出来之后：
    路由 → service.run_analysis() → graph.analyze_incident()
测试只需 monkeypatch graph.analyze_incident 或 _call_model。

🔴 本模块**保证接口任何情况不 500**：
即使 graph 内部出了意外异常，也会落一条 status=failed 的记录并正常返回，
让调用方知道「分析没成功」而不是收到一个 500。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from aiops.db_access import _classify_failure
from models import Alert, Analysis, CheckRecord, Target, db


logger = logging.getLogger('rmb03.aiops_service')


def _dumps(value: Any) -> str | None:
    """JSON 落库的统一出口。None 与空容器都存 None，省得库里一堆 '[]'。"""

    if value is None:
        return None
    if isinstance(value, (list, dict)) and len(value) == 0:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        # 序列���失败不该让整次分析白跑 —— 退化成 repr，至少信息还在
        return json.dumps(repr(value), ensure_ascii=False)


def build_incident_payload(alert: Alert) -> dict[str, Any]:
    """把 Alert 对象转成 graph 需要的 incident 入参。

    🔴 字段名必须与 graph 的约定对齐，否则 planner / answerer 会读到空值：
        graph.planner_node读 incident["failure_reason"]
        graph.answerer_node    读 incident["failure_reason"] / ["target_name"] / ["url"]
        graph.tool_agent_node  读 incident["target_id"]
        graph 用 incident["id"] 作为证据里的「巡检记录 id」

    ⚠️ 还有一个更隐蔽的对齐问题（实测踩到的）：
    规则降级路径会引用「巡检记录第 [id] 条」，这个 id 必须是**取证工具真能查到的**
    某条记录，否则 citation_check 会把它判成编造的引用（invalid_citations 非空）。
    query_check_records 默认取「最近 10 条」，而告警指向的失败记录可能更早 ——
    所以这里把 check_id 显式传进工具参数，让取证范围一定覆盖它。
    """

    payload: dict[str, Any] = {
        'alert_id': alert.id,
        'target_id': alert.target_id,
        'message': alert.message,
        'threshold': alert.threshold,
        'triggered_at': (
            alert.triggered_at.strftime('%Y-%m-%d %H:%M:%S')
            if alert.triggered_at else None
        ),
    }

    # 取该目标最近的一次失败巡检记录 —— 证据要能对上真实记录
    check = (
        CheckRecord.query
        .filter_by(target_id=alert.target_id, is_success=False)
        .order_by(CheckRecord.checked_at.desc(), CheckRecord.id.desc())
        .first()
    )
    if check is None:
        check = (
            CheckRecord.query
            .filter_by(target_id=alert.target_id)
            .order_by(CheckRecord.checked_at.desc(), CheckRecord.id.desc())
            .first()
        )

    if check is not None:
        payload['id'] = check.id
        payload['check_id'] = check.id      # graph 会用它做取证范围的锚点
        payload['failure_reason'] = (
            _classify_failure(check.status_code, check.error_msg)
            if not check.is_success else 'unknown'
        )
        payload['duration_ms'] = check.duration_ms
        payload['status_code'] = check.status_code
        payload['error_msg'] = check.error_msg
    else:
        # 该目标一条巡检记录都没有（理论上不该发生）：退回告警自身信息，
        # 并让 failure_reason 为空 —— graph 会给出"unknown"而不是编一个分类
        payload['id'] = alert.id
        payload['check_id'] = None
        payload['failure_reason'] = alert.reason or ''
        payload['duration_ms'] = alert.observed_value
        payload['status_code'] = None
        payload['error_msg'] = None

    target = db.session.get(Target, alert.target_id)
    payload['target_name'] = target.name if target else '?'
    payload['url'] = target.url if target else '?'
    return payload


def run_analysis(alert: Alert, graph_runner=None) -> Analysis:
    """对一条告警跑根因分析并落库。永远返回 Analysis，不抛异常。"""

    incident = build_incident_payload(alert)

    if graph_runner is None:
        # 延迟 import：graph 依赖 langchain，测试里若不碰 AI 就不该被它拖慢
        from aiops.graph import analyze_incident as graph_runner

    try:
        result = graph_runner(incident)
    except Exception as exc:
        # graph 自己的 analyze_incident 已经有兜底，走到这里说明兜底也挂了。
        #仍然要留下可查的痕迹，而不是 500。
        logger.exception('analysis_runner_crashed', extra={
            'event': 'analysis_runner_crashed',
            'alert_id': alert.id,
        })
        analysis = Analysis(
            alert_id=alert.id,
            status=Analysis.STATUS_FAILED,
            answer=f'分析过程异常：{type(exc).__name__}: {str(exc)[:200]}',
            degraded=True,
            degradation_reasons_json=_dumps([f'analyze 异常: {exc}']),
        )
        db.session.add(analysis)
        db.session.commit()
        return analysis

    citation_check = result.get('citation_check') or {}
    degraded = bool(result.get('degraded'))
    invalid = citation_check.get('invalid_citations') or []

    # status 三态：ok（模型正常）· degraded（走了降级但仍出答案）· failed（没答案）
    answer = result.get('answer') or ''
    if answer:
        status = Analysis.STATUS_DEGRADED if degraded else Analysis.STATUS_OK
    else:
        status = Analysis.STATUS_FAILED

    analysis = Analysis(
        alert_id=alert.id,
        status=status,
        answer=answer,
        citations_json=_dumps(citation_check.get('citations')),
        invalid_citations_json=_dumps(invalid),
        degraded=degraded,
        degradation_reasons_json=_dumps(result.get('degradation_reasons')),
        tool_calls_json=_dumps(result.get('tool_calls')),
        evidence_count=int(result.get('evidence_count') or 0),
        rounds=int(result.get('rounds') or 0),
    )
    db.session.add(analysis)
    db.session.commit()
    return analysis


def analysis_to_dict(analysis: Analysis) -> dict[str, Any]:
    """Analysis 行 → API 响应（JSON 字段还原成结构化数据）。"""

    def _load(raw):
        if not raw:
            return []
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return []

    return {
        'id': analysis.id,
        'alert_id': analysis.alert_id,
        'status': analysis.status,
        'answer': analysis.answer,
        'citations': _load(analysis.citations_json),
        'invalid_citations': _load(analysis.invalid_citations_json),
        'degraded': analysis.degraded,
        'degradation_reasons': _load(analysis.degradation_reasons_json),
        'tool_calls': _load(analysis.tool_calls_json),
        'evidence_count': analysis.evidence_count,
        'rounds': analysis.rounds,
        'created_at': (
            analysis.created_at.strftime('%Y-%m-%d %H:%M:%S')
            if analysis.created_at else None
        ),
    }