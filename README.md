# Vera Challenger — magicpin AI Challenge Submission

A deterministic, stateful message engine built for the [magicpin AI Challenge](https://magicpin.com/vera/ai-challenge).

**The deployed bot uses no LLM, external API calls, or third party dependencies** — it runs on Python 3.9+ standard library. Its deterministic rule based design avoids model generated hallucinations and keeps message composition comfortably within the challenge timeout.

## Approach

- **Trigger kind dispatch, not one prompt.** Each trigger kind in the dataset, plus family patterns and a grounded generic fallback, routes to a dedicated composer. The composer first determines the angle — why now, which merchant fact anchors it, and which category voice carries it — before constructing the message. Previously unseen judge injected trigger kinds can still compose using the trigger's own facts and the merchant's live numbers.

- **Grounding is structural.** Numbers in messages are read from the pushed context at compose time, including views, calls, CTR, peer statistics, offer titles, digest items, trigger payload values, and customer aggregates. Nothing is invented. Derived totals or counts are omitted when the required source data is not present.

- **Adaptive by construction.** `/v1/context` version bumps replace state atomically, so a new digest item or updated performance snapshot is used by the very next composition. There is no cached copy that can become stale.

- **Conversation brain.** `/v1/reply` handles auto replies, intent transitions, hostile or opt out messages, off topic questions, objections, commitment messages, turn budgets, and verbatim repeat protection. Auto reply loops use a probe → wait → end flow, while hostile or opt out messages trigger immediate termination and merchant suppression.

- **Voice and language.** Category specific styles include clinical peer, warm practical, operator, coach, and precise pharmacist. Owner first names and honorifics are used where appropriate, while customer facing messages respect `language_pref` and use `send_as: merchant_on_behalf`.

## Validation

The deployed Render service was tested using the complete regression suite against the production URL.

**69/69 checks passed**

The production suite covers:

- API schema and endpoint behavior
- Context versioning and idempotency
- All 25 dataset triggers
- Suppression and expiry
- Conversation handling
- Auto reply detection
- Intent transitions
- Hostile and opt out handling
- Off topic responses
- Objection handling
- Adaptive context injection
- Deterministic composition

## Files

| File | What it is |
|---|---|
| `bot.py` | HTTP server and state management for `/v1/healthz`, `/v1/metadata`, `/v1/context`, `/v1/tick`, `/v1/reply`, and `/v1/teardown` |
| `vera_engine.py` | Deterministic composer for 25+ trigger kinds, grounded fallbacks, and the conversation brain |
| `local_test.py` | 69 check regression suite covering HTTP behavior and challenge scenarios |
| `judge_simulator.py` | Official style LLM judge simulator for subjective quality evaluation |
| `dataset/` | Challenge categories, merchants, customers, and trigger contexts |
| `examples/` | API examples and challenge case studies |
| `challenge-brief.md` | Challenge product specification |
| `challenge-testing-brief.md` | Challenge testing specification |
| `engagement-design.md` | Engagement design reference |
| `engagement-research.md` | Engagement research reference |

## Run locally

```bash
python bot.py
```

The server listens on port `8080` by default, or the port specified by the `PORT` environment variable.

Run the complete regression suite:

```bash
python local_test.py
```

To test against a deployed instance:

```bash
python local_test.py https://your-deployed-url
```

## LLM Judge

The bot itself does not require an LLM.

The repository also includes `judge_simulator.py`, an official style LLM based evaluation tool for subjective message quality.

Configure:

- `BOT_URL`
- `LLM_PROVIDER`
- `LLM_MODEL`
- `LLM_API_KEY`

The judge uses `SIM_NOW` to pin simulated time to the challenge dataset era:

```text
2026-04-26T10:30:00Z
```

Before running a fresh judge evaluation, reset the bot state with:

```text
POST /v1/teardown
```

This ensures suppression and conversation state from previous runs does not affect the evaluation.

## Deployment

The bot requires only Python 3.9+ standard library and does not require a `requirements.txt`.

It can be deployed using services such as Render, Railway, Fly.io, or run locally through ngrok.

For Render, the start command is:

```bash
python bot.py
```

The bot reads the deployment port from the `PORT` environment variable.

### Current production deployment

```text
https://magicpin-vera-bot-ibgk.onrender.com
```

The deployed instance has been verified using the full regression suite with **69/69 checks passing**.

## Pre flight checklist

- [x] All required endpoints implemented
- [x] Correct API schemas
- [x] Context versioning and idempotency
- [x] Trigger expiry handling
- [x] Suppression key handling
- [x] Same tick suppression deduplication
- [x] Merchant send limits
- [x] Conversation state handling
- [x] Auto reply detection
- [x] Intent transition handling
- [x] Hostile and opt out handling
- [x] Objection handling
- [x] Verbatim repeat protection
- [x] Adaptive context injection
- [x] Deterministic composition
- [x] All 25 dataset triggers tested
- [x] Production deployment verified
- [x] Production regression suite: **69/69 passed**
- [ ] Submit public URL on the challenge page and keep it live
