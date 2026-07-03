"""OpenOnce + bare OpenAI SDK (chat completions tool loop) — no adapter needed.

Run offline (no API key needed):  uv run python examples/openai_sdk.py

In a bare SDK tool loop YOU execute the tool calls, so the core decorator is
the whole integration: wrap each handler with @oo.effect and dispatch through
it. Two OpenOnce concepts map onto the loop:

- scope        -> your conversation/session id (one intent boundary per chat)
- tool_call.id -> optionally narrows the scope to a single model decision,
                  so retries of the SAME call dedupe while two independent
                  decisions with identical args both execute.
"""

import json

import openonce

oo = openonce.OpenOnce()
SENT: list[str] = []  # the "external world"


# ── Tool handlers: the decorator IS the integration ──────────────────────────


@oo.effect(idempotency_fields=["to", "subject"])
def send_email(to: str, subject: str, body: str) -> str:
    SENT.append(to)
    return f"sent to {to}"


HANDLERS = {"send_email": send_email}

TOOLS = [
    {  # the ordinary OpenAI tool schema — nothing OpenOnce-specific
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "Send an email.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["to", "subject", "body"],
            },
        },
    }
]


def execute_tool_call(conversation_id: str, call) -> str:
    """Dispatch one tool call durably. This is the only glue you write."""
    args = json.loads(call.function.arguments)
    handler = HANDLERS[call.function.name]
    # scope = conversation; append the call id to make each model decision
    # its own intent (retries of the same call still share the id -> dedupe).
    with oo.scope(f"{conversation_id}/call:{call.id}"):
        try:
            return json.dumps(handler(**args))
        except openonce.ApprovalPending as p:
            return json.dumps(
                {
                    "openonce": "approval_required",
                    "effect_id": p.effect_id,
                    "instructions": "Tell the user; after approval, call again "
                    "with the same arguments.",
                }
            )
        except openonce.EffectUnknown as u:
            return json.dumps(
                {
                    "openonce": "outcome_unknown",
                    "effect_id": u.record.effect_id,
                    "instructions": "Do NOT retry; a reconciler will resolve it.",
                }
            )


# ── Real usage (needs OPENAI_API_KEY): the standard tool loop ────────────────
#
#   from openai import OpenAI
#   client = OpenAI()
#   messages = [{"role": "user", "content": "email a@b.c saying hi"}]
#   while True:
#       resp = client.chat.completions.create(
#           model="gpt-4o", messages=messages, tools=TOOLS)
#       msg = resp.choices[0].message
#       if not msg.tool_calls:
#           break
#       messages.append(msg)
#       for call in msg.tool_calls:
#           messages.append({
#               "role": "tool", "tool_call_id": call.id,
#               "content": execute_tool_call("conv-123", call),
#           })
#
# ── Offline demo: simulate the model retrying the same tool call ─────────────


class FakeCall:  # shaped like openai's ChatCompletionMessageToolCall
    def __init__(self, id: str, name: str, arguments: str) -> None:
        self.id = id
        self.function = type("F", (), {"name": name, "arguments": arguments})()


args = json.dumps({"to": "a@b.c", "subject": "hi", "body": "hello!"})

# The model (or your retry logic) issues the SAME tool call twice — e.g. after
# a dropped response. Same call id -> same effect -> one send:
r1 = execute_tool_call("conv-123", FakeCall("call_abc", "send_email", args))
r2 = execute_tool_call("conv-123", FakeCall("call_abc", "send_email", args))
assert r1 == r2 and SENT == ["a@b.c"]
print(f"1) same tool_call retried: replayed, one send  -> {r1}")

# A genuinely new model decision (new call id) with identical args executes:
execute_tool_call("conv-123", FakeCall("call_xyz", "send_email", args))
assert SENT == ["a@b.c", "a@b.c"]
print("2) new decision, same args: executed (two intents, two sends)")
