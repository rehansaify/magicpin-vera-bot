# Vera Challenger — magicpin AI Challenge submission

A deterministic, stateful message-engine bot for the [magicpin AI Challenge](https://magicpin.com/vera/ai-challenge).
**No LLM, no external calls, no dependencies** — pure Python 3.9+ stdlib, so it deploys anywhere and can never
hallucinate a fact, leak merchant data, or miss the 30s timeout (composition is <1 ms).

## Approach

- **Trigger-kind dispatch, not one prompt.** Every trigger kind in the dataset (plus family patterns and a
  grounded generic fallback) routes to its own composer that decides the *angle* first — why now, which merchant
  fact anchors it, which category voice carries it — then writes the message. Judge-injected kinds the bot has
  never seen still compose: the fallback surfaces the trigger's own facts + the merchant's live numbers vs peer
  benchmarks.
- **Grounding is structural.** Every number in a message is read from the pushed context at compose time
  (views/calls/CTR, peer stats, offer titles, digest items incl. newly-injected versions, trigger payload
  numbers, customer aggregates). Nothing is invented; totals/counts that the case studies "borrowed" (e.g. a
  ₹1,420 refill total) are deliberately omitted because the context doesn't contain them.
- **Adaptive by construction.** `/v1/context` version bumps replace state atomically, so a new digest item or
  shifted performance snapshot is what the very next send is composed from — there is no cached copy to go stale.
- **Conversation brain** (`/v1/reply`): auto-reply detection (verbatim repeats per merchant across conversations
  + canned-phrase markers → probe → wait 24h → end), intent transition to action mode on any commitment phrase
  (never re-qualifies), hostile/opt-out → immediate end + 30-day merchant suppression, off-topic asks (GST etc.)
  → polite boundary + redirect, objections → wait, 4-send turn budget → graceful end, verbatim-repeat guard.
- **Voice + language.** Category styles (clinical peer / warm practical / operator / coach / precise pharmacist),
  owner first names with honorifics, customer-facing messages honor `language_pref` (hi-en mix, Hindi, regional
  greetings) with `send_as: merchant_on_behalf`.

Tradeoff: a rule engine can't match a frontier LLM's fluency on arbitrary curveballs, but it is fully
deterministic (rubric requirement), free to run for a 60-min judge window, safe on privacy (§11), and its
specificity/grounding — the two heaviest-scored dimensions — don't depend on prompt luck.

## Files

| File | What it is |
|---|---|
| `bot.py` | HTTP server: `/v1/healthz`, `/v1/metadata`, `/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/teardown` |
| `vera_engine.py` | The composer (25+ trigger kinds + fallbacks) and the conversation brain |
| `local_test.py` | Self-test replicating `judge_simulator.py`'s HTTP scenarios without an LLM key |

## Run locally

```bash
python bot.py            # listens on :8080 (or: PORT=9000 python bot.py)
python local_test.py     # 69 checks: warmup, idempotency, all 25 triggers, replays, adaptive injection
```

To run the **official** judge (needs your LLM key): set `BOT_URL` / `LLM_PROVIDER` / `LLM_MODEL` at the top of
`judge_simulator.py`, export your key first (`export LLM_API_KEY="..."` then `python judge_simulator.py`) — the key
is never stored in the file. `SIM_NOW` (default `2026-04-26T10:30:00Z`) pins the simulated tick clock to the
dataset's era, matching the documented judge behavior (testing-brief §2.2/§4 — the judge advances *simulated*
time); set `SIM_NOW=""` to use the real UTC clock. `POST /v1/teardown` the bot before each judge run so
suppression/auto-reply/conversation state starts clean.

## Deploy (pick one)

- **ngrok (fastest):** `python bot.py` then `ngrok http 8080` → submit the https URL.
- **Render/Railway/Fly:** push this folder, start command `python bot.py`, port from `$PORT` (already handled).
  No `requirements.txt` needed — stdlib only.

Before submitting, set your identity (also overridable via env):
`TEAM_NAME`, `TEAM_MEMBERS`, `CONTACT_EMAIL` at the top of `bot.py` (`/v1/metadata`).

## Pre-flight checklist (challenge-testing-brief §12)

- [x] All 5 endpoints implemented, correct schemas
- [x] `/v1/context` idempotent on `(scope, context_id, version)`; 409 `stale_version`; higher version replaces
- [x] `/v1/tick` returns instantly; `{"actions": []}` when nothing is worth sending; ≤20 actions; ≤2 merchant
      sends per merchant per tick (customer sends separate); suppression-key dedup; expiry respected
- [x] `/v1/reply` returns `send | wait | end` with rationale, within 30s, no verbatim repeats
- [x] `judge_simulator`-equivalent local scenarios pass (auto-reply hell, intent transition, hostile, curveball)
- [x] Adaptive injection: new category version 2 + unseen trigger kinds composed correctly on the next tick
- [ ] Submit your public URL on the challenge page and keep it live
