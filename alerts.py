"""告警：规则求值 → 冷却去重 → 触发 → 通知。

三个关键设计（对应任务单与补充指示）：

1. **规则求值**（evaluate_rules）
    三种规则共用一套入口：连续失败 N 次 / 可用率低于 X% / P95 超 Y ms。
    每种规则只回答一个问题：「此刻该不该报警」。

2. **冷却去重**（is_in_cooldown）
    🔴 语义必须是「**重复抑制**」而不是「持续时长」：
        重复抑制：触发后 N 秒内不再触发 → **不影响第一次告警**
        持续时长：条件连续成立 N 秒才触发 → 会延迟第一次告警
    任务单要的是前者。从未告警过 → is_in_cooldown 返回 False → 首次必然创建。
    now / cooldown_seconds 支持注入 → 测试用 1~2 秒冷却验证边界，
    整个测试套件不必真等 300 秒。

3. **通知失败不影响告警本身**
    webhook /邮件都做了超时与异常兜底。平台对外承诺的是「告警被记录」，
    不是「通知一定送达」——通知失败必须落库成一条 failed 的 Notification，
    而不是把整个告警流程炸掉。
"""

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import config
from models import Alert, AlertRule, CheckRecord, Notification, Target, db
from sqlalchemy import func


logger = logging.getLogger('rmb03.alerts')


# --- 规则求值 ---------------------------------------------------------------

def consecutive_failures(target_id, now=None):
    """最近连续失败次数（遇到一次成功即归零）。

    按时间倒序取记录，逐条数到第一次成功为止 —— 这才是「连续」的准确含义。
    """

    now = now or datetime.now()
    rows = (
        db.session.query(CheckRecord.is_success)
        .filter(CheckRecord.target_id == target_id, CheckRecord.checked_at <= now)
        .order_by(CheckRecord.checked_at.desc(), CheckRecord.id.desc())
        .limit(100)
        .all()
    )
    count = 0
    for (is_success,) in rows:
        if is_success:
            break
        count += 1
    return count


def availability(target_id, now=None, window_minutes=30):
    """窗口内可用率（%）。无记录时返回 None —— 表示「数据不足，不判定」。"""

    now = now or datetime.now()
    since = now - timedelta(minutes=window_minutes)
    total = (
        db.session.query(func.count(CheckRecord.id))
        .filter(CheckRecord.target_id == target_id, CheckRecord.checked_at >= since)
        .scalar()
        or 0
    )
    if total == 0:
        return None
    success = (
        db.session.query(func.count(CheckRecord.id))
        .filter(
            CheckRecord.target_id == target_id,
            CheckRecord.checked_at >= since,
            CheckRecord.is_success.is_(True),
        )
        .scalar()
        or 0
    )
    return round(success / total * 100, 2)


def p95_latency(target_id, now=None, window_minutes=30):
    """窗口内 P95 响应时间（ms）。无记录时返回 None。

    SQLite 没有 percentile 函数，用「按耗时排序取第 95% 位」实现。
    数据量小，内存排序比写 SQL 窗口函数更直观，也不会引入方言差异。
    """

    now = now or datetime.now()
    since = now - timedelta(minutes=window_minutes)
    durations = [
        row[0]
        for row in db.session.query(CheckRecord.duration_ms)
        .filter(
            CheckRecord.target_id == target_id,
            CheckRecord.checked_at >= since,
            CheckRecord.is_success.is_(True),
        )
        .order_by(CheckRecord.duration_ms.asc())
        .all()
    ]
    if not durations:
        return None
    index = int(len(durations) * 0.95)
    index = min(index, len(durations) - 1)
    return round(float(durations[index]), 1)


def evaluate_rule(rule, now=None):
    """对单条规则求值。返回 (是否触发, 实测值, 说明) 或 None（数据不足）。"""

    now = now or datetime.now()

    if rule.rule_type == AlertRule.RULE_CONSECUTIVE_FAILURE:
        observed = consecutive_failures(rule.target_id, now=now)
        triggered = observed >= rule.threshold
        detail = f'连续失败 {observed} 次（阈值 {rule.threshold:g}）'
        return triggered, float(observed), detail

    if rule.rule_type == AlertRule.RULE_AVAILABILITY:
        observed = availability(rule.target_id, now=now)
        if observed is None:
            return None
        triggered = observed < rule.threshold
        detail = f'可用率 {observed}%（阈值 {rule.threshold:g}%）'
        return triggered, float(observed), detail

    if rule.rule_type == AlertRule.RULE_P95_LATENCY:
        observed = p95_latency(rule.target_id, now=now)
        if observed is None:
            return None
        triggered = observed > rule.threshold
        detail = f'P95 {observed}ms（阈值 {rule.threshold:g}ms）'
        return triggered, float(observed), detail

    return None


def evaluate_rules(now=None, rules=None):
    """求值一批规则，返回触发的 (rule, observed, detail) 列表。

    不做冷却判定 —— 冷却由 create_alert 负责。拆开是为了让「求值」和
    「去重」能各自独立测试。
    """

    now = now or datetime.now()
    if rules is None:
        rules = AlertRule.query.filter_by(enabled=True).all()

    triggered = []
    for rule in rules:
        outcome = evaluate_rule(rule, now=now)
        if outcome is None:
            continue
        is_triggered, observed, detail = outcome
        if is_triggered:
            triggered.append((rule, observed, detail))
    return triggered


# --- 冷却去重 ---------------------------------------------------------------

def is_in_cooldown(rule_id, target_id, now=None, cooldown_seconds=None):
    """判断该规则+目标是否处于冷却期（重复抑制语义）。

    边界用 `<`：elapsed 恰好等于 cooldown_seconds 时**允许**再次创建。
    「恰好到期即可再次告警」比「再多等一秒」更符合直觉，
    也与下面的 test_cooldown_boundary_allows_new_alert 一致。

    now / cooldown_seconds 支持注入：测试用 1~2 秒冷却 + 手工推进 now
    验证边界，不必真等 300 秒。
    """

    now = now or datetime.now()
    if cooldown_seconds is None:
        cooldown_seconds = config.ALERT_COOLDOWN_SECONDS

    latest = (
        db.session.query(Alert.triggered_at)
        .filter(Alert.rule_id == rule_id, Alert.target_id == target_id)
        .order_by(Alert.triggered_at.desc(), Alert.id.desc())
        .first()
    )
    if latest is None or latest[0] is None:
        # 从未告警 → 不在冷却期 → 允许首次告警
        return False
    return (now - latest[0]).total_seconds() < cooldown_seconds


# --- 触发与通知 -------------------------------------------------------------

def _latest_reason(target_id, now=None):
    """取最近一次失败记录的分类，用于给告警打reason 标签。"""

    now = now or datetime.now()
    row = (
        db.session.query(CheckRecord.status_code, CheckRecord.error_msg)
        .filter(
            CheckRecord.target_id == target_id,
            CheckRecord.checked_at <= now,
            CheckRecord.is_success.is_(False),
        )
        .order_by(CheckRecord.checked_at.desc(), CheckRecord.id.desc())
        .first()
    )
    if row is None:
        return None
    from metrics import _classify_failure

    return _classify_failure(row[0], row[1])


def create_alert(rule, observed_value, detail, now=None, cooldown_seconds=None):
    """触发一条告警（带冷却去重）。返回 Alert 或 None（冷却中）。

    去重键是 (rule_id, target_id)：同一规则对同一目标在冷却窗口内只留一条。
    """

    now = now or datetime.now()

    if is_in_cooldown(rule.id, rule.target_id, now=now, cooldown_seconds=cooldown_seconds):
        logger.debug('alert_suppressed_by_cooldown', extra={
            'event': 'alert_suppressed_by_cooldown',
            'rule_id': rule.id,
            'target_id': rule.target_id,
        })
        return None

    target = db.session.get(Target, rule.target_id)
    target_name = target.name if target is not None else f'target#{rule.target_id}'

    alert = Alert(
        rule_id=rule.id,
        target_id=rule.target_id,
        message=f'[{target_name}] {rule.name}: {detail}',
        observed_value=observed_value,
        threshold=rule.threshold,
        reason=_latest_reason(rule.target_id, now=now),
        triggered_at=now,
    )
    db.session.add(alert)
    db.session.commit()

    logger.info('alert_triggered', extra={
        'event': 'alert_triggered',
        'alert_id': alert.id,
        'rule_id': rule.id,
        'target_id': rule.target_id,
        'reason': alert.reason,
    })
    return alert


def send_webhook(alert, url=None, timeout=None):
    """推 webhook。返回 (success, status_code, error_msg)。

    绝不抛异常：通知失败要落库成 failed 记录，而不是把告警流程炸掉。
    """

    url = url if url is not None else config.ALERT_WEBHOOK_URL
    timeout = timeout if timeout is not None else config.ALERT_WEBHOOK_TIMEOUT
    if not url:
        return False, None, '未配置 webhook 地址'

    payload = json.dumps({
        'text': alert.message,
        'alert_id': alert.id,
        'rule_id': alert.rule_id,
        'target_id': alert.target_id,
        'reason': alert.reason,
        'observed_value': alert.observed_value,
        'threshold': alert.threshold,
        'triggered_at': alert.triggered_at.strftime('%Y-%m-%d %H:%M:%S'),
    }, ensure_ascii=False).encode('utf-8')

    request = urllib.request.Request(
        url,
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, response.status, None
    except urllib.error.HTTPError as exc:
        return False, exc.code, f'HTTP {exc.code}'
    except Exception as exc:
        return False, None, str(exc)[:200]


def send_email(alert, to=None):
    """邮件通道 —— 本阶段为占位实现。

    按任务单「先做 webhook + 邮件占位，不接真实短信/电话」，这里只把
    「该发什么」组织好并落库，不真的发信。
    """

    to = to if to is not None else config.ALERT_EMAIL_TO
    if not config.ALERT_EMAIL_ENABLED:
        return False, None, '邮件通道未启用'
    if not to:
        return False, None, '未配置收件人'
    return False, None, f'邮件通道占位（收件人 {to}），本阶段不实际发信'


def notify(alert, webhook_url=None, email_to=None):
    """把一条告警投递到已配置的各渠道，逐条落库Notification。"""

    results = []
    if webhook_url is not None or config.ALERT_WEBHOOK_URL:
        results.append(_deliver(alert, Notification.CHANNEL_WEBHOOK, webhook_url, send_webhook))
    if email_to is not None or config.ALERT_EMAIL_ENABLED:
        results.append(_deliver(alert, Notification.CHANNEL_EMAIL, email_to, send_email))
    db.session.commit()
    return results


def _deliver(alert, channel, target, sender):
    """调一次发送器并落库，永远不让异常冒到告警流程外面。"""

    try:
        success, status_code, error_msg = sender(alert, target)
    except Exception as exc:  # 双保险：sender 内部已兜底，这里再兜一层
        success, status_code, error_msg = False, None, str(exc)[:200]

    notification = Notification(
        alert_id=alert.id,
        channel=channel,
        target_url=target,
        success=success,
        status_code=status_code,
        error_msg=error_msg,
    )
    db.session.add(notification)
    return notification


def run_alert_cycle(now=None, cooldown_seconds=None):
    """一轮完整流程：求值 → 冷却去重 → 触发 → 通知。

    返回本轮新建的 Alert 列表。调度器在巡检后应调用它。
    """

    created = []
    for rule, observed, detail in evaluate_rules(now=now):
        alert = create_alert(
            rule,
            observed,
            detail,
            now=now,
            cooldown_seconds=cooldown_seconds,
        )
        if alert is not None:
            created.append(alert)
            notify(alert)
    return created