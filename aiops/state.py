"""XM05 AgentState 定义（参考 XM03，AIOps 场景改造）。"""
from __future__ import annotations

from typing import Any, Literal, TypedDict


class AgentState(TypedDict, total=False):
    # 输入
    incident_id: int            # 巡检记录 id（触发分析的异常）
    incident: dict[str, Any]    # 异常详情（来自 query_check_records 的一条）
    # 编排
    plan: str                   # planner 拆解的排查计划
    evidence: list[dict[str, Any]]  # 工具取证结果汇总
    tool_calls: list[str]       # 已调用工具名列表
    verdict: Literal["够", "不够", ""]
    answer: str                 # 根因 + 处置建议
    # 引用校验
    citations: list[str]
    invalid_citations: list[str]
    citation_check: dict[str, Any]
    citation_retry_count: int
    # 双上限
    rounds: int                 # supervisor 轮数
    attempts: int               # 检索/取证重试次数
    # 降级
    degraded: bool
    degradation_reasons: list[str]
    # 路由
    next_agent: str
    last_agent: str
    route_history: list[str]
