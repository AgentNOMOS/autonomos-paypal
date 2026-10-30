"""R1c release-gate tests: the scenarios of the original 14-case list (A-N) that the sealed R1c suite covered only
partially or indirectly. Kept in a separate file so the sealed R1c files stay byte-identical.

Gate letters follow the original list (NOT the letters inside test_r1c_cancel_integrity.py):
  D  capture succeeds, response is lost, then the cancel callback arrives
  F  repeated cancel callbacks
  G  return and cancel callbacks race (separate OS processes)
  H  an R1c explicit cancel later meets a provider capture with NO local attempt
  I  duplicate buyer checkout attempt (service and real HTTP server)
  J  service restart (process death) in the middle of uncertain-capture recovery
  L  captured payment, AI task fails, then cancel callback / explicit cancel / refreshes
  N  provider read temporarily unavailable, then available again

LOCAL ONLY: file-backed MOCK provider (shared by OS processes), FIXTURE AI. Not PayPal evidence.
Run from app/: python3 -B -m unittest tests.test_r1c_gate_scenarios -v
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from autonomos import receipt as R, states as S  # noqa: E402
from autonomos.ai import AIError  # noqa: E402
from autonomos.order_status import order_status  # noqa: E402
from autonomos.service import GateError  # noqa: E402
from test_r1_transactions import CTX, Q, make  # noqa: E402
from test_r1c_cancel_integrity import R1cBase, land_capture  # noqa: E402


def nav_notes(svc, tid):
    return [e for e in svc.store.events(tid) if e["evidence_kind"] == "checkout_navigation"]


def receipt_valid(svc, tid):
    rc = svc.store.receipt(tid)
    return R.verify(rc, svc.store.artifact(rc["body"]["artifact"]["digest"]), svc.store.events(tid))["valid"]


# ---------------------------------------------------------------- worker processes (module level for spawn)
def w_callbacks(d, oid, n, barrier, out):
    try:
        svc = make(d)
        barrier.wait()
        out.put(("callback", [svc.on_buyer_cancel(oid) for _ in range(n)]))
    except Exception as e:
        out.put(("callback", "EXC:" + type(e).__name__ + ":" + str(e)[:120]))


def w_return(d, oid, barrier, out):
    try:
        svc = make(d)
        barrier.wait()
        out.put(("return", svc.on_buyer_return(oid)))
    except Exception as e:
        out.put(("return", "EXC:" + type(e).__name__ + ":" + str(e)[:120]))


def w_recover_dies_after_read(d, oid):
    """A reconciler takes over the uncertain attempt, the provider GET answers, the process dies before recording."""
    svc = make(d, lease=0.3)
    real = svc.payments.get_order

    def get_then_die(order_id):
        real(order_id)
        os._exit(29)
    svc.payments.get_order = get_then_die
    svc.capture_status(oid)


class Broken:
    calls = 0

    def complete(self, *a, **k):
        Broken.calls += 1
        raise AIError("model unavailable")


# ================================================================ D: lost capture response, then cancel callback
class TestGateD(R1cBase):
    def test_D_lost_response_capture_invisible_then_callback_and_cancel(self):
        tid, oid = self.approved_order()
        self.pp.set_scenario(oid, "delayed:5")             # provider captured; response lost; next 5 GETs stale
        self.assertEqual(self.svc.on_buyer_return(oid), S.REVIEW_REQUIRED)
        self.assertEqual(self.pp.captures_performed(), 1)   # money moved at the provider
        self.assertEqual(self.svc.store.capture_attempt(oid)["state"], "OUTCOME_UNKNOWN")
        n = len(self.pp.calls)
        self.assertEqual(self.svc.on_buyer_cancel(oid), S.REVIEW_REQUIRED)        # the late cancel callback
        self.assertEqual(self.pp.calls[n:], [])                                    # no provider call
        with self.assertRaises(GateError):
            self.svc.cancel(tid)                                                   # explicit cancel refused too
        self.assertEqual(self.state(tid), S.REVIEW_REQUIRED)
        self.assertEqual(order_status(self.svc.cfg, oid, self.pp)["phase"], "CAPTURE_OUTCOME_UNKNOWN")
        for _ in range(6):                                                         # reconciliation by GET only
            if self.svc.capture_status(oid)["state"] == S.EXECUTION_AUTHORIZED:
                break
        self.assertEqual(self.state(tid), S.EXECUTION_AUTHORIZED)
        self.assertEqual(self.svc.execute(tid), S.DELIVERED)
        self.assertEqual((len(self.capture_calls()), self.pp.captures_performed()), (1, 1))
        self.assertTrue(receipt_valid(self.svc, tid))

    def test_D_lost_response_capture_visible_then_callback(self):
        tid, oid = self.approved_order()
        self.pp.set_scenario(oid, "timeout_after")         # captured, response lost, the next GET shows it
        self.assertEqual(self.svc.on_buyer_return(oid), S.EXECUTION_AUTHORIZED)
        self.assertEqual(self.svc.on_buyer_cancel(oid), S.EXECUTION_AUTHORIZED)
        with self.assertRaises(GateError):
            self.svc.cancel(tid)
        self.assertEqual(self.svc.execute(tid), S.DELIVERED)
        self.assertEqual((len(self.capture_calls()), self.pp.captures_performed()), (1, 1))


# ================================================================ F: repeated cancel callbacks
class TestGateF(R1cBase):
    def test_F_repeated_callbacks_before_during_and_after_payment(self):
        tid, oid = self.new_order()
        n = len(self.pp.calls)
        self.assertEqual([self.svc.on_buyer_cancel(oid) for _ in range(5)], [S.PAYMENT_PENDING] * 5)
        self.assertEqual(self.pp.calls[n:], [])
        self.pp.buyer_action(oid, "approve")
        self.assertEqual([self.svc.on_buyer_cancel(oid) for _ in range(3)], [S.PAYMENT_PENDING] * 3)
        self.assertEqual(self.svc.on_buyer_return(oid), S.EXECUTION_AUTHORIZED)
        self.assertEqual(self.svc.execute(tid), S.DELIVERED)
        self.assertEqual([self.svc.on_buyer_cancel(oid) for _ in range(3)], [S.DELIVERED] * 3)
        notes = nav_notes(self.svc, tid)
        self.assertEqual(len(notes), 11)
        self.assertTrue(all(e["from"] == e["to"] and e["detail"]["authoritative"] is False for e in notes))
        self.assertEqual((len(self.capture_calls()), self.pp.captures_performed()), (1, 1))
        self.assertTrue(receipt_valid(self.svc, tid))                              # hash chain intact

    def test_F_repeated_callbacks_and_explicit_cancels_after_explicit_cancel(self):
        tid, oid = self.new_order()
        self.assertEqual(self.svc.cancel(tid), S.CANCELLED)
        n = len(self.pp.calls)
        self.assertEqual([self.svc.cancel(tid) for _ in range(3)], [S.CANCELLED] * 3)      # idempotent, no GET
        self.assertEqual([self.svc.on_buyer_cancel(oid) for _ in range(3)], [S.CANCELLED] * 3)
        self.assertEqual(self.pp.calls[n:], [])
        self.assertEqual(sum(1 for e in self.svc.store.events(tid) if e["to"] == S.CANCELLED
                             and e["from"] != S.CANCELLED), 1)                    # exactly one cancellation


# ================================================================ G: return vs cancel callback, across processes
class TestGateG(unittest.TestCase):
    def test_G_callback_processes_race_return_processes(self):
        for _ in range(4):
            d = tempfile.mkdtemp(prefix="autonomos_gate_g_")
            svc = make(d)
            tid = svc.request_task({"capability": "research_snapshot", "topic": Q, "corpus": "agentnomos_quantum"})
            oid = svc.accept_offer(tid, svc.store.offer(task_id=tid)["_digest"]).rsplit("/", 1)[1]
            svc.payments.buyer_action(oid, "approve")
            b, out = CTX.Barrier(4), CTX.Queue()
            ps = [CTX.Process(target=w_callbacks, args=(d, oid, 3, b, out)) for _ in range(2)] + \
                 [CTX.Process(target=w_return, args=(d, oid, b, out)) for _ in range(2)]
            [p.start() for p in ps]
            [p.join(120) for p in ps]
            res = [out.get(timeout=5) for _ in ps]
            self.assertFalse([r for r in res if str(r[1]).startswith("EXC:")], res)
            self.assertEqual(svc.store.task(tid)["state"], S.EXECUTION_AUTHORIZED, res)
            self.assertEqual(svc.payments.captures_performed(), 1, res)
            self.assertEqual(len([c for c in svc.payments.calls if c[0] == "capture_order"]), 1, res)
            self.assertEqual(len(nav_notes(svc, tid)), 6, res)                     # every callback recorded
            self.assertEqual(svc.execute(tid), S.DELIVERED)
            self.assertTrue(receipt_valid(svc, tid))                               # chain survived the interleaving


# ================================================================ H: R1c explicit cancel, then an outside capture
class TestGateH(R1cBase):
    def test_H_capture_after_explicit_cancel_without_local_attempt(self):
        tid, oid = self.approved_order()
        self.assertEqual(self.svc.cancel(tid), S.CANCELLED)
        self.assertIsNone(self.svc.store.capture_attempt(oid))
        land_capture(self.pp, oid)                         # e.g. a dashboard capture; AutoNOMOS never sent one
        r = order_status(self.svc.cfg, tid, self.pp)
        self.assertEqual(r["phase"], "CAPTURE_VERIFIED_COMPLETED")
        self.assertIn("CAPTURE_ON_TERMINAL_TASK", r["alerts"])
        self.assertEqual(self.svc.on_buyer_return(oid), S.CANCELLED)
        self.assertEqual(self.svc.reconcile_task(tid)["state"], S.CANCELLED)
        res = self.svc.reconcile_cancelled(oid)
        self.assertEqual((res["state"], res["changed"], res["verdict"]), (S.REVIEW_REQUIRED, True, "CAPTURE_VERIFIED"))
        self.assertEqual(self.svc.on_buyer_return(oid), S.REVIEW_REQUIRED)
        with self.assertRaises(GateError):
            self.svc.execute(tid)
        self.assertEqual(self.capture_calls(), [])
        self.assertIsNone(self.svc.store.execution(tid))


# ================================================================ I: duplicate buyer checkout attempt
class TestGateI(R1cBase):
    def creates(self):
        return [c for c in self.pp.calls if c[0] == "create_order"]

    def test_I_duplicate_accepts_reuse_one_order_and_closed_tasks_refuse(self):
        tid = self.svc.request_task({"capability": "research_snapshot", "topic": Q, "corpus": "agentnomos_quantum"})
        dg = self.svc.store.offer(task_id=tid)["_digest"]
        link = self.svc.accept_offer(tid, dg)
        self.assertEqual(self.svc.accept_offer(tid, dg), link)                    # double click
        oid = link.rsplit("/", 1)[1]
        self.svc.on_buyer_cancel(oid)
        self.assertEqual(self.svc.accept_offer(tid, dg), link)                    # back from cancel_url: same order
        self.assertEqual(len(self.creates()), 1)
        self.pp.buyer_action(oid, "approve")
        self.assertEqual(self.svc.on_buyer_return(oid), S.EXECUTION_AUTHORIZED)
        with self.assertRaises(GateError):
            self.svc.accept_offer(tid, dg)                                         # paid: no new checkout
        t2 = self.svc.request_task({"capability": "research_snapshot", "topic": Q, "corpus": "agentnomos_quantum"})
        d2 = self.svc.store.offer(task_id=t2)["_digest"]
        self.svc.accept_offer(t2, d2)
        self.svc.cancel(t2)
        with self.assertRaises(GateError):
            self.svc.accept_offer(t2, d2)                                          # cancelled: no new checkout
        self.assertEqual(len(self.creates()), 2)
        self.assertEqual((len(self.capture_calls()), self.pp.captures_performed()), (1, 1))


class TestGateIHTTP(unittest.TestCase):
    from test_security import TestHTTPEndToEnd as _H
    setUp, tearDown, client, get, token, post, code = (_H.setUp, _H.tearDown, _H.client, _H.get, _H.token, _H.post,
                                                       _H.code)

    def test_I_double_submitted_accept_form_reaches_one_checkout(self):
        r = self.post("/tasks", "capability=research_snapshot&corpus=agentnomos_quantum&topic=" + Q.replace(" ", "+"))
        tid = r.geturl().rsplit("/", 1)[1]
        dg = self.svc.store.offer(task_id=tid)["_digest"]
        csrf = self.token(f"/tasks/{tid}")
        urls = [self.post(f"/tasks/{tid}/accept", f"offer_digest={dg}&csrf={csrf}", csrf_from=None).geturl()
                for _ in range(2)]
        self.assertEqual(urls[0], urls[1])
        self.assertEqual(len([c for c in self.svc.payments.calls if c[0] == "create_order"]), 1)
        self.assertEqual(self.svc.store.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)


# ================================================================ J: restart in the middle of uncertain recovery
class TestGateJ(R1cBase):
    def test_J_reconciler_dies_after_provider_read_then_restart_resolves_without_resend(self):
        tid, oid = self.approved_order()
        self.pp.set_scenario(oid, "delayed:4")
        self.assertEqual(self.svc.on_buyer_return(oid), S.REVIEW_REQUIRED)
        self.svc.on_buyer_cancel(oid)                                              # callback during the uncertainty
        p = CTX.Process(target=w_recover_dies_after_read, args=(self.d, oid))
        p.start()
        p.join(60)
        self.assertEqual(p.exitcode, 29)
        self.assertEqual(self.svc.store.capture_attempt(oid)["state"], "OUTCOME_UNKNOWN")
        time.sleep(0.4)                                                            # dead reconciler's lease expires
        svc2 = make(self.d)                                                        # the restarted service
        svc2.recover()
        for _ in range(5):
            if svc2.capture_status(oid)["state"] == S.EXECUTION_AUTHORIZED:
                break
        self.assertEqual(svc2.store.task(tid)["state"], S.EXECUTION_AUTHORIZED)
        self.assertEqual(svc2.execute(tid), S.DELIVERED)
        self.assertEqual((len(self.capture_calls()), self.pp.captures_performed()), (1, 1))
        self.assertEqual(svc2.store.capture_attempt(oid)["sends"], 1)


# ================================================================ L: captured payment, then the AI task fails
class TestGateL(R1cBase):
    def test_L_failed_paid_task_is_not_cancelled_rerun_or_recaptured(self):
        Broken.calls = 0
        self.svc.ai = Broken()
        tid, oid = self.approved_order()
        self.assertEqual(self.svc.on_buyer_return(oid), S.EXECUTION_AUTHORIZED)
        self.assertEqual(self.svc.execute(tid), S.REVIEW_REQUIRED)
        ai_calls = Broken.calls
        self.assertEqual(self.svc.on_buyer_cancel(oid), S.REVIEW_REQUIRED)        # callback: note only
        with self.assertRaises(GateError):
            self.svc.cancel(tid)                                                   # a recorded capture blocks it
        for refresh in (lambda: self.svc.on_buyer_return(oid), lambda: self.svc.capture_status(oid),
                        lambda: self.svc.reconcile_task(tid)):
            refresh()
            self.assertEqual(self.state(tid), S.REVIEW_REQUIRED)                  # review flag is not erased
            with self.assertRaises(GateError):
                self.svc.execute(tid)
        self.assertEqual(Broken.calls, ai_calls)                                   # paid AI work never re-run
        self.assertEqual(self.svc.store.execution(tid)["status"], "FAILED")
        self.assertNotEqual(self.state(tid), S.CANCELLED)
        self.assertIsNone(self.svc.store.receipt(tid))
        self.assertEqual(self.svc.store.capture(oid)["verification"]["verdict"], "CAPTURE_VERIFIED")
        self.assertEqual((len(self.capture_calls()), self.pp.captures_performed()), (1, 1))


# ================================================================ N: provider read temporarily unavailable
class TestGateN(R1cBase):
    def test_N_outage_blocks_everything_then_recovery_captures_once(self):
        for outage in ("rate_limit", "auth"):
            tid, oid = self.approved_order()
            if outage == "auth":
                self.pp.token_valid = False                                        # 401: fails closed to review
            else:
                self.pp.set_get_scenario(oid, "rate_limit:30")                     # 429s beyond the read bound
            want = S.REVIEW_REQUIRED if outage == "auth" else S.PAYMENT_PENDING
            self.assertEqual(self.svc.on_buyer_cancel(oid), S.PAYMENT_PENDING)
            self.assertEqual(self.svc.on_buyer_return(oid), want, outage)
            with self.assertRaises(GateError):
                self.svc.cancel(tid)
            r = order_status(self.svc.cfg, oid, self.pp)
            # Known, fail-closed limitation: the failed read leaves the unsent reservation's lease live, so the
            # diagnosis says CAPTURE_IN_PROGRESS (not PROVIDER_UNREADABLE) until the lease expires. Nothing was sent.
            self.assertEqual((r["provider"]["readable"], r["phase"]), (False, "CAPTURE_IN_PROGRESS"), outage)
            self.assertEqual(r["local"]["capture_attempt"]["sends"], 0, outage)
            self.assertEqual([c for c in self.capture_calls() if c[1].endswith(oid)], [], outage)
            self.pp.token_valid = True                                             # provider back
            self.pp.set_get_scenario(oid, "")
            self.assertEqual(self.svc.on_buyer_return(oid), S.EXECUTION_AUTHORIZED, outage)
            self.assertEqual(len([c for c in self.capture_calls() if c[1].endswith(oid)]), 1, outage)
        self.assertEqual(self.pp.captures_performed(), 2)

    def test_N_transient_read_errors_during_cancel_are_retried_within_bound(self):
        tid, oid = self.new_order()
        self.pp.set_get_scenario(oid, "rate_limit:2")                              # 2 x 429, then readable
        self.assertEqual(self.svc.cancel(tid), S.CANCELLED)
        self.assertEqual(self.svc.store.events(tid)[-1]["detail"]["provider_status"], "PAYER_ACTION_REQUIRED")


if __name__ == "__main__":
    unittest.main()
