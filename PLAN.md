# OpenOnce — 技术实现 Plan

> Durable side-effect layer for AI agent tool calls.
> 正确性定位：**不是 exactly-once 魔法**。外部副作用无法纳入本地事务，因此真实保证是
> **at-least-once 执行 + 幂等键去重 + reconciliation 对账**。Ledger 不是真理源，外部世界才是；
> ledger 的职责是"崩溃后可重建状态并驱动对账到终态"。

---

## 0. 一句话架构决策

MVP **不做**通用 MCP proxy，也**不做**完整 workflow engine。
MVP = **一个 Python SDK（装饰器 / 上下文管理器）+ Postgres effect ledger**，
把 caller 的 tool handler 包一层，提供：幂等去重、审批插入点、崩溃恢复、对账队列。
先在**一类高价值非幂等副作用**上把正确性做穿，MCP proxy 作为 Phase 2 的零侵入分发形态。

理由：
- 痛感最集中的生态是 Python（LangGraph / OpenAI Agents SDK / CrewAI），meet users where the pain is。
- SDK wrapper 能力最全：能轻松短路返回缓存、能把派生 idempotency key **透传给 provider**（这是外部去重的唯一可靠防线）、能精确控制哪些 tool 纳入。
- MCP 拦截规范（SEP-1763）**没有"短路返回缓存结果"的一等原语**（见 §4），proxy 形态反而更难，不适合先做。

---

## 1. Idempotency Key —— 意图指纹

**决策：显式 key 一等，派生 key 兜底，禁止语义去重。**

| 场景 | 策略 |
|---|---|
| caller 能给稳定 key | 一等契约：直接用 caller 提供的 `idempotency_key`（≤255 字符，V4 UUID 级熵）。*不要*从参数猜。 |
| agent 未传 key（常态） | 派生：`SHA256(run_id/step_id \| tool_name \| canonical_args)` |
| args 含 LLM 非确定性字段（时间戳/UUID/自然语言） | caller 声明 **字段白名单** `idempotency_fields=["owner","repo","title"]`，只让"意图指纹"字段进 hash，其余变化不触发重执行 |
| 结构性非确定性（key 顺序/空白/数字格式） | 用 **RFC 8785 JCS** 规范化 JSON 后再 hash（递归按 UTF-16 码元排序键） |
| 语义等价（措辞不同、意图相同） | **不做自动去重**。相同参数尚可能是不同意图（AWS EC2 双实例反例），相似度判等会造成"漏执行"。只在 canonical_args 完全一致或显式复用同一 key 时去重。 |

**同 key、不同 payload：拒绝。** 记录首请求的 args 指纹，后续同 key 但指纹不同 → 返 `idempotency_mismatch` 校验错误（语义拒绝；状态码用 400，*不是* Stripe 的 400 之外的 409——409 只是 brandur 参考实现的约定，别照抄成"Stripe 行为"）。

**结果缓存与回放：** 一旦 handler *开始执行*，把结果（状态码 + body，**成败都存**，含 4xx/5xx）落在 idempotency key 记录上；后续同 key 请求原样回放（一次 400 之后仍回同一个 400）。执行前的纯校验失败 / 并发冲突不缓存。

参考：Stripe idempotency docs、AWS Builders' Library（making retries safe）、RFC 8785、agent-ledger `idempotency_keys` 参数。

---

## 2. Effect Ledger —— 状态机 + 不可变日志

**决策：显式状态机 + 转移表（不用隐式状态字段）；immutable journal + materialized state。**

状态（合并你原设计与 agent-ledger 落地经验）：

```
PLANNED → POLICY_CHECKED → APPROVED(可选) → STARTED
        → RECEIPT_RECORDED → VERIFIED → COMMITTED
分支：STARTED ─crash─▶ UNKNOWN → RECONCILE → {COMMITTED | RETRY | HUMAN_REVIEW}
审批：POLICY_CHECKED → REQUIRES_APPROVAL → {APPROVED | DENIED | CANCELED}
```

用 `frozenset` 转移表校验每次跃迁的合法性（非法跃迁直接 raise）。

**存储（Postgres）：**
- `effects` 表：`idempotency_key`（**UNIQUE 约束**）、`state`、`args_fingerprint`、`cached_result`、`created_at`、`updated_at`、`lease_owner`、`lease_expires_at`、`attempt`。
- `effect_journal` 表：**append-only**，每次状态跃迁一行（`effect_id, from_state, to_state, payload, ts`）。materialized `state` 列由 journal 推导，崩溃后可从 journal 重建。
- 唯一约束 = **必要非充分**：它防重复插入，但真正的并发协调靠 **first-writer-wins**——首个 worker 拿到行（`INSERT ... ON CONFLICT DO NOTHING` 或 `SELECT ... FOR UPDATE SKIP LOCKED`），其余 worker **等待并读取已记录结果**，不重复执行。
- **Lease / fencing token**（`lease_owner + lease_expires_at`）防两个 worker 同时处理一条 STARTED 记录；租约过期才允许接管。

**崩溃边界 = "atomic phases"（借鉴 brandur rocket-rides-atomic）：**
把外部调用**单独隔离在一个阶段**，阶段前的事务提交即 recovery point。于是 `STARTED`（调用前已提交）与 `RECEIPT_RECORDED`（调用后记录回执）成为两个可重入边界，中间崩溃有明确语义。

⚠️ **不要**试图"在单个 ACID 事务里同时记录 token 和执行外部 mutation"——外部副作用进不了本地事务，这正是 UNKNOWN 问题的根源。

---

## 3. UNKNOWN Outcome —— 恢复队列与对账循环

**问题：** 崩溃发生在 `STARTED` 与 `RECEIPT_RECORDED` 之间——副作用**可能发生了但没记录**，也**可能根本没发生**。

**决策：后台 completer + 宽限期 + probe。**
- 后台进程扫描非终态（STARTED/UNKNOWN）记录，但**只处理超过宽限期的**（默认 5 分钟，**按 tool 类别可配**——快重试的短任务 vs 慢审批的长任务需要不同窗口），先给原 caller 自己重试的机会。
- 对每条 UNKNOWN，用 **probe / read-back** 回查外部真实状态来判定分支：
  - probe 显示"已发生" → 推进到 `RECEIPT_RECORDED` → `COMMITTED`
  - probe 显示"未发生" → `RETRY`
  - probe 不可判定 → `HUMAN_REVIEW`（进人工队列，绝不盲目重试）

**⚠️ 最大工程风险（未被一手来源验证，属推断层）：** 对无稳定 `provider_id`、无 settlement feed 的任意 SaaS，probe 的可行性、延迟窗口、false-negative（误判"未发生"→重复执行）都缺实证。**这是 MVP 之前必须先做的一张表**（见 §5、§7）。

---

## 4. 外部去重 —— 唯一可靠防线是"把派生 key 透传给 provider"

**决策：分层。**
- **内部**：ledger memoization 保证同一 effect 不被本层重复发起。
- **外部**：把稳定派生 key（如 `f"{effect_id}:charge"`）**作为 idempotency key 传给 provider**。这是崩溃重放时 provider 侧去重的唯一硬防线（tensorzero/durable、agent-ledger 均此模式）。

**Provider idempotency 支持矩阵**（决定每个工具落在哪一档，MVP 前必须建表）：
1. **原生支持幂等键**（Stripe、部分支付/基础设施 API）→ 透传派生 key，最干净。
2. **有自然幂等键**（业务唯一字段，如 GitHub `(owner,repo,PR title)`、邮件 `Message-ID`）→ 用业务字段做 key + probe-before-write。
3. **无任何幂等支持**（很多 SaaS 写接口）→ **probe-before-write**（先查再写）+ 最后退化到 fuzzy 匹配（`amount+time+counterparty`）。fuzzy **不可靠**，只做告警，不做自动去重决策。

安全默认：**宁可漏去重（重复执行、可对账发现）也不误去重（漏执行、静默丢副作用）**——除非该 effect 明确标注"重复=灾难"（如支付），那类必须走档位 1 或 2，档位 3 的工具**禁止**自动执行、强制审批。

---

## 5. MCP 集成（Phase 2，不进 MVP）

调研结论：SEP-1763 / Interceptors Charter 只有两类原语——
- **validator**：inspect → pass/fail，仅 `error` 级阻断；
- **mutator**：transform payload，仍需底层执行。

**没有"短路 tool call 并返回缓存/合成结果"的一等操作。** 因此：
- **审批 / policy** → 干净映射到 validator（error 阻断）。✅
- **缓存回放 / 短路** → 拦截器原语做不到，必须由 **MCP proxy（sidecar/remote）自身逻辑**拦下、查 ledger、命中则直接回包。
- 部署用 proxy 形态可"不改各个 MCP server"（Charter 的 sidecar runtime 目标，把 M×N 变 M+N），但该 runtime 仍处 **Ideating**、SEP-1763 仍 **Draft**——规范会变，Phase 2 再押。

---

## 5.5 参考 Temporal 的设计边界

一句话：**OpenOnce ≈ 把 Temporal 的 Activity 层单独抽成库；agent 框架（LangGraph 等）扮演 Workflow。**

**抄（Temporal 腰部以下）：**
- **Event History** → `effect_journal`（append-only、真理源、崩溃后从最后事件恢复）。
- **Activity 语义** → effect 本体：at-least-once、可非确定、"should be idempotent"、结果作为完成事件记入 history（Temporal 官方原话印证 §8 的被证伪清单）。
- **Heartbeat + Timeout** → lease + probe（UNKNOWN 恢复）。
- **Retry Policy** → 字段照抄：`initial_interval / backoff / max_attempts / non_retryable_errors`。
- "把大功能拆成多个小 activity" → effect 粒度要细、边界清晰。

**关键分歧：Temporal 重放代码，OpenOnce 重放数据。**
Temporal 靠确定性重放 Workflow 代码重建状态，因此强制 workflow 确定性；agent 的"workflow"是 LLM 调用，不可确定重放。OpenOnce 把持久化单位降到 effect：崩溃后不重跑 agent，只读 journal 投影每个 effect 状态（data replay），再决定回放缓存 / probe 对账 / 人工。因此**不需要也不应要求任何确定性约束**。

**不抄（抄了就变成更差的 Temporal）：**
Workflow 编排运行时、Task Queue + Worker 轮询、独立 Server 集群（Frontend/History/Matching/Worker + 单独 DB）、Signals/Queries/Timers/Child Workflow/Continue-as-New。OpenOnce 是**嵌入式库 + 一张数据库表**，被 agent runtime 内联调用。

**包结构（概念分层借 Temporal，砍掉 server）：**

```
openonce/
  keys.py            # JCS 规范化 + 字段白名单 + SHA256 派生
  state.py           # EffectState 枚举 + frozen 转移表
  ledger.py          # Event History 等价物：effects + journal（存数据、不为重放码）
  effect.py          # Activity 等价物：@effect 装饰器 / EffectHandle / RetryPolicy
  runtime.py         # 精简版 Worker：first-writer-wins、lease、缓存回放（无 poll）
  reconciler.py      # Heartbeat+Timeout 等价物：completer（宽限期→probe→分支）
  policy.py          # approval / policy 插入点（未来映射 MCP validator）
  providers/         # ← 壁垒：provider 幂等支持矩阵 + probe 适配器（Temporal 没有）
  store/             # memory / sqlite（零基建 dev）/ postgres（生产）
```

## 6. 技术选型

| 维度 | 选择 | 理由 |
|---|---|---|
| MVP 语言 | **Python** | 痛感生态（LangGraph/OpenAI Agents/CrewAI）与 agent-ledger 同栈 |
| 存储 | **Postgres**（`SKIP LOCKED` 做队列、唯一约束、JSONB 存 journal） | 无需额外中间件，事务 + 队列一体 |
| 规范化 | RFC 8785 JCS 实现 | 幂等键结构稳定性 |
| 集成形态 | 装饰器 / 上下文管理器包 tool handler | 能力最全、侵入最小 |
| Phase 2 proxy | TypeScript 或 Rust | 贴 MCP SDK 生态 |

---

## 7. Roadmap（按"先证痛感、再证正确性、后证分发"排序）

**Phase 0 — 验证痛感（先于写核心代码）**
- 客户访谈：找 5–10 个生产跑自主 agent 的团队，问"过去 3 个月有没有因 agent 崩溃/重试造成可量化的重复外部操作损失"。找不到流血的人 → 停。
- 建 **Provider idempotency 支持矩阵 + probe 端点普查**（GitHub / Slack / 邮件 / Stripe / 典型 SaaS），确认 §3/§4 的 probe 到底有多可行。这张表本身就是产品壁垒的雏形。

**Phase 1 — Core Ledger MVP（Python + Postgres）**
- effects + effect_journal 表、状态机 + 转移表、UNIQUE + first-writer-wins + lease。
- 显式/派生 idempotency key（JCS + 字段白名单）、结果缓存回放、同 key 异 payload 拒绝。
- 装饰器 API：`@openonce.effect(tool="github.create_pr", idempotency_fields=[...], approval="required-if-...")`。
- 锁定**一类**高价值非幂等副作用（建议：对外通信/邮件 或 支付/退款）做端到端 demo。

**Phase 2 — Recovery + Reconciliation**
- 后台 completer（可配宽限期）、UNKNOWN → probe → 分支、HUMAN_REVIEW 队列。
- 对账报表：`可查询审计轨迹`——"那笔退款到底成没成"。**这才是真正的产品价值**，idempotency 只是入场券。

**Phase 3 — 分发**
- MCP proxy（sidecar）零侵入形态；LangGraph / OpenAI Agents SDK 适配器。

---

## 8. 被证伪、不要写进代码/文案的说法

- ❌ "idempotency 保证副作用只发生一次，无论调用多少次" —— 只去重"同 key 的重试"，不是魔法 exactly-once。
- ❌ "唯一约束单独就保证 at-most-once" —— 必要非充分，还需原子阶段 + first-writer-wins。
- ❌ "在单个 ACID 事务内记录 token + 所有 mutation" —— 外部副作用进不了本地事务。
- ❌ "崩溃后无重复工作" —— at-least-once 本质决定副作用可能重放，外部幂等键才是防线。
- ❌ "Temporal/Cloudflare 已原生防重复副作用" —— 它们是 at-least-once，明确要求你自己把 activity 写成幂等。

## 9. 待实证解决的 open questions
1. probe/read-back 对无 provider_id、无 settlement feed 的 SaaS 的真实可行性与 false-negative 率。
2. fuzzy 匹配的误配率与安全阈值。
3. 宽限期在"长任务 vs 快重试"混合负载下的取值 / 是否按 tool 类别可配。
4. OpenOnce 内建粒度边界：纯 ledger+key 库 vs 完整 proxy 缓存回放。
