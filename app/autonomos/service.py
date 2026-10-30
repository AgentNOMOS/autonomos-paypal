"""AutoNOMOS service: the governor that moves a task from request to receipt.

Gates (each enforced in code AND by the store's transition table / UNIQUE constraints):
  G1 capability   only offered capabilities over approved document sets get an offer; refusals cost nothing.
  G2 offer        price, currency and payee come from server config; the buyer must confirm the exact offer digest.
  G3 payment      execution requires CAPTURE_VERIFIED from payment_verifier on a server-side provider query.
  G4 once         one order per offer, one capture record per order, one execution per order (UNIQUE constraints).
  G5 delivery     the artifact must pass deterministic quote verification before DELIVERY_VERIFIED.
  G6 receipt      only this module builds receipts, from stored records; the model never writes one.
The model is called only inside execute(), after G3, and its output reaches nothing but research.verify_artifact.
"""
import json
import os
import sqlite3
import threading
import time

from . import policy, research, receipt as R, states as S
from .ai import AIError, make_provider
from .canon import digest
from .corpus import load_sets
from .payment_verifier import verify_order
from .payments import ProviderError, make_payment_provider
from .payments.mock import BUYER_ACTION_ISSUES, MOCK_MERCHANT_ID
from .store import ConcurrencyError, Store, TransitionError

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS_ROOT = os.path.join(os.path.dirname(HERE), "corpus")


class GateError(RuntimeError):
    pass


class AutoNomos:
    def __init__(self, cfg, payments=None, ai=None, store_path=None):
        self.cfg = cfg
        os.makedirs(cfg.data_dir, exist_ok=True)
        self.store = Store(store_path or os.path.join(cfg.data_dir, "autonomos.sqlite"))
        self.payments = payments or make_payment_provider(cfg)
        self.ai = ai or make_provider(cfg)
        self.docsets = load_sets(CORPUS_ROOT)
        self.signer = R.DemoSigner(os.path.join(cfg.data_dir, "demo_receipt_key.json"))
        self.expected_payee = cfg.paypal_merchant_id if self.payments.environment == "SANDBOX" else MOCK_MERCHANT_ID
        self.capture_lease_s, self.backoff_s, self.max_backoff_s = 60.0, 0.5, 8.0
        self.read_attempts, self.settle_reads = 3, 3       # bounded READ retries / post-ambiguity read checks
        self.uncertain_settle_s = 300.0                    # owner re-send not before this long after the first send
        # PayPal documents that PayPal-Request-Id is stored "for a period of time" (45 days in the refunds example)
        # but gives no retention for capture. This is an ASSUMPTION, deliberately short; never raise it on a guess.
        self.idempotency_window_s = 3600.0
        self.locks = {}

    def _lock(self, tid):
        return self.locks.setdefault(tid, threading.Lock())

    # ------------------------------------------------------------------ 1-4: request, capability gate, offer
    def request_task(self, request):
        tid = self.store.new_task({k: request.get(k) for k in ("capability", "topic", "corpus", "buyer_ref")})
        try:
            spec = policy.check_request(request, self.docsets)
        except policy.PolicyRefusal as e:
            self.store.transition(tid, S.FAILED, "policy", "policy_refusal", {"reason": str(e)}, reason=str(e))
            return tid
        offer, d = policy.make_offer(tid, spec, self.docsets[spec["corpus"]].manifest_digest,
                                     self._offer_cfg())
        self.store.put_offer(offer)
        self.store.set_capability(tid, spec["capability"])
        self.store.transition(tid, S.OFFER_ISSUED, "policy", "offer", offer, {"offer_digest": d})
        self.store.transition(tid, S.BUYER_APPROVAL_REQUIRED, "policy", "offer", offer, {"offer_digest": d})
        return tid

    def _offer_cfg(self):
        class C:  # payee shown in the offer is the payee the verifier will require
            pass
        c = C()
        c.price_value, c.price_currency = self.cfg.price_value, self.cfg.price_currency
        c.paypal_merchant_id, c.payment_provider = self.expected_payee, self.payments.name
        return c

    # ------------------------------------------------------------------ 5: buyer approves the offer -> provider order
    def accept_offer(self, tid, offer_digest_seen):
        with self._lock(tid):
            t = self.store.task(tid)
            offer = self.store.offer(task_id=tid)
            if t is None or offer is None:
                raise GateError("unknown task")
            if t["state"] != S.BUYER_APPROVAL_REQUIRED:
                existing = self.store.order_for_task(tid)
                if existing and t["state"] == S.PAYMENT_PENDING:      # double click: same order, same approval link
                    return self._approve_link(existing["created"])
                raise GateError(f"offer cannot be accepted in state {t['state']}")
            if offer_digest_seen != offer["_digest"]:
                raise GateError("the offer you approved is not the stored offer (digest mismatch); nothing was charged")
            if not policy.offer_is_live(offer):
                self.store.transition(tid, S.CANCELLED, "policy", "offer_expired", {"offer_id": offer["offer_id"]},
                                      reason="offer expired")
                raise GateError("offer expired")
            if not self.store.accept_offer(offer["offer_id"]):
                # another thread/process holds the acceptance claim: never create a second order
                existing = self.store.order_for_task(tid)
                if existing:
                    return self._approve_link(existing["created"])
                raise GateError("this offer is already being accepted; reload in a moment")
            return self._create_order(tid, offer)

    def _create_order(self, tid, offer):
        """Called only by the holder of the acceptance claim (or by recover() for an orphaned claim)."""
        body = self._order_body(offer)
        request_id = "autonomos-order-" + offer["offer_id"]       # stable: a retry returns the same provider order
        if self.payments.environment == "SANDBOX":                 # owner-approved hard cap (R1b GO gate)
            n = self.store.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            if n >= self.cfg.sandbox_max_orders:
                raise GateError(f"sandbox order cap reached ({n}/{self.cfg.sandbox_max_orders}); owner approval needed")
        created = self.payments.create_order(body, request_id)
        self.store.put_order(created["id"], tid, offer["offer_id"], self.payments.name, request_id, created)
        self.store.transition(tid, S.ORDER_CREATED, "payment_gateway", "provider_order",
                              {"order_id": created["id"], "status": created.get("status")},
                              {"order_id": created["id"], "request_id": request_id})
        self.store.transition(tid, S.PAYMENT_PENDING, "payment_gateway", "approval_link",
                              {"href": self._approve_link(created)}, {})
        return self._approve_link(created)

    def _order_body(self, offer):
        pu = {"reference_id": offer["task_id"], "custom_id": offer["offer_id"], "invoice_id": offer["offer_id"],
              "description": f"AutoNOMOS {offer['title']} (sandbox)"[:127],
              "amount": {"currency_code": offer["price"]["currency_code"], "value": offer["price"]["value"]}}
        if self.expected_payee and self.payments.environment == "SANDBOX":
            pu["payee"] = {"merchant_id": self.expected_payee}
        base = self.cfg.public_base_url.rstrip("/")
        return {"intent": "CAPTURE", "purchase_units": [pu],
                "payment_source": {"paypal": {"experience_context": {
                    "brand_name": "AutoNOMOS (sandbox)", "shipping_preference": "NO_SHIPPING", "user_action": "PAY_NOW",
                    "return_url": f"{base}/paypal/return", "cancel_url": f"{base}/paypal/cancel"}}}}

    @staticmethod
    def _approve_link(order):
        for rel in ("payer-action", "approve"):
            for l in order.get("links", []):
                if l.get("rel") == rel:
                    return l["href"]
        raise GateError("provider returned no approval link")

    # ------------------------------------------------------------------ 6-8: buyer returns -> capture -> verify
    def on_buyer_return(self, order_id):
        """Called when the buyer comes back from checkout (or by reconciliation). The only input trusted is OUR order
        id lookup; everything else comes from the provider's own record, fetched server-side."""
        tid = self.store.task_for_order(order_id)
        if tid is None:
            raise GateError("unknown order")
        with self._lock(tid):                                  # in-process optimisation only; the DB lease is authoritative
            t = self.store.task(tid)
            if t["state"] not in (S.PAYMENT_PENDING, S.REVIEW_REQUIRED):
                return t["state"]                              # repeated return / refresh: no second capture
            return self._capture_with_reservation(tid)

    # ---- R1b capture policy (see R1_04_RECONCILIATION_FINAL.md) -------------------------------------------------
    # * A capture POST is sent automatically AT MOST ONCE per logical attempt (generation), and only when nothing was
    #   ever sent for it (sends == 0, written ahead of the call). Restarts, recovery, webhooks and buyer refreshes
    #   never send a capture for an attempt that may already be in flight.
    # * Any capture outcome that is not a provider response we can parse (no response, 401, 408/409, 429, 5xx,
    #   unparseable 2xx) is OUTCOME_UNKNOWN. A GET that shows no capture does NOT prove the in-flight capture will not
    #   complete later (delayed processing, stale reads), so the attempt stays OUTCOME_UNKNOWN and the task goes to
    #   REVIEW_REQUIRED. Only an authoritative GET showing a capture (-> verifier) or a VOIDED order resolves it.
    # * A re-send with the SAME PayPal-Request-Id is OWNER-MEDIATED only (owner_resend_capture), after a settle delay
    #   and inside the assumed idempotency window; outside the window it is refused (manual dashboard reconciliation).
    def _get_order(self, oid):
        """Authenticated provider READ with bounded backoff. Reads are safe to repeat; payment mutations are not."""
        for i in range(self.read_attempts):
            try:
                return self.payments.get_order(oid)
            except ProviderError as e:
                if not e.transient or i == self.read_attempts - 1:
                    raise
                delay = e.retry_after if e.retry_after is not None else self.backoff_s * (2 ** i)
                time.sleep(min(delay, self.max_backoff_s))

    def _capture_with_reservation(self, tid, may_send=True, allow_new_generation=False):
        offer, order = self.store.offer(task_id=tid), self.store.order_for_task(tid)
        oid = order["order_id"]
        verdict, att = self.store.reserve_capture(oid, lease_s=self.capture_lease_s)
        if verdict in ("BUSY", "CLOSED"):                      # another process owns the attempt / task is closed
            return self.store.task(tid)["state"]
        try:
            truth = self._get_order(oid)                       # reconcile BEFORE any capture request, always
        except ProviderError as e:
            return self._provider_unavailable(tid, e)
        if self._has_capture(truth):                           # authoritative: the provider shows a capture
            if verdict in ("OWNER", "OWNER_RECOVER"):
                self.store.capture_mark(oid, ("RESERVED", "SENT", "OUTCOME_UNKNOWN"), "RESOLVED")
            return self._verify_and_gate(tid, offer, order, truth=truth)
        if verdict == "OWNER_RECOVER":                         # something MAY be in flight: never send again here
            if truth.get("status") == "VOIDED":                # terminal at the provider: no capture can complete
                self.store.capture_mark(oid, ("SENT", "OUTCOME_UNKNOWN"), "RELEASED", error="PROVIDER_ORDER_VOIDED")
                return self._verify_and_gate(tid, offer, order, truth=truth)
            return self._park_uncertain(tid, oid, truth, "capture outcome still unknown after recovery check")
        if verdict == "RESOLVED" and allow_new_generation and att["state"] == "RELEASED" \
                and truth.get("status") == "APPROVED":
            verdict, att = self.store.reserve_capture(oid, lease_s=self.capture_lease_s, new_generation=True)
        if verdict == "EXHAUSTED":
            return self._to_review(tid, "capture refused repeatedly; human review", att)
        if verdict != "OWNER":
            return self._verify_and_gate(tid, offer, order, truth=truth)
        if truth.get("status") != "APPROVED" or not may_send:  # nothing to capture yet / recovery never sends
            self.store.capture_mark(oid, ("RESERVED",), "RELEASED")
            return self._verify_and_gate(tid, offer, order, truth=truth)
        if self._capture_cap_reached():
            self.store.capture_mark(oid, ("RESERVED",), "RELEASED")
            return self._to_review(tid, "sandbox capture-request cap reached; owner approval needed",
                                   {"order_id": oid, "cap": self.cfg.sandbox_max_capture_requests})
        return self._send_capture_once(tid, offer, order, att, ("RESERVED",))

    def _capture_cap_reached(self):
        """Owner-approved hard cap on capture POSTs per data dir (SANDBOX only)."""
        if self.payments.environment != "SANDBOX":
            return False
        n = self.store.db.execute("SELECT COALESCE(SUM(sends), 0) FROM capture_attempts").fetchone()[0]
        return n >= self.cfg.sandbox_max_capture_requests

    def _send_capture_once(self, tid, offer, order, att, frm):
        oid, rid = order["order_id"], att["request_id"]
        try:
            self.store.capture_mark(oid, frm, "SENT", sent=True)   # write-ahead: from here on, "may be in flight"
        except ConcurrencyError:                               # R1c: the task was closed (e.g. cancelled) first
            try:
                self.store.capture_mark(oid, ("RESERVED",), "RELEASED")
            except ConcurrencyError:
                pass
            self.store.note(tid, "payment_gateway", "capture_not_sent", {"order_id": oid, "request_id": rid},
                            {"note": "task closed before the write-ahead mark; no capture request was sent"})
            return self.store.task(tid)["state"]
        try:
            resp = self.payments.capture_order(oid, rid)       # no DB transaction is open across this call
        except ProviderError as e:
            ev = {"issue": e.issue, "status": e.status, "debug_id": e.debug_id, "request_id": rid}
            if e.issue == "ORDER_ALREADY_CAPTURED":
                self.store.capture_mark(oid, ("SENT",), "RESOLVED", error=e.issue, debug_id=e.debug_id)
                return self._verify_and_gate(tid, offer, order)
            if e.issue in BUYER_ACTION_ISSUES:                 # buyer must choose/confirm a funding source AT PAYPAL
                self.store.capture_mark(oid, ("SENT",), "RELEASED", error=e.issue, debug_id=e.debug_id)
                self.store.note(tid, "payment_gateway", "provider_refusal", ev,
                                {"note": "funding source declined / payer action required. The buyer must approve "
                                         "again at PayPal and then confirm here; we never pick another funding source",
                                 "approval_link": self._approve_link(order["created"])})
                return self.store.task(tid)["state"]           # stays PAYMENT_PENDING: recoverable, not FAILED
            if e.definitive_refusal:                           # provider refused: not executed
                self.store.capture_mark(oid, ("SENT",), "RELEASED", error=e.issue, debug_id=e.debug_id)
                self._fail_payment(tid, self.store.task(tid)["state"], f"capture refused by provider: {e.issue}", ev)
                return self.store.task(tid)["state"]
            # no usable answer: OUTCOME UNKNOWN. Never re-send on this basis; reconcile by reading only.
            self.store.capture_mark(oid, ("SENT",), "OUTCOME_UNKNOWN", error=e.issue, debug_id=e.debug_id)
            self.store.note(tid, "payment_gateway", "capture_outcome_unknown", ev,
                            {"note": "capture response lost or ambiguous; reconciling by GET only, no re-send"})
            return self._settle_after_uncertain(tid, offer, order)
        try:
            truth = self._get_order(oid)                       # the response alone is never trusted
        except ProviderError as e:
            self.store.capture_mark(oid, ("SENT",), "OUTCOME_UNKNOWN", error="CONFIRMING_GET_FAILED")
            return self._park_uncertain(tid, oid, None, f"capture answered but confirming GET failed: {e.issue}")
        if not self._has_capture(truth):                       # 2xx but not (yet) visible: still uncertain
            self.store.capture_mark(oid, ("SENT",), "OUTCOME_UNKNOWN", error="CAPTURE_NOT_VISIBLE_AFTER_2XX",
                                    debug_id=getattr(self.payments, "last_debug_id", None))
            return self._settle_after_uncertain(tid, offer, order)
        self.store.capture_mark(oid, ("SENT",), "RESOLVED", debug_id=getattr(self.payments, "last_debug_id", None))
        return self._verify_and_gate(tid, offer, order, cap_resp=resp, truth=truth)

    def _settle_after_uncertain(self, tid, offer, order):
        """Bounded read-only reconciliation after an ambiguous capture: a few authenticated GETs with backoff."""
        oid, truth = order["order_id"], None
        for i in range(self.settle_reads):
            if i:
                time.sleep(min(self.backoff_s * (2 ** i), self.max_backoff_s))
            try:
                truth = self._get_order(oid)
            except ProviderError as e:
                return self._park_uncertain(tid, oid, None, f"provider unreachable while reconciling: {e.issue}")
            if self._has_capture(truth):
                self.store.capture_mark(oid, ("OUTCOME_UNKNOWN",), "RESOLVED")
                return self._verify_and_gate(tid, offer, order, truth=truth)
            if truth.get("status") == "VOIDED":
                self.store.capture_mark(oid, ("OUTCOME_UNKNOWN",), "RELEASED", error="PROVIDER_ORDER_VOIDED")
                return self._verify_and_gate(tid, offer, order, truth=truth)
        return self._park_uncertain(tid, oid, truth, "no capture visible yet; absence is NOT proof of failure")

    def _park_uncertain(self, tid, oid, truth, why):
        """Keep the attempt OUTCOME_UNKNOWN (sticky), free its lease so any reconciler can read-check it later, and
        hand the task to a human. A later authoritative GET showing the capture still unlocks the task."""
        self.store.capture_mark(oid, ("SENT", "OUTCOME_UNKNOWN"), "OUTCOME_UNKNOWN", release_lease=True)
        a = self.store.capture_attempt(oid)
        ev = {"order_id": oid, "request_id": a["request_id"], "order_status": (truth or {}).get("status"),
              "first_sent": a["first_sent"], "last_error": a["last_error"], "debug_id": a["debug_id"]}
        st = self.store.task(tid)["state"]
        if st != S.REVIEW_REQUIRED and S.allowed(st, S.REVIEW_REQUIRED):
            self.store.transition(tid, S.REVIEW_REQUIRED, "payment_gateway", "capture_outcome_unknown", ev,
                                  reason="capture outcome unknown: awaiting provider record or owner reconciliation")
        else:
            self.store.note(tid, "payment_gateway", "capture_outcome_unknown", ev, {"note": why})
        return self.store.task(tid)["state"]

    def on_buyer_reapproved(self, order_id):
        """Explicit buyer statement (POST from our page) that they approved again at PayPal after a decline. It
        allows exactly one new capture generation; the provider record must show APPROVED again."""
        tid = self.store.task_for_order(order_id)
        if tid is None:
            raise GateError("unknown order")
        if not self.store.request_reapproval(order_id):
            raise GateError("no declined payment is waiting for re-approval on this order")
        with self._lock(tid):
            return self._capture_with_reservation(tid, allow_new_generation=True)

    def capture_status(self, order_id):
        """Owner reconciliation for an open task: authoritative GET + verification. Never sends a capture, but it
        WRITES (reservation, capture record, state). R1c: the read-only diagnosis is order_status.order_status()."""
        tid = self.store.task_for_order(order_id)
        if tid is None:
            raise GateError("unknown order")
        st = self.store.task(tid)["state"]
        if st in (S.PAYMENT_PENDING, S.REVIEW_REQUIRED):
            with self._lock(tid):
                st = self._capture_with_reservation(tid, may_send=False)
        return {"task_id": tid, "state": st, "attempt": self.store.capture_attempt(order_id)}

    def owner_resend_capture(self, order_id, now=None):
        """Owner-mediated re-send of an UNKNOWN capture with the SAME PayPal-Request-Id. Refused unless: the attempt
        is OUTCOME_UNKNOWN; the settle delay has passed (delayed processing had time to surface); we are still inside
        the assumed idempotency window; and a fresh GET shows APPROVED with no capture."""
        tid = self.store.task_for_order(order_id)
        if tid is None:
            raise GateError("unknown order")
        a = self.store.capture_attempt(order_id)
        if not a or a["state"] != "OUTCOME_UNKNOWN":
            raise GateError(f"no unknown capture outcome to resolve (attempt state {a and a['state']})")
        age = (now or time.time()) - (a["first_sent"] or 0)
        if age < self.uncertain_settle_s:
            raise GateError(f"too early: first send {age:.0f}s ago < settle delay {self.uncertain_settle_s:.0f}s; "
                            "a delayed capture may still appear. Run capture-status later.")
        if age > self.idempotency_window_s:
            raise GateError("refused: the PayPal-Request-Id may no longer be stored (assumed window "
                            f"{self.idempotency_window_s:.0f}s exceeded). Reconcile manually in the PayPal sandbox "
                            "dashboard; no automatic or owner re-send is permitted.")
        if self._capture_cap_reached():
            raise GateError(f"sandbox capture-request cap ({self.cfg.sandbox_max_capture_requests}) reached; "
                            "a new owner approval and a raised cap are required")
        with self._lock(tid):
            verdict, att = self.store.reserve_capture(order_id, lease_s=self.capture_lease_s)
            if verdict != "OWNER_RECOVER":
                raise GateError(f"capture attempt is held by another worker ({verdict}); try again")
            offer, order = self.store.offer(task_id=tid), self.store.order_for_task(tid)
            try:
                truth = self._get_order(order_id)
            except ProviderError as e:
                self._park_uncertain(tid, order_id, None, f"owner resend aborted: GET failed {e.issue}")
                raise GateError(f"provider unreachable ({e.issue}); nothing was sent") from None
            if self._has_capture(truth):
                self.store.capture_mark(order_id, ("SENT", "OUTCOME_UNKNOWN"), "RESOLVED")
                return self._verify_and_gate(tid, offer, order, truth=truth)
            if truth.get("status") != "APPROVED":
                self._park_uncertain(tid, order_id, truth, "owner resend refused: order not APPROVED")
                raise GateError(f"order status {truth.get('status')}: nothing to capture; nothing was sent")
            self.store.note(tid, "operator", "owner_mediated_capture_resend",
                            {"order_id": order_id, "request_id": att["request_id"], "age_s": round(age)},
                            {"note": "owner-approved re-send with the SAME PayPal-Request-Id"})
            return self._send_capture_once(tid, offer, order, att, ("SENT", "OUTCOME_UNKNOWN"))

    @staticmethod
    def _has_capture(order):
        return any(((pu.get("payments") or {}).get("captures") or []) for pu in order.get("purchase_units") or [])

    def _provider_unavailable(self, tid, e):
        if e.status == 401:
            return self._to_review(tid, "payment provider auth failed", {"issue": e.issue, "debug_id": e.debug_id})
        self.store.note(tid, "payment_gateway", "provider_unavailable", {"status": e.status, "issue": e.issue,
                                                                         "debug_id": e.debug_id},
                        {"note": "provider not reachable; no payment state change"})
        return self.store.task(tid)["state"]

    def _to_review(self, tid, why, ev):
        st = self.store.task(tid)["state"]
        if st != S.REVIEW_REQUIRED and S.allowed(st, S.REVIEW_REQUIRED):
            self.store.transition(tid, S.REVIEW_REQUIRED, "payment_gateway", "provider_error", ev, reason=why)
        return self.store.task(tid)["state"]

    def _verify_and_gate(self, tid, offer, order, cap_resp=None, truth=None):
        """Verify and gate; if another thread/process advanced the task meanwhile, our transition changes zero rows /
        is refused by the table - we then do NOT proceed and report the state the winner left."""
        try:
            return self._verify_and_gate_once(tid, offer, order, cap_resp, truth)
        except ProviderError as e:
            return self._provider_unavailable(tid, e)
        except TransitionError:
            st = self.store.task(tid)["state"]
            if st in (S.PAYMENT_PENDING, S.REVIEW_REQUIRED):
                raise                                          # not a lost race: a genuine gate refusal
            self.store.note(tid, "payment_verifier", "lost_race", {"order_id": order["order_id"]},
                            {"note": f"another worker already advanced the task to {st}; no action taken"})
            return st

    def _verify_and_gate_once(self, tid, offer, order, cap_resp=None, truth=None):
        if truth is None:
            truth = self._get_order(order["order_id"])                # server-side, authenticated provider record
        v = verify_order(truth, offer, order["order_id"], self.expected_payee, self.payments.environment, cap_resp)
        self.store.put_capture(order["order_id"], v.get("capture_id"), v.get("capture_status") or "NONE", v)
        state = self.store.task(tid)["state"]
        if v["verdict"] == "CAPTURE_VERIFIED":
            if self.store.was_cancelled(tid):                  # R1c: owner decision, never automatic execution
                self.store.note(tid, "payment_verifier", "payment_verification", v,
                                {"note": "verified capture on a task that was cancelled earlier: not executed "
                                         "automatically; owner decision (deliver, or refund in the PayPal dashboard)"})
                return state
            if self.store.execution(tid) is not None:          # gate: paid work already ran (failed/abandoned)
                self.store.note(tid, "payment_verifier", "payment_verification", v,
                                {"note": "verified capture, but this paid order was already executed: stays in "
                                         "review (never re-authorized, never re-run)"})
                return state
            self.store.transition(tid, S.PAYMENT_CAPTURED, "payment_verifier", "payment_verification", v,
                                  {"capture_id": v["capture_id"]})
            self.store.transition(tid, S.EXECUTION_AUTHORIZED, "payment_verifier", "payment_verification", v, {})
            return S.EXECUTION_AUTHORIZED
        if v["verdict"] == "CAPTURE_PENDING":
            if state == S.PAYMENT_PENDING:
                self.store.note(tid, "payment_verifier", "payment_verification", v,
                                {"note": "capture PENDING at provider; execution stays blocked"})
            return state
        if v["verdict"] == "NOT_CAPTURED":                         # R1c: a checkout exit is never a cancellation
            return self.store.task(tid)["state"]
        if v["verdict"] in ("CAPTURE_DECLINED", "ORDER_VOIDED"):
            self._fail_payment(tid, state, v["verdict"], v)
            return self.store.task(tid)["state"]
        # MISMATCH or REVERSED: money may have moved but not as offered -> a human must look; never execute
        if state != S.REVIEW_REQUIRED:
            self.store.transition(tid, S.REVIEW_REQUIRED, "payment_verifier", "payment_verification", v,
                                  reason=f"payment verification {v['verdict']}")
        return S.REVIEW_REQUIRED

    def _fail_payment(self, tid, state, why, ev):
        target = S.FAILED if S.allowed(state, S.FAILED) else S.REVIEW_REQUIRED
        self.store.transition(tid, target, "payment_verifier", "payment_failure", ev, reason=why)

    # ---- R1c cancellation model (see r1c/R1c_CANCEL_INTEGRITY.md) ---------------------------------------------
    # * The PayPal cancel redirect is NAVIGATION, not payment evidence: it is recorded and changes nothing.
    # * CANCELLED means "explicitly terminated unpaid task": only an explicit buyer action on our page (CSRF + session
    #   bound), and only when (1) no durable local capture attempt is reserved/sent/unknown/resolved, (2) an
    #   authenticated provider GET shows no capture, and (3) the same checks hold inside the write transaction.
    # * A capture found later on a CANCELLED task (legacy data) is recovered only by the owner-authorized
    #   reconcile_cancelled(), which stops at REVIEW_REQUIRED and never executes.
    def cancel(self, tid, why="buyer cancelled"):
        """EXPLICIT termination of an unpaid task. Raises GateError (nothing changed) whenever it cannot be shown that
        no payment exists or can still complete. Never sends a capture."""
        t = self.store.task(tid)
        if t is None:
            raise GateError("unknown task")
        if t["state"] == S.CANCELLED:
            return S.CANCELLED
        if not S.allowed(t["state"], S.CANCELLED):
            raise GateError(f"a task in state {t['state']} cannot be cancelled")
        with self._lock(tid):
            order = self.store.order_for_task(tid)
            evidence, provider_status = {"reason": why, "provider_order": None}, None
            if order:
                oid = order["order_id"]
                blocker = self.store.cancel_blocker(oid)
                if blocker:
                    raise GateError(f"cannot cancel: {blocker}. Nothing was changed.")
                try:
                    truth = self._get_order(oid)               # authenticated, server-side
                except ProviderError as e:
                    raise GateError(f"cannot cancel: the provider record could not be read ({e.issue}). "
                                    "Nothing was changed.") from None
                provider_status = truth.get("status")
                if self._has_capture(truth):
                    self.store.note(tid, "payment_gateway", "cancel_refused",
                                    {"order_id": oid, "provider_status": provider_status},
                                    {"note": "provider shows a capture; cancellation refused"})
                    raise GateError("cannot cancel: the provider shows a capture for this order. Nothing was changed.")
                if provider_status == "VOIDED":                # confirmed voided at the provider -> FAILED, not CANCELLED
                    return self._verify_and_gate(tid, self.store.offer(task_id=tid), order, truth=truth)
                if provider_status not in ("CREATED", "PAYER_ACTION_REQUIRED", "APPROVED"):
                    raise GateError(f"cannot cancel: unexpected provider status {provider_status!r}. "
                                    "Nothing was changed.")
                evidence = {"reason": why, "provider_order": {"order_id": oid, "status": provider_status,
                                                              "captures": 0, "source": "server-side GET"}}
            had_order = order is not None

            def guard(src):                                    # re-checked inside the write transaction
                o = self.store.order_for_task(tid)
                if (o is not None) != had_order:
                    raise TransitionError("a provider order was created concurrently; reload and retry")
                b = o and self.store.cancel_blocker(o["order_id"])
                if b:
                    raise TransitionError(b)
            try:
                self.store.transition(tid, S.CANCELLED, "buyer", "cancellation", evidence,
                                      {"explicit": True, "provider_status": provider_status}, reason=why, guard=guard)
            except TransitionError as e:
                raise GateError(f"cannot cancel: {e}. Nothing was changed.") from None
        return S.CANCELLED

    def on_buyer_cancel(self, order_id):
        """PayPal sent the browser to our cancel_url. This is NOT payment evidence: PayPal uses it for any exit from
        checkout, and anyone holding the approval URL can request it. It is recorded, and nothing else happens: no
        provider call, no capture, no state change. The buyer can return to PayPal, or cancel explicitly."""
        tid = self.store.task_for_order(order_id)
        if tid is None:
            raise GateError("unknown order")
        st, a = self.store.task(tid)["state"], self.store.capture_attempt(order_id)
        self.store.note(tid, "buyer", "checkout_navigation", {"route": "cancel_url", "order_id": order_id},
                        {"route": "cancel_url", "authoritative": False, "task_state": st,
                         "attempt_state": a and a["state"],
                         "note": "browser returned via the PayPal cancel_url; not payment evidence; nothing changed"})
        return st

    def reconcile_cancelled(self, order_id):
        """OWNER-AUTHORIZED and state-changing (CLI `reconcile ORDER --owner-confirm`). For a CANCELLED task only: one
        authenticated GET. If the provider shows a capture, record it and move the task to REVIEW_REQUIRED (operator
        edge). Never sends a capture or creates an order, never authorizes execution."""
        tid = self.store.task_for_order(order_id)
        if tid is None:
            raise GateError("unknown order")
        if self.store.task(tid)["state"] != S.CANCELLED:
            raise GateError("reconcile_cancelled applies to CANCELLED tasks only")
        order, offer = self.store.order_for_task(tid), self.store.offer(task_id=tid)
        try:
            truth = self._get_order(order_id)
        except ProviderError as e:
            raise GateError(f"provider record could not be read ({e.issue}); nothing was changed") from None
        if not self._has_capture(truth):
            return {"task_id": tid, "state": S.CANCELLED, "changed": False, "provider_status": truth.get("status")}
        v = verify_order(truth, offer, order_id, self.expected_payee, self.payments.environment)
        self.store.put_capture(order_id, v.get("capture_id"), v.get("capture_status") or "NONE", v)
        self.store.transition(
            tid, S.REVIEW_REQUIRED, "operator", "payment_verification", v,
            {"capture_id": v.get("capture_id"), "verdict": v["verdict"],
             "note": "provider capture found on a cancelled task; never executed automatically"},
            reason=f"provider capture found on a cancelled task ({v['verdict']}): owner decision required "
                   "(deliver, or refund in the PayPal dashboard); never executed automatically",
            guard=lambda src: self.store.resolve_attempt_on_closed_task(order_id, "LATE_CAPTURE_ON_CANCELLED_TASK"))
        return {"task_id": tid, "state": S.REVIEW_REQUIRED, "changed": True, "verdict": v["verdict"]}

    # ------------------------------------------------------------------ 9-12: execute, verify, receipt
    def execute(self, tid):
        t = self.store.task(tid)
        if t is None or t["state"] != S.EXECUTION_AUTHORIZED:
            raise GateError(f"execution not authorized (state {t and t['state']})")
        order = self.store.order_for_task(tid)
        cap = self.store.capture(order["order_id"])
        if not cap or cap["verification"].get("verdict") != "CAPTURE_VERIFIED":
            raise GateError("no verified capture on record")
        eid = self.store.claim_execution(order["order_id"], tid)
        if eid is None:
            raise GateError("this paid order has already been executed")
        offer = self.store.offer(task_id=tid)
        self.store.transition(tid, S.EXECUTING, "executor", "execution_claim", {"execution_id": eid}, {"execution_id": eid})
        docset = self.docsets[offer["document_set"]["id"]]
        budget = policy.CAPABILITIES[offer["capability"]]["model_budget"]
        try:
            raw, runs = research.run(offer, docset, self.ai, budget)
            deliverable, ver = research.verify_artifact(raw, docset, offer)
        except (AIError, KeyError, TypeError, ValueError) as e:
            self.store.finish_execution(eid, "FAILED", None)
            self.store.transition(tid, S.REVIEW_REQUIRED, "executor", "execution_failure", {"error": str(e)[:300]},
                                  reason="paid task failed after capture; needs a human decision (no automatic refund)")
            return S.REVIEW_REQUIRED
        art = research.final_artifact(raw, deliverable, ver)
        adig = self.store.put_artifact(tid, art)
        self.store.finish_execution(eid, ver["status"], adig)
        if ver["status"] == "FAILED":
            self.store.transition(tid, S.REVIEW_REQUIRED, "artifact_verifier", "artifact_verification", ver,
                                  reason="no finding survived quote verification")
            return S.REVIEW_REQUIRED
        self.store.transition(tid, S.DELIVERY_VERIFIED, "artifact_verifier", "artifact_verification",
                              dict(ver, artifact_digest=adig), {"artifact_digest": adig, "status": ver["status"]})
        rc = R.build(self.store.task(tid), offer, order, cap, self.store.execution(tid), adig, ver,
                     self.store.events(tid), self.signer, self.payments.environment)
        self.store.put_receipt(tid, rc)
        self.store.transition(tid, S.DELIVERED, "receipt_issuer", "receipt", rc, {"receipt_digest": digest(rc)})
        return S.DELIVERED

    def deliver_artifact(self, tid, artifact_digest):
        """Serve the delivered artifact only if it is exactly the one the receipt names."""
        rc = self.store.receipt(tid)
        if not rc or rc["body"]["artifact"]["digest"] != artifact_digest:
            raise GateError("artifact does not match the receipt")
        a = self.store.artifact(artifact_digest)
        if a is None or digest(a) != artifact_digest:
            raise GateError("stored artifact changed after delivery")
        return a

    # ------------------------------------------------------------------ webhooks (advisory -> reconcile)
    def handle_webhook(self, headers, raw_body, reconcile=True):
        """Returns a dict with `http` = the status to answer PayPal with:
          200  event durably recorded (or a duplicate of a recorded one) -> PayPal stops retrying
          400  not acceptable (no JSON, no ids, signature FAILURE) -> never processed
          503  could not verify or could not persist right now -> PayPal retries the delivery (up to 25x / 3 days)
        A webhook NEVER authorizes payment or execution: it only queues a server-side GET reconciliation."""
        h = {k.lower(): v for k, v in headers.items()}
        try:
            ev = json.loads(raw_body)
            if not isinstance(ev, dict):
                raise ValueError
        except Exception:
            return {"accepted": False, "http": 400, "reason": "body is not a JSON object"}
        tx, event_id = h.get("paypal-transmission-id"), ev.get("id")
        if not tx or not event_id:
            return {"accepted": False, "http": 400, "reason": "missing transmission id or event id"}
        if self.payments.environment == "SANDBOX" and not self.cfg.paypal_webhook_id:
            return {"accepted": False, "http": 400, "reason": "webhooks disabled: PAYPAL_WEBHOOK_ID not configured"}
        method = "MOCK_HMAC_SIMULATED" if self.payments.environment == "MOCK" else "PAYPAL_POSTBACK"
        try:
            v = self.payments.verify_webhook(headers, raw_body, self.cfg.paypal_webhook_id or "MOCK-WEBHOOK")
        except ProviderError as e:
            return {"accepted": False, "http": 503, "reason": f"verification unavailable ({e.issue}); retry later",
                    "debug_id": e.debug_id}
        except ValueError:
            return {"accepted": False, "http": 400, "reason": "body is not JSON"}
        if v.get("verification_status") != "SUCCESS":
            return {"accepted": False, "http": 400, "reason": "signature verification FAILURE; event ignored"}
        res = ev.get("resource") or {}
        order_id = (((res.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id")
                    or (res.get("id") if str(ev.get("event_type", "")).startswith("CHECKOUT.ORDER") else None))
        try:
            fresh = self.store.record_webhook(tx, event_id, "SUCCESS:" + method, ev.get("event_type"), order_id)
        except sqlite3.Error:
            return {"accepted": False, "http": 503, "reason": "could not persist the event; retry later"}
        if not fresh:
            return {"accepted": False, "http": 200, "duplicate": True,
                    "reason": "replay: transmission or event already recorded"}
        tid = self.store.task_for_order(order_id) if order_id else None
        if tid is None:
            self.store.set_webhook_outcome(tx, "no matching task")
            return {"accepted": True, "http": 200, "action": "none (no matching task)", "verification": method}
        self.store.note(tid, "webhook_reconciler", "webhook_received",
                        {"transmission_id": tx, "event_id": event_id, "event_type": ev.get("event_type")},
                        {"verification": method, "simulated": method.startswith("MOCK"),
                         "note": "advisory only; payment truth comes from a server-side GET"})
        if not reconcile:                                  # ack fast (after the durable record); reconcile async
            self.store.set_webhook_outcome(tx, "queued")
            return {"accepted": True, "http": 200, "task_id": tid, "queued": True, "transmission_id": tx}
        r = self.process_webhook(tx, tid, ev.get("event_type"))
        return dict(r, accepted=True, http=200)

    def process_webhook(self, tx, tid, event_type=None):
        """Asynchronous half: reconcile, then mark the event processed. If we crash before the mark, recover()
        finds the event still 'queued' and reconciles again (reconciliation is idempotent: GET + verifier)."""
        r = self.reconcile_task(tid, event_type)
        self.store.set_webhook_outcome(tx, r["state"])
        return r

    def reconcile_task(self, tid, event_type=None):
        """A webhook never authorizes anything by itself: it only triggers a fresh server-side verification."""
        order_id = self.store.order_for_task(tid)["order_id"]
        t = self.store.task(tid)
        if t["state"] in (S.PAYMENT_PENDING, S.REVIEW_REQUIRED):
            with self._lock(tid):
                st = self._capture_with_reservation(tid)          # reserves + reconciles; never a blind re-send
        else:
            try:
                truth = self._get_order(order_id)
            except ProviderError as e:
                self._provider_unavailable(tid, e)
                return {"task_id": tid, "state": t["state"]}
            vv = verify_order(truth, self.store.offer(task_id=tid), order_id, self.expected_payee,
                              self.payments.environment)
            self.store.note(tid, "webhook_reconciler", "post_event_reconciliation", vv,
                            {"event_type": event_type, "verdict": vv["verdict"],
                             "note": "recorded only; delivered receipts are never rewritten"})
            st = t["state"]
        return {"task_id": tid, "state": st}

    # ------------------------------------------------------------------ recovery after process interruption
    def recover(self, stale_execution_s=900.0, orphan_offer_s=120.0):
        """Run at start-up (or by an operator). A restart is never a reason to send a capture:
          * capture attempts whose lease expired -> READ-ONLY reconciliation (GET). A provider capture resolves it;
            an attempt that may be in flight stays OUTCOME_UNKNOWN (task REVIEW_REQUIRED); an attempt reserved but
            never sent is released (the next buyer return or webhook may capture it);
          * executions left STARTED by a dead process -> ABANDONED, task -> REVIEW_REQUIRED (no automatic re-run);
          * EXECUTION_AUTHORIZED tasks with no execution claim -> listed (the UNIQUE claim prevents duplicates);
          * webhooks recorded (acknowledged) but never processed -> reconciled now;
          * accepted offers without a provider order -> create it with the SAME order PayPal-Request-Id (no money
            moves at order creation; the provider de-duplicates by request id)."""
        report = {"captures": [], "executions": [], "authorized": [], "orders": [], "webhooks": [],
                  "cancelled_with_capture_attempt": []}
        now = time.time()
        for (oid, tid) in self.store.db.execute(
                "SELECT c.order_id, o.task_id FROM capture_attempts c JOIN orders o ON o.order_id=c.order_id "
                "WHERE c.state IN ('RESERVED','SENT','OUTCOME_UNKNOWN') AND c.lease_until < ?", (now,)).fetchall():
            if self.store.task(tid)["state"] in (S.PAYMENT_PENDING, S.REVIEW_REQUIRED):
                with self._lock(tid):
                    report["captures"].append((tid, self._capture_with_reservation(tid, may_send=False)))
        # R1c: legacy CANCELLED tasks whose capture may have been sent are REPORTED only (no GET, no state change);
        # the owner runs `order-status` (read-only), then `reconcile --owner-confirm`.
        for (tid, ast) in self.store.db.execute(
                "SELECT o.task_id, c.state FROM capture_attempts c JOIN orders o ON o.order_id=c.order_id JOIN tasks t "
                "ON t.task_id=o.task_id WHERE t.state=? AND (c.sends > 0 OR c.state IN ('SENT','OUTCOME_UNKNOWN'))",
                (S.CANCELLED,)).fetchall():
            report["cancelled_with_capture_attempt"].append((tid, ast))
        for e in self.store.stale_executions(stale_execution_s):
            self.store.abandon_execution(e["execution_id"])
            if self.store.task(e["task_id"])["state"] == S.EXECUTING:
                self.store.transition(e["task_id"], S.REVIEW_REQUIRED, "operator", "execution_abandoned", e,
                                      reason="execution interrupted; paid AI work is never re-run automatically")
            report["executions"].append(e["task_id"])
        for (tx, order_id, etype) in self.store.pending_webhooks():
            tid = self.store.task_for_order(order_id) if order_id else None
            if tid is None:
                self.store.set_webhook_outcome(tx, "no matching task")
                continue
            if self.store.task(tid)["state"] in (S.PAYMENT_PENDING, S.REVIEW_REQUIRED):
                with self._lock(tid):
                    st = self._capture_with_reservation(tid, may_send=False)
                self.store.set_webhook_outcome(tx, st)
            else:
                st = self.process_webhook(tx, tid, etype)["state"]
            report["webhooks"].append((tx, st))
        for (tid,) in self.store.db.execute("SELECT task_id FROM tasks WHERE state=?", (S.EXECUTION_AUTHORIZED,)).fetchall():
            if self.store.execution(tid) is None:
                report["authorized"].append(tid)
        for (tid, offer_id) in self.store.db.execute(
                "SELECT f.task_id, f.offer_id FROM offers f JOIN tasks t ON t.task_id=f.task_id LEFT JOIN orders o "
                "ON o.offer_id=f.offer_id WHERE f.accepted=1 AND o.order_id IS NULL AND t.state=?",
                (S.BUYER_APPROVAL_REQUIRED,)).fetchall():
            offer = self.store.offer(task_id=tid)
            if now - offer["created"] > orphan_offer_s and policy.offer_is_live(offer):
                report["orders"].append((tid, self._create_order(tid, offer)))
        return report

    # ------------------------------------------------------------------ views
    def view(self, tid):
        t = self.store.task(tid)
        if t is None:
            return None
        o = self.store.offer(task_id=tid)
        order = self.store.order_for_task(tid)
        cap = self.store.capture(order["order_id"]) if order else None
        return {"task": t, "offer": o, "order": order and {"order_id": order["order_id"], "provider": order["provider"]},
                "capture": cap, "execution": self.store.execution(tid), "receipt": self.store.receipt(tid),
                "events": self.store.events(tid), "environment": self.payments.environment}
