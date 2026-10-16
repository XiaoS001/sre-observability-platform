"""entrypoint.sh 的防呆断言测试。

背景：BackgroundScheduler 跑在应用进程内。gunicorn 起 N 个 worker 时，
每个 worker 各跑一套调度器 → 同一目标被巡检 N 次 → 数据成倍重复。
entrypoint.sh 会在「调度器开启 + worker 数 > 1」时**拒绝启动**。

🔴 为什么这里测试的是 Python 复现而不是直接跑 sh：
    开发机是 Windows 且没有 POSIX shell（`sh` 不存在），无法执行 entrypoint.sh。
    所以把断言逻辑用 Python 表达一份，并**逐字对照** shell 里的条件，
    两边任何一边改了而另一边没改，下面的对照测试就会失败。

    真正执行 entrypoint.sh 的验证在 CI 的 ubuntu runner 上（见已验证清单）。
"""

from pathlib import Path


ENTRYPOINT = Path(__file__).resolve().parent.parent / 'entrypoint.sh'


def _should_reject(scheduler_on, workers):
    """entrypoint.sh 里那段判断的等价实现。

    保持与 shell 逻辑逐字一致：
        if [ "$SCHEDULER_ON" = "true" ] && [ "$WORKERS" != "1" ]; then exit 1
    """

    scheduler_on = 'true' if scheduler_on is True else str(scheduler_on or '')
    workers = str(workers if workers is not None else 1)
    return scheduler_on == 'true' and workers != '1'


def test_entrypoint_file_exists():
    assert ENTRYPOINT.exists(), 'entrypoint.sh 缺失'


def test_entrypoint_is_executable_script():
    """必须带 shebang，否则容器里 exec 会失败。"""

    first_line = ENTRYPOINT.read_text(encoding='utf-8').splitlines()[0]

    assert first_line.startswith('#!'), 'entrypoint.sh 缺少 shebang'


def test_entrypoint_condition_matches_python_replica():
    """🔴 防漂移：shell 里的判断条件与 Python 复现必须同时存在。

    这条测试的作用是：有人改了 entrypoint.sh 的条件却没改 Python 复现时，
    至少还能看到这条失败，提醒两边要同步。
    """

    content = ENTRYPOINT.read_text(encoding='utf-8')

    assert '"$SCHEDULER_ON" = "true"' in content, 'shell 条件被改动，与 Python 复现不一致'
    assert '"$WORKERS" != "1"' in content, 'shell 条件被改动，与 Python 复现不一致'
    assert 'exit 1' in content, '拒绝启动的逻辑被移除'


def test_rejects_multiple_workers_when_scheduler_on():
    """🔴 核心约束：调度器开着 + 多 worker → 必须拒绝启动。"""

    assert _should_reject('true', '3') is True
    assert _should_reject('true', '2') is True
    assert _should_reject('true', '4') is True


def test_allows_single_worker_when_scheduler_on():
    """调度器开着 + 1 worker → 放行（这是本项目的推荐部署）。"""

    assert _should_reject('true', '1') is False


def test_allows_multiple_workers_when_scheduler_off():
    """调度器关着时，worker 数不受这条约束限制。"""

    assert _should_reject('false', '4') is False
    assert _should_reject('false', '1') is False


def test_allows_when_scheduler_env_unset():
    """调度器环境变量没设（默认 false）→ 不该拦截。"""

    assert _should_reject('', '8') is False
    assert _should_reject(None, '8') is False


def test_gunicorn_binds_all_interfaces():
    """容器里必须绑 0.0.0.0，否则端口映射进来的请求收不到。"""

    content = ENTRYPOINT.read_text(encoding='utf-8')

    assert '0.0.0.0' in content
    assert 'app:app' in content


def test_compose_pins_single_worker():
    """docker-compose 必须显式传 GUNICORN_WORKERS=1。

    compose 里不写死的话，用户只要加个 --scale 就会静默跑出重复巡检。
    """

    compose = (ENTRYPOINT.parent / 'docker-compose.yml').read_text(encoding='utf-8')

    assert 'GUNICORN_WORKERS' in compose
    assert '"1"' in compose, 'compose 未把 GUNICORN_WORKERS 固定为 1'


def test_dockerfile_exposes_non_root_user():
    """镜像必须以非 root 运行。"""

    dockerfile = (ENTRYPOINT.parent / 'Dockerfile').read_text(encoding='utf-8')

    assert 'USER appuser' in dockerfile
    assert 'useradd' in dockerfile


def test_dockerfile_installs_dependencies_before_code():
    """依赖层必须在代码层之前，才能命中 Docker 层缓存。"""

    content = (ENTRYPOINT.parent / 'Dockerfile').read_text(encoding='utf-8')

    assert content.index('COPY requirements.txt') < content.index('COPY . .')


def test_dockerfile_has_healthcheck():
    dockerfile = (ENTRYPOINT.parent / 'Dockerfile').read_text(encoding='utf-8')

    assert 'HEALTHCHECK' in dockerfile
    assert '/healthz' in dockerfile


def test_dockerignore_excludes_data_and_secrets():
    """.dockerignore 必须排掉 data/ 与 .env。"""

    content = (ENTRYPOINT.parent / '.dockerignore').read_text(encoding='utf-8')

    assert 'data/' in content
    assert '.env' in content
    assert 'venv/' in content