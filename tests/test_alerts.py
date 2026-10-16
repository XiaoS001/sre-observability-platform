"""告警规则求值测试 —— 三种规则 + 通知投递。"""

from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest

from alerts import (
    availability,
    consecutive_failures,
    create_alert,
    evaluate_rule,
    evaluate_rules,
    notify,
    p95_latency,
    run_alert_cycle,
    send_email,
    send_webhook,
)
from models import Alert, AlertRule, CheckRecord, Notification, Target, db


def _make_target(name='t1', interval_seconds=60):
    target = Target(name=name, url='https://example.test', timeout=5,
                    interval_seconds=interval_seconds)
    db.session.add(target)
    db.session.commit()
    return target


def _add_records(target, results, minutes_ago=0):
    """results: list[bool]，True=成功 False=失败，按时间正序插入。"""

    base = datetime.now() - timedelta(minutes=minutes_ago)
    for i, is_success in enumerate(results):
        db.session.add(CheckRecord(
            target_id=target.id,
            status_code=200 if is_success else 500,
            duration_ms=10.0,
            is_success=is_success,
            error_msg=None if is_success else '连接失败: x',
            checked_at=base - timedelta(seconds=len(results) - i),
        ))
    db.session.commit()


# --- 连续失败 -----------------------------------------------------------------

def test_consecutive_failures_counts_recent_streak(app_context):
    target = _make_target()
    _add_records(target, [False, False, False])

    assert consecutive_failures(target.id) == 3


def test_consecutive_failures_resets_on_success(app_context):
    """「连续」遇到一次成功即归零。"""

    target = _make_target()
    _add_records(target, [True, False, False])

    assert consecutive_failures(target.id) == 2


def test_consecutive_failures_zero_when_last_is_success(app_context):
    target = _make_target()
    _add_records(target, [False, False, False, True])

    assert consecutive_failures(target.id) == 0


def test_consecutive_failures_zero_with_no_records(app_context):
    target = _make_target()

    assert consecutive_failures(target.id) == 0


def test_consecutive_failure_rule_triggers_at_threshold(app_context):
    target = _make_target()
    _add_records(target, [False, False])
    rule = AlertRule(target_id=target.id, name='r', threshold=2,
                     rule_type=AlertRule.RULE_CONSECUTIVE_FAILURE)
    db.session.add(rule)
    db.session.commit()

    triggered, observed, detail = evaluate_rule(rule)

    assert triggered is True
    assert observed == 2.0
    assert '连续失败' in detail


def test_consecutive_failure_rule_below_threshold(app_context):
    target = _make_target()
    _add_records(target, [False, True])
    rule = AlertRule(target_id=target.id, name='r', threshold=3,
                     rule_type=AlertRule.RULE_CONSECUTIVE_FAILURE)
    db.session.add(rule)
    db.session.commit()

    triggered, _, _ = evaluate_rule(rule)

    assert triggered is False


# --- 可用率 -------------------------------------------------------------------

def test_availability_with_no_records_returns_none(app_context):
    """数据不足时返回 None（不判定），不能返回 0% 造成误告警。"""

    target = _make_target()

    assert availability(target.id) is None


def test_availability_computes_percentage(app_context):
    target = _make_target()
    _add_records(target, [True, True, False, False])

    assert availability(target.id) == 50.0


def test_availability_rule_triggers_below_threshold(app_context):
    target = _make_target()
    _add_records(target, [True, False, False, False])
    rule = AlertRule(target_id=target.id, name='r', threshold=50,
                     rule_type=AlertRule.RULE_AVAILABILITY)
    db.session.add(rule)
    db.session.commit()

    triggered, observed, detail = evaluate_rule(rule)

    assert triggered is True
    assert observed == 25.0
    assert '可用率' in detail


def test_availability_rule_returns_none_without_data(app_context):
    """无数据时不求值 —— 不能因为「没数据」就报警。"""

    target = _make_target()
    rule = AlertRule(target_id=target.id, name='r', threshold=50,
                     rule_type=AlertRule.RULE_AVAILABILITY)
    db.session.add(rule)
    db.session.commit()

    assert evaluate_rule(rule) is None


def test_availability_rule_not_triggered_when_above_threshold(app_context):
    target = _make_target()
    _add_records(target, [True, True, True, False])
    rule = AlertRule(target_id=target.id, name='r', threshold=50,
                     rule_type=AlertRule.RULE_AVAILABILITY)
    db.session.add(rule)
    db.session.commit()

    triggered, _, _ = evaluate_rule(rule)

    assert triggered is False


# --- P95----------------------------------------------------------------------

def test_p95_with_no_records_returns_none(app_context):
    target = _make_target()

    assert p95_latency(target.id) is None


def test_p95_latency_computed(app_context):
    target = _make_target()
    for i in range(1, 101):
        db.session.add(CheckRecord(
            target_id=target.id, status_code=200, duration_ms=float(i),
            is_success=True,
        ))
    db.session.commit()

    value = p95_latency(target.id)

    assert value is not None
    assert 90 <= value <= 100


def test_p95_rule_triggers_above_threshold(app_context):
    target = _make_target()
    for _ in range(20):
        db.session.add(CheckRecord(
            target_id=target.id, status_code=200, duration_ms=900.0, is_success=True,
        ))
    db.session.commit()
    rule = AlertRule(target_id=target.id, name='r', threshold=500,
                     rule_type=AlertRule.RULE_P95_LATENCY)
    db.session.add(rule)
    db.session.commit()

    triggered, observed, detail = evaluate_rule(rule)

    assert triggered is True
    assert observed == 900.0
    assert 'P95' in detail


def test_p95_rule_ignores_failed_records(app_context):
    """P95 只看成功记录 —— 失败请求的耗时不代表服务响应能力。"""

    target = _make_target()
    db.session.add(CheckRecord(
        target_id=target.id, status_code=200, duration_ms=10.0, is_success=True,
    ))
    db.session.add(CheckRecord(
        target_id=target.id, status_code=500, duration_ms=9999.0, is_success=False,
    ))
    db.session.commit()

    assert p95_latency(target.id) == 10.0


# --- evaluate_rules ----------------------------------------------------------

def test_evaluate_rules_skips_disabled(app_context):
    target = _make_target()
    _add_records(target, [False, False])
    db.session.add(AlertRule(
        target_id=target.id, name='r', threshold=1,
        rule_type=AlertRule.RULE_CONSECUTIVE_FAILURE, enabled=False,
    ))
    db.session.commit()

    assert evaluate_rules() == []


def test_evaluate_rules_ignores_unknown_rule_type(app_context):
    """未知 rule_type 返回 None，不应抛异常 —— 脏数据不能弄垮整个循环。"""

    target = _make_target()
    rule = AlertRule(target_id=target.id, name='r', threshold=1, rule_type='???')
    db.session.add(rule)
    db.session.commit()

    assert evaluate_rule(rule) is None


def test_run_alert_cycle_creates_alerts(app_context):
    target = _make_target()
    _add_records(target, [False, False, False])
    db.session.add(AlertRule(
        target_id=target.id, name='连续失败', threshold=2,
        rule_type=AlertRule.RULE_CONSECUTIVE_FAILURE,
    ))
    db.session.commit()

    created = run_alert_cycle(cooldown_seconds=1)

    assert len(created) == 1
    assert Alert.query.count() == 1


def test_run_alert_cycle_deduplicates(app_context):
    """连续跑两轮，第二轮被冷却吞掉。"""

    target = _make_target()
    _add_records(target, [False, False, False])
    db.session.add(AlertRule(
        target_id=target.id, name='r', threshold=2,
        rule_type=AlertRule.RULE_CONSECUTIVE_FAILURE,
    ))
    db.session.commit()

    first = run_alert_cycle(cooldown_seconds=300)
    second = run_alert_cycle(cooldown_seconds=300)

    assert len(first) == 1
    assert len(second) == 0
    assert Alert.query.count() == 1


def test_alert_records_failure_reason(app_context):
    """告警带 reason 标签（四分类），便于事后定位。"""

    target = _make_target()
    db.session.add(CheckRecord(
        target_id=target.id, status_code=None, duration_ms=5.0,
        is_success=False, error_msg='连接失败: refused',
    ))
    db.session.commit()
    rule = AlertRule(
        target_id=target.id, name='r', threshold=1,
        rule_type=AlertRule.RULE_CONSECUTIVE_FAILURE,
    )
    db.session.add(rule)
    db.session.commit()

    alert = create_alert(rule, 1.0, 'x')

    assert alert.reason == 'connect'


# --- 通知 --------------------------------------------------------------------

def test_send_webhook_without_url_reports_failure(app_context):
    alert = Alert(id=1, rule_id=1, target_id=1, message='m',
                  triggered_at=datetime.now())

    success, status, error = send_webhook(alert, url='')

    assert success is False
    assert '未配置' in error


def test_send_webhook_success(app_context):
    alert = Alert(id=1, rule_id=1, target_id=1, message='m',
                  triggered_at=datetime.now())
    response = Mock(status=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)

    with patch('alerts.urllib.request.urlopen', return_value=response):
        success, status, error = send_webhook(alert, url='https://hook.test/x')

    assert success is True
    assert status == 200
    assert error is None


def test_send_webhook_network_error_does_not_raise(app_context):
    """通知失败绝不抛异常 —— 告警本身比通知送达更重要。"""

    alert = Alert(id=1, rule_id=1, target_id=1, message='m',
                  triggered_at=datetime.now())

    with patch('alerts.urllib.request.urlopen', side_effect=OSError('网络不可达')):
        success, status, error = send_webhook(alert, url='https://hook.test/x')

    assert success is False
    assert '网络不可达' in error


def test_send_webhook_http_error(app_context):
    import urllib.error

    alert = Alert(id=1, rule_id=1, target_id=1, message='m',
                  triggered_at=datetime.now())
    error = urllib.error.HTTPError('https://hook.test', 500, 'err', {}, None)

    with patch('alerts.urllib.request.urlopen', side_effect=error):
        success, status, msg = send_webhook(alert, url='https://hook.test/x')

    assert success is False
    assert status == 500


def test_notify_records_notification(app_context):
    target = _make_target()
    db.session.add(CheckRecord(
        target_id=target.id, status_code=None, duration_ms=5.0,
        is_success=False, error_msg='请求超时: slow',
    ))
    db.session.commit()
    rule = AlertRule(
        target_id=target.id, name='r', threshold=1,
        rule_type=AlertRule.RULE_CONSECUTIVE_FAILURE,
    )
    db.session.add(rule)
    db.session.commit()
    alert = create_alert(rule, 1.0, 'x')

    results = notify(alert, webhook_url='https://hook.test/x')

    assert len(results) == 1
    assert Notification.query.count() == 1
    saved = Notification.query.first()
    assert saved.channel == Notification.CHANNEL_WEBHOOK
    assert saved.alert_id == alert.id


def test_notify_failure_still_persists_failed_notification(app_context):
    """发送失败也要落库（success=False），便于排查「告警了但没人收到」。"""

    target = _make_target()
    rule = AlertRule(
        target_id=target.id, name='r', threshold=1,
        rule_type=AlertRule.RULE_CONSECUTIVE_FAILURE,
    )
    db.session.add(rule)
    db.session.commit()
    alert = create_alert(rule, 1.0, 'x')

    with patch('alerts.urllib.request.urlopen', side_effect=OSError('down')):
        notify(alert, webhook_url='https://hook.test/x')

    saved = Notification.query.first()
    assert saved.success is False
    assert 'down' in saved.error_msg


def test_send_email_disabled_by_default(app_context):
    alert = Alert(id=1, rule_id=1, target_id=1, message='m',
                  triggered_at=datetime.now())

    success, status, error = send_email(alert, to='')

    assert success is False
    assert '未启用' in error