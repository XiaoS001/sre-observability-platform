"""「悬空标题」测试 —— 阶段 6-5。

背景：审查用眼睛看降级答案时发现「可能原因：」冒号后面是空的，
知识库里明明有 3 条 causes 却没被打印（只取了 actions）。

这类 bug 的特征是：**标题写了，内容没跟着**。
所以断言不能只查"有没有这个标题"，而要查"标题后面有没有实质内容"。
"""
import pytest

import aiops.graph as graph_module
from aiops.graph import analyze_incident, build_graph
from models import Alert, AlertRule, CheckRecord, Target, db


# ============ 工具：判断一个标题行是否悬空 ============
def dangling_headings(text):
    """找出「以冒号结尾但下一行是空/另一个标题」的行。

    这是本文件的核心 —— 把「悬空」这个视觉问题变成可断言的属性。
    """

    lines = text.splitlines()
    bad = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.endswith("：") and not stripped.endswith(":"):
            continue
        # 标题必须缩进（列表项）或顶格；列表项以"  - "开头不算标题
        if stripped.startswith("- "):
            continue
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        # 下一行是空的、或也是标题、或直接结束 → 悬空
        if not nxt or (nxt.endswith("：") and not nxt.startswith("- ")):
            bad.append((i + 1, stripped))
    return bad


@pytest.fixture()
def answer(app, app_context):
    """一条完整的降级答案（无 key）。"""

    target = Target(name='悬空测试目标', url='https://dangle.example.test',
                    timeout=5, interval_seconds=60, enabled=True)
    db.session.add(target)
    db.session.commit()
    from datetime import datetime
    db.session.add(CheckRecord(target_id=target.id, status_code=None, duration_ms=25.0,
                               is_success=False, error_msg='连接失败: refused',
                               checked_at=datetime.now()))
    db.session.commit()
    rule = AlertRule(target_id=target.id, name='r',
                     rule_type=AlertRule.RULE_CONSECUTIVE_FAILURE, threshold=2)
    db.session.add(rule)
    db.session.commit()
    alert = Alert(rule_id=rule.id, target_id=target.id, message='悬空测试告警',
                  observed_value=3.0, threshold=2.0, reason='connect')
    db.session.add(alert)
    db.session.commit()

    from aiops_service import build_incident_payload
    incident = build_incident_payload(db.session.get(Alert, alert.id))
    return build_graph().invoke(
        {"incident": incident, "incident_id": incident.get("id")}
    ).get("answer", "")


# ============ 1. 🔴 核心：没有悬空标题 ============
def test_no_dangling_heading(answer):
    """🔴 「可能原因：」后面必须有内容。

    这条直接锁住审查发现的那个 bug：标题写了，causes 没打印。
    """

    bad = dangling_headings(answer)

    assert bad == [], (
        f"发现悬空标题（后面没有内容）：{bad}\n--- 全文 ---\n{answer}"
    )


def test_possible_causes_section_is_populated(answer):
    """更直接的断言：KB 的可能原因必须真的进到答案里。"""

    assert "可能原因：" in answer
    section = answer.split("可能原因：", 1)[1]
    # 标题到下一个非列表行为止，至少要有 1 条以"  - "开头的条目
    items = [l for l in section.splitlines()[1:] if l.strip().startswith("- ")]
    assert len(items) >= 1, f"「可能原因」下面是空的：{section!r}"


def test_actions_section_is_populated(answer):
    """处置建议同理——标题后面不能空。"""

    assert "处置建议：" in answer
    section = answer.split("处置建议：", 1)[1]
    items = [l for l in section.splitlines() if l.strip().startswith("- ")]
    assert len(items) >= 1


def test_kb_causes_and_actions_are_both_present(answer):
    """🔴 回归测试：causes 与 actions 是知识库的两个不同字段，必须都取。"""

    # 「查服务端错误日志」只出现在 actions；「抛异常未捕获」类表述来自 causes
    assert "处置建议：" in answer
    assert "可能原因：" in answer
    assert len(answer) > 150, "答案太短，可能原因或处置建议被丢了"


# ============ 2. 边界：KB 条目没有 causes 时怎么办 ============
def test_heading_not_dangling_when_kb_has_no_causes(monkeypatch, app_context):
    """KB 条目缺 causes 时，不能写一个悬空的「可能原因：」标题。"""

    target = Target(name='无原因目标', url='https://nc.example.test',
                    timeout=5, interval_seconds=60, enabled=True)
    db.session.add(target)
    db.session.commit()
    from datetime import datetime
    db.session.add(CheckRecord(target_id=target.id, status_code=None, duration_ms=8.0,
                               is_success=False, error_msg='连接失败: x',
                               checked_at=datetime.now()))
    db.session.commit()
    rule = AlertRule(target_id=target.id, name='r',
                     rule_type=AlertRule.RULE_CONSECUTIVE_FAILURE, threshold=2)
    db.session.add(rule)
    db.session.commit()
    alert = Alert(rule_id=rule.id, target_id=target.id, message='无原因告警',
                  observed_value=3.0, threshold=2.0, reason='connect')
    db.session.add(alert)
    db.session.commit()

    from aiops_service import build_incident_payload
    incident = build_incident_payload(db.session.get(Alert, alert.id))

    # 让 search_knowledge 返回一个没有 causes 的条目
    original = graph_module.tools.call_tool

    def fake_call(name, **kwargs):
        if name == "search_knowledge":
            return {"ok": True, "count": 1, "results": [{
                "entry": {"id": "KB-999", "symptom": "测试",
                          "causes": [], "actions": ["做点什么"]},
                "score": 1.0}]}
        return original(name, **kwargs)

    monkeypatch.setattr(graph_module.tools, "call_tool", fake_call)

    ans = build_graph().invoke(
        {"incident": incident, "incident_id": incident.get("id")}
    ).get("answer", "")

    assert dangling_headings(ans) == [], f"仍有悬空标题：\n{ans}"
    # 应该有「未列出可能原因」这种交代，而不是空标题
    assert "未列出可能原因" in ans


def test_heading_not_dangling_when_no_kb_hit(monkeypatch, app, app_context):
    """知识库一条都没命中时，不该出现任何 KB 相关标题。"""

    target = Target(name='无KB目标', url='https://nokb.example.test',
                    timeout=5, interval_seconds=60, enabled=True)
    db.session.add(target)
    db.session.commit()
    from datetime import datetime
    db.session.add(CheckRecord(target_id=target.id, status_code=None, duration_ms=9.0,
                               is_success=False, error_msg='请求超时: x',
                               checked_at=datetime.now()))
    db.session.commit()
    rule = AlertRule(target_id=target.id, name='r',
                     rule_type=AlertRule.RULE_CONSECUTIVE_FAILURE, threshold=2)
    db.session.add(rule)
    db.session.commit()
    alert = Alert(rule_id=rule.id, target_id=target.id, message='无KB告警',
                  observed_value=3.0, threshold=2.0, reason='timeout')
    db.session.add(alert)
    db.session.commit()

    from aiops_service import build_incident_payload
    incident = build_incident_payload(db.session.get(Alert, alert.id))

    original = graph_module.tools.call_tool

    def fake_call(name, **kwargs):
        if name == "search_knowledge":
            return {"ok": True, "count": 0, "results": []}
        return original(name, **kwargs)

    monkeypatch.setattr(graph_module.tools, "call_tool", fake_call)

    ans = build_graph().invoke(
        {"incident": incident, "incident_id": incident.get("id")}
    ).get("answer", "")

    assert dangling_headings(ans) == []
    assert "可能原因" not in ans


# ============ 3. helper 自身的行为 ============
def test_dangling_detector_catches_dangling():
    """检测器自己必须有效 —— 否则上面的断言是空的。"""

    assert dangling_headings("可能原因：\n处置建议：\n") != []
    assert dangling_headings("处置建议：") != []          # 标题在最后一行


def test_dangling_detector_ignores_populated():
    assert dangling_headings("可能原因：\n  - 原因一\n  - 原因二\n") == []


def test_dangling_detector_ignores_list_items():
    """列表项以 "- " 结尾不算标题。"""

    assert dangling_headings("处置建议：\n  - 查日志：\n") == []


def test_dangling_detector_ignores_plain_text():
    assert dangling_headings("这是一句普通的话。\n") == []