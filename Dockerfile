# RMB03 · SRE 可观测性监控平台
#
# 层次顺序刻意为「先依赖后代码」：只要 requirements.txt 不变，
# 改业务代码时第 7 层起的层全部命中缓存，不重新装依赖。

FROM python:3.12-slim

# 时区：告警时间戳与状态页按本地时间展示
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖层：单独一层，只在 requirements.txt 变化时重建
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 代码层
COPY . .

# 非 root 用户运行 —— 容器逃逸时攻击者拿到的不是 root
RUN groupadd --system --gid 1001 appgroup \
 && useradd --system --uid 1001 --gid appgroup --no-create-home appuser \
 && mkdir -p /app/data \
 && chown -R appuser:appgroup /app
USER appuser

EXPOSE 5000

# 打 /healthz：不触发任何巡检，纯读配置，不写业务库
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=3).status==200 else 1)"

ENTRYPOINT ["./entrypoint.sh"]