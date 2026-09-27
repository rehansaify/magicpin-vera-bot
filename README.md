# Magicpin Vera Challenge Bot

A candidate bot for the [magicpin AI Challenge](https://magicpin.com/vera/ai-challenge): a
deterministic, stateful "Vera" merchant assistant implementing the full candidate HTTP contract —
`POST /v1/context`, `POST /v1/tick`, `POST /v1/reply`, `GET /v1/healthz`, `GET /v1/metadata` (plus
`POST /v1/teardown`). Message composition is rule-based over the challenge's 4-context model
(category / merchant / trigger / customer), with a multi-turn conversation brain for auto-reply
detection, intent transitions, objections, and hostile or off-topic replies. Pure Python 3.9+
stdlib — no LLM calls, no dependencies, no network egress — so output is reproducible,
privacy-safe, and composition runs in milliseconds against a 30-second budget.

## Repo layout

```
bot.py                        # HTTP layer: routes, versioned context store, tick pipeline, reply routing
vera_engine.py                # deterministic composer (trigger-kind dispatch) + conversation brain
local_test.py                 # behavioral regression suite (69 checks, no LLM key needed)
judge_simulator.py            # magicpin's LLM judge harness (requires an LLM API key)
dataset/
  categories/                 # 5 CategoryContexts: dentists, salons, restaurants, gyms, pharmacies
  merchants_seed.json         # 10 merchants (2 per category)
  customers_seed.json         # 15 customers
  triggers_seed.json          # 25 triggers (21 distinct kinds, merchant- and customer-scoped)
  generate_dataset.py         # deterministic expander: seeds -> 50/200/100 contexts + test_pairs.json
challenge-brief.md            # challenge spec: product, rubric, composition contract
challenge-testing-brief.md    # HTTP/API contract and judge lifecycle
examples/api-call-examples.md # exact request/response shapes for every endpoint
examples/case-studies.md      # scored reference messages ("what good looks like")
engagement-design.md          # magicpin's internal 4-context design doc (background)
engagement-research.md        # magicpin's internal data-access research (background)
.github/workflows/keepalive.yml  # GitHub Action: pings /v1/healthz every 10 min (keeps Render warm)
```

## Architecture

One direction of dependency, three layers:

```
judge ──HTTP/JSON──► bot.py (state + transport) ──pure function calls──► vera_engine.py
                                                                        (composition + reply brain)
```

**HTTP / state layer (`bot.py`).** A stdlib `ThreadingHTTPServer`. All mutable state lives in
five in-memory structures behind a single `RLock`: the versioned `CONTEXTS` store,
`CONVERSATIONS`, `MERCHANT_MSG_COUNTS` (cross-conversation auto-reply detection),
`FIRED_SUPPRESSIONS`, and `SUPPRESSED_UNTIL`. No disk, no external services.

**Context lifecycle (`POST /v1/context`).** Idempotent on `(scope, context_id, version)`: a
same-or-lower version returns `409 stale_version` with `current_version`; a higher version
replaces the payload atomically. Malformed JSON, invalid scope, non-dict payload, or non-integer
version return structured `400`s. Composition always reads the live store at tick time, so a
version-2 push (a new digest item, shifted performance numbers) is what the very next send uses —
there is no cached copy to go stale.

**Tick pipeline (`POST /v1/tick`).** Candidates from `available_triggers` pass a filter chain:
trigger must be stored → not expired against the judge-supplied `now` → suppression key not
already fired (including duplicate keys within the same tick) → merchant not inside a 30-day
opt-out window. Survivors are sorted urgency-descending (arrival order as tiebreak), capped at
2 merchant-scoped sends per merchant and 20 actions per tick (customer-scoped sends are exempt
from the merchant cap), then composed and returned as complete actions with unique conversation
ids.

**Composition (`vera_engine.py`).** Dispatch by `trigger.kind`: exact-key table (merchant or
customer table, chosen by `trigger.scope`) → family substring match → a grounded generic composer
→ a hardcoded safe message, so a composer error can never fail a tick. Every number, offer,
slot, and date is read from the pushed context at compose time; nothing is invented.
`send_as` is stamped from scope (`vera` vs `merchant_on_behalf`), category voice is applied from
a per-slug style table (honorifics, register, emoji), and customer-facing messages honor
`identity.language_pref` (Hinglish, Hindi, regional greetings, or English).

**Reply brain (`POST /v1/reply`).** An ordered classifier decides the next move (details in the
next section). Side effects — 30-day merchant suppression on opt-out, send/turn budgets, the
anti-repetition guard — are applied by `bot.py` after the engine returns.

**Determinism.** No RNG and no wall-clock reads inside composition; the tick's `now` is the only
time source. Identical request sequences produce byte-identical bodies, selection, ordering,
templates, and conversation ids across independent processes.

## Constraints enforced in code

| Constraint | Enforced in |
|---|---|
| Context idempotency, `409 stale_version` + `current_version`, atomic higher-version replace | `_handle_context` |
| Trigger expiry respected against the judge-supplied `now` | `_handle_tick` |
| Suppression-key dedup — across ticks **and** within a single tick | `_handle_tick` |
| 30-day merchant suppression after opt-out / hostile reply | `handle_reply` → `SUPPRESSED_UNTIL` |
| ≤ 2 merchant sends per merchant per tick; ≤ 20 actions per tick | `_handle_tick` |
| Deterministic urgency-descending candidate ordering | `_handle_tick` |
| Composer exceptions degrade to grounded generic → hardcoded safe message | `compose()` |
| `/v1/reply` returns 200 even on internal error (never hangs the judge) | `do_POST` |
| No verbatim body re-sent within a conversation (`(update #N)` suffix) | `_handle_reply` |
| Send budget (4 bot sends) and turn budget (turn ≥ 6) → graceful end | `handle_reply` |
| Missing optional payload fields never render as raw `None` (clauses are omitted) | composer guards + `_pct` |
| Bodies are built only from context fields — no URL-emitting path exists, whitespace collapsed | `_act` / composers |
| Full state wipe on `POST /v1/teardown` (privacy rule §11) | `do_POST` |

## Multi-turn conversation handling

`classify_reply()` in `vera_engine.py` evaluates in strict priority order:

| # | Detects | Bot move |
|---|---|---|
| 1 | Opt-out / hostile (English + Hindi patterns) | `end` immediately + 30-day merchant suppression |
| 2 | Auto-reply: same normalized message from a merchant 1× / 2× / 3+ (with canned-phrase markers) | probe `send` → `wait` 24 h → `end` |
| 3 | Objection ("too expensive", "busy", "maybe later", "baad me", …) | `wait` 30 min; thread stays open |
| 4 | Commitment ("let's do it", "haan bhejo", "confirm", "what's next", …) | Action mode: names the deliverable, offers a PUBLISH/EDIT review loop — never re-qualifies |
| 5 | Off-topic (GST, tax filing, loans, …) | Polite boundary ("your CA is the right person") + redirect to the live thread |
| 6 | Question (price, timing, kitna/kab/kaise) | Answered from the merchant's own offers and performance data |
| 7 | Anything else | Acknowledged + exactly one next step, within the send budget |

A message that mixes praise with an objection ("Sounds good but too expensive…") resolves to
objection: backing off beats misfiring into action mode. Customer-side replies (`from_role:
customer`) use a simpler booking flow — slot pick or "yes" → confirm and exit, reschedule
requests → one open question, opt-out → end.

## Tradeoffs / design decisions

- **In-memory state, no persistent store.** The testing brief explicitly allows this ("storing
  in memory is fine; just don't restart between calls"). A restart clears contexts and
  conversation state — acceptable for a 60-minute window, and the judge pushes the full dataset
  at warmup anyway. The corresponding requirement is hosting uptime (see keepalive below).
- **Rule-based composition, not an LLM.** Deterministic dispatch can't match a frontier model's
  fluency on arbitrary curveballs. In exchange: exact reproducibility (a rubric requirement),
  zero fabrication risk (every number traces to a context field), millisecond responses, no API
  cost, and no data leaving the test environment.
- **Regex ladders for intent and auto-reply detection.** Fast and deterministic, and they match
  the brief's own heuristic ("same message verbatim 3+ times = auto-reply"). They will miss
  phrasings outside the lists — an accepted cost of determinism.
- **Suppression is permanent per key until teardown.** Safest against spam and repeat
  penalties; there is no TTL or urgency-override sophistication.
- **One generic composer absorbs the unknown.** Judge-injected trigger kinds the bot has never
  seen compose via the grounded generic path — the trigger's own payload facts plus the
  merchant's live numbers against peer benchmarks — so adaptation needs no new code.

## Validation

Validated against the project's local behavioral regression suite (`local_test.py`, 69 checks):

- **Local:** 69 passed / 0 failed — re-run on fresh processes with identical output each time.
- **Production:** 69 passed / 0 failed against the Render deployment
  (`python local_test.py https://magicpin-vera-bot-ibgk.onrender.com`).
- **Suite coverage:** warmup and context idempotency/versioning (409 + replace semantics),
  action schema completeness, all 25 seed triggers composed with correct `send_as`, no
  duplicate bodies, unique conversation ids, determinism, auto-reply hell (probe → wait →
  end), intent transition (action mode, no re-qualification), hostile handling + follow-up
  suppression, off-topic boundary, engaged replies, objection → wait, and adaptive injection
  (a new category version's digest item is used immediately; an unseen trigger kind still
  composes a grounded message).
- **Determinism:** byte-identical bodies, ordering, and conversation ids across independent
  processes for the same request sequence.

These are behavioral checks against a local harness, not the official judge — they verify
contract compliance and reproducibility, and make no claim about subjective message quality.

## Running locally

Python 3.9+; stdlib only, so there is no install step.

```bash
python bot.py                  # listens on :8080
PORT=9000 python bot.py        # or: python bot.py 9000

python local_test.py           # 69-check suite against http://localhost:8080
python local_test.py http://127.0.0.1:8080   # explicit URL (on Windows prefer 127.0.0.1)
```

Optional environment variables (all have in-file defaults):

| Variable | Purpose |
|---|---|
| `PORT` | Listen port (default 8080; a positional port argument also works) |
| `TEAM_NAME`, `TEAM_MEMBERS`, `CONTACT_EMAIL` | Identity reported by `/v1/metadata` |

`POST /v1/teardown` wipes all state — call it between test runs for clean suppression and
auto-reply counters.

## Testing

Two harnesses with different purposes:

- **`local_test.py` — behavioral regression (deterministic, no API key).** Drives the full HTTP
  surface end-to-end and is the pass/fail gate for changes.
- **`judge_simulator.py` — LLM judge simulation (requires an LLM key).** magicpin's own harness;
  it scores composed messages with an LLM. `BOT_URL` and `TEST_SCENARIO` are set in the
  `CONFIGURATION` block at the top of the file; provider settings come from the environment:
  `LLM_PROVIDER` (default `gemini`; also `openai`, `anthropic`, `deepseek`, `groq`, `ollama`,
  `openrouter`), `LLM_API_KEY`, and `LLM_MODEL` (default `gemini-3.8-flash`).
  `SIM_NOW` pins the simulated tick clock (default `2026-04-26T10:30:00Z`, matching the
  dataset's era and the testing brief's simulated-time judge; set `SIM_NOW=""` to use the real
  UTC clock).

```bash
export LLM_API_KEY="..."       # never paste keys into the file
python judge_simulator.py
```

LLM-judge scores are inherently non-deterministic; treat them as directional feedback on
message quality, not as a test result.

## Deployment

Production: **https://magicpin-vera-bot-ibgk.onrender.com** (Render, Python service).

- Stdlib-only service — the start command is `python bot.py`; the server binds `0.0.0.0` on
  `$PORT`, which Render injects.
- Liveness: `GET /v1/healthz` (status, uptime, context counts). Identity: `GET /v1/metadata`.
- State is in-memory: a service restart or sleep shows zero contexts on healthz. The judge
  pushes the entire dataset during warmup, so a clean instance is the expected start state.
- `POST /v1/teardown` clears all state on demand (end-of-test privacy rule).

## Keep Render alive

Render's free tier sleeps an idle instance, and a cold start can exceed the judge's health-check
budget. `.github/workflows/keepalive.yml` prevents that: a scheduled GitHub Action
(`cron: "*/10 * * * *"` — every 10 minutes — plus manual `workflow_dispatch`) runs:

```bash
curl --fail --max-time 20 "${{ secrets.BOT_URL }}/v1/healthz"
```

Activate it once:

1. Open the GitHub repository
2. **Settings**
3. **Secrets and variables**
4. **Actions**
5. **New repository secret**
6. Name: `BOT_URL`
7. Value: the production Render URL (no trailing slash)
8. From the **Actions** tab, run "Keep Render Bot Alive" manually once to verify it goes green

## Submission

Submit to magicpin:

- The public production URL: `https://magicpin-vera-bot-ibgk.onrender.com`
- This repository — in particular `bot.py` (HTTP + state), `vera_engine.py` (composition +
  conversation brain), `local_test.py` (regression suite), `README.md`, and `dataset/`

## Pre-submission checklist

- [ ] Production URL responds
- [ ] `/v1/healthz` returns 200 with context counts
- [ ] `/v1/metadata` returns team identity
- [ ] `POST /v1/context`, `/v1/tick`, `/v1/reply` behave per contract
- [ ] `python local_test.py` passes locally
- [ ] `python local_test.py https://magicpin-vera-bot-ibgk.onrender.com` passes against production
- [ ] No API keys committed to git
- [ ] GitHub Actions keepalive configured (`BOT_URL` secret set, workflow runs green)
