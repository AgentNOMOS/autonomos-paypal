"""R1 P0 tests: durable capture reservation, reconciliation before retry, crash recovery, MULTI-PROCESS concurrency.

LOCAL ONLY: a file-backed MOCK provider shared by several OS processes, the FIXTURE AI. Not PayPal evidence.
Run from app/: python3 -B -m unittest tests.test_r1_transactions -v   (or discover)
"""
import json
import multiprocessing as mp
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from autonomos import receipt as R, states as S  # noqa: E402
from autonomos.ai import FixtureProvider  # noqa: E402
from autonomos.config import Config  # noqa: E402
from autonomos.payments.mock import MockPayPal, ProviderError  # noqa: E402
from autonomos.service import AutoNomos, GateError  # noqa: E402
from autonomos.store import ConcurrencyError  # noqa: E402

Q = "What hardware evidence exists for the quantum-kernel experiment, and what is still unverified?"
CTX = mp.get_context("spawn")


def make(d, lease=60.0, backoff=0.01, ai=None):
    svc = AutoNomos(Config.from_env(data_dir=d, payment_provider="mock", ai_provider="fixture"),
                    payments=MockPayPal(path=os.path.join(d, "mock_paypal.sqlite")), ai=ai or FixtureProvider())
    svc.capture_lease_s, svc.backoff_s = lease, backoff
    return svc


# ---------------------------------------------------------------- worker processes (module level for spawn)
def w_return_and_execute(d, oid, barrier, out):
    try:
        svc = make(d)
        barrier.wait()
        st = svc.on_buyer_return(oid)
        tid = svc.store.task_for_order(oid)
        try:
            st = svc.execute(tid)
        except GateError as e:
            st = "GATE:" + str(e)[:60]
    except Exception as e:                              # report, never die silently
        import traceback
        st = "EXC:" + type(e).__name__ + ":" + str(e)[:120] + " @ " + traceback.format_exc().splitlines()[-3][:160]
    out.put((os.getpid(), st))


def w_accept(d, tid, digest_, barrier, out):
    svc = make(d)
    barrier.wait()
    try:
        out.put(("LINK", svc.accept_offer(tid, digest_)))
    except GateError as e:
        out.put(("GATE", str(e)[:80]))


def w_capture_then_die(d, oid):
    """Owns the reservation, the provider performs the capture, the process dies before recording anything."""
    svc = make(d, lease=0.5)
    real = svc.payments.capture_order

    def capture_and_crash(order_id, request_id):
        real(order_id, request_id)
        os._exit(17)
    svc.payments.capture_order = capture_and_crash
    svc.on_buyer_return(oid)


class Crash:
    calls = 0

    def complete(self, role, system, user, max_seconds=60):
        if role == "compose":
            os._exit(19)
        return FixtureProvider().complete(role, system, user, max_seconds)


def w_execute_and_die(d, tid):
    svc = make(d, ai=Crash())
    svc.execute(tid)


def w_hold_reservation(d, oid, ready, release):
    svc = make(d, lease=30)
    v, _ = svc.store.reserve_capture(oid, lease_s=30)
    ready.put(v)
    release.wait(20)


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="autonomos_r1_")
        self.svc = make(self.d)
        self.pp = self.svc.payments

    def approved_order(self):
        tid = self.svc.request_task({"capability": "research_snapshot", "topic": Q, "corpus": "agentnomos_quantum"})
        link = self.svc.accept_offer(tid, self.svc.store.offer(task_id=tid)["_digest"])
        oid = link.rsplit("/", 1)[1]
        self.pp.buyer_action(oid, "approve")
        return tid, oid

    def capture_calls(self):
        return [c for c in self.pp.calls if c[0] == "capture_order"]


class TestMultiProcess(Base):
    def test_six_processes_return_and_execute_concurrently(self):
        tid, oid = self.approved_order()
        b, out = CTX.Barrier(6), CTX.Queue()
        ps = [CTX.Process(target=w_return_and_execute, args=(self.d, oid, b, out)) for _ in range(6)]
        [p.start() for p in ps]
        [p.join(120) for p in ps]
        results = [out.get(timeout=5) for _ in ps]
        self.assertFalse([r for _, r in results if str(r).startswith("EXC:")], results)
        self.assertEqual(self.pp.captures_performed(), 1)
        self.assertEqual({c[1] for c in self.capture_calls()}, {"autonomos-capture-" + oid})
        self.assertEqual(sum(1 for _, r in results if r == S.DELIVERED), 1, results)
        n_exec = self.svc.store.db.execute("SELECT COUNT(*) FROM executions WHERE order_id=?", (oid,)).fetchone()[0]
        self.assertEqual(n_exec, 1)
        self.assertEqual(self.svc.store.task(tid)["state"], S.DELIVERED)
        rc = self.svc.store.receipt(tid)
        self.assertTrue(R.verify(rc, self.svc.store.artifact(rc["body"]["artifact"]["digest"]),
                                 self.svc.store.events(tid))["valid"])

    def test_five_processes_accept_the_same_offer(self):
        tid = self.svc.request_task({"capability": "research_snapshot", "topic": Q, "corpus": "agentnomos_quantum"})
        dg = self.svc.store.offer(task_id=tid)["_digest"]
        b, out = CTX.Barrier(5), CTX.Queue()
        ps = [CTX.Process(target=w_accept, args=(self.d, tid, dg, b, out)) for _ in range(5)]
        [p.start() for p in ps]
        [p.join(60) for p in ps]
        res = [out.get(timeout=5) for _ in ps]
        self.assertEqual(len([c for c in self.pp.calls if c[0] == "create_order"]), 1, res)
        self.assertEqual(self.svc.store.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)
        self.assertEqual(self.svc.store.task(tid)["state"], S.PAYMENT_PENDING)

    def test_crash_after_provider_capture_is_recovered_without_a_second_capture(self):
        tid, oid = self.approved_order()
        p = CTX.Process(target=w_capture_then_die, args=(self.d, oid))
        p.start()
        p.join(60)
        self.assertEqual(p.exitcode, 17)
        self.assertEqual(self.pp.captures_performed(), 1)
        self.assertEqual(self.svc.store.capture_attempt(oid)["state"], "SENT")      # outcome never recorded
        self.assertEqual(self.svc.store.task(tid)["state"], S.PAYMENT_PENDING)
        time.sleep(0.6)                                                              # dead owner's lease expires
        rep = self.svc.recover()
        self.assertEqual(rep["captures"], [(tid, S.EXECUTION_AUTHORIZED)])
        self.assertEqual(len(self.capture_calls()), 1)                              # reconciled by GET, not resent
        self.assertEqual(self.pp.captures_performed(), 1)
        self.assertEqual(self.svc.store.capture_attempt(oid)["state"], "RESOLVED")
        self.assertEqual(self.svc.execute(tid), S.DELIVERED)

    def test_live_lease_in_another_process_blocks_capture(self):
        tid, oid = self.approved_order()
        ready, release = CTX.Queue(), CTX.Event()
        p = CTX.Process(target=w_hold_reservation, args=(self.d, oid, ready, release))
        p.start()
        self.assertEqual(ready.get(timeout=30), "OWNER")
        self.assertEqual(self.svc.on_buyer_return(oid), S.PAYMENT_PENDING)          # BUSY: no capture from us
        self.assertEqual(self.capture_calls(), [])
        release.set()
        p.join(30)

    def test_crash_during_paid_execution_is_never_rerun(self):
        tid, oid = self.approved_order()
        self.svc.on_buyer_return(oid)
        p = CTX.Process(target=w_execute_and_die, args=(self.d, tid))
        p.start()
        p.join(60)
        self.assertEqual(p.exitcode, 19)
        self.assertEqual(self.svc.store.task(tid)["state"], S.EXECUTING)
        rep = self.svc.recover(stale_execution_s=0)
        self.assertEqual(rep["executions"], [tid])
        self.assertEqual(self.svc.store.task(tid)["state"], S.REVIEW_REQUIRED)
        self.assertEqual(self.svc.store.execution(tid)["status"], "ABANDONED")
        with self.assertRaises(GateError):
            self.svc.execute(tid)


class TestReconcileBeforeRetry(Base):
    def test_lost_response_after_capture_is_reconciled_not_resent(self):
        tid, oid = self.approved_order()
        self.pp.set_scenario(oid, "timeout_after")
        self.assertEqual(self.svc.on_buyer_return(oid), S.EXECUTION_AUTHORIZED)
        self.assertEqual(len(self.capture_calls()), 1)
        self.assertEqual(self.pp.captures_performed(), 1)
        a = self.svc.store.capture_attempt(oid)
        self.assertEqual((a["state"], a["sends"], a["last_error"]), ("RESOLVED", 1, "TIMEOUT"))

    # R1b: the three R1 tests that expected an AUTOMATIC re-send after timeout / 429 were superseded. The documented
    # PayPal contract does not establish that safety (see R1_04), so ambiguity now parks the attempt for the owner.
    def test_request_lost_before_provider_is_not_resent_automatically(self):
        tid, oid = self.approved_order()
        self.pp.set_scenario(oid, "timeout_before")
        self.assertEqual(self.svc.on_buyer_return(oid), S.REVIEW_REQUIRED)
        self.assertEqual([c[1] for c in self.capture_calls()], ["autonomos-capture-" + oid])
        a = self.svc.store.capture_attempt(oid)
        self.assertEqual((a["state"], a["sends"]), ("OUTCOME_UNKNOWN", 1))
        self.assertEqual(self.pp.captures_performed(), 0)
        with self.assertRaises(GateError):                                      # owner re-send: too early
            self.svc.owner_resend_capture(oid)
        self.assertEqual(self.svc.owner_resend_capture(oid, now=a["first_sent"] + 301), S.EXECUTION_AUTHORIZED)
        self.assertEqual([c[1] for c in self.capture_calls()], ["autonomos-capture-" + oid] * 2)   # SAME id
        self.assertEqual(self.pp.captures_performed(), 1)

    def test_rate_limited_capture_is_unknown_and_records_debug_id(self):
        tid, oid = self.approved_order()
        self.pp.set_scenario(oid, "rate_limit:2")
        self.assertEqual(self.svc.on_buyer_return(oid), S.REVIEW_REQUIRED)
        self.assertEqual(len(self.capture_calls()), 1)
        a = self.svc.store.capture_attempt(oid)
        self.assertEqual((a["state"], a["debug_id"], a["last_error"]), ("OUTCOME_UNKNOWN", "mockdbg429", "RATE_LIMIT_REACHED"))

    def test_persistent_unknown_outcome_stops_without_new_attempts(self):
        tid, oid = self.approved_order()
        self.pp.set_scenario(oid, "timeout_never")
        self.assertEqual(self.svc.on_buyer_return(oid), S.REVIEW_REQUIRED)
        for _ in range(3):                                                      # refreshes, recoveries, webhooks
            self.svc.on_buyer_return(oid)
            self.svc.recover()
            self.svc.reconcile_task(tid)
        self.assertEqual(len(self.capture_calls()), 1)
        self.assertEqual(self.svc.store.capture_attempt(oid)["state"], "OUTCOME_UNKNOWN")
        self.assertEqual(self.pp.captures_performed(), 0)
        self.assertIn("capture_outcome_unknown", [e["evidence_kind"] for e in self.svc.store.events(tid)])

    def test_instrument_declined_is_recoverable_with_a_new_generation(self):
        tid, oid = self.approved_order()
        self.pp.set_scenario(oid, "declined_instrument")
        self.assertEqual(self.svc.on_buyer_return(oid), S.PAYMENT_PENDING)          # not FAILED
        a = self.svc.store.capture_attempt(oid)
        self.assertEqual((a["state"], a["last_error"], a["debug_id"]), ("RELEASED", "INSTRUMENT_DECLINED", "mockdbg422c"))
        self.assertIn("provider_refusal", [e["evidence_kind"] for e in self.svc.store.events(tid)])
        self.assertEqual(self.svc.on_buyer_return(oid), S.PAYMENT_PENDING)          # not re-approved: no new capture
        self.pp.buyer_action(oid, "approve")                                        # buyer picks another source
        self.assertEqual(self.svc.on_buyer_return(oid), S.PAYMENT_PENDING)          # refresh/webhook: still no capture
        self.svc.reconcile_task(tid)
        self.assertEqual(len(self.capture_calls()), 1)
        self.assertEqual(self.svc.on_buyer_reapproved(oid), S.EXECUTION_AUTHORIZED)  # explicit buyer confirmation
        self.assertEqual([c[1] for c in self.capture_calls()],
                         ["autonomos-capture-" + oid, "autonomos-capture-" + oid + "-g2"])
        self.assertEqual(self.pp.captures_performed(), 1)
        with self.assertRaises(GateError):
            self.svc.on_buyer_reapproved(oid)


class TestRowCountsAndTransactions(Base):
    def test_zero_row_updates_stop(self):
        tid, oid = self.approved_order()
        v, a = self.svc.store.reserve_capture(oid)
        self.assertEqual(v, "OWNER")
        other = make(self.d)                                        # a second process identity on the same DB
        with self.assertRaises(ConcurrencyError):
            other.store.capture_mark(oid, ("RESERVED",), "SENT", sent=True)
        self.assertEqual(other.store.reserve_capture(oid)[0], "BUSY")
        self.svc.on_buyer_return(oid)
        self.svc.execute(tid)
        eid = self.svc.store.execution(tid)["execution_id"]
        with self.assertRaises(ConcurrencyError):
            self.svc.store.finish_execution(eid, "VERIFIED", "x")
        self.assertFalse(self.svc.store.accept_offer(self.svc.store.offer(task_id=tid)["offer_id"]))

    def test_no_database_transaction_is_open_during_provider_calls(self):
        tid, oid = self.approved_order()
        db_path = os.path.join(self.d, "autonomos.sqlite")
        real = self.pp.capture_order
        seen = []

        def probe(order_id, request_id):
            c = sqlite3.connect(db_path, timeout=0.2, isolation_level=None)
            c.execute("BEGIN IMMEDIATE")                             # would fail if the app held the write lock
            c.execute("ROLLBACK")
            c.close()
            seen.append(True)
            return real(order_id, request_id)
        self.pp.capture_order = probe
        self.assertEqual(self.svc.on_buyer_return(oid), S.EXECUTION_AUTHORIZED)
        self.assertEqual(seen, [True])

    def test_webhook_acks_fast_and_reconciles_separately(self):
        tid, oid = self.approved_order()
        raw = json.dumps({"id": "WH-R1", "event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": oid}}).encode()
        h = {"PAYPAL-TRANSMISSION-ID": "TX-R1", "PAYPAL-TRANSMISSION-TIME": "t"}
        h["PAYPAL-TRANSMISSION-SIG"] = self.pp.sign_webhook("TX-R1", "t", raw)
        r = self.svc.handle_webhook(h, raw, reconcile=False)
        self.assertTrue(r["queued"])
        self.assertEqual(self.capture_calls(), [])                 # nothing happened inside the ack
        self.assertEqual(self.svc.reconcile_task(tid)["state"], S.EXECUTION_AUTHORIZED)
        self.assertEqual(self.pp.captures_performed(), 1)


if __name__ == "__main__":
    unittest.main()
