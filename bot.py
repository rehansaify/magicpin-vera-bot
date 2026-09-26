"""
magicpin AI Challenge — Vera challenger bot.

Stateful HTTP bot implementing the full candidate contract
(challenge-testing-brief.md §2):

    GET  /v1/healthz     — liveness + context counts
    GET  /v1/metadata    — team identity
    POST /v1/context     — idempotent (scope, context_id, version) context store
    POST /v1/tick        — proactive sends from currently-active triggers
    POST /v1/reply       — next move in an existing conversation
    POST /v1/teardown    — wipe all state (privacy rule §11)

Stdlib only (no pip installs) so it runs anywhere Python 3.9+ runs.

Run:  python bot.py [port]          (default 8080)
      PORT=9000 python bot.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import vera_engine as ve

# --- identity (edit before submitting) ---------------------------------------
TEAM_NAME = os.environ.get("TEAM_NAME", "Rehan Saifi")
TEAM_MEMBERS = os.environ.get("TEAM_MEMBERS", "Rehan Saifi").split(",")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "rsaify90@example.com")
BOT_VERSION = "1.0.0"

START = time.time()
LOCK = threading.RLock()

# --- state --------------------------------------------------------------------
# (scope, context_id) -> {"version": int, "payload": dict}
CONTEXTS: dict = {}
# conversation_id -> {"merchant_id", "customer_id", "kind", "bot_sends", "bodies": set, "turns": int}
CONVERSATIONS: dict = {}
# (merchant_id, normalized_message) -> count  (auto-reply detection across convos)
MERCHANT_MSG_COUNTS: dict = {}
# suppression keys already fired -> True
FIRED_SUPPRESSIONS: dict = {}
# merchant_id -> opt-out-until ISO (hostile / stop requests)
SUPPRESSED_UNTIL: dict = {}
CONV_SEQ = 0


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _short(mid: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (mid or "na").lower())[:40]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "vera-challenger/" + BOT_VERSION

    # -- plumbing --------------------------------------------------------------
    def log_message(self, fmt, *args):  # quiet, single-line
        sys.stderr.write("[%s] %s\n" % (_now_iso(), fmt % args))

    def _send(self, code: int, obj: dict):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/v1/healthz":
            counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
            with LOCK:
                for (scope, _), _v in CONTEXTS.items():
                    if scope in counts:
                        counts[scope] += 1
            self._send(200, {
                "status": "ok",
                "uptime_seconds": int(time.time() - START),
                "contexts_loaded": counts,
            })
        elif path == "/v1/metadata":
            self._send(200, {
                "team_name": TEAM_NAME,
                "team_members": TEAM_MEMBERS,
                "model": "deterministic rule-based composer (no external LLM)",
                "approach": "trigger-kind dispatch x category voice x merchant-state grounding; "
                            "conversation brain with auto-reply detection, intent-transition and graceful exits",
                "contact_email": CONTACT_EMAIL,
                "version": BOT_VERSION,
                "submitted_at": _now_iso(),
            })
        elif path == "/":
            self._send(200, {"bot": "vera-challenger", "status": "ok", "endpoints": [
                "/v1/healthz", "/v1/metadata", "/v1/context", "/v1/tick", "/v1/reply", "/v1/teardown"]})
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        try:
            body = self._body()
        except (ValueError, UnicodeDecodeError) as e:
            self._send(400, {"accepted": False, "reason": "malformed_json", "details": str(e)})
            return
        try:
            if path == "/v1/context":
                self._handle_context(body)
            elif path == "/v1/tick":
                self._handle_tick(body)
            elif path == "/v1/reply":
                self._handle_reply(body)
            elif path == "/v1/teardown":
                with LOCK:
                    CONTEXTS.clear()
                    CONVERSATIONS.clear()
                    MERCHANT_MSG_COUNTS.clear()
                    FIRED_SUPPRESSIONS.clear()
                    SUPPRESSED_UNTIL.clear()
                self._send(200, {"teardown": "complete", "state_cleared": True})
            else:
                self._send(404, {"error": "not_found"})
        except Exception as e:  # never hang the judge
            self._send(200 if path == "/v1/reply" else 500, {
                "error": "internal", "details": str(e)[:200]})

    # -- /v1/context -------------------------------------------------------------
    def _handle_context(self, body: dict):
        scope = body.get("scope")
        context_id = body.get("context_id")
        version = body.get("version")
        payload = body.get("payload")

        # lenient fallback: bare payload pushed directly (e.g. curl -d @file)
        if not scope and isinstance(body, dict):
            if body.get("slug") and "digest" in body:
                scope, context_id, version, payload = "category", body["slug"], 1, body
            elif body.get("merchant_id"):
                scope, context_id, version, payload = "merchant", body["merchant_id"], 1, body
            elif body.get("customer_id"):
                scope, context_id, version, payload = "customer", body["customer_id"], 1, body
            elif body.get("id") and "kind" in body:
                scope, context_id, version, payload = "trigger", body["id"], 1, body

        if scope not in ("category", "merchant", "customer", "trigger") or not context_id:
            self._send(400, {"accepted": False, "reason": "invalid_scope",
                             "details": "scope must be category|merchant|customer|trigger with a context_id"})
            return
        if not isinstance(payload, dict):
            self._send(400, {"accepted": False, "reason": "invalid_payload",
                             "details": "payload must be a JSON object"})
            return
        try:
            version = int(version)
        except (TypeError, ValueError):
            self._send(400, {"accepted": False, "reason": "invalid_version",
                             "details": "version must be an integer"})
            return

        with LOCK:
            cur = CONTEXTS.get((scope, context_id))
            if cur and cur["version"] >= version:
                self._send(409, {"accepted": False, "reason": "stale_version",
                                 "current_version": cur["version"]})
                return
            CONTEXTS[(scope, context_id)] = {"version": version, "payload": payload}
            ack = f"ack_{_short(context_id)}_v{version}_{uuid.uuid4().hex[:6]}"
        self._send(200, {"accepted": True, "ack_id": ack, "stored_at": _now_iso()})

    # -- /v1/tick ----------------------------------------------------------------
    def _handle_tick(self, body: dict):
        now_iso = body.get("now") or _now_iso()
        available = body.get("available_triggers") or []
        now = ve._parse_dt(now_iso)
        actions = []
        per_merchant_sent = {}

        with LOCK:
            # snapshot candidate triggers: (index, trigger_payload)
            candidates = []
            for idx, tid in enumerate(available):
                rec = CONTEXTS.get(("trigger", tid))
                if not rec:
                    continue
                trg = rec["payload"]
                exp = ve._parse_dt(trg.get("expires_at"))
                if exp and now and exp < now:
                    continue  # expired
                sk = trg.get("suppression_key") or f"{trg.get('kind')}:{trg.get('merchant_id')}:{tid}"
                if FIRED_SUPPRESSIONS.get(sk):
                    continue
                mid = trg.get("merchant_id")
                supp_until = ve._parse_dt(SUPPRESSED_UNTIL.get(mid))
                if supp_until and now and now < supp_until:
                    continue  # merchant opted out
                candidates.append((idx, tid, trg, sk))

            # prioritize: urgency desc, then arrival order; max 2 merchant-scoped per merchant
            candidates.sort(key=lambda c: (-(c[2].get("urgency") or 0), c[0]))
            global CONV_SEQ
            for idx, tid, trg, sk in candidates:
                if FIRED_SUPPRESSIONS.get(sk):
                    continue  # same-tick duplicate suppression key: higher-priority candidate already fired it
                mid = trg.get("merchant_id") or ""
                is_customer = trg.get("scope") == "customer" and trg.get("customer_id")
                if not is_customer:
                    if per_merchant_sent.get(mid, 0) >= 2:
                        continue
                if len(actions) >= 20:
                    break

                merchant = (CONTEXTS.get(("merchant", mid)) or {}).get("payload") or {}
                if not merchant:
                    continue
                cat_slug = merchant.get("category_slug") or (trg.get("payload") or {}).get("category")
                category = (CONTEXTS.get(("category", cat_slug)) or {}).get("payload") or {}
                customer = None
                if is_customer:
                    customer = (CONTEXTS.get(("customer", trg["customer_id"])) or {}).get("payload")

                out = ve.compose(category, merchant, trg, customer, now_iso)

                CONV_SEQ += 1
                conv_id = f"conv_{_short(mid)}_{_short(trg.get('kind', 'msg'))}_{CONV_SEQ}"
                CONVERSATIONS[conv_id] = {
                    "merchant_id": mid,
                    "customer_id": trg.get("customer_id"),
                    "trigger_id": tid,
                    "kind": trg.get("kind", ""),
                    "bot_sends": 1,
                    "bodies": {out["body"]},
                    "turns": 1,
                }
                FIRED_SUPPRESSIONS[sk] = True
                if not is_customer:  # customer sends don't count toward the merchant cap
                    per_merchant_sent[mid] = per_merchant_sent.get(mid, 0) + 1

                actions.append({
                    "conversation_id": conv_id,
                    "merchant_id": mid,
                    "customer_id": trg.get("customer_id"),
                    "send_as": out["send_as"],
                    "trigger_id": tid,
                    "template_name": out["template_name"],
                    "template_params": out["template_params"],
                    "body": out["body"],
                    "cta": out["cta"],
                    "suppression_key": out["suppression_key"],
                    "rationale": out["rationale"],
                })
        self._send(200, {"actions": actions})

    # -- /v1/reply ----------------------------------------------------------------
    def _handle_reply(self, body: dict):
        conv_id = body.get("conversation_id") or ""
        message = body.get("message") or ""
        turn = int(body.get("turn_number") or 1)
        mid = body.get("merchant_id")
        cust_id = body.get("customer_id")
        from_role = body.get("from_role") or "merchant"

        with LOCK:
            merchant = (CONTEXTS.get(("merchant", mid)) or {}).get("payload") or {}
            customer = (CONTEXTS.get(("customer", cust_id)) or {}).get("payload") if cust_id else None
            state = CONVERSATIONS.get(conv_id)
            if state is None:
                state = {
                    "merchant_id": mid, "customer_id": cust_id, "trigger_id": None,
                    "kind": "", "bot_sends": 1, "bodies": set(), "turns": 0,
                }
                CONVERSATIONS[conv_id] = state
            state["turns"] = max(state.get("turns", 0), turn)

            # auto-reply detection: verbatim repeats per merchant (across conversations)
            norm = re.sub(r"\s+", " ", message.strip().lower())
            key = (mid, norm)
            MERCHANT_MSG_COUNTS[key] = MERCHANT_MSG_COUNTS.get(key, 0) + 1
            verbatim_count = MERCHANT_MSG_COUNTS[key]

            if cust_id or from_role == "customer":
                out = ve.handle_customer_reply(state, merchant, customer, message, turn)
            else:
                out = ve.handle_reply(state, merchant, customer, message, turn, verbatim_count)

            # side effects
            if out.get("_suppress_merchant_days"):
                until = (datetime.now(timezone.utc)
                         + timedelta(days=out.pop("_suppress_merchant_days")))
                SUPPRESSED_UNTIL[mid] = until.isoformat()
            out.pop("_suppress_merchant_days", None)

            if out.get("action") == "send":
                body_out = out.get("body", "")
                sent = state.setdefault("bodies", set())
                if body_out in sent:  # anti-repetition guard
                    body_out = body_out + f" (update #{state.get('bot_sends', 1) + 1})"
                    out["body"] = body_out
                sent.add(body_out)
                state["bot_sends"] = state.get("bot_sends", 0) + 1

            response = {k: v for k, v in out.items()}
        self._send(200, response)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8080))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"vera-challenger listening on 0.0.0.0:{port} (stdlib-only, stateful in-memory)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
