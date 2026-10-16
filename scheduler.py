"""定时调度 —— 阶段 1 只落工厂函数骨架。

调度器选型：APScheduler 3.11.3（任务单「四·五」已定）。
选它的理由：misfire 补偿与优雅停机是自研循环最容易写错、又最难测的两块。
我只在它外面包一层，把「什么时候叫」与「巡检做什么」解耦——调度器只负责触发，
巡检逻辑自己可独立测试。

阶段 2 在本文件内补：
  1. run_due_targets(app) —— 扫 enabled=True 的目标，按各自 interval 决定是否该跑
  2. register_jobs(sched, app) —— add_job 注册
  3. start_scheduler(app) —— 工厂函数（已落，见下）

三条硬规矩（任务单）：
  1. 钉版本 3.11.3，不许写 >=
  2. 只用 BackgroundScheduler + 默认内存 jobstore，
     不许 SQLAlchemyJobStore / redis / mongo
  3. 🔴 不许在模块导入时启动，必须写成显式工厂函数
     —— 否则 app.py 顶层一 import 就起后台线程，pytest 一 import 就挂
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler

import config
from run_due_targets import JOB_ID_DUE_TARGETS, register_jobs


logger = logging.getLogger('rmb03.scheduler')

SCHEDULER_TIMEZONE = config.TIMEZONE


def create_scheduler():
    """构建一个未启动的调度器（内存 jobstore + daemon 线程）。

    拆出这个纯构造函数，是为了让测试能拿到一个「没 start、没线程」的对象
    去断言 add_job 的参数，而不必真的把线程拉起来。
    """

    return BackgroundScheduler(
        daemon=True,
        timezone=config.TIMEZONE,
    )


def start_scheduler(app):
    """工厂函数：显式启动调度器。

    调用方（app.py 启动流程）负责决定何时调用，**本模块 import 时什么都不做**。
    返回 BackgroundScheduler 实例，调用方负责在退出时 shutdown。
    """

    scheduler = create_scheduler()
    register_jobs(scheduler, app)
    scheduler.start()
    logger.info('scheduler_started', extra={
        'event': 'scheduler_started',
        'job_id': JOB_ID_DUE_TARGETS,
        'tick_seconds': config.SCHEDULER_TICK_SECONDS,
        'timezone': config.TIMEZONE,
    })
    return scheduler