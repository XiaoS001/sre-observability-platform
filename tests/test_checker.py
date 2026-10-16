"""巡检逻辑测试 —— 失败四分类必须保住。"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

from checker import check_target


@patch('checker.requests.get')
def test_check_target_success(mock_get):
    mock_get.return_value = Mock(status_code=200)
    target = SimpleNamespace(url='https://example.test', timeout=5)

    result = check_target(target)

    assert result['status_code'] == 200
    assert result['is_success'] is True
    assert result['error_msg'] is None
    assert result['duration_ms'] > 0


@patch('checker.requests.get')
def test_check_target_404(mock_get):
    mock_get.return_value = Mock(status_code=404)
    target = SimpleNamespace(url='https://example.test/missing', timeout=5)

    result = check_target(target)

    assert result['status_code'] == 404
    assert result['is_success'] is False


@patch('checker.requests.get')
def test_check_target_timeout(mock_get):
    mock_get.side_effect = requests.exceptions.Timeout('请求超时')
    target = SimpleNamespace(url='https://example.test', timeout=0.001)

    result = check_target(target)

    assert result['status_code'] is None
    assert result['is_success'] is False
    assert '超时' in result['error_msg']


@patch('checker.requests.get')
def test_check_target_connection_error(mock_get):
    mock_get.side_effect = requests.exceptions.ConnectionError('无法连接')
    target = SimpleNamespace(url='https://example.test', timeout=5)

    result = check_target(target)

    assert result['status_code'] is None
    assert result['is_success'] is False
    assert '连接失败' in result['error_msg']


@patch('checker.requests.get')
def test_check_target_unknown_exception(mock_get):
    """XM01 四分类里的「请求异常」：非 requests 的其它异常也要归一化。"""

    mock_get.side_effect = ValueError('不是 requests 的异常')
    target = SimpleNamespace(url='https://example.test', timeout=5)

    result = check_target(target)

    assert result['status_code'] is None
    assert result['is_success'] is False
    assert '请求异常' in result['error_msg']


@pytest.mark.parametrize('status_code, expected_success', [
    (200, True),
    (204, True),
    (301, True),
    (399, True),
    (400, False),
    (500, False),
])
@patch('checker.requests.get')
def test_check_target_status_code_boundary(mock_get, status_code, expected_success):
    """成功区间定义为 [200, 399]，边界必须准确。"""

    mock_get.return_value = Mock(status_code=status_code)
    target = SimpleNamespace(url='https://example.test', timeout=5)

    result = check_target(target)

    assert result['is_success'] is expected_success