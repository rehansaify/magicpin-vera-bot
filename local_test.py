"""
Local self-test replicating judge_simulator.py's HTTP behavior (minus LLM
scoring, which needs an API key). Covers:

  - warmup: healthz, metadata, context pushes (5 categories + 10 merchants)
  - idempotency: same version -> 409 stale_version; higher version -> replace
  - phase2_short: push 3 triggers + tick -> actions scored for schema shape
  - full: push ALL seed contexts, tick every trigger in batches of 5, print bodies
  - auto_reply_hell: 4 identical canned replies -> probe, wait, END
  - intent_transition: "Ok lets do it. Whats next?" -> action mode (not qualifying)
  - hostile: "Stop messaging me. This is useless spam." -> END
  - off-topic curveball: GST question -> boundary + redirect
  - engaged reply: "Yes please send the abstract" -> action mode
  - determinism: same compose inputs -> identical body

Run:  start bot (python bot.py) then  python local_test.py
"""

import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

BOT_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
HERE = Path(__file__).parent
DATASET = HERE / "dataset"

PASS, FAIL = 0, 0


def req(method, path, body=None, timeout=20):
    url = BOT_URL.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(r, timeout=timeout)
        return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, None


def check(name, cond, extra=""):
    global PASS, FAIL
    mark = "PASS" if cond else "FAIL"
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{mark}] {name}" + (f"  -- {extra}" if extra and not cond else ""))


def load_seed(name, container):
    d = json.load(open(DATASET / name, encoding="utf-8"))
    return {x[list(x.keys())[0]] if False else x: x for x in []} if False else d.get(container, [])


def main():
    # ---------- load dataset exactly like judge_simulator.DatasetLoader ----------
    categories = {}
    for f in (DATASET / "categories").glob("*.json"):
        d = json.load(open(f, encoding="utf-8"))
        categories[d.get("slug", f.stem)] = d
    merchants = {m["merchant_id"]: m for m in load_seed("merchants_seed.json", "merchants")}
    customers = {c["customer_id"]: c for c in load_seed("customers_seed.json", "customers")}
    triggers = {t["id"]: t for t in load_seed("triggers_seed.json", "triggers")}
    print(f"dataset: {len(categories)} cats, {len(merchants)} merchants, "
          f"{len(customers)} customers, {len(triggers)} triggers\n")

    # ---------- warmup ----------
    print("== WARMUP ==")
    st, data = req("GET", "/v1/healthz", timeout=5)
    check("healthz 200", st == 200 and data.get("status") == "ok", str(data))
    st, data = req("GET", "/v1/metadata", timeout=5)
    check("metadata 200 + team", st == 200 and bool(data.get("team_name")), str(data))

    for slug, cat in categories.items():
        st, data = req("POST", "/v1/context", {
            "scope": "category", "context_id": slug, "version": 1,
            "payload": cat, "delivered_at": "2026-04-26T09:45:00Z"})
        check(f"push category/{slug}", st == 200 and data.get("accepted") is True, str(data))
    for mid, m in merchants.items():
        st, data = req("POST", "/v1/context", {
            "scope": "merchant", "context_id": mid, "version": 1,
            "payload": m, "delivered_at": "2026-04-26T09:45:30Z"})
        check(f"push merchant/{mid[:24]}", st == 200 and data.get("accepted") is True, str(data))

    # idempotency: same version -> 409 stale_version
    st, data = req("POST", "/v1/context", {
        "scope": "merchant", "context_id": next(iter(merchants)), "version": 1,
        "payload": next(iter(merchants.values())), "delivered_at": "2026-04-26T09:46:00Z"})
    check("re-push same version -> 409 stale_version",
          st == 409 and data and data.get("reason") == "stale_version", f"{st} {data}")

    # version bump replaces
    m0 = json.loads(json.dumps(next(iter(merchants.values()))))
    m0["performance"]["views"] = 9999
    st, data = req("POST", "/v1/context", {
        "scope": "merchant", "context_id": next(iter(merchants)), "version": 2,
        "payload": m0, "delivered_at": "2026-04-26T10:00:00Z"})
    check("version bump accepted", st == 200 and data.get("accepted") is True, str(data))
    st, data = req("POST", "/v1/context", {
        "scope": "merchant", "context_id": next(iter(merchants)), "version": 1,
        "payload": next(iter(merchants.values())), "delivered_at": "2026-04-26T10:00:00Z"})
    check("old version after bump -> 409", st == 409, f"{st} {data}")

    st, data = req("GET", "/v1/healthz")
    cl = data.get("contexts_loaded", {})
    check("healthz counts (5 cats + 10 merchants)",
          cl.get("category") == len(categories) and cl.get("merchant") == len(merchants), str(cl))

    # ---------- phase2_short: 3 triggers + tick ----------
    print("\n== PHASE 2 SHORT ==")
    trigs = list(triggers.keys())[:3]
    for tid in trigs:
        req("POST", "/v1/context", {"scope": "trigger", "context_id": tid, "version": 1,
                                    "payload": triggers[tid], "delivered_at": "2026-04-26T10:05:00Z"})
    st, data = req("POST", "/v1/tick", {
        "now": "2026-04-26T10:30:00Z", "available_triggers": trigs})
    actions = data.get("actions", [])
    check("tick returns <=3 actions", st == 200 and len(actions) <= 3, f"{len(actions)}")
    for a in actions:
        need = ["conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id",
                "template_name", "template_params", "body", "cta", "suppression_key", "rationale"]
        missing = [k for k in need if k not in a]
        check(f"action schema complete ({a.get('trigger_id','?')[:30]})", not missing, f"missing {missing}")
        check("body non-empty", bool(a.get("body", "").strip()))
    # re-tick same triggers -> suppressed (0 actions)
    st, data = req("POST", "/v1/tick", {
        "now": "2026-04-26T10:35:00Z", "available_triggers": trigs})
    check("re-tick same triggers -> suppressed", data.get("actions") == [], str(data.get("actions"))[:120])

    # ---------- full: every trigger, batches of 5 ----------
    print("\n== FULL COMPOSITION SWEEP (bodies for review) ==")
    all_actions = []
    tids = list(triggers.keys())
    for tid in tids:
        req("POST", "/v1/context", {"scope": "trigger", "context_id": tid, "version": 1,
                                    "payload": triggers[tid], "delivered_at": "2026-04-26T10:10:00Z"})
    for cust in customers.values():
        req("POST", "/v1/context", {"scope": "customer", "context_id": cust["customer_id"],
                                    "version": 1, "payload": cust, "delivered_at": "2026-04-26T10:10:00Z"})
    for i in range(0, len(tids), 5):
        batch = tids[i:i + 5]
        st, data = req("POST", "/v1/tick", {
            "now": "2026-04-26T11:00:00Z", "available_triggers": batch})
        all_actions.extend(data.get("actions", []))
    check(f"full sweep produced actions ({len(all_actions)}/{len(tids)})", len(all_actions) >= len(tids) - 6)
    for a in all_actions:
        tid = a["trigger_id"]
        trg = triggers.get(tid, {})
        scope = trg.get("scope")
        send_as_ok = (scope != "customer") or a["send_as"] == "merchant_on_behalf"
        check(f"send_as correct for {tid[:38]}", send_as_ok, a.get("send_as"))
        print(f"\n--- {tid} [{trg.get('kind')}] send_as={a['send_as']} cta={a['cta']}")
        print("BODY:", a["body"].replace("\n", " | ")[:400])
        print("RATIONALE:", a["rationale"][:160])
    bodies = [a["body"] for a in all_actions]
    check("no duplicate bodies across sweep", len(bodies) == len(set(bodies)))
    convs = [a["conversation_id"] for a in all_actions]
    check("conversation ids unique", len(convs) == len(set(convs)))

    # ---------- determinism ----------
    print("\n== DETERMINISM ==")
    import importlib
    sys.path.insert(0, str(HERE))
    ve = importlib.import_module("vera_engine")
    tid = "trg_001_research_digest_dentists"
    out1 = ve.compose(categories["dentists"], merchants["m_001_drmeera_dentist_delhi"],
                      triggers[tid], None, "2026-04-26T10:30:00Z")
    out2 = ve.compose(categories["dentists"], merchants["m_001_drmeera_dentist_delhi"],
                      triggers[tid], None, "2026-04-26T10:30:00Z")
    check("compose deterministic", out1["body"] == out2["body"])

    # ---------- auto-reply hell ----------
    print("\n== AUTO-REPLY HELL ==")
    mid = next(iter(merchants))
    auto_msg = "Thank you for contacting us! Our team will respond shortly."
    saw_end, saw_wait_or_probe = False, False
    for i in range(1, 5):
        st, data = req("POST", "/v1/reply", {
            "conversation_id": f"conv_auto_{i}", "merchant_id": mid, "customer_id": None,
            "from_role": "merchant", "message": auto_msg,
            "received_at": "2026-04-26T10:45:00Z", "turn_number": i + 1})
        act = (data or {}).get("action", "?")
        print(f"  turn {i}: action={act}")
        if act == "end":
            saw_end = True
            break
        if act == "wait" or (act == "send" and "auto" in (data.get("body", "") + data.get("rationale", "")).lower()):
            saw_wait_or_probe = True
    check("auto-reply: bot ended within 4 turns", saw_end)
    check("auto-reply: probe/wait before end", saw_wait_or_probe)

    # ---------- intent transition ----------
    print("\n== INTENT TRANSITION ==")
    st, data = req("POST", "/v1/reply", {
        "conversation_id": "conv_intent_1", "merchant_id": mid, "customer_id": None,
        "from_role": "merchant", "message": "Ok lets do it. Whats next?",
        "received_at": "2026-04-26T10:45:00Z", "turn_number": 2})
    body = (data or {}).get("body", "").lower()
    qualifying = ["would you", "do you", "can you tell", "what if", "how about"]
    actioning = ["done", "sending", "draft", "here", "confirm", "proceed", "next"]
    ok = (any(w in body for w in actioning)
          and not any(w in body for w in qualifying))
    check("intent: switched to ACTION mode", ok, body[:160])
    print("  body:", (data or {}).get("body", "")[:200])

    # ---------- hostile ----------
    print("\n== HOSTILE ==")
    st, data = req("POST", "/v1/reply", {
        "conversation_id": "conv_hostile", "merchant_id": mid, "customer_id": None,
        "from_role": "merchant", "message": "Stop messaging me. This is useless spam.",
        "received_at": "2026-04-26T10:45:00Z", "turn_number": 2})
    act = (data or {}).get("action")
    body = (data or {}).get("body", "").lower()
    check("hostile: END or graceful apology", act == "end" or any(w in body for w in ["sorry", "apolog", "won't"]), act)
    print(f"  action={act}")

    # suppressed merchant: new tick for that merchant's trigger -> no action
    host_trgs = [t for t, tr in triggers.items() if tr.get("merchant_id") == mid]
    if host_trgs:
        fresh = host_trgs[0]
        req("POST", "/v1/context", {"scope": "trigger", "context_id": fresh + "_x", "version": 1,
                                    "payload": {**triggers[fresh], "id": fresh + "_x",
                                                "suppression_key": fresh + "_x"},
                                    "delivered_at": "2026-04-26T11:30:00Z"})
        st, data = req("POST", "/v1/tick", {"now": "2026-04-26T11:31:00Z",
                                            "available_triggers": [fresh + "_x"]})
        check("hostile merchant suppressed on later ticks",
              all(a["merchant_id"] != mid for a in data.get("actions", [])), str(data.get("actions"))[:150])

    # ---------- off-topic curveball ----------
    print("\n== OFF-TOPIC ==")
    st, data = req("POST", "/v1/reply", {
        "conversation_id": "conv_curve", "merchant_id": mid, "customer_id": None,
        "from_role": "merchant", "message": "Btw can you also help me file my GST?",
        "received_at": "2026-04-26T10:45:00Z", "turn_number": 2})
    body = (data or {}).get("body", "")
    check("off-topic: boundary + redirect",
          "gst" in body.lower() or "outside" in body.lower() or "ca" in body.lower(), body[:120])
    print("  body:", body[:200])

    # ---------- engaged reply ----------
    print("\n== ENGAGED ==")
    st, data = req("POST", "/v1/reply", {
        "conversation_id": "conv_engaged", "merchant_id": mid, "customer_id": None,
        "from_role": "merchant", "message": "Yes please send the abstract. Also draft the patient WhatsApp.",
        "received_at": "2026-04-26T10:45:00Z", "turn_number": 2})
    body = (data or {}).get("body", "")
    check("engaged: action mode (no qualifying)",
          any(w in body.lower() for w in ["sending", "done", "draft", "here", "ready"]) and
          not any(w in body.lower() for w in ["would you", "do you", "how about"]), body[:140])
    print("  body:", body[:200])

    # ---------- objection ----------
    print("\n== OBJECTION ==")
    st, data = req("POST", "/v1/reply", {
        "conversation_id": "conv_obj", "merchant_id": mid, "customer_id": None,
        "from_role": "merchant", "message": "Sounds interesting but I'm busy right now, maybe later.",
        "received_at": "2026-04-26T10:45:00Z", "turn_number": 2})
    check("objection: wait with seconds", (data or {}).get("action") == "wait" and "wait_seconds" in (data or {}), str(data))

    # ---------- adaptive injection (what the real judge does post-submission) ----------
    print("\n== ADAPTIVE INJECTION ==")
    # new digest item arrives as category version 2
    cats2 = json.loads(json.dumps(categories))
    new_item = {
        "id": "d_2026W18_NEW_ai_screening", "kind": "research",
        "title": "AI-assisted caries screening pilot shows 18% earlier detection in Indian clinics",
        "source": "Dental Tribune India, May 2026",
        "trial_n": 800,
        "summary": "8-clinic pilot; adjunct tool, not a replacement for radiographs.",
    }
    cats2["dentists"]["digest"] = [new_item] + cats2["dentists"]["digest"]
    cats2["dentists"]["peer_stats"]["avg_ctr"] = 0.032
    st, data = req("POST", "/v1/context", {
        "scope": "category", "context_id": "dentists", "version": 2,
        "payload": cats2["dentists"], "delivered_at": "2026-04-26T11:40:00Z"})
    check("new category version accepted", st == 200 and data.get("accepted") is True, str(data))

    # a brand-new trigger with a never-seen digest item (note: m_001 is hostile-
    # suppressed from the earlier test, so the injection goes to m_002 — same category)
    fresh_trg = {
        "id": "trg_injected_ai_screening_demo", "scope": "merchant", "kind": "research_digest",
        "source": "external", "merchant_id": "m_002_bharat_dentist_mumbai", "customer_id": None,
        "payload": {"category": "dentists", "top_item_id": "d_2026W18_NEW_ai_screening"},
        "urgency": 3, "suppression_key": "research:dentists:2026-W18",
        "expires_at": "2026-05-10T00:00:00Z"}
    st, data = req("POST", "/v1/context", {
        "scope": "trigger", "context_id": fresh_trg["id"], "version": 1,
        "payload": fresh_trg, "delivered_at": "2026-04-26T11:41:00Z"})
    check("injected trigger accepted", st == 200 and data.get("accepted") is True, str(data))
    st, data = req("POST", "/v1/tick", {"now": "2026-04-26T11:42:00Z",
                                        "available_triggers": [fresh_trg["id"]]})
    acts = data.get("actions", [])
    check("injected trigger produced action", len(acts) == 1, str(len(acts)))
    if acts:
        body = acts[0]["body"]
        check("uses NEW digest item (not stale)", "AI-assisted" in body or "18%" in body, body[:150])
        check("cites new source", "Dental Tribune" in body, body[:200])
        print("  body:", body[:300])
    unknown_trg = {
        "id": "trg_injected_unknown_kind", "scope": "merchant", "kind": "qr_payment_trend",
        "source": "external", "merchant_id": "m_010_sunrisepharm_pharmacy_lucknow", "customer_id": None,
        "payload": {"category": "pharmacies", "city": "Lucknow", "delta_pct": 0.25,
                    "note": "UPI QR payments rising at pharmacies"},
        "urgency": 2, "suppression_key": "qrtrend:lucknow:2026-W18",
        "expires_at": "2026-05-10T00:00:00Z"}
    req("POST", "/v1/context", {"scope": "trigger", "context_id": unknown_trg["id"],
                                "version": 1, "payload": unknown_trg,
                                "delivered_at": "2026-04-26T11:43:00Z"})
    st, data = req("POST", "/v1/tick", {"now": "2026-04-26T11:44:00Z",
                                        "available_triggers": [unknown_trg["id"]]})
    acts = data.get("actions", [])
    check("unknown kind still composes", len(acts) == 1 and len(acts[0]["body"]) > 80, str(acts)[:150])
    if acts:
        print("  unknown-kind body:", acts[0]["body"][:300])

    print(f"\n{'='*50}\nRESULT: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
