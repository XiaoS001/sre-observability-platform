#!/bin/sh
# RMB03 容器入口
#
# 🔴 核心职责：把「调度器与 worker 数」这条部署约束从文档变成代码。
#
# 背景：BackgroundScheduler 跑在应用进程内。gunicorn 起 N 个 worker 时，
# 每个 worker 会各跑一套调度器 → 同一个监控目标被巡检 N 次 →
# CheckRecord / Alert 数据成倍重复，指标与告警全部失真。
#
# 为什么用「拒绝启动」而不是「自动降级成 1 个 worker」：
# 静默改写用户的配置会让人以为限制没生效。直接拒绝并说明原因，
# 部署者才会真正看到这条约束。
set -eu

WORKERS="${GUNICORN_WORKERS:-1}"
SCHEDULER_ON="${RMB03_SCHEDULER_ENABLED:-false}"

if [ "$SCHEDULER_ON" = "true" ] && [ "$WORKERS" != "1" ]; then
    echo "❌ 调度器已启用时只允许 1 个 worker（当前 GUNICORN_WORKERS=$WORKERS）" >&2
    echo "   原因：每个 worker 各跑一套 BackgroundScheduler → 巡检与告警重复。" >&2
    echo "   三选一：" >&2
    echo "     1) GUNICORN_WORKERS=1（本项目默认，SQLite 单文件库）" >&2
    echo "     2) RMB03_SCHEDULER_ENABLED=false，调度交给独立进程" >&2
    echo "     3) 先把数据库换成 PostgreSQL，再拆调度器进程并横向扩容" >&2
    echo "   详见 README「部署约束：调度器与 worker 数」。" >&2
    exit 1
fi

echo "▶ 启动 RMB03 · workers=$WORKERS · scheduler=$SCHEDULER_ON"

exec gunicorn \
    --bind "0.0.0.0:${RMB03_PORT:-5000}" \
    --workers "$WORKERS" \
    --threads "${GUNICORN_THREADS:-4}" \
    --timeout "${GUNICORN_TIMEOUT:-30}" \
    --access-logfile - \
    --error-logfile - \
    --capture-output \
    "app:app"