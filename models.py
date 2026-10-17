"""数据库模型定义。

结构照抄 XM01（MonitorTarget + CheckResult）但按 SRE 平台需要扩展为 7 张表。
来源与改动的对应关系写在 README「来源与复用关系」一节。

🔴 本项目的库**永远是 data/rmb03.db 自己新建的**：
绝不复制、绝不写入 XM01 的 app.db，也不复用 XM05 的 xm01_copy.db。

七张表的职责边界：
    Target        监控目标（巡检什么）
    CheckRecord   巡检记录（巡检结果，告警规则的取数来源）
    AlertRule     告警规则（阈值配置）
    Alert         触发的告警（实例）
    Notification  通知投递记录（发没发出去）
    Role          角色字典（管理员 / 运维 / 只读）
    User          用户（关联一个角色）
"""

from datetime import datetime

from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


# --- 角色取值 ---------------------------------------------------------------
# 角色存字符串而不是自增 id：角色是固定的业务枚举，写死在代码里比建表更直观，
# 也避免「库被清空后角色 id 漂移」这类问题。阶段 3 的鉴权按这三个值判断。
ROLE_ADMIN = 'admin'
ROLE_OPERATOR = 'operator'
ROLE_VIEWER = 'viewer'
ALL_ROLES = (ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER)


class Target(db.Model):
    """监控目标。"""

    __tablename__ = 'target'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    timeout = db.Column(db.Integer, default=5, nullable=False)
    # 巡检间隔（秒）。调度器按每个目标各自的间隔决定是否该跑本轮，
    # 所以间隔必须挂在目标上而不是全局一个值。
    interval_seconds = db.Column(db.Integer, default=60, nullable=False)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    records = db.relationship(
        'CheckRecord',
        back_populates='target',
        cascade='all, delete-orphan',
        lazy='select',
    )
    rules = db.relationship(
        'AlertRule',
        back_populates='target',
        cascade='all, delete-orphan',
        lazy='select',
    )

    def __repr__(self):
        return f'<Target {self.id} {self.name}>'


class CheckRecord(db.Model):
    """巡检记录。失败分类沿用 XM01 的四分类（由 metrics._classify_failure 派生）。"""

    __tablename__ = 'check_record'

    id = db.Column(db.Integer, primary_key=True)
    target_id = db.Column(
        db.Integer,
        db.ForeignKey('target.id'),
        nullable=False,
        index=True,
    )
    status_code = db.Column(db.Integer, nullable=True)
    duration_ms = db.Column(db.Float, nullable=False)
    is_success = db.Column(db.Boolean, default=False, nullable=False)
    error_msg = db.Column(db.String(500), nullable=True)
    checked_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)

    target = db.relationship('Target', back_populates='records')


class AlertRule(db.Model):
    """告警规则。

    rule_type 取值：
        consecutive_failure  连续失败 N 次
        availability         可用率低于 X%
        p95_latency         P95 响应超 Y ms

    threshold 的单位随 rule_type 变化（次数 / 百分比 / 毫秒），
    这样三种规则能共用一张表，也共用同一套求值入口。
    """

    __tablename__ = 'alert_rule'

    RULE_CONSECUTIVE_FAILURE = 'consecutive_failure'
    RULE_AVAILABILITY = 'availability'
    RULE_P95_LATENCY = 'p95_latency'
    ALL_RULE_TYPES = (
        RULE_CONSECUTIVE_FAILURE,
        RULE_AVAILABILITY,
        RULE_P95_LATENCY,
    )

    id = db.Column(db.Integer, primary_key=True)
    target_id = db.Column(
        db.Integer,
        db.ForeignKey('target.id'),
        nullable=False,
        index=True,
    )
    name = db.Column(db.String(200), nullable=False)
    rule_type = db.Column(db.String(50), nullable=False)
    threshold = db.Column(db.Float, nullable=False)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    target = db.relationship('Target', back_populates='rules')
    alerts = db.relationship(
        'Alert',
        back_populates='rule',
        cascade='all, delete-orphan',
        lazy='select',
    )


class Alert(db.Model):
    """一次触发的告警实例。

    冷却期去重靠的是「同 rule_id + target_id 在冷却窗口内不重复插入」，
    具体求值在阶段 3 的 alerts.py。
    """

    __tablename__ = 'alert'

    id = db.Column(db.Integer, primary_key=True)
    rule_id = db.Column(
        db.Integer,
        db.ForeignKey('alert_rule.id'),
        nullable=False,
        index=True,
    )
    target_id = db.Column(
        db.Integer,
        db.ForeignKey('target.id'),
        nullable=False,
        index=True,
    )
    message = db.Column(db.String(500), nullable=False)
    # 触发时的实测值与阈值，写进历史便于事后复盘
    observed_value = db.Column(db.Float, nullable=True)
    threshold = db.Column(db.Float, nullable=True)
    # 触发时归一化出来的失败分类（timeout/connect/exception/bad_status）
    reason = db.Column(db.String(50), nullable=True)
    triggered_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    resolved_at = db.Column(db.DateTime, nullable=True)

    rule = db.relationship('AlertRule', back_populates='alerts')
    target = db.relationship('Target')
    notifications = db.relationship(
        'Notification',
        back_populates='alert',
        cascade='all, delete-orphan',
        lazy='select',
    )
    analyses = db.relationship(
        'Analysis',
        back_populates='alert',
        cascade='all, delete-orphan',
        lazy='select',
    )


class Notification(db.Model):
    """通知投递记录（一条告警 × 一个渠道 = 一条）。"""

    __tablename__ = 'notification'

    CHANNEL_WEBHOOK = 'webhook'
    CHANNEL_EMAIL = 'email'
    ALL_CHANNELS = (CHANNEL_WEBHOOK, CHANNEL_EMAIL)

    id = db.Column(db.Integer, primary_key=True)
    alert_id = db.Column(
        db.Integer,
        db.ForeignKey('alert.id'),
        nullable=False,
        index=True,
    )
    channel = db.Column(db.String(50), nullable=False)
    target_url = db.Column(db.String(500), nullable=True)
    success = db.Column(db.Boolean, default=False, nullable=False)
    status_code = db.Column(db.Integer, nullable=True)
    error_msg = db.Column(db.String(500), nullable=True)
    sent_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    alert = db.relationship('Alert', back_populates='notifications')


class Analysis(db.Model):
    """AI 根因分析结果（阶段 6）。

    🔴 为什么要落库：分析结果必须可回溯。否则刷新页面就没了，
    演示不成立、也无法事后复盘「当时给出的根因是什么」。

    字段对应 aiops.graph.analyze_incident() 的返回：
        status              ok / degraded / failed
        answer              根因分析 + 处置建议正文
        citations           答案里引用到的证据 id（应都能对上真实记录）
        invalid_citations   校验不通过、被判为编造的引用
        degraded            是否走了降级路径（无 key / 模型失败 / 超时）
        degradation_reasons 降级原因清单
        tool_calls          本次调用了哪些工具、各自返回什么
    """

    __tablename__ = 'analysis'

    STATUS_OK = 'ok'
    STATUS_DEGRADED = 'degraded'
    STATUS_FAILED = 'failed'
    ALL_STATUSES = (STATUS_OK, STATUS_DEGRADED, STATUS_FAILED)

    id = db.Column(db.Integer, primary_key=True)
    alert_id = db.Column(
        db.Integer,
        db.ForeignKey('alert.id'),
        nullable=False,
        index=True,
    )
    status = db.Column(db.String(20), nullable=False, default=STATUS_OK)
    answer = db.Column(db.Text, nullable=False, default='')
    # 用 Text 存 JSON 字符串而不是 JSON 列：
    # SQLite 的 JSON 依赖 SQLAlchemy 的序列化器，跨库行为不一致；
    # 显式 json.dumps/loads 更可控，也方便直接在 sqlite3 CLI 里看。
    citations_json = db.Column(db.Text, nullable=True)
    invalid_citations_json = db.Column(db.Text, nullable=True)
    degraded = db.Column(db.Boolean, default=False, nullable=False)
    degradation_reasons_json = db.Column(db.Text, nullable=True)
    tool_calls_json = db.Column(db.Text, nullable=True)
    evidence_count = db.Column(db.Integer, default=0, nullable=False)
    rounds = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)

    alert = db.relationship('Alert', back_populates='analyses')


class Role(db.Model):
    """角色字典。

    阶段 2 只落表结构与三条内置角色数据；注册/登录/越权在阶段 3 做。
    """

    __tablename__ = 'role'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)
    description = db.Column(db.String(200), nullable=False)

    users = db.relationship('User', back_populates='role', lazy='select')


class User(db.Model):
    """用户。密码只存 hash，绝不存明文。"""

    __tablename__ = 'user'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role_id = db.Column(
        db.Integer,
        db.ForeignKey('role.id'),
        nullable=False,
    )
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    role = db.relationship('Role', back_populates='users')

    def __repr__(self):
        return f'<User {self.id} {self.username}>'


def init_roles():
    """写入三条内置角色。幂等：已存在则不重复插入。"""

    defaults = [
        (ROLE_ADMIN, '管理员：全部权限，含告警规则详情与用户管理'),
        (ROLE_OPERATOR, '运维：可增删监控目标、触发巡检、查看告警规则'),
        (ROLE_VIEWER, '只读：只看状态页与报表，不可查看告警规则详情'),
    ]
    existing = {row[0] for row in db.session.query(Role.name).all()}
    for name, description in defaults:
        if name not in existing:
            db.session.add(Role(name=name, description=description))
    db.session.commit()