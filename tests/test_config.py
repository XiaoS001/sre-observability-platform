"""配置测试 —— 重点是「密钥只走环境变量」这条底线。"""

import importlib

import pytest

import config


def _reload_config(env_overrides):
    """改环境变量后重载 config，模拟进程重启读取新配置。"""

    for key, value in env_overrides.items():
        if value is None:
            import os
            os.environ.pop(key, None)
        else:
            import os
            os.environ[key] = value
    return importlib.reload(config)


def teardown_function():
    import os

    for key in ('RMB03_SECRET_KEY', 'RMB03_PASSWORD_SALT', 'RMB03_LOKI_URL'):
        os.environ.pop(key, None)
    importlib.reload(config)


def test_default_database_url_points_to_data_dir():
    assert config.DATABASE_URL.startswith('sqlite:///')
    assert 'data' in config.DATABASE_URL


def test_deepseek_key_reads_from_environment():
    reloaded = _reload_config({'DEEPSEEK_API_KEY': 'test-key-not-real'})

    assert reloaded.DEEPSEEK_API_KEY == 'test-key-not-real'
    assert reloaded.snapshot()['ai_key_present'] is True


def test_snapshot_never_returns_key_value():
    """快照只暴露「有没有」，绝不暴露「是什么」。"""

    reloaded = _reload_config({'DEEPSEEK_API_KEY': 'test-key-not-real'})

    snapshot = reloaded.snapshot()

    assert 'test-key-not-real' not in str(snapshot)
    assert set(snapshot) >= {'ai_enabled', 'ai_key_present'}


def test_secret_key_defaults_to_ephemeral():
    reloaded = _reload_config({'RMB03_SECRET_KEY': None})

    assert reloaded.SECRET_KEY == ''
    assert reloaded.SECRET_KEY_IS_EPHEMERAL is True
    assert reloaded.snapshot()['secret_key_ephemeral'] is True


def test_password_salt_defaults_to_ephemeral_not_hardcoded():
    """审查意见第 3 项：盐不允许有硬编码默认值。

    忘了配环境变量时，所有部署会共用一个默认盐 → 全站密码 hash 可被一把撞开。
    所以这里断言「默认为空 + 显式标记临时」，而不是悄悄给个开发默认值。
    """

    reloaded = _reload_config({'RMB03_PASSWORD_SALT': None})

    assert reloaded.PASSWORD_SALT == ''
    assert reloaded.PASSWORD_SALT_IS_EPHEMERAL is True
    assert reloaded.snapshot()['password_salt_ephemeral'] is True


def test_configured_secrets_are_not_ephemeral():
    reloaded = _reload_config({
        'RMB03_SECRET_KEY': 'a-real-secret',
        'RMB03_PASSWORD_SALT': 'a-real-salt',
    })

    assert reloaded.SECRET_KEY_IS_EPHEMERAL is False
    assert reloaded.PASSWORD_SALT_IS_EPHEMERAL is False


def test_blank_env_value_treated_as_unset():
    """设了但留空（CI 里常见）应当等同于没设，而不是拿空串当密钥。"""

    reloaded = _reload_config({'RMB03_SECRET_KEY': '   '})

    assert reloaded.SECRET_KEY == ''
    assert reloaded.SECRET_KEY_IS_EPHEMERAL is True


@pytest.mark.parametrize('value, expected', [
    ('true', True),
    ('1', True),
    ('yes', True),
    ('on', True),
    ('false', False),
    ('0', False),
    ('no', False),
])
def test_bool_env_parsing(value, expected):
    import os

    os.environ['RMB03_SCHEDULER_ENABLED'] = value
    reloaded = importlib.reload(config)

    assert reloaded.SCHEDULER_ENABLED is expected


def test_invalid_int_env_falls_back_to_default():
    """环境变量填了非法值时退回默认，而不是启动失败。"""

    import os

    os.environ['RMB03_PORT'] = 'not-a-number'
    reloaded = importlib.reload(config)
    os.environ.pop('RMB03_PORT', None)

    assert reloaded.APP_PORT == 5000


def test_timezone_default_is_shanghai():
    assert config.TIMEZONE == 'Asia/Shanghai'