"""调度器测试 —— 不依赖真实线程，全部走纯函数。"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

import scheduler
from models import CheckRecord, Target, db
from run_due_targets import due_target_ids, register_jobs, run_due_targets


def _make_target(name='t1', interval_seconds=60, enabled=True):
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


def _add_record(target, checked_at, is_success=True):
    db.session.add(CheckRecord(
        target_id=target.id,
        status_code=200 if is_success else 500,
        duration_ms=10.0,
        is_success=is_success,
        checked_at=checked_at,
    ))
    db.session.commit()


# --- due_target_ids 判定逻辑 -------------------------------------------------

def test_due_target_ids_empty_when_no_target(app_context):
    assert due_target_ids() == []


def test_due_target_ids_includes_never_checked_target(app_context):
    target = _make_target()

    assert due_target_ids() == [target.id]


def test_due_target_ids_skips_disabled_target(app_context):
    _make_target(enabled=False)

    assert due_target_ids() == []


def test_due_target_ids_skips_recently_checked_target(app_context):
    """刚巡检过且未到间隔 → 不该再巡检（防止重复巡检/数据翻倍）。"""

    target = _make_target(interval_seconds=60)
    _add_record(target, datetime.now() - timedelta(seconds=5))

    assert due_target_ids() == []


def test_due_target_ids_includes_expired_interval(app_context):
    """距上次巡检已超间隔 → 该巡检。"""

    target = _make_target(interval_seconds=60)
    _add_record(target, datetime.now() - timedelta(seconds=90))

    assert due_target_ids() == [target.id]


def test_due_target_ids_interval_boundary_is_inclusive(app_context):
    """正好等于间隔 → 到点（>= 而非 >）。"""

    target = _make_target(interval_seconds=60)
    now = datetime.now()
    _add_record(target, now - timedelta(seconds=60))

    assert due_target_ids(now=now) == [target.id]


def test_due_target_ids_respects_per_target_interval(app_context):
    """不同目标不同频率：快的到期、慢的没到期。"""

    fast = _make_target(name='fast', interval_seconds=10)
    slow = _make_target(name='slow', interval_seconds=3600)
    _add_record(fast, datetime.now() - timedelta(seconds=20))
    _add_record(slow, datetime.now() - timedelta(seconds=20))

    assert due_target_ids() == [fast.id]


def test_due_target_ids_non_positive_interval_always_due(app_context):
    """interval<=0 视为总是到点（配置填错时宁可多巡检也不静默停摆）。"""

    target = _make_target(interval_seconds=0)
    _add_record(target, datetime.now())

    assert due_target_ids() == [target.id]


# --- run_due_targets --------------------------------------------------------

def test_run_due_targets_returns_zero_when_nothing_due(app_context):
    assert run_due_targets() == 0


@patch('run_due_targets.check_and_save')
def test_run_due_targets_checks_every_due_target(mock_check, app_context):
    mock_check.return_value = None
    first = _make_target(name='a')
    second = _make_target(name='b')

    checked = run_due_targets()

    assert checked == 2
    assert mock_check.call_count == 2
    called_ids = {call.args[0].id for call in mock_check.call_args_list}
    assert called_ids == {first.id, second.id}


@patch('run_due_targets.check_and_save')
def test_run_due_targets_survives_one_target_raising(mock_check, app_context):
    """一个目标抛异常不应中断整轮其他目标。"""

    good = _make_target(name='good')
    bad = _make_target(name='bad')

    def side_effect(target):
        if target.id == bad.id:
            raise RuntimeError('boom')
        return None

    mock_check.side_effect = side_effect

    checked = run_due_targets()

    assert checked == 1
    assert mock_check.call_count == 2
    assert good.id in {c.args[0].id for c in mock_check.call_args_list}


def test_run_due_targets_persists_real_check_record(app_context):
    """端到端落库：只 mock 掉HTTP 请求，让真实 check_and_save 跑完，
    断言 CheckRecord 表里确实多了一条记录（而不是只断言返回值）。
    """

    from unittest.mock import Mock

    target = _make_target()
    with patch('checker.requests.get', return_value=Mock(status_code=200)):
        checked = run_due_targets()

    assert checked == 1
    records = CheckRecord.query.filter_by(target_id=target.id).all()
    assert len(records) == 1
    assert records[0].is_success is True
    assert records[0].status_code == 200


# --- 调度器本身 -------------------------------------------------------------

def test_create_scheduler_returns_unstarted_instance():
    """工厂函数不得在构造时就启动线程。"""

    sched = scheduler.create_scheduler()

    assert isinstance(sched, BackgroundScheduler)
    assert sched.running is False


def test_register_jobs_adds_job_with_expected_params():
    """断言 add_job 的参数：间隔、id、防重入配置。"""

    sched = scheduler.create_scheduler()
    job_id = register_jobs(sched, app=None)

    assert job_id == 'rmb03_due_targets'
    job = sched.get_job(job_id)
    assert job is not None
    assert job.max_instances == 1
    assert job.coalesce is True


def test_register_jobs_uses_configured_tick():
    sched = scheduler.create_scheduler()

    register_jobs(sched, app=None)

    job = sched.get_job('rmb03_due_targets')
    assert job.trigger.interval.total_seconds() == scheduler.config.SCHEDULER_TICK_SECONDS


def test_start_scheduler_starts_and_shuts_down(app):
    """真起一次线程再关掉，确认 start/stop 链路通。"""

    sched = scheduler.start_scheduler(app)
    try:
        assert sched.running is True
        assert sched.get_job('rmb03_due_targets') is not None
    finally:
        sched.shutdown(wait=False)
    assert sched.running is False


def test_scheduler_config_defaults_to_shanghai_timezone():
    assert scheduler.config.TIMEZONE == 'Asia/Shanghai'


@pytest.mark.parametrize('name, expected', [
    ('admin', '管理员'),
    ('operator', '运维'),
    ('viewer', '只读'),
])
def test_init_roles_creates_three_builtin_roles(app_context, name, expected):
    from models import Role, init_roles

    init_roles()

    role = Role.query.filter_by(name=name).first()
    assert role is not None
    assert expected in role.description


def test_init_roles_is_idempotent(app_context):
    from models import Role, init_roles

    init_roles()
    init_roles()

    assert Role.query.count() == 3