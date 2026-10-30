"""R1b adversarial tests: uncertain capture recovery, provider errors, webhook safety, competing workers.

LOCAL ONLY: file-backed MOCK provider (shared by OS processes), FIXTURE AI, fake HTTPS transport. Not PayPal evidence.
Run from app/: python3 -B -m unittest tests.test_r1b_adversarial -v
"""
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.error

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from autonomos import states as S  # noqa: E402
from autonomos.config import Config  # noqa: E402
from autonomos.payments.mock import ProviderError  # noqa: E402
from autonomos.payments.paypal_sandbox import PayPalSandbox  # noqa: E402
from autonomos.service import AutoNomos, GateError  # noqa: E402
from test_r1_transactions import CTX, Base, make  # noqa: E402


# ---------------------------------------------------------------- worker processes (module level for spawn)
def w_recover_and_execute(d, oid, barrier, out):
    try:
        svc = make(d)
        barrier.wait()
        svc.recover()
        st = svc.capture_status(oid)["state"]
        tid = svc.store.task_for_order(oid)
        try:
            st = svc.execute(tid)
        except GateError as e:
            st = "GATE:" + str(e)[:50]
    except Exception as e:
        st = "EXC:" + type(e).__name__ + ":" + str(e)[:120]
    out.put(st)


def w_sent_then_die(d, oid):
    """Write-ahead SENT is recorded, then the process dies BEFORE the request leaves (provider never sees it)."""
    svc = make(d, lease=0.3)
    svc.payments.capture_order = lambda order_id, request_id: os._exit(23)
    svc.on_buyer_return(oid)


class TestUncertainCaptureRecovery(Base):
    def test_delayed_provider_processing_is_not_failure_and_not_resent(self):
        tid, oid = self.approved_order()
        self.pp.set_scenario(oid, "delayed:5")                     # lost response; capture invisible for 5 GETs
        self.assertEqual(self.svc.on_buyer_return(oid), S.REVIEW_REQUIRED)
        self.assertNotEqual(self.svc.store.task(tid)["state"], S.FAILED)
        self.assertEqual(self.svc.store.capture_attempt(oid)["state"], "OUTCOME_UNKNOWN")
        with self.assertRaises(GateError):
            self.svc.execute(tid)                                  # uncertain is never success
        self.svc.recover()                                         # restart: read-only, still invisible
        self.assertEqual(self.svc.capture_status(oid)["state"], S.REVIEW_REQUIRED)
        self.assertEqual(self.svc.capture_status(oid)["state"], S.EXECUTION_AUTHORIZED)   # capture surfaces
        self.assertEqual(len(self.capture_calls()), 1)
        self.assertEqual(self.pp.captures_performed(), 1)
        self.assertEqual(self.svc.execute(tid), S.DELIVERED)

    def test_lost_successful_2xx_with_stale_reads(self):
        tid, oid = self.approved_order()
        self.pp.set_scenario(oid, "success_hidden:2")              # 200 OK, but next 2 GETs are stale
        self.assertEqual(self.svc.on_buyer_return(oid), S.EXECUTION_AUTHORIZED)
        self.assertEqual(len(self.capture_calls()), 1)
        self.assertEqual(self.svc.store.capture_attempt(oid)["state"], "RESOLVED")

    def test_5xx_after_processing_is_reconciled_not_resent(self):
        tid, oid = self.approved_order()
        self.pp.set_scenario(oid, "server_error_after")
        self.assertEqual(self.svc.on_buyer_return(oid), S.EXECUTION_AUTHORIZED)
        self.assertEqual(len(self.capture_calls()), 1)
        self.assertEqual(self.svc.store.capture_attempt(oid)["debug_id"], "mockdbg500")

    def test_restart_never_sends_a_capture(self):
        tid, oid = self.approved_order()
        self.assertEqual(self.svc.store.reserve_capture(oid, lease_s=0.05)[0], "OWNER")   # reserved, then "crash"
        time.sleep(0.1)
        rep = self.svc.recover()
        self.assertEqual(self.capture_calls(), [])
        self.assertEqual(rep["captures"], [(tid, S.PAYMENT_PENDING)])
        self.assertEqual(self.svc.store.capture_attempt(oid)["state"], "RELEASED")
        self.assertEqual(self.svc.on_buyer_return(oid), S.EXECUTION_AUTHORIZED)      # explicit buyer return
        self.assertEqual(len(self.capture_calls()), 1)

    def test_crash_between_write_ahead_and_send_parks_for_owner(self):
        tid, oid = self.approved_order()
        p = CTX.Process(target=w_sent_then_die, args=(self.d, oid))
        p.start()
        p.join(60)
        self.assertEqual(p.exitcode, 23)
        a = self.svc.store.capture_attempt(oid)
        self.assertEqual((a["state"], a["sends"]), ("SENT", 1))
        time.sleep(0.4)
        self.svc.recover()
        self.assertEqual(self.capture_calls(), [])                 # provider never saw it; we still do not resend
        self.assertEqual(self.svc.store.task(tid)["state"], S.REVIEW_REQUIRED)
        a = self.svc.store.capture_attempt(oid)
        self.assertEqual(self.svc.owner_resend_capture(oid, now=a["first_sent"] + 400), S.EXECUTION_AUTHORIZED)
        self.assertEqual([c[1] for c in self.capture_calls()], [a["request_id"]])
        self.assertEqual(self.pp.captures_performed(), 1)

    def test_expired_idempotency_window_blocks_owner_resend(self):
        tid, oid = self.approved_order()
        self.pp.set_scenario(oid, "timeout_before")
        self.svc.on_buyer_return(oid)
        a = self.svc.store.capture_attempt(oid)
        with self.assertRaises(GateError) as cm:
            self.svc.owner_resend_capture(oid, now=a["first_sent"] + self.svc.idempotency_window_s + 1)
        self.assertIn("manually", str(cm.exception))
        self.assertEqual(len(self.capture_calls()), 1)
        self.assertEqual(self.svc.store.capture_attempt(oid)["state"], "OUTCOME_UNKNOWN")

    def test_owner_resend_resolves_without_sending_when_capture_appeared(self):
        tid, oid = self.approved_order()
        self.pp.set_scenario(oid, "delayed:3")
        self.assertEqual(self.svc.on_buyer_return(oid), S.REVIEW_REQUIRED)
        a = self.svc.store.capture_attempt(oid)
        self.assertEqual(self.svc.owner_resend_capture(oid, now=a["first_sent"] + 400), S.EXECUTION_AUTHORIZED)
        self.assertEqual(len(self.capture_calls()), 1)

    def test_voided_order_resolves_uncertainty_as_failure(self):
        tid, oid = self.approved_order()
        self.pp.set_scenario(oid, "timeout_never")
        self.svc.on_buyer_return(oid)
        self.pp.mutate_order(oid, lambda o: o.update(status="VOIDED"))
        self.svc.capture_status(oid)
        self.assertEqual(self.svc.store.capture_attempt(oid)["state"], "RELEASED")
        self.assertEqual(self.svc.store.task(tid)["state"], S.FAILED)

    def test_competing_recovery_workers_across_processes(self):
        tid, oid = self.approved_order()
        self.pp.set_scenario(oid, "delayed:3")
        self.assertEqual(self.svc.on_buyer_return(oid), S.REVIEW_REQUIRED)   # 3 settle GETs consume the stale reads
        n = 5
        barrier, out = CTX.Barrier(n), CTX.Queue()
        ps = [CTX.Process(target=w_recover_and_execute, args=(self.d, oid, barrier, out)) for _ in range(n)]
        for p in ps:
            p.start()
        res = [out.get(timeout=90) for _ in ps]
        for p in ps:
            p.join(30)
        self.assertFalse([r for r in res if r.startswith("EXC")], res)
        self.assertEqual(res.count(S.DELIVERED), 1, res)
        self.assertEqual(len(self.capture_calls()), 1)             # no worker sent a second capture
        self.assertEqual(self.pp.captures_performed(), 1)
        self.assertEqual(self.svc.store.db.execute("SELECT COUNT(*) FROM executions").fetchone()[0], 1)

    def test_competing_workers_on_never_completing_capture_send_nothing(self):
        tid, oid = self.approved_order()
        self.pp.set_scenario(oid, "timeout_never")
        self.svc.on_buyer_return(oid)
        n = 4
        barrier, out = CTX.Barrier(n), CTX.Queue()
        ps = [CTX.Process(target=w_recover_and_execute, args=(self.d, oid, barrier, out)) for _ in range(n)]
        for p in ps:
            p.start()
        res = [out.get(timeout=90) for _ in ps]
        for p in ps:
            p.join(30)
        self.assertTrue(all(r.startswith("GATE") for r in res), res)
        self.assertEqual(len(self.capture_calls()), 1)
        self.assertEqual(self.pp.captures_performed(), 0)
        self.assertIsNone(self.svc.store.execution(tid))


class TestMutationGaps(Base):
    def test_capture_response_is_never_the_payment_authority(self):
        tid, oid = self.approved_order()
        self.pp.set_scenario(oid, "pending")                       # provider really holds a PENDING capture
        real = self.pp.capture_order

        def lying(order_id, request_id):
            r = real(order_id, request_id)
            r["purchase_units"][0]["payments"]["captures"][0]["status"] = "COMPLETED"   # response claims COMPLETED
            return r
        self.pp.capture_order = lying
        self.assertEqual(self.svc.on_buyer_return(oid), S.PAYMENT_PENDING)
        self.assertEqual(self.svc.store.capture(oid)["verification"]["verdict"], "CAPTURE_PENDING")

    def test_store_refuses_new_generation_without_buyer_reapproval(self):
        tid, oid = self.approved_order()
        self.pp.set_scenario(oid, "declined_instrument")
        self.svc.on_buyer_return(oid)
        self.pp.buyer_action(oid, "approve")
        self.assertEqual(self.svc.store.reserve_capture(oid, new_generation=True)[0], "RESOLVED")
        self.assertTrue(self.svc.store.request_reapproval(oid))
        self.assertEqual(self.svc.store.reserve_capture(oid, new_generation=True)[0], "OWNER")


class TestProviderErrors(Base):
    def test_read_429_bounded_backoff(self):
        tid, oid = self.approved_order()
        self.pp.set_get_scenario(oid, "rate_limit:2")
        self.assertEqual(self.svc.on_buyer_return(oid), S.EXECUTION_AUTHORIZED)
        self.pp.set_get_scenario(oid, "rate_limit:9")
        t2, o2 = self.approved_order()
        self.pp.set_get_scenario(o2, "rate_limit:9")
        self.assertEqual(self.svc.on_buyer_return(o2), S.PAYMENT_PENDING)
        self.assertEqual([c for c in self.capture_calls() if c[1].endswith(o2)], [])  # no read -> no mutation
        self.assertIn("provider_unavailable", [e["evidence_kind"] for e in self.svc.store.events(t2)])

    def test_oauth_expiry_during_capture_is_unknown_not_resent(self):
        tid, oid = self.approved_order()
        real, sent = self.pp.capture_order, []

        def expire_then_capture(order_id, request_id):
            sent.append(request_id)
            self.pp.token_valid = False
            return real(order_id, request_id)
        self.pp.capture_order = expire_then_capture
        self.assertEqual(self.svc.on_buyer_return(oid), S.REVIEW_REQUIRED)
        self.pp.token_valid = True
        self.assertEqual(self.svc.capture_status(oid)["state"], S.REVIEW_REQUIRED)
        self.svc.recover()
        self.assertEqual(len(sent), 1)                             # one send; refreshed token does not re-send
        self.assertEqual(self.svc.store.capture_attempt(oid)["state"], "OUTCOME_UNKNOWN")
        self.assertEqual(self.pp.captures_performed(), 0)

    def test_invalid_provider_response_on_capture_is_unknown(self):
        tid, oid = self.approved_order()

        def garbage(order_id, request_id):
            raise ProviderError(200, "INVALID_PROVIDER_RESPONSE", "unparseable body")
        self.pp.capture_order = garbage
        self.assertEqual(self.svc.on_buyer_return(oid), S.REVIEW_REQUIRED)
        self.assertEqual(self.svc.store.capture_attempt(oid)["state"], "OUTCOME_UNKNOWN")

    def test_definitive_refusal_fails_without_capture(self):
        tid, oid = self.approved_order()

        def refuse(order_id, request_id):
            raise ProviderError(422, "PAYEE_ACCOUNT_RESTRICTED", "", debug_id="dbg-x")
        self.pp.capture_order = refuse
        self.assertEqual(self.svc.on_buyer_return(oid), S.FAILED)
        a = self.svc.store.capture_attempt(oid)
        self.assertEqual((a["state"], a["debug_id"]), ("RELEASED", "dbg-x"))

    def test_payer_action_required_needs_buyer_never_auto_replaced(self):
        tid, oid = self.approved_order()

        def payer_action(order_id, request_id):
            raise ProviderError(422, "PAYER_ACTION_REQUIRED", "")
        self.pp.capture_order = payer_action
        self.assertEqual(self.svc.on_buyer_return(oid), S.PAYMENT_PENDING)
        self.assertEqual(self.svc.on_buyer_return(oid), S.PAYMENT_PENDING)
        self.assertEqual(self.svc.store.capture_attempt(oid)["sends"], 1)


class Transport:
    """Fake HTTPS transport with headers (LOCAL FIXTURE)."""

    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, req, timeout=30):
        self.requests.append(req)
        code, body, headers = self.responses.pop(0)
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        if code >= 400:
            raise urllib.error.HTTPError(req.full_url, code, "err", headers, io.BytesIO(raw))

        class Resp(io.BytesIO):
            status = code

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        r = Resp(raw)
        r.headers = headers
        return r


class TestSandboxAdapterErrors(unittest.TestCase):
    def pp(self, responses, webhook_id="WH1"):
        cfg = Config.from_env(payment_provider="paypal_sandbox", paypal_client_id="CID", paypal_client_secret="S3CRET",
                              paypal_merchant_id="M1", paypal_webhook_id=webhook_id, data_dir=tempfile.mkdtemp())
        t = Transport(responses)
        return PayPalSandbox(cfg, opener=t), t, cfg

    def test_capture_401_is_never_resent_by_the_adapter(self):
        pp, t, _ = self.pp([(200, {"access_token": "T", "expires_in": 3600}, {}),
                            (401, {"name": "INVALID_TOKEN"}, {})])
        with self.assertRaises(ProviderError) as cm:
            pp.capture_order("O1", "cap-1")
        self.assertEqual(cm.exception.status, 401)
        self.assertFalse(cm.exception.definitive_refusal)
        self.assertEqual(len(t.requests), 2)                       # token + ONE capture
        self.assertIsNone(pp._tok)

    def test_debug_id_and_retry_after_from_headers_and_invalid_json(self):
        pp, t, _ = self.pp([(200, {"access_token": "T", "expires_in": 3600}, {}),
                            (429, b"not json", {"Paypal-Debug-Id": "hdr-dbg", "Retry-After": "2"}),
                            (200, b"<html>oops</html>", {"Paypal-Debug-Id": "hdr-ok"})])
        with self.assertRaises(ProviderError) as cm:
            pp.get_order("O1")
        self.assertEqual((cm.exception.status, cm.exception.debug_id, cm.exception.retry_after), (429, "hdr-dbg", 2.0))
        with self.assertRaises(ProviderError) as cm:
            pp.capture_order("O1", "cap-1")
        e = cm.exception
        self.assertEqual((e.issue, e.debug_id, e.definitive_refusal), ("INVALID_PROVIDER_RESPONSE", "hdr-ok", False))

    def test_instrument_declined_issue_and_debug_id_from_body(self):
        pp, t, _ = self.pp([(200, {"access_token": "T", "expires_in": 3600}, {}),
                            (422, {"name": "UNPROCESSABLE_ENTITY", "debug_id": "b-dbg",
                                   "details": [{"issue": "INSTRUMENT_DECLINED"}]}, {})])
        with self.assertRaises(ProviderError) as cm:
            pp.capture_order("O1", "cap-1")
        self.assertEqual((cm.exception.issue, cm.exception.debug_id), ("INSTRUMENT_DECLINED", "b-dbg"))

    def test_sandbox_webhooks_disabled_without_webhook_id_and_no_network(self):
        pp, t, cfg = self.pp([], webhook_id="")
        svc = AutoNomos(cfg, payments=pp, ai=None)
        r = svc.handle_webhook({"PAYPAL-TRANSMISSION-ID": "tx"}, b'{"id":"E1","event_type":"X"}')
        self.assertEqual((r["accepted"], r["http"]), (False, 400))
        self.assertEqual(t.requests, [])


class TestSandboxOwnerGate(unittest.TestCase):
    """Hard caps that bound what the owner's GO can do, enforced in code (MOCK transport labelled SANDBOX)."""

    def svc(self, **over):
        from autonomos.ai import FixtureProvider
        from autonomos.payments.mock import MOCK_MERCHANT_ID, MockPayPal
        d = tempfile.mkdtemp()
        pp = MockPayPal(path=os.path.join(d, "m.sqlite"))
        pp.environment = "SANDBOX"                                   # exercise the SANDBOX-only branches offline
        cfg = Config.from_env(data_dir=d, payment_provider="paypal_sandbox", paypal_client_id="C",
                              paypal_client_secret="S", paypal_merchant_id=MOCK_MERCHANT_ID, ai_provider="fixture", **over)
        return AutoNomos(cfg, payments=pp, ai=FixtureProvider())

    def new_order(self, svc):
        tid = svc.request_task({"capability": "research_snapshot", "topic": "quantum hardware evidence",
                                "corpus": "agentnomos_quantum"})
        return tid, svc.accept_offer(tid, svc.store.offer(task_id=tid)["_digest"]).rsplit("/", 1)[1]

    def test_order_cap_is_one(self):
        svc = self.svc()
        self.new_order(svc)
        with self.assertRaises(GateError) as cm:
            self.new_order(svc)
        self.assertIn("order cap", str(cm.exception))
        self.assertEqual(len([c for c in svc.payments.calls if c[0] == "create_order"]), 1)

    def test_capture_request_cap(self):
        svc = self.svc(sandbox_max_capture_requests=1)
        svc.backoff_s = 0.01
        tid, oid = self.new_order(svc)
        svc.payments.buyer_action(oid, "approve")
        svc.payments.set_scenario(oid, "timeout_before")
        self.assertEqual(svc.on_buyer_return(oid), S.REVIEW_REQUIRED)
        a = svc.store.capture_attempt(oid)
        with self.assertRaises(GateError):
            svc.owner_resend_capture(oid, now=a["first_sent"] + 400)
        self.assertEqual(len([c for c in svc.payments.calls if c[0] == "capture_order"]), 1)
        self.assertEqual(svc.store.capture_attempt(oid)["state"], "OUTCOME_UNKNOWN")

    def test_serve_refuses_sandbox_without_owner_go(self):
        import subprocess
        env = dict(os.environ, AUTONOMOS_PAYMENT_PROVIDER="paypal_sandbox", PAYPAL_CLIENT_ID="x",
                   PAYPAL_CLIENT_SECRET="SECRETVALUE123", PAYPAL_MERCHANT_ID="z", AUTONOMOS_DATA_DIR=tempfile.mkdtemp())
        env.pop("AUTONOMOS_SANDBOX_OWNER_GO", None)
        r = subprocess.run([sys.executable, "-B", "-m", "autonomos", "serve"], env=env, capture_output=True, text=True,
                           timeout=30, cwd=os.path.join(os.path.dirname(__file__), ".."))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("owner's explicit GO", r.stderr)
        self.assertNotIn("SECRETVALUE123", r.stderr + r.stdout)


class TestWebhookSafety(Base):
    def hook(self, ev, tx="TX-1", sig=None, reconcile=True):
        raw = json.dumps(ev).encode()
        h = {"PAYPAL-TRANSMISSION-ID": tx, "PAYPAL-TRANSMISSION-TIME": "t"}
        h["PAYPAL-TRANSMISSION-SIG"] = sig or self.pp.sign_webhook(tx, "t", raw)
        return self.svc.handle_webhook(h, raw, reconcile=reconcile)

    def test_verified_capture_event_cannot_authorize_without_provider_capture(self):
        tid = self.svc.request_task({"capability": "research_snapshot", "topic": "q quantum hardware evidence",
                                     "corpus": "agentnomos_quantum"})
        link = self.svc.accept_offer(tid, self.svc.store.offer(task_id=tid)["_digest"])
        oid = link.rsplit("/", 1)[1]                               # NOT approved at the provider
        ev = {"id": "WH-C1", "event_type": "PAYMENT.CAPTURE.COMPLETED",
              "resource": {"id": "FAKE-CAP", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "1.00"},
                           "supplementary_data": {"related_ids": {"order_id": oid}}}}
        r = self.hook(ev)
        self.assertEqual(r["http"], 200)
        self.assertEqual(self.svc.store.task(tid)["state"], S.PAYMENT_PENDING)
        self.assertEqual(self.capture_calls(), [])
        with self.assertRaises(GateError):
            self.svc.execute(tid)
        kinds = [(e["evidence_kind"], e["detail"].get("simulated")) for e in self.svc.store.events(tid)]
        self.assertIn(("webhook_received", True), kinds)           # labelled as a SIMULATED (mock) event

    def test_http_codes_duplicate_invalid_unverifiable_unpersistable(self):
        tid, oid = self.approved_order()
        ev = {"id": "WH-D1", "event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": oid}}
        self.assertEqual(self.hook(ev, tx="A", reconcile=False)["http"], 200)
        d = self.hook(ev, tx="B", reconcile=False)
        self.assertEqual((d["http"], d.get("duplicate")), (200, True))       # duplicate delivery: ack, no re-run
        self.assertEqual(self.hook(ev, tx="C", sig="00" * 32)["http"], 400)
        self.assertEqual(self.hook({"event_type": "X"}, tx="D")["http"], 400)  # no event id
        real = self.pp.verify_webhook
        self.pp.verify_webhook = lambda *a, **k: (_ for _ in ()).throw(ProviderError(0, "TIMEOUT"))
        self.assertEqual(self.hook(dict(ev, id="WH-D2"), tx="E")["http"], 503)
        self.pp.verify_webhook = real

        def boom(*a):
            raise sqlite3.OperationalError("disk I/O error")
        self.svc.store.record_webhook = boom
        self.assertEqual(self.hook(dict(ev, id="WH-D3"), tx="F")["http"], 503)
        self.assertEqual(self.capture_calls(), [])

    def test_crash_after_ack_is_reconciled_by_recovery(self):
        tid, oid = self.approved_order()
        ev = {"id": "WH-K1", "event_type": "PAYMENT.CAPTURE.COMPLETED",
              "resource": {"supplementary_data": {"related_ids": {"order_id": oid}}}}
        self.assertTrue(self.hook(ev, tx="K", reconcile=False)["queued"])      # acked; process "crashes" here
        self.pp.capture_order(oid, "autonomos-capture-" + oid)                 # provider holds a real capture
        svc2 = make(self.d)                                                     # restart
        rep = svc2.recover()
        self.assertEqual(rep["webhooks"], [("K", S.EXECUTION_AUTHORIZED)])
        self.assertEqual(svc2.store.db.execute("SELECT outcome FROM webhooks WHERE transmission_id='K'").fetchone()[0],
                         S.EXECUTION_AUTHORIZED)
        self.assertEqual(svc2.recover()["webhooks"], [])                        # processed exactly once
        self.assertEqual(self.pp.captures_performed(), 1)

    def test_unknown_order_and_merchant_mismatch(self):
        r = self.hook({"id": "WH-U", "event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": "NOPE"}}, tx="U")
        self.assertEqual(r["action"], "none (no matching task)")
        tid, oid = self.approved_order()
        self.pp.mutate_order(oid, lambda o: o["purchase_units"][0]["payee"].update(merchant_id="OTHER"))
        self.hook({"id": "WH-M", "event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": oid}}, tx="M")
        self.assertEqual(self.svc.store.task(tid)["state"], S.REVIEW_REQUIRED)
        self.assertIsNone(self.svc.store.execution(tid))


if __name__ == "__main__":
    unittest.main()
