# Voice platform ↔ backend contract

Any voice platform (or your own pipeline) connects to the backend with two kinds of
HTTPS call. Get the prompt, first message and tool schemas with
`uv run python -m scripts.export_tools` (writes `agent_config.json`).

## Auth (every request)

```
Authorization: Bearer <TOOL_WEBHOOK_SECRET>
```
`X-Webhook-Secret: <TOOL_WEBHOOK_SECRET>` also works. Wrong or missing secret → `401`.
If the server has no secret configured it refuses everything with `503`.

## 1. Tool calls — `POST /tools/{tool_name}`

Request:
```json
{
  "call_id": "the platform's id for this phone call",
  "caller_number": "+919822012345",
  "arguments": { "locality": "Wakad" }
}
```
`arguments` is exactly what the LLM produced for that tool (schemas in `agent_config.json`).
Send the same `call_id` for every tool call in one phone call and in the end-of-call report.

| Response | Meaning |
|---|---|
| `200` + JSON object | The tool result. Give it to the LLM as-is, e.g. `{"result": "in"}` |
| `404` | Unknown tool name |
| `422` `{"error": "invalid arguments", "fields": [...]}` | The LLM sent bad arguments; tell it to retry |
| `500` `{"error": "temporary problem, please continue"}` | Backend fault; the agent should carry on or offer a callback |

Tools never return prices. `book_consultation` returns `{"confirmed": false, "reason": ...}`
rather than an error when booking fails.

## 2. End of call — `POST /webhooks/end-of-call`

Send once when the call ends, even if the agent never called `log_call`.

```json
{
  "call_id": "same id as the tool calls",
  "caller_number": "+919822012345",
  "started_at": "2026-09-03T05:20:00Z",
  "ended_at": "2026-09-03T05:26:00Z",
  "ended_reason": "caller_hangup",
  "transferred": false,
  "pickup_seconds": 3,
  "platform_cost_usd": 0.42,
  "platform_cost_inr": null,
  "summary": "optional",
  "transcript": "optional plain text; built from messages if omitted",
  "messages": [
    {"role": "agent", "text": "Hello, thank you for calling Aangan Studio..."},
    {"role": "caller", "text": "2BHK ka interior kitna padega?"}
  ]
}
```
Only `call_id` is required. `messages` is what the price-leak audit and the
PRICING_ASKED flag read, so send it whenever the platform can.

What the backend does with it:
- logs the call
- computes the cost
- flags any price the agent said, in English or Hindi
- alerts the front desk on Telegram if a qualified call has no designer handoff

## Transfers to a person

Transfers are a platform feature. `lookup_caller` returns `office_hours_now`, so the
agent knows whether to transfer (office hours) or call `route_human_review` (after hours).
Set `transferred: true` in the end-of-call report so the dashboard counts it.
