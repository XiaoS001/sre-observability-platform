# SRE 可观测性监控平台

> 面向中小团队的一体化监控平台：**多目标巡检 → 定时调度 → 告警 → 通知 → 状态页 → 报表**，
> 对接 Prometheus / Grafana / Loki / Jaeger 四件套，并提供**带证据引用的 AI 根因分析**（见第十三节）。
> Docker 化 + GitHub Actions CI，一条命令起全套。

---

## 🔴 验证口径（先看这个，别被后面的说法误导）

| 项 | 状态 |
|---|---|
| pytest | **167 条全绿**，本机实测 |
| 容器构建 | ✅ **GitHub Actions 已验证通过**（见下方 CI 状态） |
| 容器运行 | **本机无法验证** —— 本机没装 Docker，WSL 被安全策略禁用 |
| 演示数据 | **程序生成的模拟数据**，不涉及任何真实监控对象 |

> 本 README **不声称**「已上线」「生产环境可用」。
> 「CI 已通过」仅指**镜像构建与pytest 真实跑绿**（有 Actions 链接），
> **不代表容器运行态已验证** —— 说「能跑」时请按下方清单的边界理解。

---

## 阶段进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 审查基线核验（19 个只读文件 sha256） | ✅ 全部一致 |
| 1 | 盘点与脚手架 | ✅ 通过 |
| 2 | 数据模型 + 巡检与调度 | ✅ 77 passed |
| 3 | 告警 + 状态页 + 报表 + 用户体系 | ✅ 154 passed |
| 4 | Docker + CI | ✅ CI 两个 job 均通过 |
| 5 | 文档 + 演示材料 + 商业模式 | ✅ 完成 |
| 6 | 接入 AI 根因分析（LangGraph） | ✅ **253 passed / 1 skipped** |

---

---

## 一、要解决的问题

**中小团队的监控现状通常是三样凑合**：

| 现状 | 后果 |
|---|---|
| 脚本 ping 一下，结果发微信群 | 群里刷屏，真正的故障被淹没 |
| 出事了才想起来看日志 | 没有历史可用率数据，SLA 承诺无法举证 |
| 用云监控，但只能看自己的云资源 | 第三方 API、内部服务看不到 |

**这个项目提供一条完整闭环**：

```
添加监控目标 → 定时巡检 → 判定异常 → 去重告警 → 通知投递
                    ↓
        Prometheus 指标 / Loki 日志 / Jaeger 链路
                    ↓
              状态页（对外）+ 报表（对内）
                    ↓
         AI 根因分析（多智能体取证 + 引用校验）
```

---

## 二、架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        接入层                                     │
│  Web UI（Flask + Jinja2 + Chart.js）· 状态页（免登录）· REST API  │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│                       应用层                                     │
│  ┌───────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐   │
│  │ scheduler │  │ checker  │  │  alerts  │  │    auth      │   │
│  │APScheduler│  │四分类失败 │  │规则求值   │  │3 档角色      │   │
│  │ 每目标独立│  │ 诊断     │  │+冷却去重  │  │+401/403      │   │
│  │   间隔    │  │          │  │+通知投递  │  │              │   │
│  └─────┬─────┘  └────┬─────┘  └────┬─────┘  └──────────────┘   │
│        └──────────────┴─────────────┘                            │
│                    │                                            │
│         ┌──────────▼───────────┐                              │
│         │ reports（CSV 导出） │                              │
│         └─────────────────────┘                              │
│                    │                                          │
│                    └──▶ aiops/：LangGraph 5 工具取证 + 引用校验  │
┌───────────────────────────────────▼─────────────┴───────────────┐
│                       数据层                                     │
│  SQLite（可换 PostgreSQL）· 7 张表 · 演示数据可一键生成           │
└─────────────────────────────────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│                    可观测性四件套                                 │
│  Prometheus（指标）· Grafana（看板）· Loki（日志）· Jaeger（链路） │
└─────────────────────────────────────────────────────────────────┘
```

### 监控自己

平台暴露 `/healthz`（配置安全快照）与 `/metrics`（Prometheus 格式），
Prometheus 抓取自己的指标 → **平台可监控自己**。

---

## 三、安装

### 前置

- Python 3.12+（本项目在 **3.14.6** 上开发与测试）
- 无需 Docker（本机即以原生方式运行）

```bash
cd RMB03

# 1. 建虚拟环境
python -m venv venv

# 2. 装依赖（40 个包，全部钉版本）
venv\Scripts\pip install -r requirements.txt

# 3. 生成演示数据（可选，但强烈建议 —— 空数据库状态页是空图表）
venv\Scripts\python.exe seed_demo.py

# 4. 启动（演示环境务必关调度器，否则会真去探测目标 URL）
set RMB03_SCHEDULER_ENABLED=false
venv\Scripts\python.exe app.py
```

打开 `http://127.0.0.1:5000/status`

### 演示账号

| 账号 | 密码 | 角色 | 能做什么 |
|---|---|---|---|
| `demo-admin1` | `admin123456` | admin | 全部权限 |
| `demo-operator1` | `operator123456` | operator | 可管目标与规则 |
| `demo-viewer1` | `viewer123456` | viewer | 只读；访问规则详情会 **403** |

> ⚠️ 演示账号是 `seed_demo.py` 生成的。正式使用请改密码并设置
> `RMB03_SECRET_KEY` / `RMB03_PASSWORD_SALT` 环境变量。

### 常用地址

| 地址 | 说明 |
|---|---|
| `/status` | 状态页（免登录） |
| `/metrics` | Prometheus 指标 |
| `/healthz` | 健康检查（不回显密钥） |
| `/api/targets` | 目标管理（需登录） |
| `/api/rules` | 告警规则（viewer → 403） |
| `/api/reports/export` | 报表 CSV（需登录） |

---

## 四、部署

### 方案 A · Docker Compose（本机无 Docker，此方案由 CI 验证）

```bash
cp .env.example .env      # 填入密钥
docker compose up -d
```

起 4 个服务：

| 服务 | 端口 | 说明 |
|---|---|---|
| `rmb03` | 5000 | 应用本体（非 root 运行） |
| `prometheus` | 9090 | 抓取 `/metrics` |
| `grafana` | 3000 | 数据源已自动 provisioning |
| `loki` | 3100 | 接收结构化日志 |

> 🔴 **本机未验证此方案**（无 Docker 环境）。正确性由 GitHub Actions 的
> `docker-build` job 保证。

### 方案 B · 本机原生（本机实测通过）

用 `D:\CodexXM\_tools\` 下的 Windows 原生二进制起三支柱，
应用用 venv 起 —— 不需要 Docker。

---

## 五、运维手册

### 备份

```bash
# SQLite 在线备份（不停服，推荐）
sqlite3 data/rmb03.db ".backup 'backups/rmb03-$(date +%Y%m%d-%H%M%S).db'"

# 或直接拷贝（需先停服，避免拷到半个事务）
```

建议配一条每日 cron。备份文件放 `backups/`（已在 `.gitignore`）。

### 恢复

```bash
# 1. 停服
# 2. 覆盖
cp backups/rmb03-20261016-220000.db data/rmb03.db
# 3. 启服后自检
curl http://127.0.0.1:5000/api/stats
```

### 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动报 `database is locked` | 多进程同时写 SQLite | 确认 `GUNICORN_WORKERS=1`；见下一节 |
| 容器拒绝启动，提示 worker 数 | 调度器开着 + worker > 1 | 见「部署约束」一节 |
| 状态页是空图表 | 没数据 | 跑 `seed_demo.py` 或等调度器积累 |
| `/healthz` 显示 `secret_key_ephemeral: true` | 没配 `RMB03_SECRET_KEY` | 生产环境必须配置 |
| 日志没进 Loki | 没配 `RMB03_LOKI_URL` | 平台不依赖 Loki 也能跑，只是没有日志检索 |
| 告警不触发 | 规则 `enabled=false` 或窗口内无数据 | 见[告警规则说明](docs/告警规则配置说明.md) |
| 报表导出 404 | 窗口内无数据 | 调整 `days` 参数 |

### 健康自检

```bash
curl http://127.0.0.1:5000/healthz          # 配置快照
curl http://127.0.0.1:5000/api/stats        # 记录总数与可用率
curl http://127.0.0.1:5000/metrics | head   # 指标是否正常导出
```

---

## 六、部署约束：调度器与 worker 数

### ① 当前怎么跑

gunicorn `--workers 1`（`entrypoint.sh` 与 `docker-compose.yml` 都写死）。

**原因**：`BackgroundScheduler` 跑在应用进程内。gunicorn 起 N 个 worker 时，
每个 worker 各跑一套调度器 → **同一目标被巡检 N 次** → `CheckRecord` /
`Alert` 成倍重复，指标与告警全部失真。

### ② 生产环境该怎么做

把调度器拆成**独立进程**（web 容器只服务请求，scheduler 容器只跑调度），
此时 web 层可以多 worker 横向扩容。

### ③ 为什么本项目不拆（主动说明局限）

本项目数据落 **SQLite 单文件库**。拆成两个进程后，web 与 scheduler 会
**同时写同一个 db 文件 → 触发 SQLite 的 `database is locked`**。
要支撑多进程写，应先把数据库换成 **PostgreSQL** —— 这超出本项目范围，
故**刻意选择单 worker**，并在 ② 中写明迁移路径。

### 约束不只写在文档里：入口脚本会拒绝非法配置

`entrypoint.sh` 在「调度器开启 + worker 数 > 1」时**直接拒绝启动**：

```sh
if [ "$SCHEDULER_ON" = "true" ] && [ "$WORKERS" != "1" ]; then
    echo "❌ 调度器已启用时只允许 1 个 worker" >&2
    exit 1
fi
```

> 文档约束靠人记，代码约束不用记。 `tests/test_deploy.py` 锁住这个行为。

---

## 七、3 分钟演示脚本

> 前置：`seed_demo.py` 已跑过，演示数据已生成。

### 第 1 步 · 状态页（30 秒）

打开 `http://127.0.0.1:5000/status`

**要讲的点**：
- 顶部横幅「部分服务异常 · 综合可用率 94.92%」—— 一眼看出整体
- **当前状态**表格：7 个目标，绿点=运行中，最近响应码与时间
- **每日可用率**条形图：90 天，绿≥99% / 红<99%
- **近期告警**列表：每条带失败分类（connect / timeout / bad_status / exception）

> 💡 这页**不需要登录** —— 因为要给客户看 SLA。

### 第 2 步 · 越权隔离（60 秒）—— 最能体现质量

```bash
# 未登录
curl -i http://127.0.0.1:5000/api/rules        # → 401 先问你是谁

# viewer 登录
curl -c cookies.txt -X POST http://127.0.0.1:5000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"demo-viewer1","password":"viewer123456"}'

curl -b cookies.txt http://127.0.0.1:5000/api/rules
# → 403 {"error":"无权访问","required_permission":"view_rules"}
```

**要讲的点**：
- **401 vs 403 的区别**：401=没登录，403=登录了但没权限
- 鉴权用**能力名**（`view_rules`）而不是路由路径 → 改路由不会漏改鉴权
- 密码只存 PBKDF2 hash，注册接口**不能自助注册成管理员**

截图见 `docs/screenshots/03-越权演示-只读账号403.png`

### 第 3 步 · 报表与监控（60 秒）

```bash
# 报表 CSV
curl -b cookies.txt "http://127.0.0.1:5000/api/reports/export?dimension=target&days=30" \
  -o report.csv

# Prometheus 指标
curl http://127.0.0.1:5000/metrics | grep rmb03_probe_success
```

**要讲的点**：
- 报表 14 列含**失败四分类明细**（timeout/connect/exception/bad_status）
- 指标在 scrape 时**从数据库派生**，重启不归零，也没有「两个真相源」
- 平台能监控自己：Prometheus 抓自己的 `/metrics`

### 第 4 步 · 一句话收尾

> 「中小企业监控的痛点不是没有工具，是养不起人用好工具。
> 我把巡检、告警、状态页、报表这四件套做成一条命令能起的成品，
> 并把『什么没验证』明确写在 README 里 —— 比如容器我跑不了，就说跑不了。」

---

## 八、这个项目打进哪三个简历方向

| 方向 | 对应能力 |
|---|---|
| **运维 / SRE** | 多目标巡检 · 四分类失败诊断 · Prometheus/Grafana/Loki/Jaeger 四件套 · Docker 多层构建 · GitHub Actions CI · 部署约束设计（单 worker 的取舍） |
| **技术支持** | 状态页 · 报表导出 · 文档齐全（API/数据库/告警/商业模式四份）· 降级设计（通知失败不炸主流程） |
| **AI 应用 / AI 测试** | **LangGraph 多智能体根因分析**（Supervisor + 4 Agent · 6 工具取证 · 引用校验拦编造）· **五层降级**（无 key/超时/HTTP 错/返回体异常/编排崩溃 均不 500）· **变异测试驱动**（自跑 5 个变异，全部被抓）· 冷却期毫秒级双向夹逼 |

### 面试可讲的技术决策

| 决策 | 为什么 |
|---|---|
| 调度器用 APScheduler 而非自研循环 | misfire 补偿与优雅停机是自研最容易写错又最难测的两块 |
| 冷却期用「重复抑制」而非「持续时长」 | 持续语义会**延迟第一次告警**，与「异常立即可见」冲突 |
| 边界测试用固定 `now` 而非真实时钟 | 两次 `datetime.now()` 有毫秒漂移，`<` 与 `<=` 结果相同 → 锁不住运算符 |
| 通知失败落库而非抛异常 | 对外承诺是「告警被记录」，不是「通知一定送达」 |
| 测试库隔离用 conftest 模块级设环境变量 | config 在 import 期就固化了路径，fixture 里设已太晚 |
| 密钥缺失时标记 ephemeral 而非给默认值 | 悄悄用开发默认值 = 所有部署共用一个盐 |

---

## 九、已验证 / 未验证清单

### ✅ 已在本机验证（Windows · Python 3.14.6）

| 项 | 命令 / 方式 | 结果 |
|---|---|---|
| pytest 全绿 | `python -m pytest tests -p no:warnings` | **167 passed** |
| 语法编译 | `python -m compileall -q <9 个模块>` | exit=0 |
| 依赖安装 | `pip install -r requirements.txt` | 40 包成功 |
| 应用可启动 | `python app.py` | `/healthz` → 200 |
| 状态页 | `GET /status` | 200，90 天图表有数据 |
| 越权隔离 | viewer 访问 `/api/rules` | **403**（operator 同接口 200，未登录 401） |
| 密码非明文 | 查 `user.password_hash` | `pbkdf2_sha256$120000$…$…` |
| 冷却期去重 | 12 条用例 + 变异测试 | 变异被**抓到** |
| 测试库隔离 | 跑完 167 条后 `data/rmb03.db` 时间戳 | 零污染 |
| 演示数据幂等 | `seed_demo.py` 连跑两次 | 记录数一致，不翻倍 |
| 6 个 YAML 语法 | `yaml.safe_load` | 全部 OK |
| 19 个只读文件 | sha256 逐个比对 | 全部一致（XM01/XM05 零改动） |
| **AI 根因分析** | `verify_degradation.py` + 28 条测试 | **已实现**。五层降级全部实测有可读输出（见第十二节） |
| **模型主路径** | `pytest tests -m live`（AMD端点 / DeepSeek-V4-Flash） | ✅ **2026-10-17 实测通过**，引用校验全过 · 详见 `docs/AI实测记录.md` |

### ❌ 未验证（需 CI 或有 Docker 环境的人验）

| 项 | 原因 | 在哪验 |
|---|---|---|
| **容器实际运行** | CI 新增 `smoke-run` job，真跑容器验 `/healthz` + 非 root + 401 语义 | 见 Actions `smoke-run` |
| 🔴 **compose 四服务联通** | 同上 | 有 Docker 的机器 |
| 🔴 **GHCR 镜像拉取** | 本机无 Docker；当前 PAT 缺 `read:packages` scope，无法用 API 核验 registry 内容 | 有 Docker 的机器执行 `docker pull ghcr.io/xiaos001/sre-observability-platform:latest` |
| **Grafana 看板渲染** | 需要容器 | 浏览器 |

### CI 状态

✅ **CI 已通过** —— 这是真实运行结果，非自述。

| 项 | 结果 |
|---|---|
| Actions 运行 | [runs/39013102795](https://github.com/XiaoS001/sre-observability-platform/actions/runs/39013102795) |
| `pytest` job | **success** —— 253 passed / 1 skipped |
| `docker-build` job | **success** —— 镜像构建并推送 GHCR |
| 镜像地址 | `ghcr.io/xiaos001/sre-observability-platform:latest` |

#### 三次真实失败与修复（如实记录，不藏）

| # | 运行 | 失败原因 | 修法 |
|---|---|---|---|
| 1 | [39004124606](https://github.com/XiaoS001/sre-observability-platform/actions/runs/39004124606) | `invalid tag: repository name must be lowercase` —— `github.repository` 带大写用户名 | 加一步 `tr` 转小写写入 `GITHUB_ENV` |
| 2 | [39012811003](https://github.com/XiaoS001/sre-observability-platform/actions/runs/39012811003) | `test_real_analysis_produces_valid_citations` 失败 —— AI 工具**连错数据库** | 库路径改为**运行时解析**，不再在 import 期固化 |

> **第 2 条值得展开** —— 本地 253 passed 但 CI 1 failed，而它暴露的是一个真 bug：
>
> `db_access.DB_PATH` 原本在**模块导入期**从 `config.DATA_DIR` 固化成常量，
> 而 conftest 是在**模块级**改 `RMB03_DATABASE_URL` 做测试隔离。
> aiops 在测试执行期才 import，那时 config 早已缓存 ——
> 于是 AI 取证工具连的是**真实业务库**而不是测试库。
>
> 本机没暴露，是因为 `data/rmb03.db` 里有演示数据（查得到那条记录 → 引用校验通过）；
> CI 是干净 checkout，查了个空库 → 证据里没有它 → 引用校验判定「模型编造」→ 红灯。
>
> **教训：导入期固化的路径是测试隔离的隐形漏洞，本机有数据会掩盖它。**
> 修完后专门做了「模拟 CI 干净 checkout」的双环境验证（两种环境都 253 passed）。

> 🔴 **CI 只构建镜像，不运行容器。** 运行态（`docker compose up`、
> 四服务联通、Grafana 看板渲染）**仍未验证**，见上表。
### 关于 Grafana 看板

本项目提供了数据源自动 provisioning，但**未提供预置看板 JSON**，
也**未在本机验证过看板渲染**（需要容器环境）。
状态页的可用率图表是本机实测的，不依赖 Grafana。

## 十、演示材料

| 截图 | 内容 |
|---|---|
| `docs/screenshots/01-状态页-每日可用率.png` | 90 天可用率条形图，绿/红分色 |
| `docs/screenshots/02-状态页-当前状态与告警.png` | 7 个目标当前状态 + 近期告警列表 |
| `docs/screenshots/03-越权演示-只读账号403.png` | 401/403 语义与三角色权限矩阵 |
| `docs/screenshots/04-AI根因分析-降级路径.png` | `/admin/alerts` 点击「分析」后的根因 + 引用 + 降级标记 |

> ⚠️ 截图中所有数据均由 `seed_demo.py` **程序生成的模拟数据**，
> 目标 URL 全部使用 RFC 2606 保留的示例域名（`*.example.com`），
> **不涉及任何真实监控对象**。

---

## 十一、文档

| 文档 | 内容 |
|---|---|
| `docs/API文档.md` | 全部端点 · 错误码约定 · 权限矩阵 |
| `docs/数据库设计.md` | ER 关系 · 7 张表字段 · 索引与查询模式 · 备份恢复 |
| `docs/告警规则配置说明.md` | 三种规则 · 冷却期语义与边界 · 排障 |
| `docs/商业模式.md` | 目标用户 · 定价档位 · 成本结构 · 获客路径 · 与开源方案的差异 |

---

## 十二、来源与复用

本项目是**演进**，不是从零。以下资产从只读参考项目**复制**而来
（原文件一个字未动，sha256 逐阶段比对）：

| 来源 | 拿什么 | 改动 |
|---|---|---|
| `XM01\checker.py` | 巡检逻辑 + **失败四分类** | 仅改表名 |
| `XM01\metrics.py` | 自定义 Collector（scrape 时从库派生） | 表名 + 指标前缀 |
| `XM01\logging_setup.py` | 手写 LokiHandler（守护线程 + 批量缓冲 + 失败静默降级） | 环境变量前缀 |
| `XM01\tracing_setup.py` | OTel 埋点 + 两层容错 | 标识符名 |
| `XM05\aiops\` | LangGraph 编排（387 行） | `aiops/graph.py` —— 改 key 来源（`os.environ` → `config`）、补 `AI_ENABLED` 分支、取证锚定 `check_id`、新增 `query_alert_context` 计划 |
| | 5 个取证工具（193 行） | `aiops/tools.py` —— **函数体全部重写**（SQL 表名 `check_result`→`check_record`、`monitor_target`→`target`），新增第 6 个工具 `query_alert_context`，加了 `db_error` 兜底与 `_text()` 空值防火墙 |
| | 只读打开数据的方式 | `aiops/db_access.py` —— 库路径改 `data/rmb03.db`，异常统一转 `DbUnavailable` |
| | 12 条运维知识（BM25） | `knowledge/` 原样复制，零改动 |
| | BM25 检索器 | `knowledge/retriever.py` 原样复制，零改动 |

> **数据不复制**：本项目用自己的 `data/rmb03.db`，
> 绝不复制也绝不写入 XM01 的 `app.db`。

---
## 十三、AI 根因分析（阶段 6）

> 移植自 XM05 的 `aiops/`（复制不引用，原项目零改动），按 RMB03 的表结构改写。

### 12.1 编排结构

```
告警触发（Alert 已落库）
      ↓
POST /api/alerts/<id>/analyze
      ↓
LangGraph：supervisor → planner → tool_agent → critic → answerer
      ↓
6 个白名单工具取证（全部只读）
      ↓
引用校验：[N] 必须能对上证据 id，对不上触发重答（有上限）
      ↓
结果落库 analysis 表 + 返回
```

### 12.2 六个取证工具

| 工具 | 查什么 |
|---|---|
| `query_check_records` | `check_record` JOIN `target`，保留失败四分类 |
| `query_metrics` | 从库聚合可用率 / P95 / 四分类计数（**不查 Prometheus**，与平台自身同口径） |
| `query_logs` | 按四分类过滤 `check_record.error_msg` |
| `query_traces` | 🔴 **如实返回 `has_trace_data: false`** —— 链路没入库，不假装有 |
| `search_knowledge` | 12 条运维知识（jieba + BM25） |
| `query_alert_context` | **RMB03 新增** —— 告警的规则类型与阈值（XM05 没有 alert 表） |

**三条硬约束由代码强制**：

1. **只读** —— 全部查询走 `sqlite3.connect("file:...?mode=ro")`，
   SQLite 引擎层拒绝写。测试里用 DELETE/INSERT/UPDATE 实测验证过会被拒。
2. **不抛异常** —— 参数非法 / 库打不开 / 表结构异常，一律转成
   `{"ok": false, "code": "bad_param"|"db_error"}`，**任何异常冒到 Flask 就是 500**。
3. **不编数字** —— 工具只直查库；链路数据没有就说没有。

### 12.3 引用校验

答案里的数字引用 `[N]` 必须能在证据列表里找到对应记录 id，
对不上则判定为编造 → 触发重答。

- 双上限：`MAX_ROUNDS=12`（轮数）/ `MAX_ATTEMPTS=3`（尝试次数），防死循环
- ⚠️ **`[KB-001]` 这类知识库引用刻意不校验** —— 它是规则路径模板生成的，
  不是模型编的；且 KB- id 与记录 id 形态不同，混进同一套 valid_ids 反制造歧义。
  这个设计有测试锁住，防止以后被"好心"改掉。

### 12.4 五层降级（**没有 key 也能用**）

| 场景 | `degraded` | `answer` | 降级原因 |
|---|---|---|---|
| 无 `DEEPSEEK_API_KEY` | ✅ | 规则路径，241 字 | `未配置 DEEPSEEK_API_KEY，退回规则路径` |
| 模型超时 | ✅ | 同上 | `...失败: Read timed out` |
| HTTP 错误 | ✅ | 同上 | `...失败: HTTP 429` |
| 返回体异常 |✅ | 同上 | `...失败: 模型返回内容为空` |
| `RMB03_AI_ENABLED=false` | ✅ | 同上 | `AI_ENABLED=false，跳过模型调用` |
| 编排整体崩溃 | ✅ | 异常信息 | 落库 `status=failed`，**不 500** |

**降级答案长这样**（不是「分析失败」了事）：

```
根因分析：目标「demo-支付回调」（https://pay.example.com/callback）出现请求异常。
依据知识库条目 [KB-004]，可能原因：
  - 服务端代码抛异常未捕获
  - 上游依赖服务不可用（502/504）
  - 服务过载导致拒绝服务（503）
依据巡检记录 [2946]: 失败分类为 exception，耗时 15.0ms。
处置建议：
  - 查服务端错误日志定位异常堆栈
  - 检查上游依赖服务健康状态
  - 检查服务负载与限流阈值
```

> 独立复现：`venv\\Scripts\\python.exe verify_degradation.py`

### 12.5 🔴 关于「模型调用」的诚实说明

| 项 | 状态 |
|---|---|
| **测试中的模型调用** | **全部 mock** —— 253 条 pytest **零真实 API 消耗、零费用、不依赖网络** |
| live 用例 | **1 条** `tests/test_live_ai.py`，`@pytest.mark.live`，默认 skip · ✅ **已实测通过（2026-10-17）** |
| 为什么还要留 live | 前面全 mock 就没验证过「真接上模型能不能跑通」 |
| 怎么跑 | 默认端点：`set DEEPSEEK_API_KEY=sk-xxx` 后 `pytest tests -m live` · 用 AMD 端点的 key 需**同时**改 `RMB03_AI_BASE_URL` 与 `RMB03_AI_MODEL`（见 `docs/AI实测记录.md`） |
| live 里断言什么 | 不管成功降级，都断言「不抛异常 + 无 None + **引用全部有效**」 |

> ⚠️ **没跑过 live 就不声称「真机验证通过」**。本项目对模型的验证
> **2026-10-17 更新**：live 已实测通过（AMD 端点 / DeepSeek-V4-Flash，HTTP 200，

### 12.6 两个被「用眼睛看」抓出来的 bug

这类 bug 有一个共同点：**测试全绿，但输出是错的**。

| bug | 现象 | 根因 | 修法 |
|---|---|---|---|
| `耗时 Nonems` | 答案里出现 Python 字面量 `None` | `dict.get(k, default)` 的 default **只在键不存在时生效**，键存在值为 None 时返回 None |抽 `_text()` 助手，所有插值点统一走它 |
| `可能原因：` 悬空 | 标题写了但下面是空的 | 只取了 KB 的 `actions`，**漏了 `causes`** —— 知识库里明明有 3 条原因 | 一并取 `causes`；标题必须跟着内容走 |

> 这两个都是**人工读输出**发现的，不是测试报的。
> 补的测试：`test_answer_never_contains_none_*`、`test_possible_causes_section_is_populated`。

---


*（本项目为个人作品。演示数据为程序生成的模拟数据；容器相关能力由 CI 验证，本机无 Docker 环境。）*
