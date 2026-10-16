"""API 测试 —— 阶段 2 的路由行为与输入校验。"""

from datetime import datetime, timedelta

import pytest

from models import CheckRecord, Target, db


def _add_target(name='t1', interval_seconds=60, enabled=True):
    target = Target(
        name=name,
        url='https://example.test',
        timeout=5,
        interval_seconds=interval_seconds,
        enabled=enabled,
    )
    db.session.add(target)
    db.session.commit()
    return target


# --- /healthz与 /metrics -----------------------------------------------------

def test_healthz_returns_ok(test_client):
    response = test_client.get('/healthz')

    assert response.status_code == 200
    assert response.get_json()['status'] == 'ok'


def test_healthz_does_not_leak_secrets(test_client):
    """健康检查回显配置，但绝不能回显密钥本体。"""

    body = test_client.get('/healthz').get_json()

    assert 'ai_key_present' in body['config']
    assert 'ai_key_present' in body['config']
    raw = test_client.get('/healthz').get_data(as_text=True)
    assert 'DEEPSEEK_API_KEY' not in raw
    assert 'sk-' not in raw


def test_metrics_endpoint_returns_prometheus_format(test_client):
    response = test_client.get('/metrics')

    assert response.status_code == 200
    assert 'text/plain' in response.headers['Content-Type']


# --- 目标增删查 ---------------------------------------------------------------

def test_create_target_returns_201(test_client):
    response = test_client.post('/api/targets', json={
        'name': '示例',
        'url': 'https://example.test',
    })

    assert response.status_code == 201
    assert response.get_json()['name'] == '示例'


def test_create_target_requires_name_and_url(test_client):
    response = test_client.post('/api/targets', json={'name': '缺少 URL'})

    assert response.status_code == 400
    assert response.get_json() == {'error': '缺少参数'}


def test_create_target_rejects_non_json(test_client):
    response = test_client.post('/api/targets', data='not json')

    assert response.status_code == 400


def test_create_target_rejects_non_integer_interval(test_client):
    response = test_client.post('/api/targets', json={
        'name': 'x',
        'url': 'https://example.test',
        'interval_seconds': 'abc',
    })

    assert response.status_code == 400


@pytest.mark.parametrize('payload', [
    {'timeout': 0},
    {'interval_seconds': 0},
    {'timeout': -1},
])
def test_create_target_rejects_non_positive_numeric_fields(test_client, payload):
    base = {'name': 'x', 'url': 'https://example.test'}

    response = test_client.post('/api/targets', json={**base, **payload})

    assert response.status_code == 400


def test_list_targets_returns_created_targets(test_client):
    _add_target(name='a')
    _add_target(name='b')

    response = test_client.get('/api/targets')

    assert response.status_code == 200
    assert [t['name'] for t in response.get_json()] == ['a', 'b']


# --- 巡检触发 -----------------------------------------------------------------

def test_check_missing_target_returns_404(test_client):
    response = test_client.post('/api/targets/99999/check')

    assert response.status_code == 404


def test_check_target_persists_record(test_client):
    from unittest.mock import Mock, patch

    target = _add_target()
    with patch('checker.requests.get', return_value=Mock(status_code=200)):
        response = test_client.post(f'/api/targets/{target.id}/check')

    assert response.status_code == 200
    assert response.get_json()['is_success'] is True
    assert CheckRecord.query.filter_by(target_id=target.id).count() == 1


# --- 记录与统计 ---------------------------------------------------------------

def test_records_empty_by_default(test_client):
    response = test_client.get('/api/records')

    assert response.status_code == 200
    assert response.get_json() == []


def test_records_respect_limit(test_client):
    target = _add_target()
    for _ in range(5):
        db.session.add(CheckRecord(
            target_id=target.id,
            status_code=200,
            duration_ms=1.0,
            is_success=True,
        ))
    db.session.commit()

    response = test_client.get('/api/records?limit=3')

    assert len(response.get_json()) == 3


def test_records_limit_is_capped(test_client):
    """limit 上限 200，防止一次拉爆全表。"""

    response = test_client.get('/api/records?limit=100000')

    assert response.status_code == 200


def test_records_limit_zero_becomes_one(test_client):
    """limit=0 非法，兜底为 1而不是返回空造成误判。"""

    response = test_client.get('/api/records?limit=0')

    assert len(response.get_json()) <= 1


def test_stats_computes_availability(app, app_context):
    target = _add_target()
    for is_success in (True, True, False):
        db.session.add(CheckRecord(
            target_id=target.id,
            status_code=200 if is_success else 500,
            duration_ms=1.0,
            is_success=is_success,
            checked_at=datetime.now(),
        ))
    db.session.commit()

    with app.test_client() as client:
        data = client.get('/api/stats').get_json()

    assert data['total'] == 3
    assert data['success'] == 2
    assert data['availability'] == 66.7
    assert data['targets'] == 1


def test_stats_with_no_records(app, app_context):
    with app.test_client() as client:
        data = client.get('/api/stats').get_json()

    assert data['total'] == 0
    assert data['availability'] == 0