## 🧪 模型主路径实测结果

> **2026-10-17 实测通过。** 此前 README 写的是「不声称模型主路径实测通过」，
> 那句话保留到实测完成前；以下是实测记录。

| 项 | 结果 |
|---|---|
| **状态** | ✅ **live 测试通过**（`pytest tests/test_live_ai.py -m live`） |
| **端点** | AMD Radeon 兼容端点（OpenAI 协议） |
| **模型** | `DeepSeek-V4-Flash` |
| **HTTP** | 200 OK |
| **耗时** | 单次端到端约 68 秒 |
| **引用校验** | `citations: ['2','3']` · `invalid_citations: []` |
| **答案质量** | 含根因分析 / 排除项 / 结论 / 分级处置建议 |

### 关键设置：key 与端点是绑定的

本项目默认 `AI_BASE_URL=https://api.deepseek.com`。若使用 **AMD Radeon 端点**的 key，
必须同时改两项，否则会拿到 401：

```bash
set RMB03_AI_BASE_URL=https://developer.amd.com.cn/radeon/api/v1
set RMB03_AI_MODEL=DeepSeek-V4-Flash
set DEEPSEEK_API_KEY=<你的 key>
```

> 实测踩过：同一个 key 在官方端点返回
> `Authentication Fails, Your api key: ****f8d1 is invalid`，
> 换到 AMD 端点 + 对应模型名后 200 OK。**key、端点、模型名三者要配套。**

### 🔴 live 测试抓到的一个真 bug（已修）

首次跑 live 时**红了**，引用校验抓到 `['3']` 是无效引用。

**但不是模型编造 —— 是我的取证数据结构有问题**：

```
query_check_records 的 records 用字段 id
query_alert_context 返回的 check 用字段 check_id   ← 两者不一致
```

两种结构混在同一个数组里（`[{...id:4355}, {...check_id:4355}]`），
模型看到参差不齐的证据就改用「第 N 条」按序号引用，于是编出 `[1][2][3]`。

修法：把 `query_alert_context` 的结果**归一化成与 records 相同的字段**再插入，
并去重。修完后取证数组结构统一，模型改回用真实 id 引用。

> **教训：给模型看的证据必须结构一致。** 字段名不齐会让模型自己"找补"，
> 表现为"编造引用" —— 表面看是模型的问题，根因在数据结构。

### 边界说明

- 答案里的 `[KB-002]` 这类知识库引用**刻意不参与校验**（见第十三节）
- 测试中的模型调用仍然全部 mock；live 是**唯一**真调的一条，需显式 `-m live`