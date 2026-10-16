"""SRE 监控平台配置 —— 全部环境变量，密钥不落代码。

读取规则：
  1. 先读环境变量；
  2. 环境变量缺失或非法时退回默认值，**不抛异常** —— 平台必须能在
     「没配任何环境变量」的情况下启动（测试与本机演示都走这条路径）；
  3. 密码哈希盐、模型 key 一律只从环境变量取，取不到就不启用对应功能。
"""

import os


def _env(name, default=''):
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_int(name, default):
    try:
        return int(_env(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name, default=False):
    value = _env(name, '').lower()
    if not value:
        return default
    return value in ('1', 'true', 'yes', 'on')


def _env_float(name, default):
    try:
        return float(_env(name, default))
    except (TypeError, ValueError):
        return default


# --- 应用 ---
APP_HOST = _env('RMB03_HOST', '127.0.0.1')
APP_PORT = _env_int('RMB03_PORT', 5000)
LOG_LEVEL = _env('RMB03_LOG_LEVEL', 'INFO').upper()

# --- 数据库 ---
# 缺省落在 data/ 下，data/ 已在 .gitignore 中，镜像里也不会打进去。
DATA_DIR = _env('RMB03_DATA_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))
DATABASE_URL = _env('RMB03_DATABASE_URL', 'sqlite:///' + os.path.join(DATA_DIR, 'rmb03.db'))

# --- 会话与鉴权 ---
SECRET_KEY = _env('RMB03_SECRET_KEY', '')
SECRET_KEY_IS_EPHEMERAL = not SECRET_KEY
# 密码哈希盐。同样只从环境变量取：给硬编码默认值会导致「所有部署共用同一个盐」，
# 一旦漏配环境变量，所有用户的密码 hash 都能被同一个字典一把撞开。
# 所以这里默认空 + 显式标志，而不是悄悄用开发默认值。
PASSWORD_SALT = _env('RMB03_PASSWORD_SALT', '')
PASSWORD_SALT_IS_EPHEMERAL = not PASSWORD_SALT

# --- 调度器 ---
SCHEDULER_ENABLED = _env_bool('RMB03_SCHEDULER_ENABLED', False)
SCHEDULER_TICK_SECONDS = _env_int('RMB03_SCHEDULER_TICK_SECONDS', 10)
TIMEZONE = _env('RMB03_TIMEZONE', 'Asia/Shanghai')

# --- 告警 ---
ALERT_COOLDOWN_SECONDS = _env_int('RMB03_ALERT_COOLDOWN_SECONDS', 300)
ALERT_WEBHOOK_URL = _env('RMB03_ALERT_WEBHOOK_URL', '')
ALERT_WEBHOOK_TIMEOUT = _env_float('RMB03_ALERT_WEBHOOK_TIMEOUT', 2.0)
ALERT_EMAIL_ENABLED = _env_bool('RMB03_ALERT_EMAIL_ENABLED', False)
ALERT_EMAIL_TO = _env('RMB03_ALERT_EMAIL_TO', '')

# --- 三支柱（可观测性自监控）---
LOKI_URL = _env('RMB03_LOKI_URL', '')
LOKI_BATCH_SIZE = _env_int('RMB03_LOKI_BATCH_SIZE', 100)
LOKI_FLUSH_INTERVAL = _env_float('RMB03_LOKI_FLUSH_INTERVAL', 1.0)
LOKI_TIMEOUT = _env_float('RMB03_LOKI_TIMEOUT', 0.8)
OTLP_ENDPOINT = _env('RMB03_OTLP_ENDPOINT', '')

# --- AI 根因分析（key 只走环境变量；取不到就退回规则路径）---
DEEPSEEK_API_KEY = _env('DEEPSEEK_API_KEY', '')
AI_ENABLED = _env_bool('RMB03_AI_ENABLED', True)
AI_MODEL = _env('RMB03_AI_MODEL', 'deepseek-chat')
AI_BASE_URL = _env('RMB03_AI_BASE_URL', 'https://api.deepseek.com')


def snapshot():
    """返回当前配置的安全视图，用于 /healthz 自监控与启动日志。

    只暴露「开没开 / 指向什么驱动」，**绝不回显任何密钥**。
    """

    return {
        'host': APP_HOST,
        'port': APP_PORT,
        'log_level': LOG_LEVEL,
        'database_driver': DATABASE_URL.split(':', 1)[0],
        'secret_key_ephemeral': SECRET_KEY_IS_EPHEMERAL,
        'password_salt_ephemeral': PASSWORD_SALT_IS_EPHEMERAL,
        'scheduler_enabled': SCHEDULER_ENABLED,
        'loki_enabled': bool(LOKI_URL),
        'otlp_enabled': bool(OTLP_ENDPOINT),
        'ai_enabled': AI_ENABLED,
        'ai_key_present': bool(DEEPSEEK_API_KEY),
        'alert_webhook_enabled': bool(ALERT_WEBHOOK_URL),
    }