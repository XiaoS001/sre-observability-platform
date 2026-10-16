# API 文档 · RMB03

> 基准：`127.0.0.1:5000`　所有响应为 JSON（状态页 `/status` 返回 HTML）
> 鉴权方式：Session Cookie（登录后写入 `session`）

---

## 〇、错误码约定

| 码 | 含义 | 什么时候出现 |
|---|---|---|
| `400` | 参数错误 | 缺字段、类型不对、数值越界 |
| `401` | 未登录 | 没带有效 session ——「先问你是谁」 |
| `403` | 无权限 | 登录了但角色不允许 ——「再登录也没用」 |
| `404` | 资源不存在 | 目标 / 规则 id 不存在，或报表窗口内无数据 |
| `500` | **不应出现** | 设计上任何异常路径都不返 500（见下） |

> 🔴 **任何情况不返 500** 是硬要求：通知失败、规则求值遇到脏数据、
> 模型不可用——一律降级为 200 + 降级内容，并落库/记日志。

---

## 一、健康与指标（免登录）

### `GET /healthz`

Docker HEALTHCHECK 目标。**不触发任何巡检、不写业务库**。

```json
{
  "status": "ok",
  "service": "rmb03",
  "config": {
    "host": "127.0.0.1", "port": 5000, "log_level": "INFO",
    "database_driver": "sqlite",
    "secret_key_ephemeral": true,
    "password_salt_ephemeral": true,
    "scheduler_enabled": false,
    "loki_enabled": false, "otlp_enabled": false,
    "ai_enabled": true, "ai_key_present": false,
    "alert_webhook_enabled": false
  }
}
```

> 🔴 **不泄漏密钥**：只暴露「开没开」（`ai_key_present: false`），
> 绝不回显密钥本体。`*_ephemeral` 表示该密钥未配置、正在用临时值。

### `GET /metrics`

Prometheus 文本格式（`text/plain; version=0.0.4`）。指标前缀 `rmb03_`。

| 指标 | 类型 | 说明 |
|---|---|---|
| `rmb03_probe_success` | gauge | 各目标最近一次巡检是否成功（labels: target_id / target_name / url） |
| `rmb03_probe_duration_seconds` | gauge | 最近一次巡检耗时 |
| `rmb03_last_check_timestamp_seconds` | gauge | 最近巡检时间戳 |
| `rmb03_checks_total` | counter | 按目标与成功/失败累计巡检次数 |
| `rmb03_check_failures_total` | counter | 按失败四分类累计（reason: timeout/connect/exception/bad_status） |
| `rmb03_check_duration_seconds` | histogram | 成功巡检的耗时分布 |
| `rmb03_targets_total` | gauge | 按 enabled 分组的目标数 |

> 指标在 **scrape 时从数据库派生**，不是进程内计数器 ——
> 重启归零的问题不存在，也没有「两个真相源」。

---

## 二、状态页（免登录）

### `GET /status`

HTML 页面：90 天可用率条形图 + 各目标当前状态 + 近期告警。

### `GET /api/status`

同一份数据的 JSON 版，便于外部系统拉取。

```json
{
  "window_days": 90,
  "overall_availability": 94.92,
  "daily": [{"date": "2026-07-18", "total": 48, "availability": 100.0}],
  "targets": [
    {"id": 1, "name": "demo-官网首页", "enabled": true,
     "operational": true, "last_status_code": 200,
     "last_checked_at": "2026-10-16 21:08:13", "last_error": null}
  ],
  "recent_alerts": [
    {"id": 1, "message": "[demo-支付回调] ...", "reason": "connect",
     "triggered_at": "2026-10-16 19:08:13"}
  ]
}
```

---

## 三、认证

### `POST /api/auth/register`

```json
{"username": "alice", "password": "pass123456"}
```

- 密码要求 ≥6 位，用户名 ≥3 位
- 🔴 **只能自助创建 `viewer`**，即使请求里带 `"role":"admin"` 也会被忽略
- 响应 `201`：`{"id": 1, "username": "alice", "role": "viewer"}`

### `POST /api/auth/login`

```json
{"username": "demo-admin1", "password": "admin123456"}
```

- 成功 `200`：`{"username": "demo-admin1", "role": "admin"}`，并写 session
- 失败 `401`：`{"error": "用户名或密码错误"}`
- 🔴 **不区分「用户不存在」与「密码错」** → 防止用户名枚举

### `POST /api/auth/logout`

清空 session，`200`：`{"status": "ok"}`

### `GET /api/auth/me` 🔒 需登录

`200`：`{"username": "...", "role": "viewer", "permissions": ["view_records", ...]}`

---

## 四、监控目标

### `POST /api/targets` 🔒 需 `manage_targets`

```json
{
  "name": "官网首页",
  "url": "https://www.example.com/",
  "timeout": 5,
  "interval_seconds": 60,
  "enabled": true
}
```

- `201` → `{"id": 1, "name": "官网首页", ...}`
- `400` → 缺 `name`/`url` · `timeout`/`interval_seconds` 非整数 · **≤0**
  （`interval_seconds <= 0` 会被调度器当成「总是到点」，等于变相全速巡检，
  所以在入口就拒绝）

### `GET /api/targets` 🔒 需 `manage_targets`

`200` → 目标数组。

### `POST /api/targets/<id>/check` 🔒 需 `manage_targets`

手动触发一次巡检。`404` 目标不存在。

```json
{"id": 42, "target_id": 1, "status_code": 200, "duration_ms": 88.3,
 "is_success": true, "error_msg": null, "checked_at": "2026-10-16 22:10:00"}
```

---

## 五、巡检记录与统计

### `GET /api/records` 🔒 需 `view_records`

查询参数 `limit`（默认 20，上限 200，≤0 兜底为 1）。

### `GET /api/stats` 🔒 需 `view_records`

```json
{"total": 4354, "success": 4131, "availability": 94.88, "targets": 7}
```

---

## 六、告警规则 🔴 只读角色全部 403

### `GET /api/rules` 🔒 需 `view_rules`

- viewer → **`403`** `{"error":"无权访问","required_permission":"view_rules"}`
- operator / admin → `200`

### `GET /api/rules/<id>` 🔒 需 `view_rules`

同上。`404` 规则不存在。

### `POST /api/rules` 🔒 需 `manage_rules`（viewer → 403）

```json
{"target_id": 1, "name": "连续失败告警", "rule_type": "consecutive_failure", "threshold": 3}
```

`rule_type` 取值见[告警规则配置说明](告警规则配置说明.md)。

---

## 七、告警与通知

### `GET /api/alerts` 🔒 需 `view_records`

最近 100 条告警，含 `reason` 失败分类与 `observed_value` / `threshold` 实测值。

---

## 八、报表导出 🔒 需 `view_reports`（三档角色均可）

### `GET /api/reports/export`

| 参数 | 取值 | 默认 |
|---|---|---|
| `dimension` | `target` \| `daily` | `target` |
| `days` | 1~365 | `30` |

响应：`text/csv; charset=utf-8-sig`，`Content-Disposition: attachment`。

- `404` 窗口内无数据
- `400` `dimension` 非法

目标维度表头（14 列）：

```csv
target_id,target_name,enabled,total_checks,success_checks,failed_checks,
availability_pct,p95_ms,timeout,connect,exception,bad_status,failure_total,alert_count
```

---

## 九、权限矩阵

| 能力 | admin | operator | viewer |
|---|---|---|---|
| `view_status` | ✅ | ✅ | ✅ |
| `view_records` | ✅ | ✅ | ✅ |
| `view_reports` | ✅ | ✅ | ✅ |
| `view_rules` | ✅ | ✅ | 🔴 **无** |
| `manage_rules` | ✅ | ✅ | 🔴 无 |
| `manage_targets` | ✅ | ✅ | 🔴 无 |
| `manage_users` | ✅ | 🔴 无 | 🔴 无 |

> 鉴权用**能力名**而不是路由路径：改路由时不会漏改鉴权。

---

*（本文件随实现同步更新 · DSH）*