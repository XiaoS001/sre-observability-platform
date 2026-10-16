"""Flask 应用入口。

阶段 2 提供的路由：
    GET  /healthz                健康检查（Docker HEALTHCHECK 打这里）
    GET  /metrics                Prometheus 指标
    POST /api/targets            新增监控目标
    GET  /api/targets            目标列表
    POST /api/targets/<id>/check 手动触发一次巡检
    GET  /api/records            巡检记录（近期）

阶段 3 补：/status 状态页、/api/reports/export 报表、用户体系与鉴权。
"""

import logging

from flask import Flask, jsonify, request
from sqlalchemy import func

import config
from alerts import run_alert_cycle
from auth import login_required, register_auth_routes
from checker import check_and_save
from logging_setup import setup_logging
from metrics import registry as prometheus_registry
from models import Alert, AlertRule, CheckRecord, Target, db, init_roles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from reports import daily_report, status_page_data, target_report, to_csv
from tracing_setup import setup_tracing


setup_logging()
logger = logging.getLogger('rmb03.app')


def create_app(init_db=True):
    """应用工厂。

    init_db=False 时**不建表** —— pytest 用它构造一个空壳 app，
    由conftest 自己掌控schema 的建/清时机，从而保证测试不会碰到
    data/rmb03.db（真实业务库）。
    """

    app = Flask(__name__, template_folder='templates', static_folder='static')
    app.config['SQLALCHEMY_DATABASE_URI'] = config.DATABASE_URL
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = config.SECRET_KEY or 'rmb03-ephemeral-dev-key'

    db.init_app(app)
    _register_routes(app)
    register_auth_routes(app)
    setup_tracing(app)

    if init_db:
        with app.app_context():
            db.create_all()
            init_roles()

    return app


def _register_routes(app):
    @app.get('/healthz')
    def healthz():
        """健康检查端点。

        Docker HEALTHCHECK 打这里：不触发任何巡检，纯读配置，不写业务库。
        """

        return jsonify({
            'status': 'ok',
            'service': 'rmb03',
            'config': config.snapshot(),
        })

    @app.get('/metrics')
    def prometheus_metrics():
        from flask import Response

        return Response(
            generate_latest(prometheus_registry),
            content_type=CONTENT_TYPE_LATEST,
        )

    @app.post('/api/targets')
    def create_target():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': '缺少参数'}), 400

        name = data.get('name')
        url = data.get('url')
        if not name or not url:
            return jsonify({'error': '缺少参数'}), 400

        try:
            timeout = int(data.get('timeout', 5))
            interval_seconds = int(data.get('interval_seconds', 60))
        except (TypeError, ValueError):
            return jsonify({'error': 'timeout/interval_seconds 必须是整数'}), 400

        # 边界：timeout 与 interval 都必须为正，否则调度器语义会变得很奇怪
        #（interval<=0 会被当成「总是到点」，等于变相全速巡检）。
        if timeout <= 0 or interval_seconds <= 0:
            return jsonify({'error': 'timeout 与 interval_seconds 必须大于 0'}), 400

        target = Target(
            name=name,
            url=url,
            timeout=timeout,
            interval_seconds=interval_seconds,
            enabled=bool(data.get('enabled', True)),
        )
        db.session.add(target)
        db.session.commit()
        return jsonify(_target_json(target)), 201

    @app.get('/api/targets')
    def list_targets():
        targets = Target.query.order_by(Target.id).all()
        return jsonify([_target_json(t) for t in targets])

    @app.post('/api/targets/<int:target_id>/check')
    def check_one(target_id):
        target = db.session.get(Target, target_id)
        if target is None:
            return jsonify({'error': '目标不存在'}), 404

        record = check_and_save(target)
        return jsonify(_record_json(record))

    @app.get('/api/records')
    def list_records():
        limit = request.args.get('limit', default=20, type=int)
        limit = max(1, min(limit, 200))

        rows = (
            db.session.query(CheckRecord)
            .order_by(CheckRecord.checked_at.desc(), CheckRecord.id.desc())
            .limit(limit)
            .all()
        )
        return jsonify([_record_json(r) for r in rows])

    @app.get('/api/stats')
    def stats():
        total = db.session.query(func.count(CheckRecord.id)).scalar() or 0
        success = (
            db.session.query(func.count(CheckRecord.id))
            .filter(CheckRecord.is_success.is_(True))
            .scalar()
            or 0
        )
        availability = round(success / total * 100, 1) if total else 0
        return jsonify({
            'total': total,
            'success': success,
            'availability': availability,
            'targets': Target.query.count(),
        })

    # --- 状态页（免登录）----------------------------------------------------
    # 状态页是给外部看的，所以刻意不加 login_required：
    # 任务单「状态页不需要登录」，它只暴露可用率与告警文案，不含规则详情。

    @app.get('/status')
    def status_page():
        from flask import render_template

        return render_template('status.html', **status_page_data())

    @app.get('/api/status')
    def status_data():
        return jsonify(status_page_data())

    # --- 报表（需登录）------------------------------------------------------

    @app.get('/api/reports/export')
    @login_required('view_reports')
    def export_report():
        from flask import Response

        dimension = request.args.get('dimension', default='target')
        days = request.args.get('days', default=30, type=int)
        days = max(1, min(days, 365))

        if dimension == 'daily':
            rows = daily_report(days=days)
        elif dimension == 'target':
            rows = target_report(days=days)
        else:
            return jsonify({'error': 'dimension 只能是 target 或 daily'}), 400

        if not rows:
            return jsonify({'error': '窗口内没有数据'}), 404

        filename = f'rmb03-report-{dimension}-{days}d.csv'
        return Response(
            to_csv(rows),
            content_type='text/csv; charset=utf-8-sig',
            headers={'Content-Disposition': f'attachment; filename={filename}'},
        )

    # --- 告警规则（需登录 + 权限）-------------------------------------------

    @app.get('/api/rules')
    @login_required('view_rules')
    def list_rules():
        rows = AlertRule.query.order_by(AlertRule.id).all()
        return jsonify([
            {
                'id': r.id,
                'name': r.name,
                'target_id': r.target_id,
                'rule_type': r.rule_type,
                'threshold': r.threshold,
                'enabled': r.enabled,
            }
            for r in rows
        ])

    @app.post('/api/rules')
    @login_required('manage_rules')
    def create_rule():
        data = request.get_json(silent=True) or {}
        target_id = data.get('target_id')
        name = data.get('name')
        rule_type = data.get('rule_type')
        threshold = data.get('threshold')

        if not name or target_id is None or threshold is None:
            return jsonify({'error': '缺少参数'}), 400
        if rule_type not in AlertRule.ALL_RULE_TYPES:
            return jsonify({
                'error': 'rule_type 必须是 ' + ', '.join(AlertRule.ALL_RULE_TYPES),
            }), 400
        if db.session.get(Target, target_id) is None:
            return jsonify({'error': '目标不存在'}), 404
        try:
            threshold = float(threshold)
        except (TypeError, ValueError):
            return jsonify({'error': 'threshold 必须是数字'}), 400

        rule = AlertRule(
            target_id=target_id,
            name=name,
            rule_type=rule_type,
            threshold=threshold,
            enabled=bool(data.get('enabled', True)),
        )
        db.session.add(rule)
        db.session.commit()
        return jsonify({'id': rule.id, 'name': rule.name}), 201

    # --- 告警列表 -------------------------------------------------------------

    @app.get('/api/alerts')
    @login_required('view_records')
    def list_alerts():
        rows = Alert.query.order_by(Alert.triggered_at.desc(), Alert.id.desc()).limit(100).all()
        return jsonify([
            {
                'id': a.id,
                'rule_id': a.rule_id,
                'target_id': a.target_id,
                'message': a.message,
                'reason': a.reason,
                'observed_value': a.observed_value,
                'threshold': a.threshold,
                'triggered_at': a.triggered_at.strftime('%Y-%m-%d %H:%M:%S'),
            }
            for a in rows
        ])


def _target_json(target):
    return {
        'id': target.id,
        'name': target.name,
        'url': target.url,
        'timeout': target.timeout,
        'interval_seconds': target.interval_seconds,
        'enabled': target.enabled,
    }


def _record_json(record):
    return {
        'id': record.id,
        'target_id': record.target_id,
        'status_code': record.status_code,
        'duration_ms': round(record.duration_ms, 1),
        'is_success': record.is_success,
        'error_msg': record.error_msg,
        'checked_at': record.checked_at.strftime('%Y-%m-%d %H:%M:%S'),
    }


app = create_app()


if __name__ == '__main__':
    app.run(host=config.APP_HOST, port=config.APP_PORT, debug=True)