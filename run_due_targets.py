"""巡检调度 —— 阶段 2 实现。

调度器只负责「什么时候叫」，本模块负责「被叫之后该巡检谁」——
两者解耦，所以巡检逻辑可以脱离 APScheduler 独立测试。

调度模型：
    每个目标有自己的 interval_seconds。调度器按固定 tick（如 10 秒）醒来，
    遍历 enabled=True 的目标，检查「距该目标上次巡检是否已超过它的 interval」，
    超了就巡检一次。这样：
      · 每个目标可以有自己的频率，不必全局统一
      · 服务重启/卡顿后不会漏掉整段窗口（按「上次巡检时间」判断，不是按计数器）
      · 一个目标超时不会拖慢其他目标的调度
"""

import logging
from datetime import datetime

import config
from checker import check_and_save
from models import CheckRecord, Target, db


logger = logging.getLogger('rmb03.scheduler')

JOB_ID_DUE_TARGETS = 'rmb03_due_targets'


def due_target_ids(now=None):
    """返回「本轮该巡检」的目标 id 列表（不真正发请求）。

    判定规则：目标 enabled=True，且（从未巡检过 或 距上次巡检 >= interval_seconds）。

    边界：interval_seconds <= 0 视为「总是到点」——配置填错时宁可多巡检，
    也不能静默变成永不巡检。
    """

    now = now or datetime.now()
    targets = Target.query.filter_by(enabled=True).order_by(Target.id).all()

    due = []
    for target in targets:
        if target.interval_seconds <= 0:
            due.append(target.id)
            continue

        latest_checked_at = (
            db.session.query(CheckRecord.checked_at)
            .filter(CheckRecord.target_id == target.id)
            .order_by(CheckRecord.checked_at.desc(), CheckRecord.id.desc())
            .first()
        )
        if latest_checked_at is None:
            due.append(target.id)
            continue

        elapsed = (now - latest_checked_at[0]).total_seconds()
        if elapsed >= target.interval_seconds:
            due.append(target.id)
    return due


def run_due_targets(app=None, now=None, evaluate_alerts=True):
    """执行本轮所有到点目标的巡检，并顺带跑一轮告警求值。

    evaluate_alerts=False 时只巡检不求值（测试里想单独隔离两者时用）。

    返回本次巡检的目标数，供测试断言与日志使用。
    """

    target_ids = due_target_ids(now=now)
    if not target_ids:
        logger.debug('scheduler_no_due_target', extra={
            'event': 'scheduler_no_due_target',
        })
        return 0

    checked = 0
    for target_id in target_ids:
        target = db.session.get(Target, target_id)
        if target is None:
            continue
        # 单个目标巡检抛异常不应中断整轮其他目标。
        # check_and_save 内部已把四类失败归一化，正常路径不抛；这里兜底。
        try:
            check_and_save(target)
            checked += 1
        except Exception:
            logger.exception('scheduler_check_failed', extra={
                'event': 'scheduler_check_failed',
                'target_id': target_id,
            })

    # 巡检完再求值：告警规则要基于刚落库的巡检记录判断。
    alert_count = 0
    if evaluate_alerts:
        try:
            from alerts import run_alert_cycle

            alert_count = len(run_alert_cycle(now=now))
        except Exception:
            logger.exception('scheduler_alert_cycle_failed', extra={
                'event': 'scheduler_alert_cycle_failed',
            })

    logger.info('scheduler_round_done', extra={
        'event': 'scheduler_round_done',
        'due_count': len(target_ids),
        'checked_count': checked,
        'alert_count': alert_count,
    })
    return checked


def register_jobs(sched, app=None):
    """把巡检 job 注册到调度器。返回 job id。

    max_instances=1 + coalesce=True：单进程内防重入 + 合并积压。
    ⚠️ 这只防单进程内重入，防不住 gunicorn 多 worker
    （每 worker 各一套调度器 → 同一目标被巡检 N 次）。
    根治方案见 README 第六节：调度器独立进程跑，或 gunicorn --workers 1。
    """

    sched.add_job(
        run_due_targets,
        trigger='interval',
        seconds=config.SCHEDULER_TICK_SECONDS,
        id=JOB_ID_DUE_TARGETS,
        args=[app],
        max_instances=1,
        coalesce=True,
    )
    return JOB_ID_DUE_TARGETS