# OpenOnce

**AI エージェントのツール呼び出しに、耐久性のある副作用を。**

[English](README.md) | **日本語** | [中文](README.zh.md) | [Español](README.es.md)

---

エージェントがメールを送信し、PR を作成し、返金を実行する——その直後にプロセスが
クラッシュし、LLM がリトライし、あるいは 2 つのワーカーが競合する。副作用は実行
されたのか？ もう一度実行してよいのか？

エージェントシステムで最も危険な窓は、*「ツール呼び出しが外に出た」* 瞬間から
*「結果が記録された」* 瞬間までの間です。この窓の中でクラッシュすると、どんな
リトライポリシーも答えられない問いに直面します：**実行されたのか？**

OpenOnce はツール呼び出しをクラッシュセーフなライフサイクルで包みます：

- **重複は再実行ではなく再生（リプレイ）される。** 同じ意図には同じレシート——
  失敗も含めて（400 は 400 のまま）。
- **結果不明の呼び出しは決して盲目的にリトライされない。** 課金中のタイムアウトは
  effect を `UNKNOWN` として保留し、リコンサイラが*外部世界*に照会して解決する。
  それでも不明なら人間が判断する。
- **すべての effect が監査可能なレシートの履歴を残す。** 状態遷移・承認・プローブの
  すべてを追記専用ジャーナルに記録。

これはワークフローエンジンでは**ありません**。オーケストレーションもタスクキューも
サーバーもなし——ライブラリと SQLite/Postgres のテーブルだけで、既存のエージェント
フレームワークにそのまま組み込めます。

## 正直な保証

外部システムに対する exactly-once の副作用は、ローカルプロセスからは物理的に
不可能です。OpenOnce が提供するのは、実在するもののうち最強の保証です：

> **at-least-once 実行 + 冪等性 + リコンシリエーション（照合）**

```
Planned → PolicyChecked → (ApprovalGranted) → Started
        → ReceiptRecorded → Committed

Started と ReceiptRecorded の間でクラッシュ？
        → Unknown → プロバイダに照会 → Commit / 再実行可能化 / HumanReview
```

## インストール

```bash
pip install openonce             # コアは標準ライブラリのみ、SQLite 同梱
pip install openonce[postgres]   # 本番用ストア
```

## クイックスタート

```python
import openonce

oo = openonce.OpenOnce("openonce.db")   # または ":memory:" — インフラ不要

@oo.effect(tool="github.create_pr", idempotency_fields=["owner", "repo", "title"])
def create_pr(owner: str, repo: str, title: str, body: str) -> dict:
    ...  # 実際のツールコード

with oo.scope("run-2026-07-02-a"):          # 重複排除をこの実行に紐付ける
    create_pr(owner="acme", repo="api", title="Fix login", body="...")
    # LLM が本文を書き換えてリトライ —— 意図の指紋は同じなのでリプレイされる：
    create_pr(owner="acme", repo="api", title="Fix login", body="reworded")
```

`idempotency_fields` は**意図の指紋**です。指定したフィールドだけがキーに入るため、
LLM のノイズ（文章やタイムスタンプ）が重複排除を無効化することはありません。キー
導出は厳格です——RFC 8785 正規化、float は拒否（最小通貨単位の整数を使うこと）、
派生キーには実行スコープが必須（同じ呼び出しを正当に必要とする 2 つの*実行*が
静かに潰されないように）。

async ハンドラも同一のセマンティクスで動作し、イベントループは決してブロック
されません：

```python
@oo.effect(tool="email.send", idempotency_fields=["to", "subject"])
async def send(to: str, subject: str, body: str) -> str:
    ...
```

## 3 つの失敗クラスを明示的に

| クラス | 意味 | 挙動 |
|---|---|---|
| 任意の例外 | 確定的な失敗（ビジネスエラー） | キャッシュされ、同じキーに対して**リプレイ** |
| `RetryableEffectError`、接続段階のネットワークエラー | 確定的に**実行されなかった** | `max_attempts` まで再実行 |
| `UnknownOutcomeError`、読み取り段階のタイムアウト・切断 | 実行された**かもしれない** | `UNKNOWN` として保留、照合で解決、盲目的リトライは決してしない |

分類は現実のライブラリを理解します：`requests.ReadTimeout` や
`httpx.TimeoutException` などは例外の MRO 内のクラス名で認識され、読み取り
タイムアウトは `UNKNOWN` として保留、接続タイムアウト（何も送信されていない）は
自動リトライされます。完全に設定可能で、明示的な `raise` が常に優先されます。

## 承認——設計からして再入可能

```python
oo = openonce.OpenOnce("openonce.db",
                       policy=openonce.require_approval_for(["stripe.*"]))

try:
    refund(charge="ch_1", amount_cents=500)
except openonce.ApprovalPending as p:
    notify_human(p.effect_id)

# 後で oo.approve(effect_id) の後：
refund(charge="ch_1", amount_cents=500)   # 同じ呼び出し、同じキー → ちょうど 1 回実行
```

独立した再開パスはありません：エージェントは同じ呼び出しをリトライするだけで、
同じ冪等キーに到達し、処理が続行されます。

## 結果不明——このライブラリが存在する理由

```python
@oo.effect(tool="stripe.charge")
def charge(amount_cents: int) -> dict:
    ctx = openonce.current_effect()
    # ctx.provider_key を Stripe の Idempotency-Key ヘッダに渡す：リクエストが
    # プロセスを離れた後の重複に対する唯一の強力な防御。
    return stripe.PaymentIntent.create(..., idempotency_key=ctx.provider_key)
```

リクエスト送信後に `charge` がタイムアウトすると、effect は `UNKNOWN` として
保留されます。`Reconciler` がプロバイダに照会します：

```python
rec = openonce.Reconciler(oo.store, grace_seconds=300)
rec.register("stripe.charge", StripeProber.from_api_key(STRIPE_KEY))
rec.run_once()   # HAPPENED → レシート付きでコミット；NOT_HAPPENED → 再実行可能化；
                 # 判定不能 / プローバなし → 人間のレビューへ。盲目的リトライは決してしない。
```

CLI からデーモンとして直接実行することもできます：

```console
$ openonce --db openonce.db reconcile --probers myapp.probers:PROBERS --watch
```

## プロバイダ：3 段階の誠実さ

「この effect は本当に実行されたのか？」の照会はプロバイダ固有の知識です。
同梱のプローバは 3 つの階層のリファレンス実装です：

| 階層 | 例 | 照会の根拠 | ミスの意味 |
|---|---|---|---|
| 1 — ネイティブ冪等キー | **Stripe** | メタデータ検索（+24h のプロバイダキー重複排除バックストップ） | インデックス遅延窓内は判定不能、その後は not-happened |
| 2 — 自然なビジネスキー | **GitHub PR**（`owner`, `repo`, `head`） | 権威あるプライマリストアの読み取り | 本当に not-happened |
| 2/3 — 送信者制御キー | **Email**（決定的 `Message-ID`） | 送信済みストアの検索 | 送信済みストアが権威的な場合のみ not-happened；素の SMTP は永遠に人間へエスカレーション |

## フレームワーク統合

**LangGraph** — デコレータ 1 つでハンドラが耐久性のある effect と LangGraph
ツールの両方になります。スコープは `thread_id` に紐付き、`ApprovalPending` は
`interrupt()` に対応します。OpenOnce の承認は再入可能なので、LangGraph の
「再開時ノード再実行」がまさに正しい動作になります：

```python
from openonce.integrations.langgraph import effect_tool

@effect_tool(oo, tool="stripe.refund", idempotency_fields=["charge"])
def refund(charge: str) -> str:
    """Refund a Stripe charge."""
    ...

graph.invoke(Command(resume={"approved": True, "by": "eric"}), config)
```

**OpenAI Agents SDK** — 制御フローがモデルの読める構造化 JSON ツール出力に
なります：`approval_required` は「ユーザーに伝え、承認後に同じ引数で再度呼び
出す」ことをモデルに教え、`outcome_unknown` は*リトライ禁止*を指示します。
オプションの `dedup="call"` は `tool_call_id` によって重複排除を単一のモデル
決定にスコープします：

```python
from openonce.integrations.openai_agents import OpenOnceRunContext, effect_function_tool

@effect_function_tool(oo, tool="stripe.refund", idempotency_fields=["charge"])
def refund(charge: str) -> str:
    """Refund a Stripe charge."""
    ...

result = Runner.run_sync(agent, "refund ch_1",
                         context=OpenOnceRunContext(openonce_scope="conv-123"))
```

## レシートを見えるところに

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

`--db` は SQLite パスまたは Postgres DSN を受け付けます。

## 設計

系譜：Stripe の冪等キー、AWS Builders' Library（クライアントリクエスト ID、
パラメータ不一致の拒否）、brandur の rocket-rides-atomic（アトミックフェーズ +
コンプリータ）、Temporal の Activity セマンティクス（at-least-once +「冪等に
せよ」）——これらをライブラリの規模に凝縮したものです。

Temporal との決定的な違い：**Temporal はコードをリプレイし、OpenOnce はデータを
リプレイします。** エージェントの「ワークフロー」は LLM 推論であり決定論的に
再実行できないため、OpenOnce は effect のレベルで永続化し、コードに決定論の
制約を一切課しません。

## ステータス

Alpha。セマンティクス（重複排除、リプレイ、承認、UNKNOWN/照合、並行時の
first-writer-wins）は、インメモリ・SQLite・Postgres の 3 ストアに対して同一に
実行されるテストスイート、および実際の LangGraph と OpenAI Agents SDK ランタイム
に対する統合テストでカバーされています。

## ライセンス

[MIT](LICENSE)
