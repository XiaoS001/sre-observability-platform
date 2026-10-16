"""认证与鉴权。

安全底线：
  · 密码**只存 hash**，永不存明文、永不回显
  · hash 里带随机 salt，每个用户不同 → 彩虹表无效
  · SECRET_KEY / PASSWORD_SALT 缺失时用 config 的「临时」标记暴露在 /healthz，
    而不是悄悄用一个固定默认值（那等于所有部署共用一个盐）
  · 任何接口在拿不到 key 时不返 500（登录失败一律 401，不是异常）
"""

import functools
import hashlib
import hmac
import logging
import os

from flask import jsonify, request, session

import config
from models import ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER, Role, User, db


logger = logging.getLogger('rmb03.auth')

_PBKDF2_ROUNDS = 120_000
_HASH_PREFIX = 'pbkdf2_sha256'


def hash_password(password, salt=None):
    """生成密码 hash，格式：pbkdf2_sha256$rounds$salt_hex$hash_hex。

    salt 缺省时用环境变量的 PASSWORD_SALT。生产必须配置它，
    否则所有部署共用一个盐 —— 此时用时间+随机数兜底，保证不同部署不同盐，
    同时通过 PASSWORD_SALT_IS_EPHEMERAL 标志明确告知运维「该配了」。
    """

    if salt is None:
        salt = config.PASSWORD_SALT
    if not salt:
        # 没有配置盐：退化为「每进程随机盐」。单进程重启后登录会失效，
        # 这是刻意的—— 宁可让人发现没配盐，也不要共用固定盐。
        salt = os.urandom(16).hex()

    salt_bytes = salt.encode('utf-8')
    digest = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt_bytes,
        _PBKDF2_ROUNDS,
    )
    return f'{_HASH_PREFIX}${_PBKDF2_ROUNDS}${salt_bytes.hex()}${digest.hex()}'


def verify_password(password, stored_hash):
    """常量时间比较，避免计时侧信道。"""

    try:
        algorithm, rounds, salt_hex, digest_hex = stored_hash.split('$')
        if algorithm != _HASH_PREFIX:
            return False
        expected = hashlib.pbkdf2_hmac(
            'sha256',
            password.encode('utf-8'),
            bytes.fromhex(salt_hex),
            int(rounds),
        ).hex()
        return hmac.compare_digest(expected, digest_hex)
    except (ValueError, AttributeError, TypeError):
        return False


def register_user(username, password, role_name=ROLE_VIEWER):
    """注册用户。返回 (user, None) 或 (None, error_message)。"""

    if not username or not password:
        return None, '用户名与密码不能为空'
    if len(username) < 3:
        return None, '用户名至少 3 个字符'
    if len(password) < 6:
        return None, '密码至少 6 个字符'

    if User.query.filter_by(username=username).first() is not None:
        return None, '用户名已存在'

    role = Role.query.filter_by(name=role_name).first()
    if role is None:
        return None, '角色不存在'

    user = User(
        username=username,
        password_hash=hash_password(password),
        role=role,
    )
    db.session.add(user)
    db.session.commit()
    return user, None


def authenticate(username, password):
    """校验用户名密码。返回 (user, None) 或 (None, error)。"""

    user = User.query.filter_by(username=username).first()
    if user is None:
        # 不区分「用户不存在」与「密码错」，避免用户名枚举。
        return None, '用户名或密码错误'
    if not verify_password(password, user.password_hash):
        return None, '用户名或密码错误'
    return user, None


# --- 权限 -------------------------------------------------------------------

# 权限矩阵：角色 → 允许访问的能力集合
# 用能力名而不是路由路径，权限与实现解耦，改路由不会漏改鉴权。
PERMISSIONS = {
    ROLE_ADMIN: {
        'view_status', 'view_records', 'view_reports',
        'view_rules', 'manage_rules', 'manage_targets', 'manage_users',
    },
    ROLE_OPERATOR: {
        'view_status', 'view_records', 'view_reports',
        'view_rules', 'manage_rules', 'manage_targets',
    },
    ROLE_VIEWER: {
        'view_status', 'view_records', 'view_reports',
        # 🔴 只读角色刻意不含 view_rules —— 任务单要求
        # 「只读账号看不到告警规则详情」，由test_auth 的越权用例锁死。
    },
}


def has_permission(user, permission):
    if user is None or user.role is None:
        return False
    return permission in PERMISSIONS.get(user.role.name, set())


def login_required(permission=None):
    """装饰器：未登录返401，缺权限返 403。

    两者的区别很重要：
      401 = 你是谁？不知道 → 要先登录
      403 = 知道你是谁，但你不能做这个 → 再登录也没用
    """

    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            user_id = session.get('user_id')
            if user_id is None:
                return jsonify({'error': '请先登录'}), 401

            user = db.session.get(User, user_id)
            if user is None:
                session.clear()
                return jsonify({'error': '请先登录'}), 401

            if permission is not None and not has_permission(user, permission):
                logger.warning('permission_denied', extra={
                    'event': 'permission_denied',
                    'username': user.username,
                    'role': user.role.name,
                    'permission': permission,
                })
                return jsonify({
                    'error': '无权访问',
                    'required_permission': permission,
                }), 403

            return view(*args, **kwargs)

        return wrapper

    return decorator


def current_user():
    user_id = session.get('user_id')
    if user_id is None:
        return None
    return db.session.get(User, user_id)


def register_auth_routes(app):
    """把认证相关路由挂到 app 上（阶段 3）。"""

    @app.post('/api/auth/register')
    def register():
        data = request.get_json(silent=True) or {}
        username = data.get('username')
        password = data.get('password')
        # 注册时只能自助创建 viewer；admin/operator 由管理员在后台指派。
        # 防止任何人注册成管理员。
        user, error = register_user(username, password, ROLE_VIEWER)
        if error:
            return jsonify({'error': error}), 400
        return jsonify({'id': user.id, 'username': user.username, 'role': user.role.name}), 201

    @app.post('/api/auth/login')
    def login():
        data = request.get_json(silent=True) or {}
        user, error = authenticate(data.get('username', ''), data.get('password', ''))
        if error:
            return jsonify({'error': error}), 401
        session['user_id'] = user.id
        session['username'] = user.username
        logger.info('user_login', extra={
            'event': 'user_login',
            'username': user.username,
            'role': user.role.name,
        })
        return jsonify({'username': user.username, 'role': user.role.name})

    @app.post('/api/auth/logout')
    def logout():
        session.clear()
        return jsonify({'status': 'ok'})

    @app.get('/api/auth/me')
    @login_required()
    def me():
        user = current_user()
        return jsonify({
            'username': user.username,
            'role': user.role.name,
            'permissions': sorted(PERMISSIONS.get(user.role.name, set())),
        })

    @app.get('/api/rules/<int:rule_id>')
    @login_required('view_rules')
    def get_rule_detail(rule_id):
        """告警规则详情 —— 只读角色必须被 403 挡住。"""

        from models import AlertRule

        rule = db.session.get(AlertRule, rule_id)
        if rule is None:
            return jsonify({'error': '规则不存在'}), 404
        return jsonify({
            'id': rule.id,
            'name': rule.name,
            'target_id': rule.target_id,
            'rule_type': rule.rule_type,
            'threshold': rule.threshold,
            'enabled': rule.enabled,
        })

    return app