"""R1d payment-safety, recovery, tampering and compatibility tests (mission scenarios 17-23 and 25, plus cross-process
concurrency). Offline: MOCK payments, MOCK AI over injected transports. Not PayPal evidence, not model evidence.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r1d_support import (APP, SET, FakeMessagesAPI, mock_runtime, new_service, paid_task, payment_snapshot)
from autonomos import receipt as R, states as S
from autonomos.corpus import DocumentSet
from autonomos.r1d.receipt2 import verify_any
from autonomos.service import GateError

HERE = os.path.dirname(os.path.abspath(__file__))
RUN2 = next((p for p in (os.environ.get("AUTONOMOS_RUN2_EVIDENCE", ""),
                         os.path.abspath(os.path.join(APP, "..", "..", "r1c", "evidence_sandbox_run2")),
                         "/root/research/autonomos_paypal_hackathon_r0/r1c/evidence_sandbox_run2")
             if p and os.path.isdir(p)), None)


def child(data_dir, tid, step, log, start_at=0, envelope=None):
    arg = json.dumps({"data_dir": data_dir, "tid": tid, "step": step, "log": log, "start_at": start_at,
                      "envelope": envelope or {}})
    return subprocess.Popen([sys.executable, "-B", "-W", "ignore", os.path.join(HERE, "r1d_child.py"), arg],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=APP,
                            env={"PATH": os.environ.get("PATH", ""), "PYTHONHASHSEED": "0"})   # minimal environment


def log_lines(path):
    return open(path).read().splitlines() if os.path.exists(path) else []


class T17PaymentNotCaptured(unittest.TestCase):
    def test_no_execution_and_no_model_request_without_verified_capture(self):
        rt, api = mock_runtime()
        svc = new_service(rt)
        tid = svc.request_task({"capability": "research_snapshot", "topic": "What hardware evidence exists for the "
                                "quantum-kernel experiment?", "corpus": SET})
        self.assertRaises(GateError, svc.execute, tid)                           # offer stage
        svc.accept_offer(tid, svc.store.offer(task_id=tid)["_digest"])
        self.assertEqual(svc.store.task(tid)["state"], S.PAYMENT_PENDING)
        self.assertRaises(GateError, svc.execute, tid)                           # approved? not captured
        svc.cancel(tid, "buyer cancelled")
        self.assertRaises(GateError, svc.execute, tid)                           # cancelled
        self.assertEqual(len(api.requests), 0)
        self.assertEqual(svc.ledger.calls(task_id=tid), [])
        self.assertIsNone(svc.store.execution(tid))


class T18CaptureBeforeExecution(unittest.TestCase):
    def test_order_of_events_and_model_request(self):
        rt, api = mock_runtime()
        svc = new_service(rt)
        tid = paid_task(svc)
        self.assertEqual(svc.execute(tid), S.DELIVERED)
        ev = {e["to"]: e for e in svc.store.events(tid) if e["from"] != e["to"]}
        call = svc.ledger.calls(task_id=tid)[0]
        self.assertLess(ev[S.PAYMENT_CAPTURED]["ts"], ev[S.EXECUTION_AUTHORIZED]["ts"])
        self.assertLessEqual(ev[S.EXECUTION_AUTHORIZED]["ts"], ev[S.EXECUTING]["ts"])
        self.assertLessEqual(ev[S.EXECUTING]["ts"], call["created"])             # reserved after EXECUTING
        self.assertLessEqual(call["created"], call["sent"])
        self.assertLessEqual(call["finished"], ev[S.DELIVERY_VERIFIED]["ts"])
        self.assertEqual(ev[S.PAYMENT_CAPTURED]["actor"], "payment_verifier")
        rc = svc.store.receipt(tid)
        self.assertEqual(rc["body"]["payment"]["verification_verdict"], "CAPTURE_VERIFIED")


class T19CapturedThenAIFailure(unittest.TestCase):
    def test_failure_is_visible_and_money_is_not_hidden(self):
        rt, api = mock_runtime(FakeMessagesAPI(("http", 529, "overloaded_error")))
        svc = new_service(rt)
        tid = paid_task(svc)
        oid = svc.store.order_for_task(tid)["order_id"]
        before = payment_snapshot(svc)
        self.assertEqual(svc.execute(tid), S.REVIEW_REQUIRED)
        t = svc.store.task(tid)
        self.assertIn("paid task failed after capture", t["reason"])
        self.assertIsNone(svc.store.receipt(tid))
        self.assertEqual(svc.store.capture(oid)["verification"]["verdict"], "CAPTURE_VERIFIED")   # payment visible
        # buyer refresh, owner reconciliation, webhook reconcile and restart: none re-authorizes or re-runs
        svc.on_buyer_return(oid)
        svc.capture_status(oid)
        svc.reconcile_task(tid)
        svc.recover()
        self.assertEqual(svc.store.task(tid)["state"], S.REVIEW_REQUIRED)
        self.assertRaises(GateError, svc.execute, tid)
        self.assertEqual(len(api.requests), 1)
        self.assertEqual(len(svc.ledger.calls(task_id=tid)), 1)
        self.assertEqual(payment_snapshot(svc)["captures"], before["captures"])
        self.assertEqual(payment_snapshot(svc)["orders"], before["orders"])
        rec = svc.ledger.record(svc.store.execution(tid)["execution_id"])
        self.assertTrue(rec["outcome"].startswith("FAILED_"))
        self.assertEqual(rec["record"]["requests"][0]["state"], "OUTCOME_UNKNOWN")


class T20T21CrashAndRestart(unittest.TestCase):
    def test_process_crash_mid_request_then_restart_never_duplicates(self):
        rt, _ = mock_runtime()
        svc = new_service(rt)
        tid = paid_task(svc)
        before = payment_snapshot(svc)
        log = os.path.join(svc.cfg.data_dir, "transport.log")
        p = child(svc.cfg.data_dir, tid, "exit", log)
        p.communicate(timeout=60)
        self.assertEqual(p.returncode, 9)                                       # died inside the model request
        self.assertEqual(len(log_lines(log)), 1)
        calls = svc.ledger.calls(task_id=tid)
        self.assertEqual([c["state"] for c in calls], ["SENT"])                  # write-ahead mark survived
        self.assertEqual(svc.store.task(tid)["state"], S.EXECUTING)
        # immediate restart: a new service on the same DB; nothing may re-run or re-send
        rt2, api2 = mock_runtime()
        svc2 = new_service(rt2, data_dir=svc.cfg.data_dir)
        svc2.recover()
        self.assertEqual(svc2.store.task(tid)["state"], S.EXECUTING)
        self.assertRaises(GateError, svc2.execute, tid)
        p2 = child(svc.cfg.data_dir, tid, "honest", log)                         # another process tries again
        out2 = json.loads(p2.communicate(timeout=60)[0])
        self.assertIn("refused", out2)
        # later restart, after the stale threshold and the request lease
        svc2.recover(stale_execution_s=0)
        svc2.ledger.recover(now=time.time() + 3600)
        self.assertEqual(svc2.store.task(tid)["state"], S.REVIEW_REQUIRED)
        self.assertEqual(svc2.store.execution(tid)["status"], "ABANDONED")
        c = svc2.ledger.calls(task_id=tid)[0]
        self.assertEqual((c["state"], c["charged_usd"]), ("OUTCOME_UNKNOWN", c["worst_case_usd"]))
        self.assertRaises(GateError, svc2.execute, tid)
        self.assertEqual(len(log_lines(log)), 1)                                 # one request, ever
        self.assertEqual(len(api2.requests), 0)
        self.assertEqual(payment_snapshot(svc2), before)

    def test_restart_after_delivery_changes_nothing(self):
        rt, api = mock_runtime()
        svc = new_service(rt)
        tid = paid_task(svc)
        self.assertEqual(svc.execute(tid), S.DELIVERED)
        snap = (payment_snapshot(svc), svc.ledger.calls(task_id=tid), svc.store.receipt(tid))
        rt2, api2 = mock_runtime()
        svc2 = new_service(rt2, data_dir=svc.cfg.data_dir)
        rep = svc2.recover(stale_execution_s=0)
        svc2.ledger.recover(now=time.time() + 3600)
        self.assertEqual(rep["ai_calls"], [])
        self.assertRaises(GateError, svc2.execute, tid)
        self.assertEqual((payment_snapshot(svc2), svc2.ledger.calls(task_id=tid), svc2.store.receipt(tid)), snap)
        self.assertEqual(len(api2.requests), 0)

    def test_stale_reservation_released_unsent(self):
        rt, api = mock_runtime()
        svc = new_service(rt)
        tid = paid_task(svc)
        eid = svc.store.claim_execution(svc.store.order_for_task(tid)["order_id"], tid)
        svc.store.transition(tid, S.EXECUTING, "executor", "execution_claim", {"execution_id": eid})
        cid = svc.ledger.reserve(execution_id=eid, task_id=tid, seq=1, env=rt.envelope, est_input_tokens=100,
                                 payment_environment="MOCK")                    # crash before the SENT mark
        svc.ledger.recover(now=time.time() + 120)
        self.assertEqual(svc.ledger.call(cid)["state"], "RELEASED_UNSENT")
        self.assertEqual(svc.ledger.totals(rt.envelope.authorization_id)["requests"], 0)


class T22T23Tampering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rt, _ = mock_runtime()
        cls.svc = new_service(rt)
        cls.tid = paid_task(cls.svc)
        assert cls.svc.execute(cls.tid) == S.DELIVERED
        cls.rc = cls.svc.store.receipt(cls.tid)
        cls.art = cls.svc.store.artifact(cls.rc["body"]["artifact"]["digest"])
        cls.ev = cls.svc.store.events(cls.tid)
        cls.ds = DocumentSet(os.path.join(cls.svc.corpus_root, SET), SET)

    def clone(self, x):
        return json.loads(json.dumps(x))

    def test_untampered_is_valid(self):
        self.assertTrue(verify_any(self.rc, self.art, self.ev, self.ds, self.svc.signer.public_hex)["valid"])

    def test_artifact_tampering(self):
        for mutate in (lambda a: a["report"]["findings"][0].update(claim_text="IonQ hardware ran the kernel."),
                       lambda a: a["ai_execution"].update(execution_class="REAL_MODEL_EXECUTION"),
                       lambda a: a["report"]["findings"][0]["evidence"][0].update(quote="We ran three jobs"),
                       lambda a: a["delivery_verification"].update(status="PARTIAL")):
            a = self.clone(self.art)
            mutate(a)
            r = verify_any(self.rc, a, self.ev, self.ds)
            self.assertFalse(r["valid"])
            self.assertFalse(r["checks"]["artifact_digest"])
        a = self.clone(self.art)                                                  # quote edited -> re-check fails
        a["report"]["findings"][0]["evidence"][0]["quote"] = "We ran three jobs on `ibm_marrakesh`"
        self.assertFalse(verify_any(self.rc, a, self.ev, self.ds)["checks"]["quotes_recheck_against_collection"])

    def test_stored_artifact_tampering_blocks_delivery(self):
        rt, _ = mock_runtime()
        svc = new_service(rt)
        tid = paid_task(svc)
        svc.execute(tid)
        d = svc.store.receipt(tid)["body"]["artifact"]["digest"]
        a = svc.store.artifact(d)
        a["report"]["summary"] = "quantum advantage proven"
        svc.store.db.execute("UPDATE artifacts SET content_json=? WHERE digest=?", (json.dumps(a), d))
        self.assertRaises(GateError, svc.deliver_artifact, tid, d)

    def test_collection_change_detected_by_offline_recheck(self):
        root = tempfile.mkdtemp()
        shutil.copytree(os.path.join(self.svc.corpus_root, SET), os.path.join(root, SET))
        with open(os.path.join(root, SET, "qkrl_README.md"), "w") as f:
            f.write("# replaced\n\nNothing here.\n")
        r = verify_any(self.rc, self.art, self.ev, DocumentSet(os.path.join(root, SET), SET))
        self.assertFalse(r["checks"]["quotes_recheck_against_collection"])

    def test_receipt_signature_tampering(self):
        for path, val in ((("offer", "amount"), "0.01"), (("ai_execution", "real_model_inference"), True),
                          (("ai_execution", "model_requests"), 0), (("payment", "capture_status"), "COMPLETED_X"),
                          (("artifact", "status"), "VERIFIED_PLUS")):
            rc = self.clone(self.rc)
            rc["body"][path[0]][path[1]] = val
            self.assertFalse(verify_any(rc, self.art, self.ev)["checks"]["signature"], path)
        rc = self.clone(self.rc)
        rc["signature_hex"] = ("00" if rc["signature_hex"][:2] != "00" else "11") + rc["signature_hex"][2:]
        self.assertFalse(verify_any(rc, self.art, self.ev)["valid"])

    def test_resigned_with_another_key_fails_the_pinned_issuer_check(self):
        from autonomos.receipt import DemoSigner
        other = DemoSigner(os.path.join(tempfile.mkdtemp(), "k.json"))
        rc = self.clone(self.rc)
        rc["body"]["offer"]["amount"] = "0.01"
        rc["body"]["issuer"].update(key_id=other.key_id, public_key_hex=other.public_hex)
        rc["signature_hex"] = other.sign(rc["body"])
        self.assertTrue(verify_any(rc, self.art, self.ev)["checks"]["signature"])  # self-consistent forgery ...
        r = verify_any(rc, self.art, self.ev, trusted_public_key_hex=self.svc.signer.public_hex)
        self.assertFalse(r["valid"])                                              # ... caught by the pinned key
        self.assertFalse(r["checks"]["issuer_key_is_trusted"])


@unittest.skipIf(RUN2 is None, "sealed Run 2 evidence not found")
class T25R1cCompatibility(unittest.TestCase):
    def test_sealed_run2_receipt_still_verifies(self):
        rc = json.load(open(os.path.join(RUN2, "receipt.json")))
        art = json.load(open(os.path.join(RUN2, "artifact.json")))
        ev = json.load(open(os.path.join(RUN2, "events.json")))
        self.assertTrue(R.verify(rc, art, ev)["valid"])                           # the R1c verifier
        r = verify_any(rc, art, ev, trusted_public_key_hex=rc["body"]["issuer"]["public_key_hex"])
        self.assertEqual((r["valid"], r["receipt_version"]), (True, "1"))        # the R1d verifier
        self.assertEqual(rc["body"]["environment"], "SANDBOX")
        self.assertEqual({m["provider"] for m in art["model_runs"]}, {"FIXTURE"})   # it never claimed a model

    def test_r1d_opens_a_copy_of_the_run2_database_without_touching_payment_rows(self):
        d = tempfile.mkdtemp()
        shutil.copyfile(os.path.join(RUN2, "run2_db_snapshot.sqlite"), os.path.join(d, "autonomos.sqlite"))  # bytes only
        rt, api = mock_runtime()
        svc = new_service(rt, data_dir=d)
        before = payment_snapshot(svc)
        tid = svc.store.db.execute("SELECT task_id FROM tasks").fetchone()[0]
        self.assertEqual(svc.store.task(tid)["state"], S.DELIVERED)
        rep = svc.recover()
        self.assertEqual(payment_snapshot(svc), before)
        self.assertEqual(rep["ai_calls"], [])
        rc = svc.store.receipt(tid)
        self.assertTrue(verify_any(rc, svc.store.artifact(rc["body"]["artifact"]["digest"]),
                                   svc.store.events(tid))["valid"])
        self.assertRaises(GateError, svc.execute, tid)
        self.assertEqual(len(api.requests), 0)

    def test_r1c_verifier_accepts_r1d_v2_receipts(self):
        rt, _ = mock_runtime()
        svc = new_service(rt)
        tid = paid_task(svc)
        svc.execute(tid)
        rc = svc.store.receipt(tid)
        art = svc.store.artifact(rc["body"]["artifact"]["digest"])
        self.assertTrue(R.verify(rc, art, svc.store.events(tid))["valid"])
        self.assertEqual(rc["body"]["receipt_version"], "2")
        v1_keys = set(json.load(open(os.path.join(RUN2, "receipt.json")))["body"])
        self.assertLessEqual(v1_keys, set(rc["body"]))                            # v2 body = v1 body + ai_execution


class CrossProcessConcurrency(unittest.TestCase):
    def test_four_processes_execute_one_task_once(self):
        rt, _ = mock_runtime()
        svc = new_service(rt)
        tid = paid_task(svc)
        log = os.path.join(svc.cfg.data_dir, "transport.log")
        start = time.time() + 1.5
        procs = [child(svc.cfg.data_dir, tid, "slow", log, start) for _ in range(4)]
        outs = [json.loads(p.communicate(timeout=120)[0]) for p in procs]
        self.assertEqual(sum(o.get("state") == S.DELIVERED for o in outs), 1, outs)
        self.assertEqual(sum("refused" in o for o in outs), 3, outs)
        self.assertEqual(len(log_lines(log)), 1)
        self.assertEqual(len(svc.ledger.calls(task_id=tid)), 1)
        self.assertEqual(svc.store.db.execute("SELECT COUNT(*) FROM executions WHERE task_id=?", (tid,)).fetchone()[0],
                         1)

    def test_shared_budget_race_across_processes(self):
        rt, _ = mock_runtime()
        svc = new_service(rt)
        tids = [paid_task(svc) for _ in range(3)]
        log = os.path.join(svc.cfg.data_dir, "transport.log")
        start = time.time() + 1.5
        procs = [child(svc.cfg.data_dir, t, "slow", log, start, {"max_requests_total": 1}) for t in tids]
        outs = [json.loads(p.communicate(timeout=120)[0]) for p in procs]
        states = sorted(o.get("state") for o in outs)
        self.assertEqual(states, [S.DELIVERED, S.REVIEW_REQUIRED, S.REVIEW_REQUIRED], outs)
        self.assertEqual(len(log_lines(log)), 1)                                  # one paid-style request in total
        reasons = [json.dumps(svc.ledger.record(svc.store.execution(t)["execution_id"])) for t in tids]
        self.assertEqual(sum("REQUEST_LIMIT_TOTAL" in r for r in reasons), 2)


if __name__ == "__main__":
    unittest.main()
