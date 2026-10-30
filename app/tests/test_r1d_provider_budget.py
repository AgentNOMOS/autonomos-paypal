"""R1d provider, cost-envelope and labelling tests (mission scenarios 10-16 and 24, plus authorization, preflight,
secret handling and the legacy-path guard). Offline: injected transports only; the REAL transport is never invoked.
"""
import http.client
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r1d_support import (APP, FAKE_KEY, Q, FakeMessagesAPI, NetworkBlocked, honest_answer, message, mock_runtime,
                         new_service, paid_task, payment_snapshot)
from autonomos import states as S
from autonomos.config import Config
from autonomos.r1d import (FIXTURE_EXECUTION, MOCK_EXECUTION, REAL_MODEL_EXECUTION, aiconfig, envelope as E, receipt2)
from autonomos.r1d.engine import SCHEMA
from autonomos.r1d.governed import GovernedModelClient, ModelCallFailed
from autonomos.r1d.ledger import LedgerRefusal
from autonomos.r1d.preflight import preflight
from autonomos.r1d.providers import API_URL, AnthropicMessagesProvider, FixtureProviderR1d, ProviderFailure
from autonomos.service import GateError

PROJECT_ROOT = APP          # repository root for the "outside the repository" rule (robust in temp copies)


def fail_case(step, **env):
    rt, api = mock_runtime(FakeMessagesAPI(step), **env)
    svc = new_service(rt)
    tid = paid_task(svc)
    before = payment_snapshot(svc)
    st = svc.execute(tid)
    return svc, tid, st, api, before


class FailureMixin:
    def assert_review_no_retry(self, svc, tid, st, api, before, ledger_state, kind):
        self.assertEqual(st, S.REVIEW_REQUIRED)
        self.assertEqual(len(api.requests), 1, "a failed model request is never retried")
        self.assertIsNone(svc.store.receipt(tid))
        self.assertEqual(svc.store.execution(tid)["status"], "FAILED")
        calls = svc.ledger.calls(task_id=tid)
        self.assertEqual([c["state"] for c in calls], [ledger_state])
        self.assertIn(kind, svc.store.task(tid)["reason"] + json.dumps(svc.ledger.record(
            svc.store.execution(tid)["execution_id"])["record"]))
        self.assertEqual(payment_snapshot(svc), before)
        self.assertEqual(svc.store.capture(svc.store.order_for_task(tid)["order_id"])["status"], "COMPLETED")
        with self.assertRaises(GateError):
            svc.execute(tid)                                                     # never re-run
        return calls[0]


class T10Timeout(FailureMixin, unittest.TestCase):
    def test_socket_timeout_is_unknown_and_charged_worst_case(self):
        svc, tid, st, api, before = fail_case(("raise", socket.timeout("timed out")))
        c = self.assert_review_no_retry(svc, tid, st, api, before, "OUTCOME_UNKNOWN", "TIMEOUT")
        self.assertEqual(c["charged_usd"], c["worst_case_usd"])

    def test_slow_drip_is_cut_at_the_deadline(self):
        rt, api = mock_runtime(FakeMessagesAPI(("sleep", 3.0, ("answer", honest_answer))), request_timeout_s=1.0)
        rt.provider.DEADLINE_GRACE_S = 0.2
        svc = new_service(rt)
        tid = paid_task(svc)
        before = payment_snapshot(svc)
        t0 = time.time()
        st = svc.execute(tid)
        self.assertLess(time.time() - t0, 2.5)
        self.assert_review_no_retry(svc, tid, st, api, before, "OUTCOME_UNKNOWN", "TIMEOUT")


class T11RateLimit(FailureMixin, unittest.TestCase):
    def test_429_is_rejected_not_retried_not_charged(self):
        svc, tid, st, api, before = fail_case(("http", 429, "rate_limit_error", {"retry-after": "30"}))
        c = self.assert_review_no_retry(svc, tid, st, api, before, "REJECTED", "RATE_LIMITED")
        self.assertEqual(c["charged_usd"], "0")


class T12ApiFailure(FailureMixin, unittest.TestCase):
    def test_server_errors_are_unknown(self):
        for code, err, kind in ((500, "api_error", "SERVER_ERROR"), (529, "overloaded_error", "OVERLOADED"),
                                (504, "timeout_error", "TIMEOUT")):
            with self.subTest(code):
                self.assert_review_no_retry(*fail_case(("http", code, err)), "OUTCOME_UNKNOWN", kind)

    def test_client_errors_are_rejected(self):
        for code, err, kind in ((400, "invalid_request_error", "INVALID_REQUEST"),
                                (401, "authentication_error", "AUTHENTICATION"), (403, "permission_error", "PERMISSION"),
                                (404, "not_found_error", "NOT_FOUND"), (413, "request_too_large", "REQUEST_TOO_LARGE"),
                                (402, "billing_error", "BILLING")):
            with self.subTest(code):
                self.assert_review_no_retry(*fail_case(("http", code, err)), "REJECTED", kind)

    def test_refusal_truncation_and_odd_content_are_billed_failures(self):
        cases = [("REFUSAL", lambda b: dict(message("", model=b["model"], stop="refusal"), content=[],
                                            stop_details={"type": "refusal", "category": "cyber"})),
                 ("TRUNCATED", lambda b: message('{"answer_summary": "', model=b["model"], stop="max_tokens")),
                 ("UNEXPECTED_CONTENT", lambda b: message("{}", model=b["model"], extra_blocks=(
                     {"type": "tool_use", "id": "t1", "name": "bash", "input": {"cmd": "id"}},))),
                 ("NO_TEXT", lambda b: dict(message("", model=b["model"]), content=[{"type": "thinking",
                                                                                     "thinking": ""}])),
                 ("MODEL_MISMATCH", lambda b: message(json.dumps(honest_answer()), model="claude-opus-4-8"))]
        for kind, m in cases:
            with self.subTest(kind):
                c = self.assert_review_no_retry(*fail_case(("message", m)), "COMPLETED", kind)
                self.assertEqual(c["input_tokens"], 1200)                       # usage of the billed answer kept


class T13UncertainAfterInterruptedRequest(FailureMixin, unittest.TestCase):
    def test_interrupted_or_unreadable_response_is_unknown(self):
        for step, kind in ((("read_raise", http.client.IncompleteRead(b"{\"type\":")), "TRANSPORT_ERROR"),
                           (("raise", ConnectionResetError(104, "reset")), "TRANSPORT_ERROR"),
                           (("raw", b"<html>gateway</html>"), "UNPARSEABLE_RESPONSE"),
                           (("message", {"type": "message", "id": "msg_x", "model": "claude-opus-5-5",
                                         "stop_reason": "end_turn", "content": []}), "NO_USAGE")):
            with self.subTest(kind):
                c = self.assert_review_no_retry(*fail_case(step), "OUTCOME_UNKNOWN", kind)
                self.assertEqual(c["charged_usd"], c["worst_case_usd"])


def pay_then_execute(n, api=None, **constrained):
    """Pay n tasks under a generous envelope, then execute them with the constrained one (same authorization id, same
    database): models an owner lowering the budget after offers were paid."""
    rt0, _ = mock_runtime()
    svc0 = new_service(rt0)
    tids = [paid_task(svc0) for _ in range(n)]
    rt, api = mock_runtime(api, **constrained)
    return new_service(rt, data_dir=svc0.cfg.data_dir), tids, api, rt


class T14TokenBudget(unittest.TestCase):
    def test_input_bound_above_per_request_limit_sends_nothing(self):
        svc, (tid,), api, rt = pay_then_execute(1, max_input_tokens_per_request=2000)
        self.assertEqual(svc.execute(tid), S.REVIEW_REQUIRED)
        self.assertEqual(len(api.requests), 0)
        self.assertEqual(svc.ledger.calls(task_id=tid), [])
        self.assertIn("INPUT_TOKEN_LIMIT", json.dumps(svc.ledger.record(svc.store.execution(tid)["execution_id"])))

    def test_total_token_budget_exhausts_across_tasks(self):
        svc, (t1, t2), api, rt = pay_then_execute(2, max_total_tokens=31_000)   # bound ~29.4k; first uses 2,000
        self.assertEqual(svc.execute(t1), S.DELIVERED)
        self.assertEqual(svc.execute(t2), S.REVIEW_REQUIRED)
        self.assertEqual(len(api.requests), 1)
        self.assertIn("TOKEN_BUDGET", json.dumps(svc.ledger.record(svc.store.execution(t2)["execution_id"])))

    def test_usd_budget_counts_unknown_outcomes_at_worst_case(self):
        svc, (t1, t2), api, rt = pay_then_execute(
            2, FakeMessagesAPI(("raise", TimeoutError("timed out")), ("answer", honest_answer)),
            price_in=Decimal("4"), price_out=Decimal("20"), max_usd_total=Decimal("0.50"), price_basis="TEST")
        self.assertEqual(svc.execute(t1), S.REVIEW_REQUIRED)                    # unknown: charged ~0.31 USD
        self.assertEqual(svc.execute(t2), S.REVIEW_REQUIRED)                    # 0.31 + 0.31 > 0.50: refused
        self.assertEqual(len(api.requests), 1)
        self.assertLessEqual(Decimal(svc.ledger.totals(rt.envelope.authorization_id)["usd"]), Decimal("0.50"))
        self.assertIn("USD_BUDGET", json.dumps(svc.ledger.record(svc.store.execution(t2)["execution_id"])))

    def test_post_hoc_excess_freezes_the_authorization(self):
        big = lambda b: message(json.dumps(honest_answer(b["messages"][0]["content"])), model=b["model"],
                                usage={"input_tokens": 999_999, "output_tokens": 10})
        svc, (t1, t2), api, rt = pay_then_execute(2, FakeMessagesAPI(("message", big)))
        self.assertEqual(svc.execute(t1), S.REVIEW_REQUIRED)
        self.assertTrue(svc.ledger.authorization_state(rt.envelope.authorization_id)["frozen"])
        self.assertEqual(svc.execute(t2), S.REVIEW_REQUIRED)
        self.assertEqual(len(api.requests), 1)
        t3 = svc.request_task({"capability": "research_snapshot", "topic": Q, "corpus": "agentnomos_quantum"})
        with self.assertRaises(GateError):                                      # frozen: no new orders either
            svc.accept_offer(t3, svc.store.offer(task_id=t3)["_digest"])


PRICED = dict(price_in=Decimal("4"), price_out=Decimal("20"), max_usd_total=Decimal("5.00"), price_basis="TEST")


class T14bUnknownCostNeverConfirmedZero(FailureMixin, unittest.TestCase):
    """Closes the D18 gap: under a NON-ZERO price an unknown outcome must be carried at its worst case (a conservative
    bound, never a confirmed 0), and must stay distinct from a known charge (reported usage) and from a documented
    refusal before processing (0). The zero-price offline envelope cannot tell these apart, so every check here is
    priced."""

    def priced_case(self, *script):
        rt, api = mock_runtime(FakeMessagesAPI(*script), **PRICED)
        svc = new_service(rt)
        tid = paid_task(svc)
        return svc, tid, svc.execute(tid), api, rt

    def assert_reserved_at_worst_case(self, svc, tid, rt, c):
        worst = Decimal(c["worst_case_usd"])
        self.assertGreater(worst, 0)
        self.assertEqual(c["state"], "OUTCOME_UNKNOWN")
        self.assertEqual(Decimal(c["charged_usd"]), worst, "an unknown outcome is carried at its worst case")
        self.assertIsNone(c["output_tokens"], "no usage is invented for an unknown outcome")
        rec = svc.ledger.record(svc.store.execution(tid)["execution_id"])["record"]
        (r,) = rec["requests"]
        self.assertEqual((r["state"], Decimal(r["charged_usd"])), ("OUTCOME_UNKNOWN", worst))
        t = svc.ledger.totals(rt.envelope.authorization_id)
        self.assertEqual(Decimal(t["usd"]), worst)
        self.assertEqual(Decimal(rec["authorization_totals_after"]["usd"]), worst)
        self.assertEqual(t["tokens"], c["est_input_tokens_max"] + c["max_output_tokens"])

    def test_every_unknown_outcome_is_worst_case_not_zero(self):
        for step, kind in ((("raise", socket.timeout("timed out")), "TIMEOUT"),
                           (("http", 500, "api_error"), "SERVER_ERROR"),
                           (("http", 529, "overloaded_error"), "OVERLOADED"),
                           (("read_raise", http.client.IncompleteRead(b"{")), "TRANSPORT_ERROR"),
                           (("raw", b"<html>gateway</html>"), "UNPARSEABLE_RESPONSE"),
                           (("message", {"type": "message", "id": "msg_x", "model": "claude-opus-5-5",
                                         "stop_reason": "end_turn", "content": []}), "NO_USAGE")):
            with self.subTest(kind):
                svc, tid, st, api, rt = self.priced_case(step)
                before = payment_snapshot(svc)
                self.assertEqual(st, S.REVIEW_REQUIRED)
                self.assertEqual(len(api.requests), 1)
                (c,) = svc.ledger.calls(task_id=tid)
                self.assert_reserved_at_worst_case(svc, tid, rt, c)
                with self.assertRaises(GateError):
                    svc.execute(tid)                                             # never re-run, never re-charged
                self.assertEqual(len(api.requests), 1)
                self.assertEqual(payment_snapshot(svc), before)

    def test_known_charge_and_refusal_are_distinct_from_the_worst_case(self):
        svc, tid, st, api, rt = self.priced_case(("answer", honest_answer))
        self.assertEqual(st, S.DELIVERED)
        (c,) = svc.ledger.calls(task_id=tid)
        known = (Decimal(1200) * 4 + Decimal(800) * 20) / 1_000_000                # FakeMessagesAPI's reported usage
        self.assertEqual((c["state"], Decimal(c["charged_usd"])), ("COMPLETED", known))
        self.assertLess(known, Decimal(c["worst_case_usd"]))
        self.assertEqual(Decimal(svc.ledger.totals(rt.envelope.authorization_id)["usd"]), known)
        cost = svc.store.artifact(svc.store.execution(tid)["artifact_digest"])["ai_execution"]["cost"]
        self.assertEqual(Decimal(cost["estimated_charge_usd"]), known)
        self.assertEqual(cost["worst_case_usd"], c["worst_case_usd"])
        self.assertEqual(svc.store.receipt(tid)["body"]["ai_execution"]["cost"], cost)
        svc, tid, st, api, rt = self.priced_case(("http", 429, "rate_limit_error"))
        (c,) = svc.ledger.calls(task_id=tid)
        self.assertEqual((c["state"], c["charged_usd"]), ("REJECTED", "0"))     # refused before processing
        self.assertGreater(Decimal(c["worst_case_usd"]), 0)
        self.assertEqual(Decimal(svc.ledger.totals(rt.envelope.authorization_id)["usd"]), 0)

    def test_recovered_in_flight_request_is_worst_case_not_zero(self):
        """Another process's recovery marks the in-flight (SENT, lease expired) request OUTCOME_UNKNOWN while the
        request is open; the late answer is discarded (LEDGER_CONFLICT) and the recovered charge must stand."""
        box = {}

        def recover_mid_request(body):
            box["recovered"] = box["svc"].ledger.recover(now=time.time() + 3600)
            return message(json.dumps(honest_answer(body["messages"][0]["content"])), model=body["model"])
        rt, api = mock_runtime(FakeMessagesAPI(("message", recover_mid_request)), **PRICED)
        svc = box["svc"] = new_service(rt)
        tid = paid_task(svc)
        self.assertEqual(svc.execute(tid), S.REVIEW_REQUIRED)
        (c,) = svc.ledger.calls(task_id=tid)
        self.assertEqual(box["recovered"], [(c["call_id"], "OUTCOME_UNKNOWN")])
        self.assertIn("LEDGER_CONFLICT", json.dumps(svc.ledger.record(svc.store.execution(tid)["execution_id"])))
        self.assert_reserved_at_worst_case(svc, tid, rt, c)
        self.assertEqual(len(api.requests), 1)


class T15RequestCountLimit(unittest.TestCase):
    def test_total_request_limit(self):
        svc, (t1, t2), api, rt = pay_then_execute(2, max_requests_total=1)
        self.assertEqual(svc.execute(t1), S.DELIVERED)
        self.assertEqual(svc.execute(t2), S.REVIEW_REQUIRED)
        self.assertEqual(len(api.requests), 1)
        self.assertIn("REQUEST_LIMIT_TOTAL", json.dumps(svc.ledger.record(svc.store.execution(t2)["execution_id"])))

    def test_execution_limit_and_second_request_in_one_execution(self):
        svc, (t1, t2), api, rt = pay_then_execute(2, max_executions=1)
        self.assertEqual(svc.execute(t1), S.DELIVERED)
        self.assertEqual(svc.execute(t2), S.REVIEW_REQUIRED)
        self.assertIn("EXECUTION_LIMIT", json.dumps(svc.ledger.record(svc.store.execution(t2)["execution_id"])))
        # a second request inside one (still STARTED) execution is refused by the ledger
        rt3, api3 = mock_runtime()
        svc3 = new_service(rt3)
        t3 = paid_task(svc3)
        eid = svc3.store.claim_execution(svc3.store.order_for_task(t3)["order_id"], t3)
        svc3.store.transition(t3, S.EXECUTING, "executor", "execution_claim", {"execution_id": eid})
        c = svc3.ai_client()
        c.call(execution_id=eid, task_id=t3, seq=1, system="s", user="u", schema=SCHEMA, timeout_s=5)
        for seq in (1, 2):
            with self.assertRaises(LedgerRefusal) as cm:
                c.call(execution_id=eid, task_id=t3, seq=seq, system="s", user="u", schema=SCHEMA, timeout_s=5)
            self.assertEqual(cm.exception.code, "REQUEST_LIMIT_EXECUTION")
        self.assertEqual(len(api3.requests), 1)


class CapacityGateBeforePayment(unittest.TestCase):
    def test_exhausted_envelope_refuses_before_any_order(self):
        rt, api = mock_runtime(max_requests_total=1)
        svc = new_service(rt)
        t1 = paid_task(svc)
        self.assertEqual(svc.execute(t1), S.DELIVERED)
        t2 = svc.request_task({"capability": "research_snapshot", "topic": Q, "corpus": "agentnomos_quantum"})
        before = payment_snapshot(svc)
        with self.assertRaises(GateError) as cm:
            svc.accept_offer(t2, svc.store.offer(task_id=t2)["_digest"])
        self.assertIn("REQUEST_LIMIT_TOTAL", str(cm.exception))
        self.assertIsNone(svc.store.order_for_task(t2))                          # nothing created, nothing charged
        self.assertEqual(svc.store.task(t2)["state"], S.BUYER_APPROVAL_REQUIRED)
        self.assertEqual(payment_snapshot(svc), before)
        self.assertTrue(any(e["evidence_kind"] == "ai_capacity_refusal" for e in svc.store.events(t2)))

    def test_envelope_too_small_for_the_largest_prompt_refuses_offers(self):
        rt, api = mock_runtime(max_input_tokens_per_request=20_000)
        svc = new_service(rt)
        tid = svc.request_task({"capability": "research_snapshot", "topic": Q, "corpus": "agentnomos_quantum"})
        with self.assertRaises(GateError) as cm:
            svc.accept_offer(tid, svc.store.offer(task_id=tid)["_digest"])
        self.assertIn("INPUT_TOKEN_LIMIT", str(cm.exception))
        self.assertIsNone(svc.store.order_for_task(tid))

    def test_double_click_after_acceptance_is_unaffected(self):
        rt, api = mock_runtime()
        svc = new_service(rt)
        tid = svc.request_task({"capability": "research_snapshot", "topic": Q, "corpus": "agentnomos_quantum"})
        d = svc.store.offer(task_id=tid)["_digest"]
        self.assertEqual(svc.accept_offer(tid, d), svc.accept_offer(tid, d))      # same order, same link


class T16UnauthorizedModelCall(unittest.TestCase):
    def test_no_call_without_a_started_execution_of_a_paid_task(self):
        rt, api = mock_runtime()
        svc = new_service(rt)
        c = svc.ai_client()
        tid = svc.request_task({"capability": "research_snapshot", "topic": Q, "corpus": "agentnomos_quantum"})
        for eid, task in (("EXEC-DOESNOTEXIST", tid), ("", ""), ("EXEC-X' OR '1'='1", tid)):
            with self.assertRaises(LedgerRefusal) as cm:
                c.call(execution_id=eid, task_id=task, seq=1, system="s", user="u", schema=SCHEMA, timeout_s=5)
            self.assertEqual(cm.exception.code, "UNAUTHORIZED_CALL")
        self.assertEqual(len(api.requests), 0)

    def test_finished_execution_cannot_call_again(self):
        rt, api = mock_runtime()
        svc = new_service(rt)
        tid = paid_task(svc)
        self.assertEqual(svc.execute(tid), S.DELIVERED)
        eid = svc.store.execution(tid)["execution_id"]
        with self.assertRaises(LedgerRefusal):
            svc.ai_client().call(execution_id=eid, task_id=tid, seq=2, system="s", user="u", schema=SCHEMA,
                                 timeout_s=5)
        self.assertEqual(len(api.requests), 1)

    def test_forced_execution_row_without_verified_capture_is_refused(self):
        """Defence in depth: even if a bug created an EXECUTING task with a STARTED execution, the ledger re-checks the
        capture verdict itself."""
        rt, api = mock_runtime()
        svc = new_service(rt)
        tid = svc.request_task({"capability": "research_snapshot", "topic": Q, "corpus": "agentnomos_quantum"})
        oid = svc.accept_offer(tid, svc.store.offer(task_id=tid)["_digest"]).rsplit("/", 1)[1]
        eid = svc.store.claim_execution(oid, tid)                                # no capture exists
        svc.store.db.execute("UPDATE tasks SET state=? WHERE task_id=?", (S.EXECUTING, tid))   # simulated bug
        with self.assertRaises(LedgerRefusal) as cm:
            svc.ai_client().call(execution_id=eid, task_id=tid, seq=1, system="s", user="u", schema=SCHEMA,
                                 timeout_s=5)
        self.assertEqual(cm.exception.code, "PAYMENT_NOT_VERIFIED")
        self.assertEqual(len(api.requests), 0)

    def test_payment_environment_must_be_authorized(self):
        rt, api = mock_runtime(allowed_payment_environments=("SANDBOX",))
        svc = new_service(rt)
        tid = svc.request_task({"capability": "research_snapshot", "topic": Q, "corpus": "agentnomos_quantum"})
        with self.assertRaises(GateError):                                        # layer 1: before any order
            svc.accept_offer(tid, svc.store.offer(task_id=tid)["_digest"])
        svc2, (t2,), api2, _ = pay_then_execute(1, allowed_payment_environments=("SANDBOX",))
        self.assertEqual(svc2.execute(t2), S.REVIEW_REQUIRED)                     # layer 2: the ledger itself
        self.assertEqual(len(api.requests) + len(api2.requests), 0)
        self.assertIn("PAYMENT_ENVIRONMENT_NOT_AUTHORIZED", json.dumps(
            svc2.ledger.record(svc2.store.execution(t2)["execution_id"])))

    def test_expired_authorization(self):
        svc, (tid,), api, rt = pay_then_execute(1, not_after=time.time() - 1)
        self.assertEqual(svc.execute(tid), S.REVIEW_REQUIRED)
        self.assertEqual(len(api.requests), 0)
        self.assertIn("AUTHORIZATION_EXPIRED", json.dumps(svc.ledger.record(svc.store.execution(tid)["execution_id"])))


class T24Labelling(unittest.TestCase):
    def test_injected_transport_is_always_mock(self):
        p = AnthropicMessagesProvider("claude-opus-5-5", transport=FakeMessagesAPI())
        self.assertEqual(p.execution_class, MOCK_EXECUTION)
        with self.assertRaises(ProviderFailure):
            AnthropicMessagesProvider("claude-opus-5-5", api_key=FAKE_KEY, transport=FakeMessagesAPI())
        with self.assertRaises(ProviderFailure):
            AnthropicMessagesProvider("claude-opus-5-5")                         # REAL needs the owner's key
        self.assertEqual(FixtureProviderR1d().execution_class, FIXTURE_EXECUTION)
        rec = p.complete("s", "u", SCHEMA, 100, 5)
        self.assertEqual((rec["execution_class"], rec["provider"]), (MOCK_EXECUTION, "anthropic_messages"))
        self.assertNotEqual(rec["transport"], API_URL)

    def test_envelope_and_provider_class_must_agree(self):
        rt, _ = mock_runtime()
        svc = new_service(rt)
        real_env = E.Envelope(authorization_id="AUTH-TEST-1", execution_class=REAL_MODEL_EXECUTION,
                              provider="anthropic_messages", model="claude-opus-5-5")
        with self.assertRaises(ValueError):
            GovernedModelClient(rt.provider, real_env, svc.ledger, "MOCK")    # a MOCK provider can't be filed as REAL

    def test_mock_and_fixture_receipts_say_so_and_tampered_labels_fail(self):
        for mode in ("fixture", "mock"):
            with self.subTest(mode):
                rt = aiconfig.offline_runtime(mode) if mode == "fixture" else mock_runtime()[0]
                svc = new_service(rt)
                tid = paid_task(svc)
                self.assertEqual(svc.execute(tid), S.DELIVERED)
                rc = svc.store.receipt(tid)
                art = svc.store.artifact(rc["body"]["artifact"]["digest"])
                ai = rc["body"]["ai_execution"]
                self.assertFalse(ai["real_model_inference"])
                self.assertEqual(ai["execution_class"], FIXTURE_EXECUTION if mode == "fixture" else MOCK_EXECUTION)
                self.assertTrue(receipt2.verify_any(rc, art, svc.store.events(tid))["valid"])
                forged = json.loads(json.dumps(rc))
                forged["body"]["ai_execution"].update(execution_class=REAL_MODEL_EXECUTION, real_model_inference=True)
                r = receipt2.verify_any(forged, art, svc.store.events(tid))
                self.assertFalse(r["checks"]["signature"])
                self.assertFalse(r["checks"]["execution_label_consistent_with_evidence"])
                forged["signature_hex"] = svc.signer.sign(forged["body"])        # even the issuer can't relabel
                r = receipt2.verify_any(forged, art, svc.store.events(tid))
                self.assertTrue(r["checks"]["signature"])
                self.assertFalse(r["valid"])
                self.assertFalse(r["checks"]["execution_label_consistent_with_evidence"])

    def test_real_label_requires_per_request_provider_evidence(self):
        """Unit test of the label checker on synthetic records (no REAL-labelled run is ever produced in tests)."""
        good = {"provider": "anthropic_messages", "transport": API_URL, "request_id": "req_abc",
                "response_id": "msg_abc", "usage": {"input_tokens": 1, "output_tokens": 1}, "model": "claude-opus-5-5"}
        ai = {"execution_class": REAL_MODEL_EXECUTION, "real_model_inference": True, "model_requests": 1,
              "authorization_ref": "AUTH-OWNER-1", "requested_model": "claude-opus-5-5"}
        self.assertTrue(receipt2._label_consistent(ai, {"ai_execution": {"requests": [good]}}))
        for bad in ({"transport": "test-fake"}, {"request_id": None}, {"response_id": "mock_1"},
                    {"usage": {}}, {"provider": "FIXTURE"}, {"model": "claude-sonnet-5-5"}):
            self.assertFalse(receipt2._label_consistent(ai, {"ai_execution": {"requests": [dict(good, **bad)]}}), bad)
        self.assertFalse(receipt2._label_consistent(dict(ai, authorization_ref="OFFLINE-MOCK"),
                                                    {"ai_execution": {"requests": [good]}}))
        self.assertFalse(receipt2._label_consistent(dict(ai, real_model_inference=False),
                                                    {"ai_execution": {"requests": [good]}}))


class RealProviderRequestShapeAndSecrets(unittest.TestCase):
    """The REAL code path, with its opener swapped for a recorder AFTER construction (test-only). Records are not
    stored anywhere and no REAL-labelled artifact or receipt is produced."""

    def _provider(self, api):
        p = AnthropicMessagesProvider("claude-opus-5-5", effort="medium", api_key=FAKE_KEY)
        self.assertEqual(p.execution_class, REAL_MODEL_EXECUTION)
        p._open = api
        return p

    def test_request_shape(self):
        api = FakeMessagesAPI()
        p = self._provider(api)
        p.complete("SYSTEM", "<question>q</question>", SCHEMA, 12000, 30)
        req = api.requests[0]
        h = {k.lower(): v for k, v in req.header_items()}
        body = json.loads(req.data)
        self.assertEqual(req.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(set(h), {"x-api-key", "anthropic-version", "content-type"})
        self.assertEqual(h["anthropic-version"], "2023-06-01")
        self.assertEqual(set(body), {"model", "max_tokens", "system", "messages", "output_config"})
        self.assertEqual(body["output_config"], {"format": {"type": "json_schema", "schema": SCHEMA},
                                                 "effort": "medium"})
        self.assertEqual(body["max_tokens"], 12000)
        self.assertEqual(len(body["messages"]), 1)

    def test_key_never_leaves_the_header(self):
        api = FakeMessagesAPI(("http", 401, "authentication_error"))
        p = self._provider(api)
        with self.assertRaises(ProviderFailure) as cm:
            p.complete("s", "u", SCHEMA, 100, 5)
        self.assertNotIn(FAKE_KEY, str(cm.exception) + repr(cm.exception.__dict__) + repr(p))
        self.assertNotIn(FAKE_KEY.encode(), api.requests[0].data)
        api2 = FakeMessagesAPI()
        rec = self._provider(api2).complete("s", "u", SCHEMA, 100, 5)
        self.assertNotIn(FAKE_KEY, json.dumps(rec))

    def test_real_transport_is_blocked_in_tests(self):
        with self.assertRaises(NetworkBlocked):
            socket.create_connection(("api.anthropic.com", 443), timeout=1)


FAKE_USR_KEY = "sk-ant-usr-TESTONLY-NOT-A-REAL-KEY-" + "0123456789abcdefABCDEF_-" * 3
FAKE_API_KEY = "sk-ant-api03-TESTONLY-NOT-A-REAL-KEY-" + "0123456789abcdefABCDEF_-" * 3
REFUSED_TOKENS = {
    "oauth_access": "sk-ant-oat01-TESTONLY-NOT-A-REAL-TOKEN-" + "a" * 60,
    "oauth_refresh": "sk-ant-ort01-TESTONLY-NOT-A-REAL-TOKEN-" + "b" * 60,
    "admin": "sk-ant-admin01-TESTONLY-NOT-A-REAL-KEY-" + "c" * 60,
    "session": "sk-ant-sid01-TESTONLY-NOT-A-REAL-KEY-" + "d" * 60,
    "unknown_family": "sk-ant-xyz01-TESTONLY-NOT-A-REAL-KEY-" + "e" * 60,
    "api_without_version": "sk-ant-api-TESTONLY-" + "f" * 60,
    "usr_too_short": "sk-ant-usr-short",
    "usr_bad_chars": "sk-ant-usr-TESTONLY/" + "g" * 40,
    "usr_bad_char_after_valid_prefix": "sk-ant-usr-TESTONLY" + "m" * 30 + "/x",
    "internal_space": "sk-ant-usr-TESTONLY " + "h" * 40,
    "two_lines": "sk-ant-usr-TESTONLY" + "i" * 40 + "\nsk-ant-api03-" + "j" * 40,
    "bearer_prefixed": "Bearer sk-ant-api03-" + "k" * 40,
    "non_ascii": "sk-ant-usr-TESTONLY" + "é" * 40,
    "not_anthropic": "sk-proj-TESTONLY-" + "l" * 60,
    "empty": "",
}


class ApiKeyFamilies(unittest.TestCase):
    """R1d.1: accept only the Console API key families (sk-ant-apiNN-, sk-ant-usr-), refuse OAuth / refresh / admin /
    session tokens and anything unknown, send accepted keys only as x-api-key, never echo a value."""

    def test_accepted_families(self):
        self.assertEqual(aiconfig.classify_api_key(FAKE_API_KEY), (True, "console_api_key"))
        self.assertEqual(aiconfig.classify_api_key(FAKE_USR_KEY), (True, "console_personal_key_usr"))
        self.assertEqual(aiconfig.classify_api_key(FAKE_KEY), (True, "console_api_key"))     # the suite's own key

    def test_refused_tokens_with_named_reasons(self):
        named = {"oauth_access": "OAuth access token", "oauth_refresh": "OAuth refresh token", "admin": "Admin API key",
                 "session": "session key"}
        for name, tok in REFUSED_TOKENS.items():
            with self.subTest(name):
                ok, label = aiconfig.classify_api_key(tok)
                self.assertFalse(ok)
                if tok:
                    self.assertNotIn(tok, label)
                    self.assertNotIn(tok[12:30], label)
                if name in named:
                    self.assertIn(named[name], label)

    def _owner_files(self, key):
        d = tempfile.mkdtemp(prefix="r1d1_owner_")                                # outside the repository
        auth, kf = os.path.join(d, "ai_authorization.json"), os.path.join(d, "anthropic.key")
        a = {"authorization_id": "AUTH-R1D1-KEYS", "mode": "REAL_MODEL_EXECUTION", "provider": "anthropic_messages",
             "model": "claude-opus-5-5", "effort": "medium", "max_executions": 1, "max_requests_total": 1,
             "max_requests_per_execution": 1, "max_input_tokens_per_request": 34000,
             "max_output_tokens_per_request": 12000, "max_total_tokens": 46000, "max_usd_total": "0.50",
             "price_usd_per_mtok": {"input": "4.40", "output": "22.00"}, "allowed_payment_environments": ["MOCK"],
             "request_timeout_s": 600, "not_after": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600)),
             "bound_data_dir": os.path.join(d, "out", "var"),
             "owner_statement": "Offline test authorization; no request is ever sent by this test."}
        for path, text in ((auth, json.dumps(a)), (kf, key + "\n")):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, text.encode())
            os.close(fd)
        env = {"AUTONOMOS_R1D_AI_PROVIDER": "anthropic_real", "AUTONOMOS_R1D_AI_AUTHORIZATION": auth,
               "AUTONOMOS_DATA_DIR": os.path.join(d, "out", "var"), "AUTONOMOS_ANTHROPIC_KEY_FILE": kf}
        return d, env

    def test_preflight_and_runtime_accept_usr_key(self):
        d, env = self._owner_files(FAKE_USR_KEY)
        out, code = preflight(env, PROJECT_ROOT)                                  # no GO: blocked only by the GO
        self.assertEqual((out["verdict"], out["key_family"]), ("REAL_PROVIDER_BLOCKED", "console_personal_key_usr"))
        self.assertTrue(out["real_checks"]["key_file_format"])
        self.assertEqual([k for k, v in out["real_checks"].items() if not v], ["owner_go_matches_authorization"])
        go = dict(env, AUTONOMOS_R1D_REAL_AI_GO="real-ai:AUTH-R1D1-KEYS")
        out, code = preflight(go, PROJECT_ROOT)
        self.assertEqual((out["verdict"], code, out["network_calls"]), ("REAL_PROVIDER_READY", 0, 0))
        self.assertNotIn(FAKE_USR_KEY, json.dumps(out))
        rt = aiconfig.runtime_from_env(go, PROJECT_ROOT)
        self.assertEqual((rt.provider.execution_class, rt.provider.calls), (REAL_MODEL_EXECUTION, 0))
        self.assertNotIn(FAKE_USR_KEY, repr(rt.provider) + repr(rt.envelope))

    def test_preflight_and_runtime_refuse_oauth_and_unknown_tokens_even_without_go(self):
        for name in ("oauth_access", "oauth_refresh", "admin", "session", "unknown_family"):
            with self.subTest(name):
                d, env = self._owner_files(REFUSED_TOKENS[name])
                out, code = preflight(env, PROJECT_ROOT)
                self.assertFalse(out["real_checks"]["key_file_format"])
                self.assertTrue(any("key file refused" in b for b in out["blocking"]))
                self.assertNotIn(REFUSED_TOKENS[name], json.dumps(out))
                go = dict(env, AUTONOMOS_R1D_REAL_AI_GO="real-ai:AUTH-R1D1-KEYS")
                out, code = preflight(go, PROJECT_ROOT)
                self.assertEqual((out["verdict"], code), ("REAL_PROVIDER_BLOCKED", 1))
                with self.assertRaises(aiconfig.AIConfigError) as cm:
                    aiconfig.runtime_from_env(go, PROJECT_ROOT)
                self.assertNotIn(REFUSED_TOKENS[name], str(cm.exception))

    def test_usr_key_is_sent_only_as_x_api_key(self):
        p = AnthropicMessagesProvider("claude-opus-5-5", effort="medium", api_key=FAKE_USR_KEY)
        self.assertEqual(p.execution_class, REAL_MODEL_EXECUTION)
        api = FakeMessagesAPI()
        p._open = api                                                             # recorder, test-only
        rec = p.complete("SYSTEM", "<question>q</question>", SCHEMA, 100, 5)
        h = {k.lower(): v for k, v in api.requests[0].header_items()}
        self.assertEqual(set(h), {"x-api-key", "anthropic-version", "content-type"})  # no Authorization, no beta
        self.assertEqual(h["x-api-key"], FAKE_USR_KEY)
        self.assertNotIn(FAKE_USR_KEY.encode(), api.requests[0].data)
        self.assertNotIn(FAKE_USR_KEY, json.dumps(rec) + repr(p))

    # R1e: the R1d.1 Test 1 launcher is not part of r1e (Test 1 is spent). This check now rehearses the R1e unified-demo
    # launcher with the same assertions, and a missing launcher FAILS instead of skipping (the R1d.2 "283 tests, 1
    # skipped" ambiguity). The mutation runner passes the launcher path in AUTONOMOS_R1E_LAUNCHER.
    def test_launcher_dry_run_accepts_usr_and_refuses_oauth(self):
        import subprocess
        from r1e_launcher_support import LAUNCHER, owner_files
        self.assertTrue(os.path.isfile(LAUNCHER), f"launcher missing: {LAUNCHER}")
        for key, expect_ok in ((FAKE_USR_KEY, True), (REFUSED_TOKENS["oauth_access"], False)):
            with self.subTest(expect_ok=expect_ok):
                d, run_env = owner_files(key)
                r = subprocess.run(["bash", LAUNCHER, "--dry-run"], env=run_env, capture_output=True, text=True,
                                   timeout=120)
                text = r.stdout + r.stderr
                self.assertNotIn(key, text)
                self.assertFalse(os.path.exists(run_env["OUT"]))
                if expect_ok:
                    self.assertEqual(r.returncode, 0, text)
                    self.assertIn("format OK (console_personal_key_usr)", text)
                    self.assertIn("key family (app preflight): console_personal_key_usr", text)
                    self.assertIn("DRY RUN COMPLETE", text)
                else:
                    self.assertEqual(r.returncode, 2, text)
                    self.assertIn("format NOT OK (OAuth access token", text)


class AuthorizationAndPreflight(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="r1d_owner_")                         # outside the repository
        self.auth = os.path.join(self.dir, "ai_authorization.json")
        self.key = os.path.join(self.dir, "anthropic.key")
        self.base = {"authorization_id": "AUTH-R1D-TEST-01", "mode": "REAL_MODEL_EXECUTION",
                     "provider": "anthropic_messages", "model": "claude-opus-5-5", "effort": "medium",
                     "max_executions": 1, "max_requests_total": 1, "max_requests_per_execution": 1,
                     "max_input_tokens_per_request": 34000, "max_output_tokens_per_request": 12000,
                     "max_total_tokens": 46000, "max_usd_total": "0.50",
                     "price_usd_per_mtok": {"input": "4.00", "output": "20.00"},
                     "allowed_payment_environments": ["MOCK"], "request_timeout_s": 120,
                     "not_after": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 86400)),
                     "bound_data_dir": os.path.join(self.dir, "data"),
                     "owner_statement": "I authorize one real model request for an offline-paid MOCK task."}
        self.write(self.base)
        fd = os.open(self.key, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, (FAKE_KEY + "\n").encode())
        os.close(fd)
        os.makedirs(os.path.join(self.dir, "data"))
        self.env = {"AUTONOMOS_R1D_AI_PROVIDER": "anthropic_real", "AUTONOMOS_R1D_AI_AUTHORIZATION": self.auth,
                    "AUTONOMOS_DATA_DIR": os.path.join(self.dir, "data"),
                    "AUTONOMOS_ANTHROPIC_KEY_FILE": self.key, "AUTONOMOS_R1D_REAL_AI_GO": "real-ai:AUTH-R1D-TEST-01"}

    def write(self, d, mode=0o600):
        if os.path.exists(self.auth):
            os.unlink(self.auth)
        fd = os.open(self.auth, os.O_WRONLY | os.O_CREAT, mode)
        os.write(fd, json.dumps(d).encode())
        os.close(fd)
        os.chmod(self.auth, mode)

    def test_ready_only_with_every_precondition_and_no_request_made(self):
        out, code = preflight(self.env, PROJECT_ROOT)
        self.assertEqual((out["verdict"], code, out["network_calls"]), ("REAL_PROVIDER_READY", 0, 0))
        self.assertNotIn(FAKE_KEY, json.dumps(out))
        self.assertEqual(Decimal(out["cost"]["worst_case_authorization_usd"]), Decimal("0.376"))
        self.assertTrue(out["real_checks"]["envelope_fits_largest_prompt"])
        rt = aiconfig.runtime_from_env(self.env, PROJECT_ROOT)
        self.assertEqual(rt.provider.execution_class, REAL_MODEL_EXECUTION)   # constructed, never called here
        self.assertEqual(rt.provider.calls, 0)
        self.assertNotIn(FAKE_KEY, repr(rt.provider) + repr(rt.envelope))

    def test_each_missing_precondition_blocks(self):
        variants = {
            "no_go": dict(self.env, AUTONOMOS_R1D_REAL_AI_GO=""),
            "wrong_go": dict(self.env, AUTONOMOS_R1D_REAL_AI_GO="real-ai:OTHER"),
            "api_key_in_env": dict(self.env, ANTHROPIC_API_KEY="x"),
            "legacy_provider": dict(self.env, AUTONOMOS_AI_PROVIDER="anthropic_api"),
            "model_override": dict(self.env, AUTONOMOS_R1D_MODEL="claude-fable-5-1"),
            "other_data_dir": dict(self.env, AUTONOMOS_DATA_DIR=tempfile.mkdtemp()),
            "envelope_too_small_for_prompt": "SMALL",
            "no_key_file": {k: v for k, v in self.env.items() if k != "AUTONOMOS_ANTHROPIC_KEY_FILE"},
            "no_authorization": {k: v for k, v in self.env.items() if k != "AUTONOMOS_R1D_AI_AUTHORIZATION"},
        }
        for name, env in variants.items():
            with self.subTest(name):
                if env == "SMALL":
                    self.write(dict(self.base, max_input_tokens_per_request=20000, max_total_tokens=32000))
                    env = self.env
                else:
                    self.write(self.base)
                out, code = preflight(env, PROJECT_ROOT)
                self.assertEqual((out["verdict"], code), ("REAL_PROVIDER_BLOCKED", 1))
                if name != "envelope_too_small_for_prompt":       # runtime build does not re-check; preflight does
                    with self.assertRaises(aiconfig.AIConfigError):
                        aiconfig.runtime_from_env(env, PROJECT_ROOT)

    def test_real_runtime_is_bound_to_its_database(self):
        rt = aiconfig.runtime_from_env(self.env, PROJECT_ROOT)
        from autonomos.r1d.service import R1dService
        for d, ok in ((tempfile.mkdtemp(), False), (os.path.join(self.dir, "data"), True)):
            cfg = Config.from_env(data_dir=d, payment_provider="mock", ai_provider="fixture")
            if ok:
                svc = R1dService(cfg, runtime=rt)                            # constructed; no request is made
                self.assertEqual(svc.runtime.provider.calls, 0)
            else:
                with self.assertRaises(GateError):
                    R1dService(cfg, runtime=rt)

    def test_authorization_file_rules(self):
        bad = {
            "price_below_reference": dict(self.base, price_usd_per_mtok={"input": "1.00", "output": "5.00"}),
            "price_missing": {k: v for k, v in self.base.items() if k != "price_usd_per_mtok"},
            "expired": dict(self.base, not_after="2020-01-01T00:00:00Z"),
            "envelope_exceeds_usd": dict(self.base, max_usd_total="0.10"),
            "over_hard_ceiling": dict(self.base, max_requests_total=1000),
            "mode_wrong": dict(self.base, mode="MOCK_EXECUTION"),
            "zero_price_unknown_model": dict(self.base, model="claude-future-9",
                                            price_usd_per_mtok={"input": "0", "output": "0"}),
            "bad_env": dict(self.base, allowed_payment_environments=["LIVE"]),
            "relative_data_dir": dict(self.base, bound_data_dir="var"),
        }
        for name, d in bad.items():
            with self.subTest(name):
                self.write(d)
                with self.assertRaises(E.EnvelopeError):
                    E.load_authorization(self.auth, PROJECT_ROOT)
        self.write(self.base, mode=0o644)
        with self.assertRaises(E.EnvelopeError):
            E.load_authorization(self.auth, PROJECT_ROOT)
        self.write(dict(self.base, model="claude-future-9"))                     # unknown model: owner price accepted
        env = E.load_authorization(self.auth, PROJECT_ROOT)
        self.assertIn("not in the reference table", env.price_basis)

    def test_authorization_inside_repository_refused(self):
        inside = os.path.join(APP, "var_test_auth.json")
        try:
            fd = os.open(inside, os.O_WRONLY | os.O_CREAT, 0o600)
            os.write(fd, json.dumps(self.base).encode())
            os.close(fd)
            with self.assertRaises(E.EnvelopeError) as cm:
                E.load_authorization(inside, PROJECT_ROOT)
            self.assertIn("inside the project repository", str(cm.exception))
        finally:
            os.unlink(inside)

    def test_offline_preflight_reports_cost_unknown(self):
        out, code = preflight({}, PROJECT_ROOT)
        self.assertEqual((out["verdict"], code, out["network_calls"]), ("OFFLINE_MODE_FIXTURE", 0, 0))
        self.assertTrue(out["cost"]["estimation"].startswith("UNKNOWN"))
        self.assertEqual(out["real_provider_if_requested"], "BLOCKED")


class CliRealDemoRefusals(unittest.TestCase):
    """The owner-run REAL command refuses without confirmation and without a READY preflight. Only refusal paths are
    exercised: the READY path would make a billed request and is never run in tests."""

    def _run(self, *extra, env=None):
        import subprocess
        out = os.path.join(tempfile.mkdtemp(), "run")
        e = {"PATH": os.environ.get("PATH", ""), "AUTONOMOS_PAYMENT_PROVIDER": "mock",
             "AUTONOMOS_DATA_DIR": os.path.join(out, "var"), **(env or {})}
        r = subprocess.run([sys.executable, "-B", "-m", "autonomos", "demo", out, "--ai", "anthropic_real", *extra],
                           cwd=APP, env=e, capture_output=True, text=True, timeout=120)
        return r, out

    def test_needs_owner_confirm(self):
        r, out = self._run()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--owner-confirm", r.stderr)
        self.assertFalse(os.path.exists(out))

    def test_needs_ready_preflight(self):
        r, out = self._run("--owner-confirm", env={"AUTONOMOS_R1D_AI_PROVIDER": "anthropic_real"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ai-preflight is not READY", r.stderr)
        self.assertFalse(os.path.exists(out))

    def test_needs_matching_data_dir(self):
        r, out = self._run("--owner-confirm", env={"AUTONOMOS_DATA_DIR": tempfile.mkdtemp()})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("bound_data_dir", r.stderr)


class LegacyPathsDisabled(unittest.TestCase):
    def test_r1d_service_refuses_legacy_ai_selectors(self):
        for legacy in ("claude_cli", "anthropic_api"):
            cfg = Config.from_env(data_dir=tempfile.mkdtemp(), payment_provider="mock", ai_provider=legacy,
                                  anthropic_api_key=FAKE_KEY)
            with self.assertRaises(GateError):
                from autonomos.r1d.service import R1dService
                R1dService(cfg, runtime=aiconfig.offline_runtime("fixture"))

    def test_every_cli_service_entry_point_builds_r1d_service(self):
        src = open(os.path.join(APP, "autonomos", "__main__.py")).read()
        self.assertNotIn("AutoNomos(", src)
        self.assertGreaterEqual(src.count("_r1d_service("), 6)

    def test_r1d_modules_have_no_shell_or_payment_privileges(self):
        import ast
        forbidden_imports = {"subprocess", "shutil", "socket", "ctypes", "pty", "multiprocessing"}
        forbidden_names = {"system", "popen", "execv", "execve", "spawnv", "fork"}
        for mod in ("engine", "verifier", "providers", "governed", "ledger", "envelope", "receipt2"):
            tree = ast.parse(open(os.path.join(APP, "autonomos", "r1d", f"{mod}.py")).read())
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    self.assertFalse({a.name.split(".")[0] for a in n.names} & forbidden_imports, mod)
                if isinstance(n, ast.ImportFrom):
                    self.assertFalse((n.module or "").split(".")[0] in forbidden_imports, mod)
                    self.assertNotIn("payments", n.module or "", mod)
                if isinstance(n, ast.Attribute):
                    self.assertNotIn(n.attr, forbidden_names, mod)
        tree = ast.parse(open(os.path.join(APP, "autonomos", "r1d", "engine.py")).read())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
            {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertFalse(names & {"store", "payments", "open", "environ", "signer", "ledger", "db"}, names)

    def test_ledger_never_writes_payment_tables(self):
        import re
        src = open(os.path.join(APP, "autonomos", "r1d", "ledger.py")).read()
        targets = set(re.findall(r"(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+(\w+)", src))
        self.assertTrue(targets)
        self.assertTrue(all(t.startswith("r1d_") for t in targets), targets)


if __name__ == "__main__":
    unittest.main()
