# OpenOnce

**Efectos secundarios durables para las llamadas a herramientas de agentes de IA.**

[English](README.md) | [日本語](README.ja.md) | [中文](README.zh.md) | **Español**

---

Tu agente envía un correo, abre un PR, emite un reembolso — y entonces el
proceso se cae, el LLM reintenta, o dos workers compiten. ¿El efecto ocurrió?
¿Debería ejecutarse otra vez?

La ventana más peligrosa de cualquier sistema de agentes es la que va desde
*"la llamada a la herramienta ya salió"* hasta *"el resultado quedó
registrado"*. Si el proceso se cae dentro de esa ventana, te enfrentas a la
pregunta que ninguna política de reintentos puede responder: **¿ocurrió?**

OpenOnce envuelve cada llamada a herramienta en un ciclo de vida a prueba de
caídas, de modo que:

- **Los duplicados se reproducen (replay) en lugar de re-ejecutarse.** Misma
  intención, mismo recibo — incluidos los fallos (un 400 sigue siendo un 400).
- **Los resultados ambiguos nunca se reintentan a ciegas.** Un timeout en
  mitad de un cobro deja el efecto aparcado como `UNKNOWN`; un reconciliador
  lo resuelve contra el *mundo externo*, o lo decide un humano.
- **Cada efecto deja un rastro de recibos auditable.** Un diario append-only
  de cada transición de estado, aprobación y sondeo.

**No** es un motor de workflows. Sin orquestación, sin colas de tareas, sin
servidor — una biblioteca y una tabla SQLite/Postgres, integrada en el
framework de agentes que ya uses.

## La garantía honesta

Los efectos secundarios exactly-once contra sistemas externos son físicamente
imposibles desde un proceso local. OpenOnce te da lo más fuerte que existe:

> **ejecución at-least-once + idempotencia + reconciliación**

```
Planned → PolicyChecked → (ApprovalGranted) → Started
        → ReceiptRecorded → Committed

¿caída entre Started y ReceiptRecorded?
        → Unknown → sondear al proveedor → Commit / Rearmar / HumanReview
```

## Instalación

```bash
pip install openonce             # núcleo solo stdlib, SQLite incluido
pip install openonce[postgres]   # almacén de producción
```

## Inicio rápido

```python
import openonce

oo = openonce.OpenOnce("openonce.db")   # o ":memory:" — cero infraestructura

@oo.effect(tool="github.create_pr", idempotency_fields=["owner", "repo", "title"])
def create_pr(owner: str, repo: str, title: str, body: str) -> dict:
    ...  # tu código real de herramienta

with oo.scope("run-2026-07-02-a"):          # liga la deduplicación a esta ejecución
    create_pr(owner="acme", repo="api", title="Fix login", body="...")
    # El LLM reintenta con el cuerpo reescrito — misma huella de intención, replay:
    create_pr(owner="acme", repo="api", title="Fix login", body="reworded")
```

`idempotency_fields` es la **huella de la intención**: solo esos campos entran
en la clave, así que el ruido del LLM (prosa, timestamps) no rompe la
deduplicación. Todo lo demás en la derivación de claves es estricto —
canonicalización RFC 8785, floats rechazados (usa enteros en unidades
mínimas), y las claves derivadas exigen un scope de ejecución para que dos
*ejecuciones* que legítimamente quieren la misma llamada no se fusionen en
silencio.

Los handlers async funcionan de forma idéntica, y el event loop nunca se
bloquea:

```python
@oo.effect(tool="email.send", idempotency_fields=["to", "subject"])
async def send(to: str, subject: str, body: str) -> str:
    ...
```

## Tres clases de fallo, explícitas

| clase | significado | comportamiento |
|---|---|---|
| cualquier excepción | fallo definitivo (error de negocio) | cacheado y **reproducido** para la misma clave |
| `RetryableEffectError`, errores de red en fase de conexión | definitivamente **no** ocurrió | re-ejecutado hasta `max_attempts` |
| `UnknownOutcomeError`, timeouts de lectura y desconexiones | **puede** haber ocurrido | aparcado en `UNKNOWN`, reconciliado, nunca reintentado a ciegas |

La clasificación entiende las bibliotecas del mundo real:
`requests.ReadTimeout`, `httpx.TimeoutException` y compañía se reconocen por
nombre de clase a lo largo del MRO de la excepción — un timeout de lectura se
aparca como `UNKNOWN`, un timeout de conexión (no se envió nada) se reintenta
automáticamente. Totalmente configurable, y un `raise` explícito siempre gana.

## Aprobaciones — reentrantes por diseño

```python
oo = openonce.OpenOnce("openonce.db",
                       policy=openonce.require_approval_for(["stripe.*"]))

try:
    refund(charge="ch_1", amount_cents=500)
except openonce.ApprovalPending as p:
    notify_human(p.effect_id)

# después, tras oo.approve(effect_id):
refund(charge="ch_1", amount_cents=500)   # misma llamada, misma clave → se ejecuta una vez
```

No hay ruta de reanudación separada: el agente simplemente reintenta la
llamada, coincide con la misma clave de idempotencia, y continúa.

## Resultados desconocidos — la razón de ser de esta biblioteca

```python
@oo.effect(tool="stripe.charge")
def charge(amount_cents: int) -> dict:
    ctx = openonce.current_effect()
    # Pasa ctx.provider_key como cabecera Idempotency-Key de Stripe: la única
    # defensa firme contra duplicados una vez que la petición salió del proceso.
    return stripe.PaymentIntent.create(..., idempotency_key=ctx.provider_key)
```

Si `charge` sufre un timeout después de enviar la petición, el efecto se
aparca en `UNKNOWN`. Un `Reconciler` sondea al proveedor:

```python
rec = openonce.Reconciler(oo.store, grace_seconds=300)
rec.register("stripe.charge", StripeProber.from_api_key(STRIPE_KEY))
rec.run_once()   # HAPPENED → commit con recibo; NOT_HAPPENED → rearmar;
                 # no concluyente / sin prober → revisión humana. Nunca un reintento a ciegas.
```

O ejecútalo como demonio directamente desde la CLI:

```console
$ openonce --db openonce.db reconcile --probers myapp.probers:PROBERS --watch
```

## Proveedores: tres niveles de honestidad

Sondear "¿este efecto realmente ocurrió?" es conocimiento específico de cada
proveedor. Los probers incluidos son implementaciones de referencia de los
tres niveles:

| nivel | ejemplo | base del sondeo | un miss significa |
|---|---|---|---|
| 1 — claves de idempotencia nativas | **Stripe** | búsqueda por metadata (+ respaldo de deduplicación de 24h por provider-key) | no concluyente dentro de la ventana de retraso del índice, después not-happened |
| 2 — clave natural de negocio | **GitHub PR** (`owner`, `repo`, `head`) | lectura autoritativa del almacén primario | genuinamente not-happened |
| 2/3 — clave controlada por el emisor | **Email** (`Message-ID` determinista) | búsqueda en el almacén de enviados | not-happened solo si el almacén de enviados es autoritativo; SMTP puro escala a un humano, siempre |

## Integraciones con frameworks

**LangGraph** — un decorador convierte un handler en efecto durable y
herramienta de LangGraph a la vez. El scope se liga al `thread_id`;
`ApprovalPending` se mapea a `interrupt()`, y como las aprobaciones de
OpenOnce son reentrantes, el replay del nodo al reanudar en LangGraph es
exactamente lo correcto:

```python
from openonce.integrations.langgraph import effect_tool

@effect_tool(oo, tool="stripe.refund", idempotency_fields=["charge"])
def refund(charge: str) -> str:
    """Refund a Stripe charge."""
    ...

graph.invoke(Command(resume={"approved": True, "by": "eric"}), config)
```

**OpenAI Agents SDK** — el flujo de control se convierte en salidas JSON
estructuradas que el modelo lee y sobre las que actúa: `approval_required` le
enseña a informar al usuario y volver a llamar con los mismos argumentos tras
la aprobación; `outcome_unknown` instruye *NO reintentar*. El `dedup="call"`
opcional acota la deduplicación a una única decisión del modelo mediante el
`tool_call_id`:

```python
from openonce.integrations.openai_agents import OpenOnceRunContext, effect_function_tool

@effect_function_tool(oo, tool="stripe.refund", idempotency_fields=["charge"])
def refund(charge: str) -> str:
    """Refund a Stripe charge."""
    ...

result = Runner.run_sync(agent, "refund ch_1",
                         context=OpenOnceRunContext(openonce_scope="conv-123"))
```

## Los recibos, visibles

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

`--db` acepta una ruta SQLite o un DSN de Postgres.

## Diseño

Linaje: las claves de idempotencia de Stripe, la AWS Builders' Library (IDs de
petición del cliente, rechazo por discrepancia de parámetros), el
rocket-rides-atomic de brandur (fases atómicas + completer), la semántica de
Activities de Temporal (at-least-once + "hazlo idempotente") — reducido a la
escala de una biblioteca.

La divergencia clave respecto a Temporal: **Temporal reproduce código,
OpenOnce reproduce datos.** El "workflow" de un agente es inferencia de LLM y
no puede re-ejecutarse de forma determinista, así que OpenOnce persiste a
nivel de efecto y nunca impone restricciones de determinismo a tu código.

## Estado

Alpha. La semántica (deduplicación, replay, aprobación, UNKNOWN/reconciliación,
first-writer-wins bajo concurrencia) está cubierta por una suite de tests que
se ejecuta de forma idéntica contra los almacenes en memoria, SQLite y
Postgres, más tests de integración contra los runtimes reales de LangGraph y
del OpenAI Agents SDK.

## Licencia

[MIT](LICENSE)
