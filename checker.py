"""网站检查与检查结果保存。

移植自 XM01 checker.py（逻辑零改动），仅按 RMB03 的表名适配：
    CheckResult -> CheckRecord
失败四分类（超时 / 连接失败 / 响应异常 / 未知状态码）原样保留在
metrics._classify_failure，checker 只负责原样记录 error_msg。
"""

import logging
import time

import requests

from models import db, CheckRecord
from metrics import _classify_failure
from tracing_setup import (
    mark_span_error,
    safe_span,
    set_span_attributes,
)


logger = logging.getLogger('rmb03.checker')


def check_target(target):
    start_time = time.perf_counter()

    try:
        response = requests.get(target.url, timeout=target.timeout)
        duration_ms = (time.perf_counter() - start_time) * 1000
        status_code = response.status_code
        return {
            'status_code': status_code,
            'duration_ms': duration_ms,
            'is_success': 200 <= status_code <= 399,
            'error_msg': None,
        }
    except requests.exceptions.Timeout as exc:
        duration_ms = (time.perf_counter() - start_time) * 1000
        return {
            'status_code': None,
            'duration_ms': duration_ms,
            'is_success': False,
            'error_msg': f'请求超时: {str(exc)[:200]}',
        }
    except requests.exceptions.ConnectionError as exc:
        duration_ms = (time.perf_counter() - start_time) * 1000
        return {
            'status_code': None,
            'duration_ms': duration_ms,
            'is_success': False,
            'error_msg': f'连接失败: {str(exc)[:200]}',
        }
    except Exception as exc:
        duration_ms = (time.perf_counter() - start_time) * 1000
        return {
            'status_code': None,
            'duration_ms': duration_ms,
            'is_success': False,
            'error_msg': f'请求异常: {str(exc)[:200]}',
        }


def check_and_save(target):
    # The HTTP server span only shows transport latency. This manual span records
    # the business meaning of one target check: target identity, outcome, duration,
    # and the same failure reason used by metrics and structured logs.
    with safe_span('check_target') as span:
        result = check_target(target)
        failure_reason = None if result['is_success'] else _classify_failure(
            result['status_code'],
            result['error_msg'],
        )
        # OTLP cannot store null attributes, so -1 means no HTTP response; an
        # empty reason keeps successful spans shape-stable.
        set_span_attributes(span, {
            'target_id': target.id,
            'target_name': target.name,
            'url': target.url,
            'status_code': (
                result['status_code']
                if result['status_code'] is not None
                else -1
            ),
            'duration_ms': round(result['duration_ms'], 1),
            'is_success': result['is_success'],
            'reason': failure_reason or '',
        })
        if failure_reason is not None:
            mark_span_error(span, failure_reason)

        record = CheckRecord(
            target_id=target.id,
            status_code=result['status_code'],
            duration_ms=result['duration_ms'],
            is_success=result['is_success'],
            error_msg=result['error_msg'],
        )
        db.session.add(record)
        db.session.commit()

    # Metrics and logs share the exact same failure classifier so that a reason
    # such as "connect" in a metric can be located directly in the log stream.
    logger.info('check_done', extra={
        'event': 'check_done',
        'target_id': target.id,
        'target_name': target.name,
        'url': target.url,
        'status_code': result['status_code'],
        'duration_ms': round(result['duration_ms'], 1),
        'is_success': result['is_success'],
        'reason': failure_reason,
    })
    return record
