"""RMB03 Prometheus 指标采集。

这里使用自定义 Collector 在每次 scrape 时从数据库派生指标，而不是在巡检
流程里维护全局计数器。这样指标始终与 SQLite 中的真实巡检记录一致，进程
重启后也能保留历史；代价是每次 scrape 需要执行若干聚合查询。当前数据量很
小，这个取舍是可接受的。如果未来记录量显著增长，应改为应用内计数器配合
定时持久化导出的方案。

Histogram 使用 prometheus-client 的默认桶，单位是秒；默认桶从 0.005 秒到
10 秒，覆盖几十毫秒到几秒的 HTTP 响应耗时。
"""

from collections import Counter, defaultdict

from prometheus_client import CollectorRegistry, Histogram
from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
)
from sqlalchemy import func

from models import CheckRecord, Target, db


TIMEOUT_PREFIX = '请求超时:'
CONNECT_PREFIX = '连接失败:'
EXCEPTION_PREFIX = '请求异常:'


def _classify_failure(status_code, error_msg):
    if error_msg is not None and error_msg.startswith(TIMEOUT_PREFIX):
        return 'timeout'
    if error_msg is not None and error_msg.startswith(CONNECT_PREFIX):
        return 'connect'
    if error_msg is not None and error_msg.startswith(EXCEPTION_PREFIX):
        return 'exception'
    if status_code is not None:
        return 'bad_status'
    return 'exception'


class RMB03Collector:
    """从数据库派生 RMB03 Prometheus 指标。"""

    def collect(self):
        latest_rows = self._latest_rows()

        yield self._probe_success(latest_rows)
        yield self._probe_duration_seconds(latest_rows)
        yield self._last_check_timestamp_seconds(latest_rows)
        yield self._checks_total()
        yield self._check_failures_total()
        yield self._check_duration_seconds()
        yield self._targets_total()

    @staticmethod
    def _latest_rows():
        ranked = (
            db.session.query(
                CheckRecord.target_id.label('target_id'),
                CheckRecord.is_success.label('is_success'),
                CheckRecord.duration_ms.label('duration_ms'),
                CheckRecord.checked_at.label('checked_at'),
                func.row_number().over(
                    partition_by=CheckRecord.target_id,
                    order_by=(
                        CheckRecord.checked_at.desc(),
                        CheckRecord.id.desc(),
                    ),
                ).label('row_number'),
            )
            .subquery()
        )

        return (
            db.session.query(
                Target.id.label('target_id'),
                Target.name.label('target_name'),
                Target.url.label('url'),
                ranked.c.is_success,
                ranked.c.duration_ms,
                ranked.c.checked_at,
            )
            .join(ranked, Target.id == ranked.c.target_id)
            .filter(ranked.c.row_number == 1)
            .order_by(Target.id)
            .all()
        )

    @staticmethod
    def _probe_success(rows):
        family = GaugeMetricFamily(
            'rmb03_probe_success',
            'Whether the latest check for each target succeeded.',
            labels=['target_id', 'target_name', 'url'],
        )
        for row in rows:
            family.add_metric(
                [str(row.target_id), row.target_name, row.url],
                1 if row.is_success else 0,
            )
        return family

    @staticmethod
    def _probe_duration_seconds(rows):
        family = GaugeMetricFamily(
            'rmb03_probe_duration_seconds',
            'Duration in seconds of the latest check for each target.',
            labels=['target_id', 'target_name'],
        )
        for row in rows:
            family.add_metric(
                [str(row.target_id), row.target_name],
                float(row.duration_ms) / 1000,
            )
        return family

    @staticmethod
    def _last_check_timestamp_seconds(rows):
        family = GaugeMetricFamily(
            'rmb03_last_check_timestamp_seconds',
            'Unix timestamp of the latest check for each target.',
            labels=['target_id', 'target_name'],
        )
        for row in rows:
            timestamp = row.checked_at.timestamp() if row.checked_at else 0
            family.add_metric(
                [str(row.target_id), row.target_name],
                timestamp,
            )
        return family

    @staticmethod
    def _checks_total():
        family = CounterMetricFamily(
            'rmb03_checks_total',
            'Total number of checks by target and status.',
            labels=['target_id', 'target_name', 'status'],
        )
        rows = (
            db.session.query(
                Target.id.label('target_id'),
                Target.name.label('target_name'),
                CheckRecord.is_success,
                func.count(CheckRecord.id).label('count'),
            )
            .join(CheckRecord, CheckRecord.target_id == Target.id)
            .group_by(
                Target.id,
                Target.name,
                CheckRecord.is_success,
            )
            .order_by(Target.id, CheckRecord.is_success)
            .all()
        )
        for row in rows:
            family.add_metric(
                [
                    str(row.target_id),
                    row.target_name,
                    'success' if row.is_success else 'failure',
                ],
                row.count,
            )
        return family

    @staticmethod
    def _check_failures_total():
        family = CounterMetricFamily(
            'rmb03_check_failures_total',
            'Total failed checks by target and reason.',
            labels=['target_id', 'target_name', 'reason'],
        )
        rows = (
            db.session.query(
                Target.id.label('target_id'),
                Target.name.label('target_name'),
                CheckRecord.status_code,
                CheckRecord.error_msg,
            )
            .join(CheckRecord, CheckRecord.target_id == Target.id)
            .filter(CheckRecord.is_success.is_(False))
            .order_by(Target.id, CheckRecord.id)
            .all()
        )

        counts = Counter()
        for row in rows:
            reason = _classify_failure(row.status_code, row.error_msg)
            counts[(row.target_id, row.target_name, reason)] += 1

        for (target_id, target_name, reason), count in sorted(counts.items()):
            family.add_metric(
                [str(target_id), target_name, reason],
                count,
            )
        return family

    @staticmethod
    def _check_duration_seconds():
        family = HistogramMetricFamily(
            'rmb03_check_duration_seconds',
            'Successful check duration distribution in seconds.',
            labels=['target_id', 'target_name'],
        )
        rows = (
            db.session.query(
                Target.id.label('target_id'),
                Target.name.label('target_name'),
                CheckRecord.duration_ms,
            )
            .join(CheckRecord, CheckRecord.target_id == Target.id)
            .filter(CheckRecord.is_success.is_(True))
            .order_by(Target.id, CheckRecord.id)
            .all()
        )

        durations = defaultdict(list)
        target_names = {}
        for row in rows:
            durations[row.target_id].append(float(row.duration_ms) / 1000)
            target_names[row.target_id] = row.target_name

        for target_id, values in sorted(durations.items()):
            buckets = []
            for bound in Histogram.DEFAULT_BUCKETS:
                bucket_name = '+Inf' if bound == float('inf') else str(bound)
                bucket_count = sum(value <= bound for value in values)
                buckets.append((bucket_name, bucket_count))

            family.add_metric(
                [str(target_id), target_names[target_id]],
                buckets,
                sum(values),
            )
        return family

    @staticmethod
    def _targets_total():
        family = GaugeMetricFamily(
            'rmb03_targets_total',
            'Number of monitoring targets grouped by enabled state.',
            labels=['enabled'],
        )
        rows = (
            db.session.query(
                Target.enabled,
                func.count(Target.id).label('count'),
            )
            .group_by(Target.enabled)
            .all()
        )
        counts = {bool(row.enabled): row.count for row in rows}
        family.add_metric(['true'], counts.get(True, 0))
        family.add_metric(['false'], counts.get(False, 0))
        return family


registry = CollectorRegistry()
registry.register(RMB03Collector())
