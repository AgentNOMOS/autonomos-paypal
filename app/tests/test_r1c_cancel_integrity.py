"""R1c adversarial tests: cancel-callback integrity, explicit cancellation, cancel/capture atomicity, recovery of a
capture on a CANCELLED task, and the read-only order-status diagnosis.

LOCAL ONLY: file-backed MOCK provider (shared by OS processes), FIXTURE AI, fake HTTPS transport. Not PayPal evidence.
The optional incident replay reads a COPY of the frozen incident database (skipped when it is not present).
Run from app/: python3 -B -m unittest tests.test_r1c_cancel_integrity -v
"""
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from autonomos import receipt as R, states as S  # noqa: E402
from autonomos.config import Config  # noqa: E402
from autonomos.order_status import ReadOnlyOrders, classify, order_status  # noqa: E402
from autonomos.payments.mock import ProviderError  # noqa: E402
from autonomos.payments.paypal_sandbox import PayPalSandbox  # noqa: E402
from autonomos.service import GateError  # noqa: E402
from autonomos.store import TransitionError  # noqa: E402
from test_r1_transactions import CTX, Q, Base, make  # noqa: E402
from test_r1b_adversarial import Transport  # noqa: E402

APP = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LEGACY_REASON = "buyer cancelled at PayPal checkout"


def land_capture(pp, oid, status="COMPLETED"):
    """The provider completes a capture that this process did not observe (late / in-flight capture)."""
    def f(o):
        pu = o["purchase_units"][0]
        pu["payments"] = {"captures": [{"id": "MOCK-CAPTURE-LATE01", "status": status, "amount": copy.deepcopy(pu["amount"]),
                                        "custom_id": pu.get("custom_id"), "invoice_id": pu.get("invoice_id"),
                                        "final_capture": True}]}
        o["status"] = "COMPLETED"
    pp.mutate_order(oid, f)


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest() if os.path.exists(path) else None


# ---------------------------------------------------------------- worker processes (module level for spawn)
def w_cancel(d, tid, barrier, out):
    try:
        svc = make(d)
        barrier.wait()
        try:
            out.put(("cancel", svc.cancel(tid)))
        except GateError:
            out.put(("cancel", "GATE"))
    except Exception as e:
        out.put(("cancel", "EXC:" + type(e).__name__ + ":" + str(e)[:120]))


def w_return(d, oid, barrier, out):
    try:
        svc = make(d)
        barrier.wait()
        out.put(("return", svc.on_buyer_return(oid)))
    except Exception as e:
        out.put(("return", "EXC:" + type(e).__name__ + ":" + str(e)[:120]))


class R1cBase(Base):
    def new_order(self):
        """Order created, buyer sent to checkout, nothing approved (the incident's starting point)."""
        tid = self.svc.request_task({"capability": "research_snapshot", "topic": Q, "corpus": "agentnomos_quantum"})
        link = self.svc.accept_offer(tid, self.svc.store.offer(task_id=tid)["_digest"])
        return tid, link.rsplit("/", 1)[1]

    def state(self, tid):
        return self.svc.store.task(tid)["state"]

    def kinds(self, tid):
        return [e["evidence_kind"] for e in self.svc.store.events(tid)]

    def legacy_cancelled_with_unknown_capture(self):
        """What pre-R1c code could leave behind (incident D3): an uncertain capture, then a redirect cancellation."""
        tid, oid = self.approved_order()
        self.pp.set_scenario(oid, "timeout_never")
        self.assertEqual(self.svc.on_buyer_return(oid), S.REVIEW_REQUIRED)
        self.assertEqual(self.svc.store.capture_attempt(oid)["state"], "OUTCOME_UNKNOWN")
        self.svc.store.transition(tid, S.CANCELLED, "buyer", "cancellation", {"reason": LEGACY_REASON},
                                  reason=LEGACY_REASON)    # the R1b transition, written directly
        return tid, oid


# ================================================================ cancel redirect = navigation only
class TestCancelRedirectIsNavigation(R1cBase):
    def test_A_buyer_leaves_checkout_task_stays_recoverable_then_pays(self):
        tid, oid = self.new_order()
        n = len(self.pp.calls)
        self.assertEqual(self.svc.on_buyer_cancel(oid), S.PAYMENT_PENDING)       # the incident's request
        self.assertEqual(len(self.pp.calls), n)                                   # no provider call at all
        ev = [e for e in self.svc.store.events(tid) if e["evidence_kind"] == "checkout_navigation"]
        self.assertEqual(len(ev), 1)
        self.assertIs(ev[0]["detail"]["authoritative"], False)
        self.assertEqual(ev[0]["from"], ev[0]["to"])                              # a note, not a transition
        self.pp.buyer_action(oid, "approve")                                      # buyer goes back and approves
        self.assertEqual(self.svc.on_buyer_return(oid), S.EXECUTION_AUTHORIZED)
        self.assertEqual(self.svc.execute(tid), S.DELIVERED)
        self.assertEqual(len(self.capture_calls()), 1)
        rc = self.svc.store.receipt(tid)
        self.assertTrue(R.verify(rc, self.svc.store.artifact(rc["body"]["artifact"]["digest"]),
                                 self.svc.store.events(tid))["valid"])

    def test_B_cancel_redirect_after_approval_does_not_drop_the_payment(self):
        tid, oid = self.approved_order()
        self.assertEqual(self.svc.on_buyer_cancel(oid), S.PAYMENT_PENDING)       # stale tab / history / replay
        self.assertEqual(self.svc.on_buyer_return(oid), S.EXECUTION_AUTHORIZED)
        self.assertEqual(self.pp.captures_performed(), 1)

    def test_C_cancel_redirect_and_cancel_while_capture_in_flight(self):
        tid, oid = self.approved_order()
        other = make(self.d)                                                      # another worker / process
        real, seen = self.pp.capture_order, {}

        def capture(order_id, request_id):
            seen["redirect"] = self.svc.on_buyer_cancel(order_id)
            try:
                other.cancel(tid)
            except GateError as e:
                seen["cancel"] = str(e)
            return real(order_id, request_id)
        self.pp.capture_order = capture
        self.assertEqual(self.svc.on_buyer_return(oid), S.EXECUTION_AUTHORIZED)
        self.assertEqual(seen["redirect"], S.PAYMENT_PENDING)
        self.assertIn("in flight", seen["cancel"])
        self.assertEqual(self.svc.store.capture_attempt(oid)["state"], "RESOLVED")

    def test_D_cancel_during_unknown_outcome_then_late_capture_is_recovered(self):
        tid, oid = self.approved_order()
        self.pp.set_scenario(oid, "timeout_never")
        self.assertEqual(self.svc.on_buyer_return(oid), S.REVIEW_REQUIRED)
        self.assertEqual(self.svc.on_buyer_cancel(oid), S.REVIEW_REQUIRED)
        with self.assertRaises(GateError):
            self.svc.cancel(tid)                                                  # explicit cancel refused too
        self.assertEqual(self.state(tid), S.REVIEW_REQUIRED)
        self.assertEqual(self.svc.store.capture_attempt(oid)["state"], "OUTCOME_UNKNOWN")
        land_capture(self.pp, oid)                                                # the in-flight capture lands
        self.assertEqual(self.svc.capture_status(oid)["state"], S.EXECUTION_AUTHORIZED)
        self.assertEqual(self.svc.execute(tid), S.DELIVERED)
        self.assertEqual(len(self.capture_calls()), 1)                            # never re-sent

    def test_E_cancel_redirect_after_delivery_changes_nothing(self):
        tid, oid = self.approved_order()
        self.svc.on_buyer_return(oid)
        self.svc.execute(tid)
        self.assertEqual(self.svc.on_buyer_cancel(oid), S.DELIVERED)
        with self.assertRaises(GateError):
            self.svc.cancel(tid)
        self.assertEqual(self.state(tid), S.DELIVERED)
        rc = self.svc.store.receipt(tid)
        self.assertTrue(R.verify(rc, self.svc.store.artifact(rc["body"]["artifact"]["digest"]),
                                 self.svc.store.events(tid))["valid"])


# ================================================================ explicit cancellation (terminated unpaid task)
class TestExplicitCancel(R1cBase):
    def test_F_explicit_cancel_reads_provider_and_closes_capture_for_good(self):
        tid, oid = self.new_order()
        self.assertEqual(self.svc.cancel(tid, "buyer cancelled explicitly"), S.CANCELLED)
        self.assertIn(("get_order", oid), self.pp.calls)                          # provider consulted first
        ev = self.svc.store.events(tid)[-1]
        self.assertEqual((ev["to"], ev["detail"]["explicit"], ev["detail"]["provider_status"]),
                         (S.CANCELLED, True, "PAYER_ACTION_REQUIRED"))
        self.pp.buyer_action(oid, "approve")                                      # buyer approves anyway, later
        self.assertEqual(self.svc.on_buyer_return(oid), S.CANCELLED)
        self.assertEqual(self.capture_calls(), [])
        self.assertEqual(self.svc.store.reserve_capture(oid)[0], "CLOSED")        # no attempt can be reserved
        self.assertIsNone(self.svc.store.capture_attempt(oid))

    def test_G_explicit_cancel_refused_when_provider_unreadable(self):
        tid, oid = self.new_order()
        self.pp.token_valid = False
        with self.assertRaises(GateError) as cm:
            self.svc.cancel(tid)
        self.assertIn("could not be read", str(cm.exception))
        self.pp.token_valid = True
        self.pp.set_get_scenario(oid, "rate_limit:9")                             # transient, retries exhausted
        with self.assertRaises(GateError):
            self.svc.cancel(tid)
        self.assertEqual(self.state(tid), S.PAYMENT_PENDING)

    def test_H_explicit_cancel_refused_when_provider_shows_a_capture(self):
        tid, oid = self.approved_order()
        land_capture(self.pp, oid)                                                # not known locally
        with self.assertRaises(GateError):
            self.svc.cancel(tid)
        self.assertEqual(self.state(tid), S.PAYMENT_PENDING)
        self.assertIn("cancel_refused", self.kinds(tid))

    def test_I_voided_order_is_failed_not_cancelled(self):
        tid, oid = self.new_order()
        self.pp.mutate_order(oid, lambda o: o.update(status="VOIDED"))
        self.assertEqual(self.svc.cancel(tid), S.FAILED)
        self.assertEqual(self.svc.store.task(tid)["reason"], "ORDER_VOIDED")

    def test_J_cancel_before_any_order_needs_no_provider(self):
        tid = self.svc.request_task({"capability": "research_snapshot", "topic": Q, "corpus": "agentnomos_quantum"})
        self.assertEqual(self.svc.cancel(tid), S.CANCELLED)
        self.assertEqual(self.pp.calls, [])
        self.assertEqual(self.svc.cancel(tid), S.CANCELLED)                       # idempotent

    def test_K_cancel_refused_while_a_live_worker_holds_the_reservation(self):
        tid, oid = self.approved_order()
        self.assertEqual(make(self.d).store.reserve_capture(oid, lease_s=30)[0], "OWNER")
        with self.assertRaises(GateError) as cm:
            self.svc.cancel(tid)
        self.assertIn("RESERVED", str(cm.exception))

    def test_L_cancel_allowed_after_definitive_decline(self):
        tid, oid = self.approved_order()
        self.pp.set_scenario(oid, "declined_instrument")
        self.assertEqual(self.svc.on_buyer_return(oid), S.PAYMENT_PENDING)
        self.assertEqual(self.svc.store.capture_attempt(oid)["state"], "RELEASED")
        self.assertEqual(self.svc.cancel(tid), S.CANCELLED)

    def test_M_local_resolved_attempt_blocks_cancel_even_if_provider_read_is_stale(self):
        tid, oid = self.approved_order()
        self.pp.set_scenario(oid, "pending")                                      # capture PENDING at provider
        self.assertEqual(self.svc.on_buyer_return(oid), S.PAYMENT_PENDING)
        self.assertEqual(self.svc.store.capture_attempt(oid)["state"], "RESOLVED")
        with self.pp._state() as st:                                              # next GETs hide the capture
            st["hidden"][oid] = 5
        with self.assertRaises(GateError):
            self.svc.cancel(tid)
        self.assertEqual(self.state(tid), S.PAYMENT_PENDING)

    def test_N_explicit_cancel_after_approval_is_allowed_and_nothing_is_captured(self):
        tid, oid = self.approved_order()
        self.assertEqual(self.svc.cancel(tid), S.CANCELLED)
        self.assertEqual(self.svc.store.events(tid)[-1]["detail"]["provider_status"], "APPROVED")
        self.assertEqual(self.svc.on_buyer_return(oid), S.CANCELLED)
        self.assertEqual(self.pp.captures_performed(), 0)


# ================================================================ cancel vs capture atomicity
class TestCancelCaptureAtomicity(R1cBase):
    def test_O_cancel_committed_before_write_ahead_means_no_capture_is_sent(self):
        tid, oid = self.approved_order()
        self.svc.capture_lease_s = 0.05
        other, real_get, seen = make(self.d), self.pp.get_order, {}

        def get_then_cancel(order_id):                                            # between reservation and SENT
            if "cancel" not in seen:
                time.sleep(0.15)                                                  # our lease has expired
                seen["cancel"] = other.cancel(tid)
            return real_get(order_id)
        self.pp.get_order = get_then_cancel
        self.assertEqual(self.svc.on_buyer_return(oid), S.CANCELLED)
        self.assertEqual(seen["cancel"], S.CANCELLED)
        self.assertEqual(self.capture_calls(), [])
        a = self.svc.store.capture_attempt(oid)
        self.assertEqual((a["state"], a["sends"]), ("RELEASED", 0))
        self.assertIn("capture_not_sent", self.kinds(tid))

    def test_O2_capture_written_ahead_during_the_cancel_read_blocks_the_cancel(self):
        tid, oid = self.approved_order()
        other, real_get = make(self.d), self.pp.get_order

        def get_while_other_sends(order_id):                                      # after the pre-check, before txn
            if other.store.capture_attempt(order_id) is None:
                self.assertEqual(other.store.reserve_capture(order_id)[0], "OWNER")
                other.store.capture_mark(order_id, ("RESERVED",), "SENT", sent=True)
            return real_get(order_id)
        self.pp.get_order = get_while_other_sends
        with self.assertRaises(GateError) as cm:
            self.svc.cancel(tid)
        self.assertIn("in flight", str(cm.exception))
        self.assertEqual(self.state(tid), S.PAYMENT_PENDING)

    def test_P_processes_racing_return_and_cancel_never_capture_a_cancelled_task(self):
        outcomes = set()
        for _ in range(4):
            d = tempfile.mkdtemp(prefix="autonomos_r1c_race_")
            svc = make(d)
            tid = svc.request_task({"capability": "research_snapshot", "topic": Q, "corpus": "agentnomos_quantum"})
            oid = svc.accept_offer(tid, svc.store.offer(task_id=tid)["_digest"]).rsplit("/", 1)[1]
            svc.payments.buyer_action(oid, "approve")
            b, out = CTX.Barrier(4), CTX.Queue()
            ps = [CTX.Process(target=w_cancel, args=(d, tid, b, out)) for _ in range(2)] + \
                 [CTX.Process(target=w_return, args=(d, oid, b, out)) for _ in range(2)]
            [p.start() for p in ps]
            [p.join(120) for p in ps]
            res = [out.get(timeout=5) for _ in ps]
            self.assertFalse([r for r in res if str(r[1]).startswith("EXC:")], res)
            st, a = svc.store.task(tid)["state"], svc.store.capture_attempt(oid)
            captured = svc.payments.captures_performed()
            if st == S.CANCELLED:
                self.assertEqual(captured, 0, res)
                self.assertTrue(a is None or a["sends"] == 0, (a, res))
            else:
                self.assertEqual((st, captured), (S.EXECUTION_AUTHORIZED, 1), res)
            outcomes.add(st)
        self.assertTrue(outcomes <= {S.CANCELLED, S.EXECUTION_AUTHORIZED})


# ================================================================ capture on a CANCELLED task (legacy data)
class TestLateCaptureOnCancelledTask(R1cBase):
    def test_Q_order_status_detects_it_and_writes_nothing(self):
        tid, oid = self.legacy_cancelled_with_unknown_capture()
        land_capture(self.pp, oid)
        self.svc.store._db.close()                                                # quiesce our own connection
        db = os.path.join(self.d, "autonomos.sqlite")
        before = (sha(db), sha(db + "-wal"), sorted(os.listdir(self.d)))
        n = len(self.pp.calls)
        r = order_status(self.svc.cfg, tid, self.pp)
        self.assertEqual((sha(db), sha(db + "-wal"), sorted(os.listdir(self.d))), before)
        self.assertEqual({c[0] for c in self.pp.calls[n:]}, {"get_order"})
        self.assertEqual(r["phase"], "CAPTURE_VERIFIED_COMPLETED")
        self.assertEqual(r["task"]["state"], S.CANCELLED)
        for alert in ("CAPTURE_ON_TERMINAL_TASK", "CAPTURE_ATTEMPT_ON_CANCELLED_TASK", "LEGACY_REDIRECT_CANCELLATION"):
            self.assertIn(alert, r["alerts"])
        self.assertEqual((r["approval"]["buyer_approved"], r["capture"]["present"], r["merchant"]["match"]),
                         (True, True, True))
        self.assertIn("reconcile", r["next_step"])

    def test_R_recover_reports_it_without_reading_or_changing_anything(self):
        tid, oid = self.legacy_cancelled_with_unknown_capture()
        land_capture(self.pp, oid)
        n = len(self.pp.calls)
        rep = self.svc.recover()
        self.assertIn((tid, "OUTCOME_UNKNOWN"), [tuple(x) for x in rep["cancelled_with_capture_attempt"]])
        self.assertEqual(self.pp.calls[n:], [])
        self.assertEqual(self.state(tid), S.CANCELLED)

    def test_S_no_automatic_trigger_reopens_or_executes_it(self):
        tid, oid = self.legacy_cancelled_with_unknown_capture()
        land_capture(self.pp, oid)
        self.assertEqual(self.svc.capture_status(oid)["state"], S.CANCELLED)
        self.assertEqual(self.svc.on_buyer_return(oid), S.CANCELLED)
        self.assertEqual(self.svc.on_buyer_cancel(oid), S.CANCELLED)
        self.assertEqual(self.svc.reconcile_task(tid)["state"], S.CANCELLED)       # webhook path: note only
        a = self.svc.store.capture_attempt(oid)
        with self.assertRaises(GateError):
            self.svc.owner_resend_capture(oid, now=a["first_sent"] + 400)          # inside the resend window
        self.assertEqual(self.svc.store.reserve_capture(oid)[0], "CLOSED")
        self.assertEqual(len(self.capture_calls()), 1)
        self.assertIsNone(self.svc.store.execution(tid))
        self.assertEqual(self.state(tid), S.CANCELLED)

    def test_T_owner_reconcile_moves_to_review_and_never_executes(self):
        tid, oid = self.legacy_cancelled_with_unknown_capture()
        land_capture(self.pp, oid)
        r = self.svc.reconcile_cancelled(oid)
        self.assertEqual((r["state"], r["changed"], r["verdict"]), (S.REVIEW_REQUIRED, True, "CAPTURE_VERIFIED"))
        self.assertEqual(self.svc.store.capture_attempt(oid)["state"], "RESOLVED")
        self.assertEqual(self.svc.store.capture(oid)["status"], "COMPLETED")
        ev = self.svc.store.events(tid)[-1]
        self.assertEqual((ev["from"], ev["to"], ev["actor"]), (S.CANCELLED, S.REVIEW_REQUIRED, "operator"))
        # nothing afterwards turns it into paid work by itself
        self.assertEqual(self.svc.capture_status(oid)["state"], S.REVIEW_REQUIRED)
        self.assertEqual(self.svc.on_buyer_return(oid), S.REVIEW_REQUIRED)
        self.assertEqual(self.svc.reconcile_task(tid)["state"], S.REVIEW_REQUIRED)
        with self.assertRaises(GateError):
            self.svc.execute(tid)
        with self.assertRaises(GateError):
            self.svc.cancel(tid)                                                  # a recorded capture blocks it
        with self.assertRaises(GateError):
            self.svc.reconcile_cancelled(oid)                                     # no longer CANCELLED
        self.assertEqual(len(self.capture_calls()), 1)

    def test_U_reconcile_without_provider_capture_changes_nothing(self):
        tid, oid = self.legacy_cancelled_with_unknown_capture()                   # provider: APPROVED, no capture
        r = self.svc.reconcile_cancelled(oid)
        self.assertEqual((r["state"], r["changed"], r["provider_status"]), (S.CANCELLED, False, "APPROVED"))
        self.assertEqual(self.state(tid), S.CANCELLED)
        st = order_status(self.svc.cfg, oid, self.pp)
        self.assertEqual(st["phase"], "CAPTURE_OUTCOME_UNKNOWN")
        self.assertIn("APPROVED_AT_PROVIDER_AFTER_LOCAL_CANCEL", st["alerts"])

    def test_V_reopen_edge_is_operator_only_with_payment_verification(self):
        tid, oid = self.new_order()
        self.svc.cancel(tid)
        self.assertFalse(S.allowed(S.CANCELLED, S.REVIEW_REQUIRED))
        self.assertTrue(S.allowed(S.CANCELLED, S.REVIEW_REQUIRED, "operator"))
        for actor, kind in (("buyer", "payment_verification"), ("payment_gateway", "payment_verification"),
                            ("webhook_reconciler", "payment_verification"), ("operator", None),
                            ("operator", "provider_error")):
            with self.assertRaises(TransitionError):
                self.svc.store.transition(tid, S.REVIEW_REQUIRED, actor, kind, {"x": 1})
        for dst in (S.PAYMENT_CAPTURED, S.EXECUTION_AUTHORIZED, S.PAYMENT_PENDING):
            with self.assertRaises(TransitionError):
                self.svc.store.transition(tid, dst, "payment_verifier", "payment_verification", {"x": 1})
        self.assertEqual(self.state(tid), S.CANCELLED)


# ================================================================ order-status: genuinely read-only
class TestOrderStatusReadOnly(R1cBase):
    def test_W_phases_across_the_lifecycle(self):
        tid, oid = self.new_order()
        offer_id = self.svc.store.offer(task_id=tid)["offer_id"]
        for ref in (tid, offer_id, oid):
            self.assertEqual(order_status(self.svc.cfg, ref, self.pp)["phase"], "AWAITING_PAYER_ACTION")
        self.svc.on_buyer_cancel(oid)
        r = order_status(self.svc.cfg, oid, self.pp)
        self.assertEqual((r["phase"], r["approval"]["buyer_approved"], r["capture"]["present"]),
                         ("BUYER_NAVIGATED_AWAY", False, False))
        self.pp.buyer_action(oid, "approve")
        self.assertEqual(order_status(self.svc.cfg, oid, self.pp)["phase"], "BUYER_APPROVED")
        make(self.d).store.reserve_capture(oid, lease_s=30)
        self.assertEqual(order_status(self.svc.cfg, oid, self.pp)["phase"], "CAPTURE_IN_PROGRESS")

    def test_X_classify_covers_every_phase(self):
        now = 1000.0
        cap = {"status": "COMPLETED", "purchase_units": [{"payments": {"captures": [{"status": "COMPLETED"}]}}]}
        cases = [
            ((S.PAYMENT_PENDING, None, None, {"status": "PAYER_ACTION_REQUIRED"}, False), "AWAITING_PAYER_ACTION"),
            ((S.PAYMENT_PENDING, None, None, {"status": "PAYER_ACTION_REQUIRED"}, True), "BUYER_NAVIGATED_AWAY"),
            ((S.PAYMENT_PENDING, None, None, {"status": "APPROVED"}, True), "BUYER_APPROVED"),
            ((S.PAYMENT_PENDING, {"state": "RESERVED", "lease_until": now + 5}, None, {"status": "APPROVED"}, False),
             "CAPTURE_IN_PROGRESS"),
            ((S.PAYMENT_PENDING, {"state": "SENT"}, None, None, False), "CAPTURE_IN_PROGRESS"),
            ((S.REVIEW_REQUIRED, {"state": "OUTCOME_UNKNOWN"}, None, {"status": "APPROVED"}, False),
             "CAPTURE_OUTCOME_UNKNOWN"),
            ((S.DELIVERED, {"state": "RESOLVED"}, "CAPTURE_VERIFIED", cap, False), "CAPTURE_VERIFIED_COMPLETED"),
            ((S.REVIEW_REQUIRED, {"state": "RESOLVED"}, "MISMATCH", cap, False), "CAPTURE_NOT_VERIFIED"),
            ((S.FAILED, None, "ORDER_VOIDED", {"status": "VOIDED"}, False), "ORDER_VOIDED"),
            ((S.CANCELLED, None, None, {"status": "PAYER_ACTION_REQUIRED"}, True), "TERMINATED_UNPAID"),
            ((S.CANCELLED, None, None, None, False), "PROVIDER_UNREADABLE"),
            ((S.PAYMENT_PENDING, {"state": "RESERVED", "lease_until": now - 5}, None, {"status": "SAVED"}, False),
             "UNKNOWN_PROVIDER_STATUS"),
        ]
        for args, want in cases:
            self.assertEqual(classify(*args, now=now), want, args)

    def test_Y_provider_surface_is_get_only(self):
        ro = ReadOnlyOrders(self.pp)
        for name in ("capture_order", "create_order", "verify_webhook", "buyer_action", "mutate_order"):
            with self.assertRaises(AttributeError):
                getattr(ro, name)
        with self.assertRaises(AttributeError):
            ro.capture_order = lambda *a: None

    def test_Z_sandbox_adapter_token_and_get_only_output_sanitized(self):
        tid, oid = self.new_order()
        secret, tok = "S3CRET-value-xyz", "ACCESS-T0KEN-abc"
        cfg = Config.from_env(payment_provider="paypal_sandbox", paypal_client_id="CID-123456", paypal_client_secret=secret,
                              paypal_merchant_id="MERCHANT01", data_dir=self.d)
        body = {"id": oid, "status": "COMPLETED", "intent": "CAPTURE", "create_time": "2026-10-15T19:44:43Z",
                "payer": {"payer_id": "PAYERID12345", "email_address": "buyer@example.com"},
                "links": [{"rel": "self", "href": f"https://api-m.sandbox.paypal.com/v2/checkout/orders/{oid}"},
                          {"rel": "payer-action", "href": f"https://www.sandbox.paypal.com/checkoutnow?token={oid}"}],
                "purchase_units": [{"reference_id": tid, "custom_id": self.svc.store.offer(task_id=tid)["offer_id"],
                                    "amount": {"currency_code": "USD", "value": "1.00"},
                                    "payee": {"merchant_id": "MERCHANT01"},
                                    "payments": {"captures": [{"id": "CAPTUREID9876543", "status": "COMPLETED",
                                                               "amount": {"currency_code": "USD", "value": "1.00"}}]}}]}
        t = Transport([(200, {"access_token": tok, "expires_in": 3600}, {}), (200, body, {"Paypal-Debug-Id": "dbg1"})])
        r = order_status(cfg, tid, PayPalSandbox(cfg, opener=t))
        self.assertEqual([(q.get_method(), q.full_url.split("paypal.com", 1)[1]) for q in t.requests],
                         [("POST", "/v1/oauth2/token"), ("GET", f"/v2/checkout/orders/{oid}")])
        dump = json.dumps(r, ensure_ascii=False)
        for leaked in (secret, tok, "https://", oid, "CAPTUREID9876543", "PAYERID12345", "buyer@example.com", "CID-123456"):
            self.assertNotIn(leaked, dump)
        self.assertEqual((r["approval"]["payer_recorded"], r["capture"]["present"], r["merchant"]["match"]),
                         (True, True, True))
        self.assertEqual(r["capture"]["verifier_verdict"], "CAPTURE_VERIFIED")
        self.assertEqual(r["phase"], "CAPTURE_VERIFIED_COMPLETED")
        body["purchase_units"][0]["payee"] = {"merchant_id": "SOMEONE-ELSE"}
        t2 = Transport([(200, {"access_token": tok, "expires_in": 3600}, {}), (200, body, {})])
        r2 = order_status(cfg, oid, PayPalSandbox(cfg, opener=t2))
        self.assertIn("PAYEE_MISMATCH", r2["alerts"])
        self.assertEqual((r2["merchant"]["match"], r2["capture"]["verifier_verdict"]), (False, "MISMATCH"))
        self.assertEqual(self.state(tid), S.PAYMENT_PENDING)

    def test_ZB_live_server_database_is_read_consistently_without_new_files(self):
        tid, oid = self.new_order()                                               # our Store connection stays open
        db = os.path.join(self.d, "autonomos.sqlite")
        self.assertTrue(os.path.exists(db + "-wal"))
        listing = sorted(os.listdir(self.d))
        self.svc.on_buyer_cancel(oid)                                             # committed, still in the WAL
        r = order_status(self.svc.cfg, tid, self.pp)
        self.assertEqual(len(r["local"]["checkout_navigation"]), 1)               # sees the live WAL content
        self.assertEqual(sorted(os.listdir(self.d)), listing)

    def test_ZC_diagnosis_connection_refuses_writes_with_and_without_wal(self):
        import sqlite3
        from autonomos.order_status import open_ro
        tid, oid = self.new_order()
        db = os.path.join(self.d, "autonomos.sqlite")
        for phase in ("live WAL", "checkpointed"):
            if phase == "checkpointed":
                self.svc.store._db.close()
                self.assertFalse(os.path.exists(db + "-wal"))
            con = open_ro(db)
            try:
                self.assertEqual(con.execute("SELECT state FROM tasks WHERE task_id=?", (tid,)).fetchone()[0],
                                 S.PAYMENT_PENDING)
                for sql in ("UPDATE tasks SET state='CANCELLED'", "CREATE TABLE x (a)", "DELETE FROM events"):
                    with self.assertRaises(sqlite3.OperationalError, msg=(phase, sql)):
                        con.execute(sql)
            finally:
                con.close()

    def test_ZA_errors_change_nothing_and_create_nothing(self):
        tid, oid = self.new_order()
        with self.assertRaises(LookupError):
            order_status(self.svc.cfg, "TASK-NOPE", self.pp)
        missing = os.path.join(tempfile.mkdtemp(), "absent", "autonomos.sqlite")
        with self.assertRaises(FileNotFoundError):
            order_status(self.svc.cfg, tid, self.pp, db_path=missing)
        self.assertFalse(os.path.exists(os.path.dirname(missing)))
        self.pp.token_valid = False
        r = order_status(self.svc.cfg, tid, self.pp)
        self.assertEqual((r["provider"]["readable"], r["phase"]), (False, "PROVIDER_UNREADABLE"))
        self.pp.token_valid = True
        self.assertEqual(self.state(tid), S.PAYMENT_PENDING)


class TestCli(unittest.TestCase):
    def run_cli(self, *args, **env):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("PAYPAL_", "AUTONOMOS_"))}
        e.update(AUTONOMOS_PAYMENT_PROVIDER="mock", AUTONOMOS_AI_PROVIDER="fixture", **env)
        return subprocess.run([sys.executable, "-B", "-m", "autonomos", *args], cwd=APP, env=e,
                              capture_output=True, text=True, timeout=60)

    def test_capture_status_is_retired_and_reconcile_needs_confirmation(self):
        d = os.path.join(tempfile.mkdtemp(), "data")
        r = self.run_cli("capture-status", "X", AUTONOMOS_DATA_DIR=d)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("order-status", r.stderr)
        r = self.run_cli("reconcile", "X", AUTONOMOS_DATA_DIR=d)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--owner-confirm", r.stderr)
        r = self.run_cli("order-status", "X", AUTONOMOS_DATA_DIR=d)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no AutoNOMOS database", r.stderr)
        self.assertFalse(os.path.exists(d))                                       # diagnosis created nothing


class TestHTTPCancel(unittest.TestCase):
    """The real local server: the incident's browser path (checkout -> cancel_url) and the explicit cancel POST."""
    from test_security import TestHTTPEndToEnd as _H
    setUp, tearDown, client, get, token, post, code = (_H.setUp, _H.tearDown, _H.client, _H.get, _H.token, _H.post,
                                                       _H.code)

    def checkout(self):
        r = self.post("/tasks", "capability=research_snapshot&corpus=agentnomos_quantum&topic=" + Q.replace(" ", "+"))
        tid = r.geturl().rsplit("/", 1)[1]
        dg = self.svc.store.offer(task_id=tid)["_digest"]
        r = self.post(f"/tasks/{tid}/accept", f"offer_digest={dg}", csrf_from=f"/tasks/{tid}")
        return tid, r.geturl().rsplit("/", 1)[1]

    def test_checkout_exit_keeps_task_open_and_only_an_explicit_post_cancels(self):
        tid, oid = self.checkout()
        r = self.post(f"/mock-paypal/{oid}/cancel", "", csrf_from=f"/mock-paypal/checkout/{oid}")   # -> cancel_url
        page = r.read().decode()
        self.assertTrue(r.geturl().endswith(f"/tasks/{tid}"))
        self.assertEqual(self.svc.store.task(tid)["state"], S.PAYMENT_PENDING)
        for text in ("Payment not confirmed", "You came back from PayPal", "Continue at PayPal", "Cancel this order"):
            self.assertIn(text, page)
        self.code(lambda: self.get(f"/paypal/cancel?token={oid}", ua=self.client()))  # anyone with the token
        self.assertEqual(self.svc.store.task(tid)["state"], S.PAYMENT_PENDING)
        stranger = self.client()
        self.get("/", ua=stranger)
        self.assertEqual(self.code(lambda: self.post(f"/tasks/{tid}/cancel", "", ua=stranger)), 403)
        self.assertEqual(self.code(lambda: self.post(f"/tasks/{tid}/cancel", "", origin="https://evil.example",
                                                     csrf_from=f"/tasks/{tid}")), 403)
        self.assertEqual(self.svc.store.task(tid)["state"], S.PAYMENT_PENDING)
        self.post(f"/tasks/{tid}/cancel", "", csrf_from=f"/tasks/{tid}")
        t = self.svc.store.task(tid)
        self.assertEqual((t["state"], t["reason"]), (S.CANCELLED, "buyer cancelled explicitly on the AutoNOMOS task page"))
        self.svc.payments.buyer_action(oid, "approve")
        self.code(lambda: self.get(f"/paypal/return?token={oid}"))
        self.assertEqual(self.svc.store.task(tid)["state"], S.CANCELLED)
        self.assertEqual([c for c in self.svc.payments.calls if c[0] == "capture_order"], [])

    def test_explicit_cancel_post_refused_while_capture_outcome_unknown(self):
        self.svc.backoff_s = 0.01
        tid, oid = self.checkout()
        self.svc.payments.set_scenario(oid, "timeout_never")
        self.svc.payments.buyer_action(oid, "approve")
        self.code(lambda: self.get(f"/paypal/return?token={oid}"))
        self.assertEqual(self.svc.store.task(tid)["state"], S.REVIEW_REQUIRED)
        page = self.get(f"/tasks/{tid}").read().decode()
        self.assertIn("Payment outcome being reconciled", page)
        self.assertNotIn("Cancel this order", page)                    # no cancel offered while uncertain
        self.assertEqual(self.code(lambda: self.post(f"/tasks/{tid}/cancel", "", csrf_from="/")), 409)
        self.assertEqual(self.svc.store.task(tid)["state"], S.REVIEW_REQUIRED)


# ================================================================ forensic replay of the real incident (optional)
INCIDENT_DB = os.environ.get("AUTONOMOS_INCIDENT_DB") or os.path.abspath(
    os.path.join(APP, "..", "..", "incident_sandbox_run1", "original", "autonomos.sqlite"))


@unittest.skipUnless(os.access(INCIDENT_DB, os.R_OK), "frozen incident database not present (internal evidence)")
class TestIncidentReplay(unittest.TestCase):
    def test_incident_order_status_on_a_copy_of_the_frozen_database(self):
        import sqlite3
        d = tempfile.mkdtemp(prefix="autonomos_incident_")
        db = os.path.join(d, "autonomos.sqlite")
        shutil.copyfile(INCIDENT_DB, db)
        before = sha(db)
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        oid, created = con.execute("SELECT order_id, created_json FROM orders WHERE task_id='TASK-29A4A3D29F09'").fetchone()
        con.close()
        pu = json.loads(created)["purchase_units"][0]

        class Recorded:                                                           # the sanitized provider answer
            environment = "SANDBOX"

            def get_order(self, order_id):
                return {"id": oid, "status": "PAYER_ACTION_REQUIRED", "intent": "CAPTURE",
                        "create_time": "2026-10-15T19:44:43Z", "payment_source": {"paypal": {}},
                        "links": [{"rel": "self", "method": "GET"}, {"rel": "payer-action", "method": "GET"}],
                        "purchase_units": [{k: pu[k] for k in ("reference_id", "custom_id", "invoice_id", "amount",
                                                                 "payee")}]}
        cfg = Config.from_env(payment_provider="paypal_sandbox", paypal_client_id="CID", paypal_client_secret="S",
                              paypal_merchant_id="LBFAZLU47G3FA", data_dir=d)
        r = order_status(cfg, "TASK-29A4A3D29F09", Recorded())
        self.assertEqual(sha(db), before)
        self.assertEqual(r["phase"], "TERMINATED_UNPAID")
        self.assertEqual(r["alerts"], ["LEGACY_REDIRECT_CANCELLATION"])
        self.assertEqual((r["approval"]["buyer_approved"], r["approval"]["payer_recorded"], r["capture"]["present"],
                          r["merchant"]["match"]), (False, False, False, True))
        self.assertIsNone(r["local"]["capture_attempt"])
        self.assertNotIn(oid, json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
