"""AI 根因分析的 API 层测试（阶段 6-3）。

三条硬约束在这里被锁死：
  1. 🔴 viewer 调 analyze 必须 **403**（不是 401，不是 200）
  2. 🔴 无 key 时也必须 **200 + degraded**，绝不能500
  3. 🔴 结果必须落库可回溯
"""
import pytest

from models import ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER, Alert, AlertRule, Analysis, Target, db


# ============ fixture ============
@pytest.fixture()
def alert_fixture(app, app_context):
    """一条真实告警 + 目标 + 规则（不经过 seed_demo，自给自足）。"""

    target = Target(
        name='测试故障目标', url='https://fault.example.test',
        timeout=5, interval_seconds=60, enabled=True,
    )
    db.session.add(target)
    db.session.commit()

    rule = AlertRule(
        target_id=target.id, name='可用率告警',
        rule_type=AlertRule.RULE_AVAILABILITY, threshold=95.0,
    )
    db.session.add(rule)
    db.session.commit()

    alert = Alert(
        rule_id=rule.id, target_id=target.id,
        message='[测试故障目标] 可用率告警',
        observed_value=88.2, threshold=95.0, reason='timeout',
    )
    db.session.add(alert)
    db.session.commit()
    return alert


def _login(client, username, password):
    return client.post('/api/auth/login',
                       json={'username': username, 'password': password})


def _create_user(username, role_name):
    from auth import register_user
    register_user(username, 'pass123456', role_name)


# ============ 1. 未登录 → 401 ============
def test_analyze_requires_login(app, app_context, alert_fixture):
    r = app.test_client().post(f'/api/alerts/{alert_fixture.id}/analyze')
    assert r.status_code == 401


def test_get_analysis_requires_login(app, app_context, alert_fixture):
    r = app.test_client().get(f'/api/alerts/{alert_fixture.id}/analysis')
    assert r.status_code == 401


# ============ 2. 🔴 viewer → 403（重点）============
def test_viewer_cannot_analyze(app, app_context, alert_fixture):
    _create_user('t_viewer', ROLE_VIEWER)
    c = app.test_client()
    _login(c, 't_viewer', 'pass123456')

    r = c.post(f'/api/alerts/{alert_fixture.id}/analyze')

    assert r.status_code == 403
    assert r.get_json()['required_permission'] == 'analyze_incident'


def test_viewer_cannot_read_analysis(app, app_context, alert_fixture):
    _create_user('t_viewer2', ROLE_VIEWER)
    c = app.test_client()
    _login(c, 't_viewer2', 'pass123456')

    assert c.get(f'/api/alerts/{alert_fixture.id}/analysis').status_code == 403


def test_viewer_permission_matrix_excludes_analyze():
    """🔴 直接锁权限矩阵 —— 变异测试把 analyze_incident 加给 viewer 时，这条会失败。"""

    from auth import PERMISSIONS

    assert 'analyze_incident' not in PERMISSIONS[ROLE_VIEWER]
    assert 'analyze_incident' in PERMISSIONS[ROLE_OPERATOR]
    assert 'analyze_incident' in PERMISSIONS[ROLE_ADMIN]


# ============ 3. operator / admin → 200 ============
@pytest.mark.parametrize("username, role_name", [
    ('t_op', ROLE_OPERATOR),
    ('t_admin', ROLE_ADMIN),
])
def test_operator_and_admin_can_analyze(app, app_context, alert_fixture,
                                       username, role_name):
    _create_user(username, role_name)
    c = app.test_client()
    _login(c, username, 'pass123456')

    r = c.post(f'/api/alerts/{alert_fixture.id}/analyze')

    assert r.status_code == 200
    assert r.get_json()['answer']


# ============ 4. 🔴 无 key → 200 + degraded，不 500 ============
def test_analyze_without_api_key_degrades(app, app_context, alert_fixture, monkeypatch):
    """拔掉 key 必须 200 + degraded=True，而不是 500。"""

    import aiops.graph as graph_module
    monkeypatch.setattr(graph_module.config, 'DEEPSEEK_API_KEY', '')
    monkeypatch.setattr(graph_module.config, 'AI_ENABLED', False)

    _create_user('t_op2', ROLE_OPERATOR)
    c = app.test_client()
    _login(c, 't_op2', 'pass123456')

    r = c.post(f'/api/alerts/{alert_fixture.id}/analyze')

    assert r.status_code == 200
    body = r.get_json()
    assert body['degraded'] is True
    assert body['status'] == Analysis.STATUS_DEGRADED
    assert body['degradation_reasons']


def test_analyze_survives_graph_crash(app, app_context, alert_fixture, monkeypatch):
    """🔴 graph 内部整体崩溃时，仍然 200 + status=failed，不 500。"""

    import aiops.graph as graph_module

    def boom(_incident):
        raise RuntimeError('编排崩了')

    monkeypatch.setattr(graph_module, 'analyze_incident', boom)

    _create_user('t_op3', ROLE_OPERATOR)
    c = app.test_client()
    _login(c, 't_op3', 'pass123456')

    r = c.post(f'/api/alerts/{alert_fixture.id}/analyze')

    assert r.status_code == 200
    assert r.get_json()['status'] == Analysis.STATUS_FAILED
    assert '编排崩了' in r.get_json()['answer']


# ============ 5. 落库可回溯 ============
def test_analysis_is_persisted_and_readable(app, app_context, alert_fixture):
    _create_user('t_op4', ROLE_OPERATOR)
    c = app.test_client()
    _login(c, 't_op4', 'pass123456')

    posted = c.post(f'/api/alerts/{alert_fixture.id}/analyze').get_json()
    fetched = c.get(f'/api/alerts/{alert_fixture.id}/analysis').get_json()

    assert fetched['answer'] == posted['answer']
    assert fetched['status'] == posted['status']
    assert Analysis.query.count() == 1


def test_analysis_before_any_run_returns_404(app, app_context, alert_fixture):
    _create_user('t_op5', ROLE_OPERATOR)
    c = app.test_client()
    _login(c, 't_op5', 'pass123456')

    assert c.get(f'/api/alerts/{alert_fixture.id}/analysis').status_code == 404


def test_multiple_analyses_keep_latest(app, app_context, alert_fixture):
    _create_user('t_op6', ROLE_OPERATOR)
    c = app.test_client()
    _login(c, 't_op6', 'pass123456')

    c.post(f'/api/alerts/{alert_fixture.id}/analyze')
    latest = c.post(f'/api/alerts/{alert_fixture.id}/analyze').get_json()

    assert Analysis.query.count() == 2
    fetched = c.get(f'/api/alerts/{alert_fixture.id}/analysis').get_json()
    assert fetched['id'] == latest['id']


def test_deleting_alert_cascades_analysis(app, app_context, alert_fixture):
    """告警删了，分析记录不该留成孤儿。"""

    _create_user('t_op7', ROLE_OPERATOR)
    c = app.test_client()
    _login(c, 't_op7', 'pass123456')
    c.post(f'/api/alerts/{alert_fixture.id}/analyze')

    db.session.delete(db.session.get(Alert, alert_fixture.id))
    db.session.commit()

    assert Analysis.query.count() == 0


# ============ 6. 404 ============
def test_analyze_missing_alert_returns_404(app, app_context):
    _create_user('t_op8', ROLE_OPERATOR)
    c = app.test_client()
    _login(c, 't_op8', 'pass123456')

    assert c.post('/api/alerts/99999/analyze').status_code == 404


# ============ 7. /status 免登录页不受影响 ============
def test_status_page_still_public(app, app_context):
    """🔴 接 AI 不许动免登录状态页 —— 这条锁住它仍然 200。"""

    assert app.test_client().get('/status').status_code == 200
    assert app.test_client().get('/api/status').status_code == 200


def test_admin_alerts_page_requires_login(app, app_context):
    _create_user('t_op9', ROLE_OPERATOR)
    anon = app.test_client()
    assert anon.get('/admin/alerts').status_code == 401

    c = app.test_client()
    _login(c, 't_op9', 'pass123456')
    assert c.get('/admin/alerts').status_code == 200


def test_admin_alerts_page_hides_button_for_viewer(app, app_context):
    """viewer 看到的按钮应是禁用态 —— 但真正的拦截在后端。"""

    _create_user('t_viewer3', ROLE_VIEWER)
    c = app.test_client()
    _login(c, 't_viewer3', 'pass123456')

    body = c.get('/admin/alerts').get_data(as_text=True)

    assert 'btn-viewer' in body


# ============ 8. incident payload 字段对齐 ============
def test_incident_payload_uses_failure_reason_not_reason(app, app_context, alert_fixture):
    """🔴 graph 读的是 failure_reason —— 字段名错了模型会读到空值。"""

    from aiops_service import build_incident_payload

    payload = build_incident_payload(db.session.get(Alert, alert_fixture.id))

    assert 'failure_reason' in payload
    assert 'target_name' in payload
    assert 'url' in payload
    assert payload['target_name'] == '测试故障目标'