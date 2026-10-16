"""认证与权限测试 —— 重点是只读账号越权必须 403。"""

import pytest

from auth import (
    PERMISSIONS,
    authenticate,
    hash_password,
    register_user,
    verify_password,
)
from models import ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER, Role, Target, db, init_roles


def _create_user(username, password='pass123456', role_name=ROLE_VIEWER):
    init_roles()
    user, error = register_user(username, password, role_name)
    assert error is None, error
    return user


def _login(client, username, password='pass123456'):
    return client.post('/api/auth/login', json={
        'username': username,
        'password': password,
    })


# --- 密码 hash ---------------------------------------------------------------

def test_password_hash_is_not_plaintext():
    """补充指示明确要求贴实际值证明不是明文。"""

    stored = hash_password('my-secret-password')

    assert 'my-secret-password' not in stored
    assert stored.startswith('pbkdf2_sha256$')
    assert len(stored.split('$')) == 4


def test_same_password_different_salt_produces_different_hash():
    """hash 里带随机盐 → 同密码不同用户 hash 不同 → 彩虹表无效。"""

    first = hash_password('same-password', salt='salt-a')
    second = hash_password('same-password', salt='salt-b')

    assert first != second


def test_verify_password_accepts_correct():
    stored = hash_password('correct-password')

    assert verify_password('correct-password', stored) is True


def test_verify_password_rejects_wrong():
    stored = hash_password('correct-password')

    assert verify_password('wrong-password', stored) is False


@pytest.mark.parametrize('broken', ['', 'not-a-hash', 'a$b$c', None, 'x$y$z$w$extra'])
def test_verify_password_handles_malformed_hash(broken):
    """库里存了脏数据也不能抛异常 —— 返 False 即可。"""

    assert verify_password('anything', broken) is False


def test_registered_user_hash_differs_from_password(app, app_context):
    user = _create_user('alice')

    assert user.password_hash != 'pass123456'
    assert 'pass123456' not in user.password_hash


# --- 注册 ---------------------------------------------------------------------

def test_register_creates_viewer_by_default(app, app_context):
    response = _signup(app.test_client(), 'bob')

    assert response.status_code == 201
    assert response.get_json()['role'] == ROLE_VIEWER


def test_register_rejects_duplicate_username(app, app_context):
    _create_user('carol')
    init_roles()

    user, error = register_user('carol', 'pass123456', ROLE_VIEWER)

    assert user is None
    assert '已存在' in error


def test_register_rejects_short_password(app, app_context):
    init_roles()

    user, error = register_user('dave', '123')

    assert user is None
    assert '密码' in error


def test_register_rejects_short_username(app, app_context):
    init_roles()

    user, error = register_user('ab', 'pass123456', ROLE_VIEWER)

    assert user is None
    assert '用户名' in error


def test_register_cannot_self_assign_admin(app, app_context):
    """注册接口只能建 viewer，防止任何人注册成管理员。"""

    client = app.test_client()

    response = client.post('/api/auth/register', json={
        'username': 'evil',
        'password': 'pass123456',
        'role': ROLE_ADMIN,
    })

    assert response.status_code == 201
    assert response.get_json()['role'] == ROLE_VIEWER


# --- 登录 ---------------------------------------------------------------------

def test_login_with_valid_credentials(app, app_context):
    _create_user('erin')
    client = app.test_client()

    response = _login(client, 'erin')

    assert response.status_code == 200
    assert response.get_json()['username'] == 'erin'


def test_login_with_wrong_password_returns_401(app, app_context):
    _create_user('frank')
    client = app.test_client()

    response = _login(client, 'frank', 'wrong-password')

    assert response.status_code == 401


def test_login_unknown_user_returns_401(app, app_context):
    """不区分「用户不存在」与「密码错」→ 防止用户名枚举。"""

    client = app.test_client()

    response = _login(client, 'nobody')

    assert response.status_code == 401


def test_logout_clears_session(app, app_context):
    _create_user('grace')
    client = app.test_client()
    _login(client, 'grace')

    client.post('/api/auth/logout')
    response = client.get('/api/auth/me')

    assert response.status_code == 401


# --- 未登录 vs 越权 -----------------------------------------------------------

def test_anonymous_access_to_me_returns_401(app, app_context):
    response = app.test_client().get('/api/auth/me')

    assert response.status_code == 401


def test_anonymous_access_to_rule_detail_returns_401(app, app_context):
    """没登录 → 401（先问你是谁），不是 403。"""

    response = app.test_client().get('/api/rules/1')

    assert response.status_code == 401


def test_viewer_access_to_rule_detail_returns_403(app, app_context):
    """🔴 任务单硬要求：只读账号访问告警规则详情必须 403。"""

    target = Target(name='t', url='https://example.test', timeout=5, interval_seconds=60)
    db.session.add(target)
    db.session.commit()
    from models import AlertRule
    rule = AlertRule(target_id=target.id, name='r', threshold=1,
                     rule_type=AlertRule.RULE_CONSECUTIVE_FAILURE)
    db.session.add(rule)
    db.session.commit()

    _create_user('heidi', role_name=ROLE_VIEWER)
    client = app.test_client()
    _login(client, 'heidi')

    response = client.get(f'/api/rules/{rule.id}')

    assert response.status_code == 403
    assert '无权访问' in response.get_json()['error']


def test_viewer_cannot_list_rules(app, app_context):
    _create_user('ivan', role_name=ROLE_VIEWER)
    client = app.test_client()
    _login(client, 'ivan')

    response = client.get('/api/rules')

    assert response.status_code == 403


def test_viewer_cannot_create_rule(app, app_context):
    _create_user('judy', role_name=ROLE_VIEWER)
    client = app.test_client()
    _login(client, 'judy')

    response = client.post('/api/rules', json={
        'name': 'x', 'target_id': 1, 'rule_type': 'consecutive_failure', 'threshold': 1,
    })

    assert response.status_code == 403


def test_operator_can_read_rule_detail(app, app_context):
    target = Target(name='t', url='https://example.test', timeout=5, interval_seconds=60)
    db.session.add(target)
    db.session.commit()
    from models import AlertRule
    rule = AlertRule(target_id=target.id, name='r', threshold=1,
                     rule_type=AlertRule.RULE_CONSECUTIVE_FAILURE)
    db.session.add(rule)
    db.session.commit()

    _create_user('kate', role_name=ROLE_OPERATOR)
    client = app.test_client()
    _login(client, 'kate')

    response = client.get(f'/api/rules/{rule.id}')

    assert response.status_code == 200
    assert response.get_json()['name'] == 'r'


def test_admin_can_read_rule_detail(app, app_context):
    target = Target(name='t', url='https://example.test', timeout=5, interval_seconds=60)
    db.session.add(target)
    db.session.commit()
    from models import AlertRule
    rule = AlertRule(target_id=target.id, name='r', threshold=1,
                     rule_type=AlertRule.RULE_CONSECUTIVE_FAILURE)
    db.session.add(rule)
    db.session.commit()

    _create_user('liam', role_name=ROLE_ADMIN)
    client = app.test_client()
    _login(client, 'liam')

    assert client.get(f'/api/rules/{rule.id}').status_code == 200


def test_me_returns_permissions_list(app, app_context):
    _create_user('mia', role_name=ROLE_OPERATOR)
    client = app.test_client()
    _login(client, 'mia')

    body = client.get('/api/auth/me').get_json()

    assert body['role'] == ROLE_OPERATOR
    assert 'manage_rules' in body['permissions']


# --- 权限矩阵本身 -------------------------------------------------------------

def test_viewer_permissions_exclude_view_rules():
    """权限矩阵里viewer 不得含 view_rules —— 这是 403 的根。"""

    assert 'view_rules' not in PERMISSIONS[ROLE_VIEWER]


def test_operator_cannot_manage_users():
    """运营角色不该能管用户（那是管理员的事）。"""

    assert 'manage_users' not in PERMISSIONS[ROLE_OPERATOR]
    assert 'manage_users' in PERMISSIONS[ROLE_ADMIN]


def test_all_roles_can_view_status():
    for role in (ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER):
        assert 'view_status' in PERMISSIONS[role]


# --- 状态页免登录 ---------------------------------------------------------------

def test_status_page_requires_no_login(app, app_context):
    """任务单硬要求：状态页不需要登录。"""

    response = app.test_client().get('/status')

    assert response.status_code == 200


def test_status_api_requires_no_login(app, app_context):
    assert app.test_client().get('/api/status').status_code == 200


def test_status_page_shows_targets(app, app_context):
    target = Target(name='公开服务', url='https://example.test', timeout=5,
                    interval_seconds=60)
    db.session.add(target)
    db.session.commit()

    response = app.test_client().get('/status')

    assert '公开服务' in response.get_data(as_text=True)


# --- 报表 ---------------------------------------------------------------------

def test_report_requires_login(app, app_context):
    assert app.test_client().get('/api/reports/export').status_code == 401


def test_report_allowed_for_viewer(app, app_context):
    _create_user('nina', role_name=ROLE_VIEWER)
    client = app.test_client()
    _login(client, 'nina')

    response = client.get('/api/reports/export')

    # 无数据时是 404（不是 403）—— 证明权限校验已通过
    assert response.status_code == 404


def test_report_rejects_unknown_dimension(app, app_context):
    _create_user('oscar', role_name=ROLE_VIEWER)
    client = app.test_client()
    _login(client, 'oscar')

    response = client.get('/api/reports/export?dimension=unknown')

    assert response.status_code == 400


def test_report_returns_csv(app, app_context):
    from datetime import datetime

    from models import CheckRecord

    _create_user('peggy', role_name=ROLE_VIEWER)
    target = Target(name='报告目标', url='https://example.test', timeout=5,
                    interval_seconds=60)
    db.session.add(target)
    db.session.commit()
    for is_success in (True, False):
        db.session.add(CheckRecord(
            target_id=target.id, status_code=200 if is_success else 500,
            duration_ms=50.0, is_success=is_success,
            error_msg=None if is_success else '请求超时: slow',
            checked_at=datetime.now(),
        ))
    db.session.commit()

    client = app.test_client()
    _login(client, 'peggy')
    response = client.get('/api/reports/export?dimension=target')

    assert response.status_code == 200
    assert 'text/csv' in response.headers['Content-Type']
    body = response.get_data(as_text=True)
    assert 'target_name' in body
    assert '报告目标' in body


def test_daily_report_returns_csv(app, app_context):
    from datetime import datetime

    from models import CheckRecord

    _create_user('quinn', role_name=ROLE_VIEWER)
    target = Target(name='日维度', url='https://example.test', timeout=5,
                    interval_seconds=60)
    db.session.add(target)
    db.session.commit()
    db.session.add(CheckRecord(
        target_id=target.id, status_code=200, duration_ms=10.0,
        is_success=True, checked_at=datetime.now(),
    ))
    db.session.commit()

    client = app.test_client()
    _login(client, 'quinn')
    response = client.get('/api/reports/export?dimension=daily')

    assert response.status_code == 200
    assert 'availability_pct' in response.get_data(as_text=True)


def _signup(client, username):
    return client.post('/api/auth/register', json={
        'username': username,
        'password': 'pass123456',
    })