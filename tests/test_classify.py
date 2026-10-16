"""失败四分类测试。

分类逻辑在 metrics._classify_failure（移植自 XM01），是指标 reason 标签、
日志 reason 字段、告警 reason 字段三处的共同来源 —— 所以必须单独锁死。
"""

import pytest

from metrics import _classify_failure


@pytest.mark.parametrize('error_msg_prefix, expected_reason', [
    ('请求超时: Read timed out', 'timeout'),
    ('连接失败: Max retries exceeded', 'connect'),
    ('请求异常: boom', 'exception'),
])
def test_classify_failure_from_error_message(error_msg_prefix, expected_reason):
    assert _classify_failure(None, error_msg_prefix) == expected_reason


@pytest.mark.parametrize('status_code', [400, 404, 500, 502, 503])
def test_classify_failure_falls_back_to_bad_status(status_code):
    """有状态码但不是 requests 异常 → 归为 bad_status（未知/异常状态码）。"""

    assert _classify_failure(status_code, None) == 'bad_status'


def test_classify_failure_bad_status_with_related_message():
    """既有状态码又带 error_msg 时，按前缀归类，不是 bad_status。"""

    assert _classify_failure(500, '请求超时: slow') == 'timeout'


def test_classify_failure_no_signal_defaults_to_exception():
    """既无状态码也无错误消息时兜底为 exception，不能返回 None。"""

    assert _classify_failure(None, None) == 'exception'