"""演示数据生成脚本。

⚠️ 本脚本生成的是**模拟数据**，不涉及任何真实监控对象。
   目标 URL 全部使用 RFC 2606 保留的示例域名（*.test / example.com），
   永远不会真的发请求到第三方站点。

用途：
  · 让状态页的 90 天可用率图有东西可画（空图表做不了演示材料）
  · 让 README 的「3 分钟演示脚本」能真实走一遍业务闭环

幂等：可重复执行。演示数据带 DEMO_PREFIX 标记，重复跑时先清掉旧演示数据
再重建，不会翻倍，也不会碰到用户自己创建的数据。

只写默认业务库 data/rmb03.db —— 不碰测试库（tests/_tmp_testdata/）。
"""

import random
from datetime import datetime, timedelta

import config
from models import (
    Alert,
    AlertRule,
    CheckRecord,
    Notification,
    Role,
    Target,
    User,
    db,
    init_roles,
)


DEMO_PREFIX = 'demo-'

# 示例域名（RFC 2606 保留，不会解析到真实站点）
TARGETS = [
    # (name, url, interval, 是否不稳定, 基础成功率)
    ('官网首页', 'https://www.example.com/', 60, False, 0.995),
    ('登录接口', 'https://api.example.com/login', 30, False, 0.998),
    ('订单查询接口', 'https://api.example.com/orders', 60, False, 0.997),
    ('静态资源 CDN', 'https://cdn.example.com/assets', 300, False, 0.999),
    ('支付回调', 'https://pay.example.com/callback', 30, True, 0.94),
    ('库存同步服务', 'https://stock.example.com/sync', 120, True, 0.90),
    ('报表导出服务', 'https://report.example.com/export', 300, True, 0.88),
]

DEMO_USERS = [
    ('admin1', 'admin123456', 'admin', '演示：管理员'),
    ('operator1', 'operator123456', 'operator', '演示：运维'),
    ('viewer1', 'viewer123456', 'viewer', '演示：只读（访问规则详情会被 403）'),
]

# 四分类的模拟错误消息（与 metrics._classify_failure 的前缀约定一致）
FAILURE_PRESETS = [
    ('请求超时: Read timed out. (read timeout=5)', 'timeout', 30.0),
    ('连接失败: HTTPConnectionPool(host=\'db.example.com\', port=443): Max retries exceeded', 'connect', 8.0),
    ('请求异常: JSONDecodeError: Expecting value', 'exception', 15.0),
]

STATUS_CODES = [502, 503, 504, 500, 404]


def clear_demo_data():
    """清掉上一次生成的演示数据。"""

    target_ids = [
        row[0] for row in db.session.query(Target.id)
        .filter(Target.name.like(f'%{DEMO_PREFIX}%')).all()
    ]
    if target_ids:
        rule_ids = [
            row[0] for row in db.session.query(AlertRule.id)
            .filter(AlertRule.target_id.in_(target_ids)).all()
        ]
        alert_ids = [
            row[0] for row in db.session.query(Alert.id)
            .filter(Alert.target_id.in_(target_ids)).all()
        ]
        if alert_ids:
            db.session.query(Notification).filter(
                Notification.alert_id.in_(alert_ids)
            ).delete(synchronize_session=False)
        db.session.query(Alert).filter(Alert.target_id.in_(target_ids)).delete(
            synchronize_session=False
        )
        if rule_ids:
            db.session.query(AlertRule).filter(
                AlertRule.id.in_(rule_ids)
            ).delete(synchronize_session=False)
        db.session.query(CheckRecord).filter(
            CheckRecord.target_id.in_(target_ids)
        ).delete(synchronize_session=False)
        db.session.query(Target).filter(Target.id.in_(target_ids)).delete(
            synchronize_session=False
        )

    db.session.query(User).filter(User.username.like(f'{DEMO_PREFIX}%')).delete(
        synchronize_session=False
    )
    db.session.commit()


def seed_targets(rng):
    """建监控目标，返回 {业务名: Target}。"""

    created = {}
    for name, url, interval, unstable, _ in TARGETS:
        display_name = f'{DEMO_PREFIX}{name}'
        target = Target(
            name=display_name,
            url=url,
            timeout=5,
            interval_seconds=interval,
            enabled=True,
            created_at=datetime.now() - timedelta(days=90),
        )
        db.session.add(target)
        created[name] = target
    db.session.commit()
    return created


def seed_records(rng, targets):
    """按时间分布生成过去 30~90 天的巡检记录。

    🔴 时间要铺开（不是全堆在今天），否则状态页的 90 天条形图只有一根柱子。

    🔴 采用「事故日」模型而不是「每天独立抽样」：
        每天独立按成功率抽样 → 每��都会随机掉几个点 → 90 天全是红色，
        看起来像「天天在故障」，反而不像真实系统。
        真实系统的形态是「绝大部分时间正常，偶尔出一次事故」。
        所以先随机挑 4 天作为事故日（落在不稳定的 3 个目标上），
        其余 87 天正常。事故日的可用率才会跌破 99%，条形图才有红绿对比。
    """

    now = datetime.now()

    # 4 个事故日：最近 90 天内随机挑，且不在最近 3 天（保证「当前状态」好看）
    candidates = [now - timedelta(days=d) for d in range(6, 88)]
    incident_days = {rng.choice(candidates).date() for _ in range(4)}

    unstable_ids = {targets[name].id for name, _u, _i, unstable, _s in TARGETS if unstable}
    total = 0

    for name, _url, interval, unstable, base_success in TARGETS:
        target = targets[name]
        # 每天 8 个采样点 × 90 天 = 720 条/目标，够画图又不至于把库撑大
        for day_offset in range(90, 0, -1):
            day = now - timedelta(days=day_offset)
            sample_count = 8 if day.weekday() < 5 else 4

            is_incident_day = day.date() in incident_days and target.id in unstable_ids
            # 正常日的失败率极低（1~2‰），事故日显著恶化
            failure_rate = 0.0 if not is_incident_day else rng.uniform(0.10, 0.35)

            for _ in range(sample_count):
                checked_at = day.replace(
                    hour=rng.randint(0, 23),
                    minute=rng.randint(0, 59),
                    second=rng.randint(0, 59),
                )

                if rng.random() >= failure_rate:
                    # 正常：耗时呈长尾分布，P95 才有的算
                    base_ms = rng.uniform(30, 180)
                    if rng.random() < 0.05:
                        base_ms *= rng.uniform(3, 8)  # 少量慢请求
                    db.session.add(CheckRecord(
                        target_id=target.id,
                        status_code=200,
                        duration_ms=round(base_ms, 1),
                        is_success=True,
                        error_msg=None,
                        checked_at=checked_at,
                    ))
                else:
                    message, _reason, duration = rng.choice(FAILURE_PRESETS)
                    db.session.add(CheckRecord(
                        target_id=target.id,
                        status_code=None if '超时' in message else rng.choice(STATUS_CODES),
                        duration_ms=round(duration, 1),
                        is_success=False,
                        error_msg=message,
                        checked_at=checked_at,
                    ))
                total += 1

        # 今天的记录也要有，否则状态页「当前状态」全是「尚未巡检」
        for hour_offset in range(6, 0, -1):
            db.session.add(CheckRecord(
                target_id=target.id,
                status_code=200,
                duration_ms=round(rng.uniform(40, 200), 1),
                is_success=True,
                error_msg=None,
                checked_at=now - timedelta(hours=hour_offset),
            ))
            total += 1

    db.session.commit()
    return total, len(incident_days)


def seed_rules(targets):
    """三种规则类型各建一条，证明三种规则都实现了。"""

    rules = [
        AlertRule(
            target_id=targets['支付回调'].id,
            name=f'{DEMO_PREFIX}连续失败告警（3 次）',
            rule_type=AlertRule.RULE_CONSECUTIVE_FAILURE,
            threshold=3,
            enabled=True,
        ),
        AlertRule(
            target_id=targets['库存同步服务'].id,
            name=f'{DEMO_PREFIX}可用率告警（低于 95%）',
            rule_type=AlertRule.RULE_AVAILABILITY,
            threshold=95,
            enabled=True,
        ),
        AlertRule(
            target_id=targets['报表导出服务'].id,
            name=f'{DEMO_PREFIX}P95 延迟告警（超 500ms）',
            rule_type=AlertRule.RULE_P95_LATENCY,
            threshold=500,
            enabled=True,
        ),
    ]
    db.session.add_all(rules)
    db.session.commit()
    return rules


def seed_alerts(rng, targets, rules):
    """造 4 条告警：2 条进行中、2 条已解决。"""

    now = datetime.now()
    alerts_data = [
        (rules[0], targets['支付回调'], 'connect', 5.0, 3.0, 3, None),
        (rules[1], targets['库存同步服务'], 'timeout', 88.2, 95.0, 12, None),
        (rules[2], targets['报表导出服务'], 'bad_status', 782.4, 500.0, 26, 8),
        (rules[0], targets['库存同步服务'], 'exception', 6.0, 3.0, 40, 35),
    ]

    created = []
    for rule, target, reason, observed, threshold, hours_ago, resolved_hours in alerts_data:
        triggered_at = now - timedelta(hours=hours_ago)
        alert = Alert(
            rule_id=rule.id,
            target_id=target.id,
            message=(
                f'[{target.name}] {rule.name}: '
                f'触发值 {observed}（阈值 {threshold}）'
            ),
            observed_value=observed,
            threshold=threshold,
            reason=reason,
            triggered_at=triggered_at,
            resolved_at=(
                triggered_at + timedelta(hours=resolved_hours)
                if resolved_hours else None
            ),
        )
        db.session.add(alert)
        created.append(alert)
    db.session.commit()
    return created


def seed_notifications(alerts):
    """每条告警 1~2 条通知，含 1 条 success=false（展示失败也落库）。"""

    rows = []
    for index, alert in enumerate(alerts):
        rows.append(Notification(
            alert_id=alert.id,
            channel=Notification.CHANNEL_WEBHOOK,
            target_url='https://hooks.example.com/services/T000/B000/demo',
            success=True,
            status_code=200,
            error_msg=None,
            sent_at=alert.triggered_at + timedelta(seconds=2),
        ))
        if index == 0:
            # 刻意留一条失败：通知发不出去时也要留痕，便于排查「告警了但没人收到」
            rows.append(Notification(
                alert_id=alert.id,
                channel=Notification.CHANNEL_EMAIL,
                target_url='demo-ops@example.com',
                success=False,
                status_code=None,
                error_msg='邮件通道占位（收件人 demo-ops@example.com），本阶段不实际发信',
                sent_at=alert.triggered_at + timedelta(seconds=3),
            ))

    db.session.add_all(rows)
    db.session.commit()
    return rows


def seed_users():
    """3 个演示账号，角色各一。"""

    from auth import hash_password
    from models import ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER

    created = []
    for username, password, role_name, _desc in DEMO_USERS:
        role = Role.query.filter_by(name=role_name).first()
        user = User(
            username=f'{DEMO_PREFIX}{username}',
            password_hash=hash_password(password),
            role=role,
            created_at=datetime.now() - timedelta(days=30),
        )
        db.session.add(user)
        created.append((f'{DEMO_PREFIX}{username}', password, role_name))
    db.session.commit()
    return created


def seed_all(seed=20261016):
    """主入口：幂等，重复执行结果一致。"""

    rng = random.Random(seed)

    print('=' * 66)
    print('⚠️  本脚本生成的是【模拟数据】，不涉及任何真实监控对象。')
    print('    目标 URL 全部使用 RFC 2606 保留的示例域名（*.example.com）。')
    print('=' * 66)
    print()

    print('数据库 :', config.DATABASE_URL)
    print('清理旧的演示数据 ...')
    clear_demo_data()

    print('创建监控目标 ...')
    targets = seed_targets(rng)

    print('生成 90 天巡检记录 ...')
    record_count, incident_day_count = seed_records(rng, targets)

    print('创建告警规则（三种类型各一条）...')
    rules = seed_rules(targets)

    print('创建告警与通知记录 ...')
    alerts = seed_alerts(rng, targets, rules)
    notifications = seed_notifications(alerts)

    print('创建演示账号 ...')
    users = seed_users()

    print()
    print('=' * 66)
    print(f'✅ 完成：目标 {len(targets)} 个 · 巡检记录 {record_count} 条 · '
          f'告警 {len(alerts)} 条 · 通知 {len(notifications)} 条 · 用户 {len(users)} 个')
    print(f'   90 天窗口内安排了 {incident_day_count} 个「事故日」，其余为正常日')
    print('=' * 66)
    print()
    print('演示账号（登录用）：')
    for username, password, role_name in users:
        print(f'  {username:<16} / {password:<16} 角色={role_name}')
    print()
    print('下一步：')
    print('  set RMB03_SCHEDULER_ENABLED=false')
    print('  venv\\Scripts\\python.exe app.py')
    print('  然后打开 http://127.0.0.1:5000/status')
    print()


if __name__ == '__main__':
    from app import app

    with app.app_context():
        db.create_all()
        init_roles()
        seed_all()