# OpenOnce

**为 AI Agent 工具调用提供可靠的（durable）副作用层。**

[English](README.md) | [日本語](README.ja.md) | **中文** | [Español](README.es.md)

---

你的 agent 发送了邮件、创建了 PR、执行了退款——然后进程崩溃、LLM 重试、或者两个
worker 竞争执行。副作用到底发生了没有？该不该再执行一次？

任何 agent 系统里最危险的窗口，是从*"工具调用已经发出"*到*"结果已被记录"*之间的
那一段。在这个窗口里崩溃，你就会面对任何重试策略都无法回答的问题：**它发生了吗？**

OpenOnce 把工具调用包进一个崩溃安全的生命周期，做到：

- **重复调用回放结果，而不是重新执行。** 相同的意图，相同的回执——包括失败
  （一次 400 永远是同一个 400）。
- **结果不明的调用绝不盲目重试。** 扣款途中超时，effect 停靠为 `UNKNOWN`；由
  reconciler 向*外部世界*求证解决，或者交给人来判断。
- **每个 effect 都留下可审计的回执轨迹。** 每次状态跃迁、审批、探查都记入
  append-only 日志。

它**不是** workflow engine。没有编排、没有任务队列、没有服务器——就是一个库加一张
SQLite/Postgres 表，直接嵌入你已经在用的 agent 框架。

## 诚实的保证

对外部系统的 exactly-once 副作用在本地进程中物理上不可能。OpenOnce 提供的是
现实中存在的最强保证：

> **at-least-once 执行 + 幂等 + 对账（reconciliation）**

```
Planned → PolicyChecked → (ApprovalGranted) → Started
        → ReceiptRecorded → Committed

在 Started 和 ReceiptRecorded 之间崩溃？
        → Unknown → 探查 provider → Commit / 重新武装 / HumanReview
```

## 安装

```bash
pip install openonce             # 核心仅依赖标准库，内置 SQLite
pip install openonce[postgres]   # 生产级存储
```

## 快速开始

```python
import openonce

oo = openonce.OpenOnce("openonce.db")   # 或 ":memory:" — 零基础设施

@oo.effect(tool="github.create_pr", idempotency_fields=["owner", "repo", "title"])
def create_pr(owner: str, repo: str, title: str, body: str) -> dict:
    ...  # 你真正的工具代码

with oo.scope("run-2026-07-02-a"):          # 把去重绑定到本次 agent 运行
    create_pr(owner="acme", repo="api", title="Fix login", body="...")
    # LLM 改写正文后重试 —— 意图指纹相同，回放：
    create_pr(owner="acme", repo="api", title="Fix login", body="reworded")
```

`idempotency_fields` 是**意图指纹**：只有这些字段进入幂等键，LLM 的噪声（文案、
时间戳）不会击穿去重。键派生的其余部分都是严格的——RFC 8785 规范化、拒绝浮点数
（金额请用最小货币单位的整数）、派生键强制要求 run scope（两个合法需要同一调用的
*运行*不会被静默合并）。

async handler 语义完全一致，且事件循环永不被阻塞：

```python
@oo.effect(tool="email.send", idempotency_fields=["to", "subject"])
async def send(to: str, subject: str, body: str) -> str:
    ...
```

## 显式区分三类失败

| 类别 | 含义 | 行为 |
|---|---|---|
| 任意异常 | 确定性失败（业务错误） | 缓存并对同一键**回放** |
| `RetryableEffectError`、连接阶段网络错误 | 确定**没有**发生 | 重新执行至 `max_attempts` |
| `UnknownOutcomeError`、读取阶段超时与断连 | **可能**已发生 | 停靠 `UNKNOWN`，走对账，绝不盲目重试 |

分类器理解真实世界的库：`requests.ReadTimeout`、`httpx.TimeoutException` 等按
异常 MRO 中的类名识别——读超时停靠 `UNKNOWN`，连接超时（什么都没发出）自动重试。
完全可配置，显式 `raise` 永远优先。

## 审批——从设计上可重入

```python
oo = openonce.OpenOnce("openonce.db",
                       policy=openonce.require_approval_for(["stripe.*"]))

try:
    refund(charge="ch_1", amount_cents=500)
except openonce.ApprovalPending as p:
    notify_human(p.effect_id)

# 稍后，oo.approve(effect_id) 之后：
refund(charge="ch_1", amount_cents=500)   # 同一调用，同一键 → 恰好执行一次
```

没有独立的恢复路径：agent 只需重试同一调用，命中同一幂等键，继续执行。

## 结果不明——这个库存在的理由

```python
@oo.effect(tool="stripe.charge")
def charge(amount_cents: int) -> dict:
    ctx = openonce.current_effect()
    # 把 ctx.provider_key 作为 Stripe 的 Idempotency-Key 头传入：这是请求离开
    # 进程之后对抗重复的唯一硬防线。
    return stripe.PaymentIntent.create(..., idempotency_key=ctx.provider_key)
```

如果 `charge` 在请求发出后超时，effect 停靠为 `UNKNOWN`。`Reconciler` 向
provider 探查：

```python
rec = openonce.Reconciler(oo.store, grace_seconds=300)
rec.register("stripe.charge", StripeProber.from_api_key(STRIPE_KEY))
rec.run_once()   # HAPPENED → 带回执提交；NOT_HAPPENED → 重新武装；
                 # 无法判定 / 无 prober → 人工审查。绝不盲目重试。
```

也可以直接从 CLI 以守护进程方式运行：

```console
$ openonce --db openonce.db reconcile --probers myapp.probers:PROBERS --watch
```

## Provider：三档诚实度

"这个 effect 到底发生了没有？"的探查是 provider 特定的知识。内置的 prober 是
三个档位的参考实现：

| 档位 | 示例 | 探查依据 | miss 的含义 |
|---|---|---|---|
| 1 — 原生幂等键 | **Stripe** | metadata 搜索（+24h provider-key 去重兜底） | 索引滞后窗口内不可判定，之后为 not-happened |
| 2 — 自然业务键 | **GitHub PR**（`owner`、`repo`、`head`） | 主存储权威读取 | 真正的 not-happened |
| 2/3 — 发送方控制的键 | **Email**（确定性 `Message-ID`） | 已发送存储搜索 | 仅当发送存储权威时才是 not-happened；裸 SMTP 永远升级给人 |

## 框架集成

**LangGraph** —— 一个装饰器让 handler 同时成为 durable effect 和 LangGraph
工具。scope 绑定到 `thread_id`；`ApprovalPending` 映射到 `interrupt()`，而
OpenOnce 的审批是可重入的，所以 LangGraph "恢复时重跑 node" 恰好就是正确行为：

```python
from openonce.integrations.langgraph import effect_tool

@effect_tool(oo, tool="stripe.refund", idempotency_fields=["charge"])
def refund(charge: str) -> str:
    """Refund a Stripe charge."""
    ...

graph.invoke(Command(resume={"approved": True, "by": "eric"}), config)
```

**OpenAI Agents SDK** —— 控制流变成模型可读、可执行的结构化 JSON 工具输出：
`approval_required` 教模型"告知用户，审批后用相同参数再调一次"；
`outcome_unknown` 明确指示*禁止重试*。可选的 `dedup="call"` 通过 `tool_call_id`
把去重收窄到单次模型决策：

```python
from openonce.integrations.openai_agents import OpenOnceRunContext, effect_function_tool

@effect_function_tool(oo, tool="stripe.refund", idempotency_fields=["charge"])
def refund(charge: str) -> str:
    """Refund a Stripe charge."""
    ...

result = Runner.run_sync(agent, "refund ch_1",
                         context=OpenOnceRunContext(openonce_scope="conv-123"))
```

## 回执，看得见

```console
$ openonce --db openonce.db review
* eff_76cd…  requires_approval  stripe.refund   attempt 0/3  2026-07-02 12:01:07Z
1 effect(s) need a human. approve/deny by effect_id.

$ openonce --db openonce.db show eff_fd61…
  journal:
    2026-07-02 12:01:07Z        planned -> approved
    2026-07-02 12:01:07Z       approved -> started
    2026-07-02 12:01:09Z        started -> unknown   {"error": "TimeoutError(...)"}
    2026-07-02 12:03:21Z        unknown -> receipt_recorded  {"probe": "happened", ...}
    2026-07-02 12:03:22Z receipt_recorded -> committed
```

`--db` 接受 SQLite 路径或 Postgres DSN。

## 设计

血统：Stripe 的幂等键、AWS Builders' Library（客户端请求 ID、参数不匹配拒绝）、
brandur 的 rocket-rides-atomic（原子阶段 + completer）、Temporal 的 Activity
语义（at-least-once + "自己做成幂等"）——收缩到一个库的尺度。

与 Temporal 的关键分歧：**Temporal 重放代码，OpenOnce 重放数据。** agent 的
"workflow" 是 LLM 推理，无法确定性重跑，所以 OpenOnce 在 effect 层持久化，
对你的代码不施加任何确定性约束。

## 状态

Alpha。核心语义（去重、回放、审批、UNKNOWN/对账、并发下 first-writer-wins）由
同一套测试在内存、SQLite、Postgres 三个存储上跑通，另有针对真实 LangGraph 与
OpenAI Agents SDK 运行时的集成测试。

## 协议

[MIT](LICENSE)
