"""报表导出：按目标维度 / 时间维度统计可用率、P95、失败分类占比。

导出格式为 CSV。按任务单边界「不引前端框架」，用标准库 csv 生成，
不引 pandas —— 依赖越少，CI 越不容易因为构建链出问题。
"""

import csv
import io
from datetime import datetime, timedelta

from sqlalchemy import func

from metrics import _classify_failure
from models import Alert, CheckRecord, Target, db


def failure_breakdown(target_id, since=None):
    """失败分类占比统计（四分类来自 metrics._classify_failure）。"""

    query = db.session.query(
        CheckRecord.status_code,
        CheckRecord.error_msg,
    ).filter(CheckRecord.target_id == target_id)
    if since is not None:
        query = query.filter(CheckRecord.checked_at >= since)
    query = query.filter(CheckRecord.is_success.is_(False))

    counts = {}
    for status_code, error_msg in query.all():
        reason = _classify_failure(status_code, error_msg)
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def target_report(days=30):
    """目标维度报表。返回 list[dict]，字段顺序即 CSV 表头顺序。"""

    since = datetime.now() - timedelta(days=days)
    rows = []
    targets = Target.query.order_by(Target.id).all()

    for target in targets:
        base = db.session.query(
            func.count(CheckRecord.id),
            func.sum(func.cast(CheckRecord.is_success, db.Integer)),
        ).filter(
            CheckRecord.target_id == target.id,
            CheckRecord.checked_at >= since,
        ).first()
        total = int(base[0] or 0)
        success = int(base[1] or 0)

        durations = [
            row[0]
            for row in db.session.query(CheckRecord.duration_ms)
            .filter(
                CheckRecord.target_id == target.id,
                CheckRecord.checked_at >= since,
                CheckRecord.is_success.is_(True),
            )
            .order_by(CheckRecord.duration_ms.asc())
            .all()
        ]
        p95 = 0.0
        if durations:
            index = min(int(len(durations) * 0.95), len(durations) - 1)
            p95 = round(float(durations[index]), 1)

        breakdown = failure_breakdown(target.id, since=since)
        failure_total = sum(breakdown.values())

        rows.append({
            'target_id': target.id,
            'target_name': target.name,
            'enabled': int(bool(target.enabled)),
            'total_checks': total,
            'success_checks': success,
            'failed_checks': total - success,
            'availability_pct': round(success / total * 100, 2) if total else 0.0,
            'p95_ms': p95,
            'timeout': breakdown.get('timeout', 0),
            'connect': breakdown.get('connect', 0),
            'exception': breakdown.get('exception', 0),
            'bad_status': breakdown.get('bad_status', 0),
            'failure_total': failure_total,
            'alert_count': Alert.query.filter(
                Alert.target_id == target.id,
                Alert.triggered_at >= since,
            ).count(),
        })
    return rows


def daily_report(days=30):
    """时间维度报表：按天统计。"""

    since = datetime.now() - timedelta(days=days)
    rows = (
        db.session.query(
            func.date(CheckRecord.checked_at).label('date'),
            func.count(CheckRecord.id),
            func.sum(func.cast(CheckRecord.is_success, db.Integer)),
        )
        .filter(CheckRecord.checked_at >= since)
        .group_by(func.date(CheckRecord.checked_at))
        .order_by(func.date(CheckRecord.checked_at))
        .all()
    )

    result = []
    for date_value, total, success in rows:
        total = int(total or 0)
        success = int(success or 0)
        result.append({
            'date': str(date_value),
            'total_checks': total,
            'success_checks': success,
            'failed_checks': total - success,
            'availability_pct': round(success / total * 100, 2) if total else 0.0,
        })
    return result


def to_csv(rows):
    """把 list[dict] 转成 CSV 文本。空数据时也输出表头。"""

    if not rows:
        return ''

    buffer = io.StringIO()
    fieldnames = list(rows[0].keys())
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def status_page_data(days=90):
    """状态页数据（免登录）：当前状态 + 每日可用率 + 近期告警。

    90 天窗口是任务单对状态页的明确要求。
    """

    since = datetime.now() - timedelta(days=days)

    daily = (
        db.session.query(
            func.date(CheckRecord.checked_at).label('date'),
            func.count(CheckRecord.id),
            func.sum(func.cast(CheckRecord.is_success, db.Integer)),
        )
        .filter(CheckRecord.checked_at >= since)
        .group_by(func.date(CheckRecord.checked_at))
        .order_by(func.date(CheckRecord.checked_at))
        .all()
    )

    bars = []
    for date_value, total, success in daily:
        total = int(total or 0)
        success = int(success or 0)
        bars.append({
            'date': str(date_value),
            'total': total,
            'availability': round(success / total * 100, 2) if total else 0.0,
        })

    # 当前状态：取每个目标最近一次巡检结果
    current = []
    for target in Target.query.order_by(Target.id).all():
        latest = (
            db.session.query(CheckRecord)
            .filter(CheckRecord.target_id == target.id)
            .order_by(CheckRecord.checked_at.desc(), CheckRecord.id.desc())
            .first()
        )
        current.append({
            'id': target.id,
            'name': target.name,
            'enabled': bool(target.enabled),
            'operational': bool(latest and latest.is_success),
            'last_status_code': latest.status_code if latest else None,
            'last_checked_at': (
                latest.checked_at.strftime('%Y-%m-%d %H:%M:%S') if latest else None
            ),
            'last_error': latest.error_msg if latest else None,
        })

    recent_alerts = (
        db.session.query(Alert)
        .order_by(Alert.triggered_at.desc(), Alert.id.desc())
        .limit(20)
        .all()
    )

    overall = 0.0
    if bars:
        total_checks = sum(b['total'] for b in bars)
        total_success = sum(round(b['availability'] / 100 * b['total']) for b in bars)
        overall = round(total_success / total_checks * 100, 2) if total_checks else 0.0

    return {
        'window_days': days,
        'overall_availability': overall,
        'daily': bars,
        'targets': current,
        'recent_alerts': [
            {
                'id': a.id,
                'message': a.message,
                'reason': a.reason,
                'triggered_at': a.triggered_at.strftime('%Y-%m-%d %H:%M:%S'),
            }
            for a in recent_alerts
        ],
    }