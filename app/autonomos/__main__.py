"""AutoNOMOS CLI (R1d: every command that builds the service uses R1dService; AI provider selection is
AUTONOMOS_R1D_AI_PROVIDER = fixture | mock | anthropic_real, see autonomos/r1d/aiconfig.py).

  python -m autonomos serve                      local web UI on 127.0.0.1:8765
  python -m autonomos demo OUT_DIR [--question Q] [--ai fixture|mock]
                                                 headless end-to-end run with the MOCK payment provider (writes evidence)
  python -m autonomos demo OUT_DIR --ai recorded [--recording DIR]
                                                 R1e: the same MOCK-paid run with one historical model answer REPLAYED
                                                 offline (labelled RECORDED_REPLAY / MOCK_EXECUTION; not a model call)
  python -m autonomos demo OUT_DIR --ai anthropic_real --owner-confirm
                                                 OWNER-RUN ONLY: the same MOCK-paid run with ONE real model request,
                                                 refused unless ai-preflight is READY for OUT_DIR/var
  python -m autonomos recover                     reconcile interrupted captures / executions (run at start-up)
  python -m autonomos order-status REF            READ-ONLY diagnosis (REF = task, offer or order id): local DB opened
                                                 mode=ro, provider GET only; approval / capture / merchant reported
                                                 separately. Changes nothing.
  python -m autonomos reconcile ORDER_ID --owner-confirm
                                                 OWNER ONLY, STATE-CHANGING: open task -> GET + verify (never sends a
                                                 capture); CANCELLED task with a provider capture -> REVIEW_REQUIRED
                                                 (never executed automatically)
  python -m autonomos capture-resend ORDER_ID --owner-confirm
                                                 OWNER ONLY: re-send an UNKNOWN capture with the SAME PayPal-Request-Id
                                                 (refused before the settle delay and after the assumed idempotency window)
  python -m autonomos sandbox-preflight          offline: show the sanitized config + safety checks (no network)
  python -m autonomos ai-preflight               R1d, read-only: can a REAL model provider be enabled safely? worst-case
                                                 cost of the envelope; no network, key value never shown
  python -m autonomos ai-ledger TASK_ID          R1d, read-only: the model-request ledger rows of a task (DB mode=ro)
  python -m autonomos verify-receipt RECEIPT.json [ARTIFACT.json] [EVENTS.json] [--corpus DIR] [--issuer-key HEX]
                                                 v1 (R1c) and v2 (R1d) receipts; --corpus re-checks every delivered
                                                 quotation against the collection directory
  python -m autonomos execution-summary DIR      R1d.2, read-only: payment provider, AI provider, delivery status,
                                                 research completeness, claim support and truth as separate fields
  python -m autonomos replay-verify DIR [--corpus C]
                                                 R1d.2, offline: re-verify a historical model output with the current
                                                 verifier (NEW OFFLINE REASSESSMENT; nothing is re-signed)
  python -m autonomos paypal-sandbox-smoke       OWNER-RUN ONLY: token + create one sandbox order, print approval link
                                                 (not part of the R1b GO plan)
"""
import json
import os
import sys

from .config import Config


def main(argv):
    cmd = argv[0] if argv else "help"
    if cmd == "serve":
        from .config import SANDBOX_GO_TOKEN
        from .server import serve
        cfg = Config.from_env()
        if cfg.payment_provider == "paypal_sandbox" and cfg.sandbox_owner_go != SANDBOX_GO_TOKEN:
            raise SystemExit("paypal_sandbox needs the owner's explicit GO: AUTONOMOS_SANDBOX_OWNER_GO is not set "
                             "(credentials alone are not permission). See r1/R1_08_OWNER_SANDBOX_SETUP.md")
        from .r1d.aiconfig import AIConfigError
        from .r1d.demo_mode import DemoModeError, resolve
        try:                                       # R1e.1: the demo mode is checked before anything listens
            svc = _r1d_service(cfg)
            svc.demo_mode = resolve(cfg, svc.runtime)
        except (AIConfigError, DemoModeError) as e:
            raise SystemExit(f"serve refused: {e}")
        env = svc.runtime.envelope
        print(f"MODE {svc.demo_mode['mode']}: {svc.demo_mode['banner']}")
        print(f"R1d AI: {env.execution_class} provider={svc.runtime.provider!r} authorization={env.authorization_id}")
        print("start-up recovery:", json.dumps(svc.recover()))
        serve(svc, port=int(os.environ.get("AUTONOMOS_LISTEN_PORT", "8765")))     # R1e: loopback port selectable
    elif cmd == "demo":
        out = argv[1]
        q = argv[argv.index("--question") + 1] if "--question" in argv else \
            "What hardware evidence exists for the quantum-kernel experiment, and what is still unverified?"
        from .r1d import aiconfig
        cfg = Config.from_env(data_dir=os.path.join(out, "var"))
        if cfg.payment_provider != "mock":
            raise SystemExit("demo drives the MOCK checkout page; use `serve` for a real sandbox checkout")
        mode = argv[argv.index("--ai") + 1] if "--ai" in argv else os.environ.get("AUTONOMOS_R1D_AI_PROVIDER", "fixture")
        if mode == "anthropic_real":
            # OWNER-RUN ONLY: one MOCK-paid task, ONE real model request under the owner's authorization.
            from .r1d.preflight import preflight
            if "--owner-confirm" not in argv:
                raise SystemExit("refused: a REAL model request is billed to the owner's API key; add --owner-confirm")
            if os.path.realpath(os.environ.get("AUTONOMOS_DATA_DIR", "")) != os.path.realpath(cfg.data_dir):
                raise SystemExit(f"refused: set AUTONOMOS_DATA_DIR={cfg.data_dir} (the authorization's "
                                 "bound_data_dir) so the preflight checks the same database")
            pf, code = preflight()
            if code != 0 or pf["verdict"] != "REAL_PROVIDER_READY":
                raise SystemExit("refused: ai-preflight is not READY: " + "; ".join(pf["blocking"]))
            print("REAL model request authorized by", pf["envelope"]["authorization_id"], "worst case USD",
                  pf["cost"]["worst_case_authorization_usd"])
            os.makedirs(out, exist_ok=False)
            svc = _r1d_service(cfg)
        elif mode in ("fixture", "mock"):
            os.makedirs(out, exist_ok=False)
            svc = _r1d_service(cfg, aiconfig.offline_runtime(mode))
        elif mode == "recorded":
            # R1e: the unified MOCK-paid flow with ONE historical Opus 5.5 answer replayed offline. Labelled
            # RECORDED_REPLAY / MOCK_EXECUTION; no model is called, nothing is billed.
            from .r1d.recorded import recording_runtime
            rec = argv[argv.index("--recording") + 1] if "--recording" in argv else os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "recordings", "real_ai_test1_opus55")
            os.makedirs(out, exist_ok=False)
            svc = _r1d_service(cfg, recording_runtime(rec))
            print("RECORDED REPLAY (not a model call):", svc.runtime.provider.transport_label)
        else:
            raise SystemExit("demo: --ai fixture | mock | recorded | anthropic_real (the last needs --owner-confirm)")
        tid = svc.request_task({"capability": "research_snapshot", "topic": q, "corpus": "agentnomos_quantum",
                                "buyer_ref": "demo-buyer"})
        offer = svc.store.offer(task_id=tid)
        link = svc.accept_offer(tid, offer["_digest"])
        oid = link.rsplit("/", 1)[1]
        svc.payments.buyer_action(oid, "approve")                  # what the buyer does on the (mock) checkout page
        svc.on_buyer_return(oid)
        st = svc.execute(tid)
        v = svc.view(tid)
        rc = v["receipt"]
        json.dump(v, open(os.path.join(out, "task_view.json"), "w"), indent=1, default=str)
        if rc:
            art = svc.store.artifact(rc["body"]["artifact"]["digest"])
            json.dump(rc, open(os.path.join(out, "receipt.json"), "w"), indent=1)
            json.dump(art, open(os.path.join(out, "artifact.json"), "w"), indent=1)
            json.dump(svc.store.events(tid), open(os.path.join(out, "events.json"), "w"), indent=1)
        from .r1d.summary import execution_summary
        es = execution_summary(st, rc, art if rc else None)
        print(json.dumps({"task_id": tid, "final_state": st, "receipt": bool(rc),
                          "receipt_version": rc and rc["body"]["receipt_version"],
                          # R1d.2: payment, AI, delivery, research, claim support and truth are separate fields
                          "payment_provider": es["payment_provider"] if rc else svc.payments.environment,
                          "ai_provider": es["ai_provider"],
                          "ai_execution_class": svc.runtime.envelope.execution_class,
                          **{k: es[k] for k in ("delivery_status", "delivery_note", "research_completeness",
                                                "research_completeness_reasons", "claim_support",
                                                "claim_support_verdict", "independent_truth", "headline")}},
                         indent=1))
    elif cmd == "capture-status":
        raise SystemExit("renamed in R1c: `order-status REF` is the read-only diagnosis; `reconcile ORDER_ID "
                         "--owner-confirm` is the state-changing reconciliation (capture-status was not read-only)")
    elif cmd == "order-status":
        from .order_status import order_status
        from .payments import ProviderError, make_payment_provider
        cfg = Config.from_env()
        try:
            r = order_status(cfg, argv[1], make_payment_provider(cfg))
        except (LookupError, FileNotFoundError) as e:
            raise SystemExit(f"order-status: {e}")
        except ProviderError as e:
            raise SystemExit(f"order-status: provider error {e.status} {e.issue} debug_id={e.debug_id}")
        print(json.dumps(r, indent=1, ensure_ascii=False))
        sys.exit(0 if r["provider"]["readable"] else 2)
    elif cmd == "reconcile":
        from . import states as S
        from .service import GateError
        if "--owner-confirm" not in argv:
            raise SystemExit("refused: reconcile changes persistent task state; add --owner-confirm "
                             "(run `order-status` first: it is read-only)")
        svc = _r1d_service(Config.from_env())
        try:
            tid = svc.store.task_for_order(argv[1])
            if tid is None:
                raise GateError("unknown order")
            st = svc.store.task(tid)["state"]
            if st == S.CANCELLED:
                r = svc.reconcile_cancelled(argv[1])
            elif st in S.OPEN_FOR_CAPTURE:
                r = svc.capture_status(argv[1])
            else:
                raise GateError(f"nothing to reconcile in state {st}")
            print(json.dumps(r, indent=1, default=str))
        except GateError as e:
            raise SystemExit(f"refused: {e}")
    elif cmd == "capture-resend":
        from .service import GateError
        svc = _r1d_service(Config.from_env())
        try:
            if "--owner-confirm" not in argv:
                raise SystemExit("refused: add --owner-confirm (re-sends a payment mutation)")
            print(json.dumps({"state": svc.owner_resend_capture(argv[1]),
                              "attempt": svc.store.capture_attempt(argv[1])}, indent=1, default=str))
        except GateError as e:
            raise SystemExit(f"refused: {e}")
    elif cmd == "sandbox-preflight":
        import stat
        from .config import SANDBOX_API_BASE, SANDBOX_GO_TOKEN
        cfg = Config.from_env()
        envf = os.environ.get("AUTONOMOS_ENV_FILE", "")
        mode = (oct(stat.S_IMODE(os.stat(envf).st_mode)) if envf and os.path.exists(envf) else None)
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        checks = {"provider_is_paypal_sandbox": cfg.payment_provider == "paypal_sandbox",
                  "api_base_is_sandbox": cfg.paypal_api_base == SANDBOX_API_BASE,
                  "client_id_set": bool(cfg.paypal_client_id), "secret_set": bool(cfg.paypal_client_secret),
                  "merchant_id_set": bool(cfg.paypal_merchant_id),
                  "env_file_mode_0600_or_0400": mode in ("0o600", "0o400"),
                  "env_file_outside_repo": bool(envf) and not os.path.abspath(envf).startswith(repo),
                  "owner_go_set": cfg.sandbox_owner_go == SANDBOX_GO_TOKEN,
                  "public_base_url_is_loopback": cfg.public_base_url.startswith(("http://127.0.0.1", "http://localhost"))}
        print(json.dumps({"network_calls": 0, "config": cfg.public_view(), "env_file_mode": mode, "checks": checks},
                         indent=1))
        sys.exit(0 if all(v for k, v in checks.items() if k != "owner_go_set") else 1)
    elif cmd == "recover":
        print(json.dumps(_r1d_service(Config.from_env()).recover(), indent=1))
    elif cmd == "verify-receipt":
        from .r1d.receipt2 import verify_any
        args = [a for a in argv[1:]]
        docset, trusted = None, None
        if "--issuer-key" in args:
            i = args.index("--issuer-key")
            trusted = args[i + 1]
            del args[i:i + 2]
        if "--corpus" in args:
            from .corpus import DocumentSet
            i = args.index("--corpus")
            cdir = os.path.abspath(args[i + 1])
            docset = DocumentSet(cdir, os.path.basename(cdir.rstrip("/")))
            del args[i:i + 2]
        rc = json.load(open(args[0]))
        art = json.load(open(args[1])) if len(args) > 1 else None
        ev = json.load(open(args[2])) if len(args) > 2 else None
        r = verify_any(rc, art, ev, docset, trusted)
        print(json.dumps(r, indent=1))
        sys.exit(0 if r["valid"] else 1)
    elif cmd == "execution-summary":
        # R1d.2, read-only: the separated summary of an evidence directory (receipt.json, artifact.json, task_view.json)
        from .r1d.summary import execution_summary
        d = argv[1] if len(argv) > 1 else "."
        L = lambda n: json.load(open(os.path.join(d, n))) if os.path.exists(os.path.join(d, n)) else None  # noqa: E731
        tv = L("task_view.json") or {}
        print(json.dumps(execution_summary((tv.get("task") or {}).get("state"), L("receipt.json"), L("artifact.json")),
                         indent=1))
    elif cmd == "replay-verify":
        # R1d.2, offline: re-verify a historical model output (artifact.json + raw_output.txt + offer.json) with the
        # current verifier; prints a comparison; writes nothing, re-signs nothing
        from .r1d.replay import ReplayRefused, replay_dir
        d = argv[1] if len(argv) > 1 else "."
        corpus = argv[argv.index("--corpus") + 1] if "--corpus" in argv else os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "corpus")
        try:
            r = replay_dir(d, corpus)
        except ReplayRefused as e:
            raise SystemExit(f"replay-verify refused: {e}")
        print(json.dumps({k: r[k] for k in ("inputs", "comparison")}, indent=1, ensure_ascii=False))
    elif cmd == "ai-preflight":
        from .r1d.preflight import preflight
        r, code = preflight()
        print(json.dumps(r, indent=1, default=str))
        sys.exit(code)
    elif cmd == "ai-ledger":
        import sqlite3
        from .r1d.ledger import COLS
        path = os.path.join(Config.from_env().data_dir, "autonomos.sqlite")
        if not os.path.exists(path):
            raise SystemExit(f"ai-ledger: no database at {path}")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = con.execute("SELECT " + ", ".join(COLS) + " FROM r1d_ai_calls WHERE task_id=? ORDER BY seq",
                           (argv[1],)).fetchall()
        print(json.dumps([dict(zip(COLS, r)) for r in rows], indent=1))
    elif cmd == "paypal-sandbox-smoke":
        from .config import SANDBOX_GO_TOKEN
        from .payments.paypal_sandbox import PayPalSandbox
        cfg = Config.from_env(payment_provider="paypal_sandbox")
        if cfg.sandbox_owner_go != SANDBOX_GO_TOKEN:
            raise SystemExit("refused: needs AUTONOMOS_SANDBOX_OWNER_GO (and it bypasses the app's order cap; "
                             "it is NOT part of the R1b GO plan)")
        pp = PayPalSandbox(cfg)
        body = {"intent": "CAPTURE", "purchase_units": [{"reference_id": "SMOKE", "custom_id": "SMOKE",
                "amount": {"currency_code": "USD", "value": "1.00"}, "payee": {"merchant_id": cfg.paypal_merchant_id}}],
                "payment_source": {"paypal": {"experience_context": {"user_action": "PAY_NOW",
                "shipping_preference": "NO_SHIPPING", "return_url": cfg.public_base_url + "/paypal/return",
                "cancel_url": cfg.public_base_url + "/paypal/cancel"}}}}
        o = pp.create_order(body, "autonomos-smoke-" + os.urandom(6).hex())
        print(json.dumps({"order_id": o.get("id"), "status": o.get("status"),
                          "links": [l for l in o.get("links", []) if l.get("rel") in ("payer-action", "approve")]}, indent=1))
    else:
        print(__doc__)


def _r1d_service(cfg, runtime=None):
    from .r1d.aiconfig import AIConfigError
    from .r1d.service import R1dService
    from .service import GateError
    try:
        return R1dService(cfg, runtime=runtime)
    except (AIConfigError, GateError) as e:
        raise SystemExit(f"R1d refused to start: {e}")


if __name__ == "__main__":
    main(sys.argv[1:])
