"""pytest 公共fixture。

🔴 隔离要点（审查意见明确要求）：
app.py 顶层会 create_app() → create_all()，如果直接 import 会把库建到
data/rmb03.db 上，测试就会污染真实业务库。

所以这里用**会话级独立测试库**：测试开始前设好 RMB03_DATABASE_URL 指向
tmp_path_factory 造的临时目录，再 import app；并在 import 后断言「测试库
路径 != 真实库路径」，确认隔离真的生效。结束后删掉临时库文件。
"""

import os
from pathlib import Path

import pytest


# 🔴 必须在**模块导入期**就指向测试库，而不是在 fixture 里设。
#
# 原因：config.py 在被 import 的那一刻就把 DATABASE_URL 读成模块级常量了。
# pytest 的收集阶段会先 import 各个 test_*.py，其中 `import scheduler` 会连锁
# import config —— 那时任何 fixture 都还没跑，config 已经把
# data/rmb03.db（真实业务库）路径缓存下来了。之后 fixture 再设环境变量也无效，
# 于是测试会真的连上业务库。
#
# conftest.py 由 pytest 在**所有 test_*.py 之前**导入，所以把赋值放在这里
# 就能保证 config 被 import 时读到的是测试库。
REAL_DB_PATH = Path(Path(__file__).resolve().parent.parent, 'data', 'rmb03.db')

_TEST_DB_DIR = Path(__file__).resolve().parent / '_tmp_testdata'
_TEST_DB_DIR.mkdir(exist_ok=True)
_TEST_DB_PATH = _TEST_DB_DIR / 'test.db'

_PREVIOUS_DATABASE_URL = os.environ.get('RMB03_DATABASE_URL')
os.environ['RMB03_DATABASE_URL'] = f'sqlite:///{_TEST_DB_PATH.as_posix()}'


@pytest.fixture(scope='session')
def app():
    import config
    from app import app as flask_app
    from models import db, init_roles

    # 隔离的硬断言：如果这里相等，说明 config 被更早 import 并缓存了真实库，
    # 测试正在污染 data/rmb03.db —— 必须失败，不能放行。
    assert _TEST_DB_PATH.resolve() != REAL_DB_PATH.resolve(), (
        '测试库路径与真实业务库相同，测试会污染 data/rmb03.db'
    )
    assert config.DATABASE_URL == f'sqlite:///{_TEST_DB_PATH.as_posix()}', (
        f'config.DATABASE_URL 未指向测试库，实际为 {config.DATABASE_URL}'
    )

    flask_app.config.update(TESTING=True)

    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        init_roles()

    yield flask_app

    with flask_app.app_context():
        db.session.remove()
        db.drop_all()
        db.engine.dispose()

    _TEST_DB_PATH.unlink(missing_ok=True)

    if _PREVIOUS_DATABASE_URL is None:
        os.environ.pop('RMB03_DATABASE_URL', None)
    else:
        os.environ['RMB03_DATABASE_URL'] = _PREVIOUS_DATABASE_URL


@pytest.fixture()
def app_context(app):
    """每个用例前重建干净 schema。

    用drop_all/create_all 而不是事务回滚：测试里会显式检查表结构与
    外键级联行为，事务回滚方案在这些用例上语义不如重建直观。
    """

    from models import db, init_roles

    with app.app_context():
        db.drop_all()
        db.create_all()
        # 每例重建 schema 后角色表是空的，必须重新写入内置角色，
        # 否则注册/授权用例会因为「角色不存在」而 400。
        init_roles()
        yield
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def test_client(app, app_context):
    return app.test_client()