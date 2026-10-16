"""告警冷却期测试 —— 对应补充指示 2-2 / 2-3 的硬性要求。

🔴 这些用例**不真等 300 秒**：cooldown_seconds 注入 1~2 秒，
now 手工推进来验证边界。整套测试秒级跑完。
"""

from datetime import datetime, timedelta

import pytest

from alerts import create_alert, is_in_cooldown, run_alert_cycle
from models import Alert, AlertRule, CheckRecord, Target, db


def _make_rule(target_id=1, rule_type=AlertRule.RULE_CONSECUTIVE_FAILURE, threshold=2):
    rule = AlertRule(
        target_id=target_id,
        name='连续失败告警',
        rule_type=rule_type,
        threshold=threshold,
    )
    db.session.add(rule)
    db.session.commit()
    return rule


def _make_alert(rule, seconds_ago=0, triggered_at=None):
    """造一条历史告警。

    🔴 边界用例必须用 triggered_at 显式指定时间戳，不能用 seconds_ago：
    seconds_ago 内部调 datetime.now()，is_in_cooldown 内部又调一次
    datetime.now()，两次之间有毫秒级漂移。实测名义「恰好 60 秒」实际是
    60.0036 秒 → `<` 与 `<=` 结果完全相同 → 测试锁不住比较符。
    """

    alert = Alert(
        rule_id=rule.id,
        target_id=rule.target_id,
        message='test',
        triggered_at=(
            triggered_at
            if triggered_at is not None
            else datetime.now() - timedelta(seconds=seconds_ago)
        ),
    )
    db.session.add(alert)
    db.session.commit()
    return alert


# --- 补充指示 2-3 的四条边界用例 ---------------------------------------------

def test_cooldown_first_alert_is_never_suppressed(app_context):
    """从未告警 → is_in_cooldown 必须 False → 首次告警一定创建。"""

    rule = _make_rule()

    assert is_in_cooldown(rule.id, rule.target_id) is False


def test_cooldown_suppresses_repeat_within_window(app_context):
    """冷却期内再触发 → 抑制。"""

    rule = _make_rule()
    _make_alert(rule, seconds_ago=10)

    # 冷却 60 秒，10 秒前刚告警过 → 还在冷却期
    assert is_in_cooldown(rule.id, rule.target_id, cooldown_seconds=60) is True


def test_cooldown_boundary_allows_new_alert(app_context):
    """elapsed 恰好 == cooldown → **允许**再次创建。

    🔴 边界行为写死为 `<` 判定：恰好到期即可再次告警。
    「再多等一秒」不符合直觉，而且会让运维以为规则没生效。

    这条与下一条构成毫秒级夹逼（60.000 vs 59.999），
    变异把 `<` 改成 `<=` 时**必须**有一条失败。
    """

    rule = _make_rule()
    anchor = datetime(2026, 10, 16, 12, 0, 0)  # 固定时刻，不用 now()
    _make_alert(rule, triggered_at=anchor - timedelta(seconds=60))

    assert is_in_cooldown(
        rule.id, rule.target_id, now=anchor, cooldown_seconds=60,
    ) is False


def test_cooldown_allows_new_alert_after_window(app_context):
    """冷却期过后再触发 → 允许。"""

    rule = _make_rule()
    _make_alert(rule, seconds_ago=120)

    assert is_in_cooldown(rule.id, rule.target_id, cooldown_seconds=60) is False


# --- 冷却期的精细边界（变异测试会盯这几条）---------------------------------

def test_cooldown_just_inside_window_is_suppressed(app_context):
    """比边界少 1 毫秒 → 仍在冷却内。

    这条是 test_cooldown_boundary_allows_new_alert 的反向夹逼：
    60.000 秒允许、59.999 秒抑制，两条一起才能锁死比较符是 < 而不是 <=。
    单靠任何一条都锁不住（变异把 < 改成 <= 时二者结果可能仍相同）。
    """

    rule = _make_rule()
    anchor = datetime(2026, 10, 16, 12, 0, 0)
    _make_alert(
        rule,
        triggered_at=anchor - timedelta(seconds=59, milliseconds=999),
    )

    assert is_in_cooldown(
        rule.id, rule.target_id, now=anchor, cooldown_seconds=60,
    ) is True


def test_cooldown_zero_seconds_never_suppresses(app_context):
    """cooldown=0 表示关掉去重，任何时候都允许创建。"""

    rule = _make_rule()
    _make_alert(rule, seconds_ago=0)

    assert is_in_cooldown(rule.id, rule.target_id, cooldown_seconds=0) is False


def test_cooldown_only_looks_at_same_rule(app_context):
    """冷却键是 (rule_id, target_id)：不同规则的告警互不影响。

    这条防止「按 target_id 查最近告警」的错误实现——
    那会导致 A 规则的告警压掉B 规则的告警。
    """

    target = Target(name='t', url='https://example.test', timeout=5, interval_seconds=60)
    db.session.add(target)
    db.session.commit()

    rule_a = _make_rule(target_id=target.id, threshold=1)
    rule_b = _make_rule(target_id=target.id, threshold=2)
    _make_alert(rule_a, seconds_ago=5)

    assert is_in_cooldown(rule_a.id, target.id, cooldown_seconds=60) is True
    assert is_in_cooldown(rule_b.id, target.id, cooldown_seconds=60) is False


def test_cooldown_only_looks_at_same_target(app_context):
    """不同目标的告警互不影响。"""

    first = Target(name='a', url='https://a.test', timeout=5, interval_seconds=60)
    second = Target(name='b', url='https://b.test', timeout=5, interval_seconds=60)
    db.session.add_all([first, second])
    db.session.commit()

    rule_first = _make_rule(target_id=first.id)
    rule_second = _make_rule(target_id=second.id)
    _make_alert(rule_first, seconds_ago=5)

    assert is_in_cooldown(rule_second.id, second.id, cooldown_seconds=60) is False


def test_cooldown_uses_latest_alert_not_oldest(app_context):
    """多条告警时必须看**最近一条**：很老的一条 + 很近的一条 → 冷却中。

    错误实现会取最早那条，算出 elapsed 很大 → 误判为「不在冷却期」→ 告警刷屏。
    """

    rule = _make_rule()
    _make_alert(rule, seconds_ago=600)
    _make_alert(rule, seconds_ago=5)

    assert is_in_cooldown(rule.id, rule.target_id, cooldown_seconds=60) is True


def test_cooldown_respects_injected_now(app_context):
    """now 注入：手工推进时间，不 sleep。"""

    rule = _make_rule()
    _make_alert(rule, seconds_ago=10)
    later = datetime.now() + timedelta(seconds=120)

    assert is_in_cooldown(rule.id, rule.target_id, now=later, cooldown_seconds=60) is False


def test_create_alert_respects_cooldown(app_context):
    """create_alert 层面也要去重，不只是 is_in_cooldown 函数本身。"""

    rule = _make_rule()
    first = create_alert(rule, 2.0, '连续失败 2 次', cooldown_seconds=60)
    second = create_alert(rule, 3.0, '连续失败 3 次', cooldown_seconds=60)

    assert first is not None
    assert second is None
    assert Alert.query.count() == 1


def test_create_alert_allows_after_cooldown(app_context):
    """冷却期过后 create_alert 能再建一条。"""

    rule = _make_rule()
    create_alert(rule, 2.0, 'x', cooldown_seconds=1)
    later = datetime.now() + timedelta(seconds=5)
    second = create_alert(
        rule, 3.0, 'x', now=later, cooldown_seconds=1,
    )

    assert second is not None
    assert Alert.query.count() == 2