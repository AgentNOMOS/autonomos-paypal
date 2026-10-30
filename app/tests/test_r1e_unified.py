"""R1e: one customer workflow = R1c payment gates + R1d.1 Anthropic adapter + R1d.2/R1e evidence verifier.
Written BEFORE the R1e code (record: r1e/evidence/test_first_before_fix.txt). Offline: MOCK payment, recorded or
scripted model responses, network guard installed. A RECORDED replay is labelled as such and is never a model call.
Run from app/: python3 -B -m unittest tests/test_r1e_unified.py -v
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r1d_support import (CORPUS, Q, SET, FakeMessagesAPI, honest_answer, mock_runtime, new_service,  # noqa: E402
                         paid_task, payment_snapshot)
from autonomos import states as S  # noqa: E402
from autonomos.corpus import DocumentSet  # noqa: E402
from autonomos.r1d import engine, verifier  # noqa: E402
from autonomos.r1d.receipt2 import verify_any  # noqa: E402
from autonomos.service import GateError  # noqa: E402

APP = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REC = os.path.join(APP, "recordings", "real_ai_test1_opus55")
DS = DocumentSet(os.path.join(CORPUS, SET), SET)
CLASSES = ("HARDWARE_EXECUTION", "REPORTED_HARDWARE_RESULT", "RAW_EVIDENCE_AVAILABILITY",
           "CLASSICAL_SIMULATION_OR_EXACT", "COMPILED_ONLY", "PLANNED_NOT_PERFORMED", "UNKNOWN_OR_UNSUPPORTED")


def recorded_runtime():
    from autonomos.r1d.recorded import recording_runtime
    return recording_runtime(REC)


def verify_one(anchor, quote, claim, cls):
    f = {"finding_id": "F1", "claim": claim, "source_document": anchor.split("#")[0], "anchor": f"{SET}/{anchor}",
         "quote": quote, "evidence_class": cls, "limitation": "test"}
    provided = {f["anchor"]: DS.passages[f["anchor"]][:engine.PASSAGE_CAP]}
    offer = {"offer_id": "O", "task_id": "T", "question": Q, "max_findings": 5,
             "document_set": {"id": SET, "manifest_digest": DS.manifest_digest}}
    raw = {"offer_id": "O", "task_id": "T", "question": Q, "document_set": offer["document_set"],
           "flagged_passages": [], "input_flags": []}
    return verifier.verify(raw, {"answer_summary": "", "findings": [f], "not_established": [],
                                 "instructions_found_in_sources": False, "instructions_found_in_question": False},
                           DS, offer, provided, "FIXTURE_EXECUTION")


def outcome(anchor, quote, claim, cls):
    rep, _ = verify_one(anchor, quote, claim, cls)
    if rep["findings"]:
        return "SUPPORTED", rep["findings"][0]
    if rep.get("unconfirmed_findings"):
        return "UNCONFIRMED", rep["unconfirmed_findings"][0]
    return rep["removed_findings"][0]["decision"], rep["removed_findings"][0]


def supported_under_any_class(anchor, quote, claim):
    return [c for c in CLASSES if outcome(anchor, quote, claim, c)[0] == "SUPPORTED"]


# ====================================================================== 1. non-numeric conclusions (item 12)
R2, R6, R8, R15 = "qkrl_README.md#p002", "qkrl_README.md#p006", "qkrl_README.md#p008", "qkrl_README.md#p015"
L1, P2, P20, P35 = ("qkrl_LIMITATIONS.md#p001", "qkrl_IONQ_HARDWARE_PLAN.md#p002", "qkrl_IONQ_HARDWARE_PLAN.md#p020",
                    "qkrl_IONQ_HARDWARE_PLAN.md#p035")
QUESTION = ("When the same 9-qubit quantum-kernel workload runs on a trapped-ion device with all-to-all connectivity and "
            "a native arbitrary-angle entangler (IonQ Forte), does measurably more kernel contrast survive than in the "
            "2026-08-23 IBM baseline")
RAN_ONCE = ("This package reproduces a 9-qubit quantum-kernel experiment that we ran once on IBM Quantum hardware, and "
            "plans a controlled repeat of it on IonQ trapped-ion hardware.")
IONQ_COMPILED = ("The 144-ZZ (original structure) and 108-ZZ (simplified) circuits have only been compiled and verified "
                 "locally. Neither has run on IonQ hardware.")
PUBLIC = ("What remains public: the input vectors (CC-BY-4.0), our logical circuits and the exact compiled circuits that "
          "were submitted")


class N01WrongNonNumericConclusions(unittest.TestCase):
    """Each wrong conclusion quotes the document correctly; none may be CLAIM_SUPPORTED under ANY evidence class."""
    CASES = [
        ("question turned into a finding", P2, QUESTION,
         "The documents report that measurably more kernel contrast survives on the IonQ Forte trapped-ion device than "
         "in the 2026-08-23 IBM baseline."),
        ("question turned into a finding", "qrg_README.md#p002",
         "Does the evidence actually justify the proposed conclusion?",
         "The documents report that the evidence actually justifies the proposed conclusion."),
        ("conditional made factual", P35, "if each ZZ had error ε and errors combined multiplicatively",
         "The documents report that each ZZ had error ε and errors combined multiplicatively."),
        ("plan presented as result", R2, RAN_ONCE,
         "The documents report a controlled repeat of the 9-qubit quantum-kernel experiment on IonQ trapped-ion "
         "hardware, and a reproduction package for IBM Quantum hardware."),
        ("polarity flip", P20, "Debiasing/error mitigation stays off.",
         "The documents report that debiasing/error mitigation stays on."),
        ("polarity flip", P20, "Submission mode: native gates only, so that per IonQ's documentation the compiler and "
                               "optimiser are bypassed.",
         "The documents report that, per IonQ's documentation, the compiler and optimiser are used in native gates "
         "submission mode."),
        ("polarity flip", "qkrl_WITHHELD_EVIDENCE.md#p004", PUBLIC,
         "The documents report that the input vectors and logical circuits remain private."),
        ("wrong agent", R8, IONQ_COMPILED,
         "The 144-ZZ and 108-ZZ circuits have only been compiled and verified locally. Neither has run on IBM "
         "hardware."),
        ("wrong agent", R6, "We do not assume that connectivity alone caused the IBM degradation.",
         "IonQ does not assume that connectivity alone caused the IBM degradation."),
        ("negation moved to another verb", R15, "circuit produced and checked offline, never executed on hardware",
         "The documents report that the circuit was never produced and checked offline, but executed on hardware."),
        ("role swap in a contrast pair", L1, "SHA256 shows integrity since sealing, not origin.",
         "The documents report that SHA256 shows origin, not integrity since sealing."),
        ("role swap in a contrast pair", L1,
         "This project is about whether hardware preserves kernel *contrast*, not whether the kernel is useful.",
         "The documents report that this project is about whether the kernel is useful, not whether hardware preserves "
         "kernel contrast."),
        ("scope inflation", L1, "Only the account holder can retrieve IBM's own record of the job.",
         "The documents report that anyone can retrieve IBM's own record of the job."),
        ("certainty escalation", R8, "The hardware numbers below are reported results.",
         "The documents prove that the hardware numbers below are correct results."),
    ]

    def test_no_wrong_conclusion_is_supported_under_any_class(self):
        for cat, anchor, quote, claim in self.CASES:
            with self.subTest(cat=cat, claim=claim[:60]):
                self.assertEqual(supported_under_any_class(anchor, quote, claim), [])

    def test_reasons_are_named(self):
        want = {"QUESTION_PRESENTED_AS_FINDING": self.CASES[0], "CONDITION_DROPPED": self.CASES[2],
                "PLAN_DROPPED": self.CASES[3], "POLARITY_FLIP": self.CASES[4], "ENTITY_NOT_IN_EVIDENCE": self.CASES[7],
                "NEGATION_SCOPE_CHANGED": self.CASES[9], "STRENGTH_ESCALATION": self.CASES[12],
                # R1e.1: the "X, not Y" swap is now withheld outright (hard) instead of left unconfirmed
                "CONTRAST_REVERSED": self.CASES[10]}
        for code, (cat, anchor, quote, claim) in want.items():
            with self.subTest(code=code):
                d, r = outcome(anchor, quote, claim, "UNKNOWN_OR_UNSUPPORTED")
                why = " ".join(r.get("reasons", []) + r.get("claim_unconfirmed_reasons", []))
                self.assertIn(code, why)


class N02CorrectParaphrasesStaySupported(unittest.TestCase):
    CASES = [
        ("real F3", R8, "Its raw job files and provider metadata are not in this repository until redistribution "
                        "rights are confirmed (`WITHHELD_EVIDENCE.md`).",
         "The raw IBM job files and provider metadata are not in the repository until redistribution rights are "
         "confirmed.", "RAW_EVIDENCE_AVAILABILITY"),
        ("real F4", R8, IONQ_COMPILED, "The 144-ZZ and 108-ZZ circuits have only been compiled and verified locally. "
                                       "Neither has run on IonQ hardware.", "COMPILED_ONLY"),
        ("real F5", L1, "IonQ's server-side handling, ZZ angle limits and precision, and gate parallelism are "
                        "unverified until IonQ/Qollab confirm them or a run occurs.",
         "IonQ's server-side handling, ZZ angle limits and precision, and gate parallelism are unverified until "
         "IonQ/Qollab confirm them or a run occurs.", "UNKNOWN_OR_UNSUPPORTED"),
        ("plan kept as plan", "qkrl_IONQ_HARDWARE_PLAN.md#p000",
         "IonQ hardware experiment plan (PROPOSED — no IonQ job has been run)",
         "The IonQ hardware experiment is a proposed plan; no IonQ job has been run.", "PLANNED_NOT_PERFORMED"),
        ("availability kept", "qkrl_WITHHELD_EVIDENCE.md#p004", PUBLIC,
         "The documents report that the input vectors, logical circuits and exact compiled circuits that were "
         "submitted remain public.", "CLASSICAL_SIMULATION_OR_EXACT"),
    ]

    def test_controls(self):
        for name, anchor, quote, claim, cls in self.CASES:
            with self.subTest(name=name):
                d, r = outcome(anchor, quote, claim, cls)
                self.assertEqual(d, "SUPPORTED", r)

    def test_real_output_replay_still_five_of_five(self):
        from autonomos.r1d.replay import replay_dir
        r = replay_dir(os.path.join(APP, "tests", "fixtures", "real_ai_test1"), CORPUS)
        self.assertEqual(r["verification"]["counts"]["claim_supported"], 5)
        self.assertEqual(r["verification"]["research_completeness"], "PASS")


# ====================================================================== 2. verified vs unconfirmed (items 7, 8, 9)
class V01Separation(unittest.TestCase):
    def setUp(self):
        self.rep, self.ver = verify_one(R8, IONQ_COMPILED, "The 144-ZZ and 108-ZZ circuits have only been compiled "
                                        "and verified locally. Neither has run on IonQ hardware.", "COMPILED_ONLY")
        self.rep2, self.ver2 = verify_one(R6, "We do not assume that connectivity alone caused the IBM degradation.",
                                          "The documents report that connectivity is not assumed to be the only "
                                          "cause of the observed IBM degradation in kernel contrast.",
                                          "UNKNOWN_OR_UNSUPPORTED")

    def test_supported_and_unconfirmed_are_separate_lists(self):
        self.assertEqual(len(self.rep["findings"]), 1)
        self.assertEqual(self.rep["unconfirmed_findings"], [])
        self.assertEqual(self.rep2["findings"], [])
        u = self.rep2["unconfirmed_findings"][0]
        self.assertEqual((u["quote_status"], u["claim_status"]), ("QUOTE_VERIFIED", "CLAIM_UNCONFIRMED"))
        self.assertIn("NOT confirmed", u["claim"])
        self.assertEqual(self.ver2["counts"]["claim_unconfirmed"], 1)
        self.assertEqual(self.ver2["counts"]["delivered"], 1)

    def test_no_truth_language(self):
        for rep in (self.rep, self.rep2):
            blob = json.dumps(rep).lower()
            self.assertNotIn("independently verified as true\"", blob)
            for f in rep["findings"] + rep["unconfirmed_findings"]:
                self.assertEqual(f["truth_status"], "NOT_INDEPENDENTLY_VERIFIED")
                self.assertEqual(f["independent_evidence"], "NOT_ESTABLISHED")

    def test_receipt_recheck_covers_unconfirmed_excerpts(self):
        art = {"document_set": {"manifest_digest": DS.manifest_digest},
               "report": {"findings": [], "unconfirmed_findings": self.rep2["unconfirmed_findings"]}}
        self.assertEqual(verifier.recheck_delivered(art, DS), (True, []))
        art["report"]["unconfirmed_findings"][0]["evidence"][0]["quote"] = "We assume that connectivity alone caused it."
        self.assertFalse(verifier.recheck_delivered(art, DS)[0])


    def test_truth_check_covers_unconfirmed_excerpts(self):
        svc, tid, st, _ = run_recorded()
        rc = svc.store.receipt(tid)
        art = json.loads(json.dumps(svc.store.artifact(rc["body"]["artifact"]["digest"])))
        u = dict(art["report"]["findings"][0], truth_status="TRUTH_INDEPENDENTLY_VERIFIED")
        art["report"]["unconfirmed_findings"] = [u]
        v = verify_any(rc, art, svc.store.events(tid))
        self.assertFalse(v["checks"]["no_truth_verification_claimed"])


class V02HistoricalReceipts(unittest.TestCase):
    def test_test1_receipt_still_verifies(self):
        F = os.path.join(APP, "tests", "fixtures", "real_ai_test1")
        L = lambda n: json.load(open(os.path.join(F, n)))  # noqa: E731
        key = open(os.path.join(F, "issuer_public_key.hex")).read().strip()
        v = verify_any(L("receipt.json"), L("artifact.json"), L("events.json"), docset=DS, trusted_public_key_hex=key)
        self.assertTrue(v["valid"], v)


# ====================================================================== 3. unified offline E2E (items 1-6, 13)
def run_recorded(svc=None):
    rt = recorded_runtime()
    svc = svc or new_service(rt)
    tid = paid_task(svc, Q)
    st = svc.execute(tid)
    return svc, tid, st, rt


class E01RecordedReplayHappyPath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc, cls.tid, cls.st, cls.rt = run_recorded()
        cls.rc = cls.svc.store.receipt(cls.tid)
        cls.art = cls.svc.store.artifact(cls.rc["body"]["artifact"]["digest"])

    def test_delivered_with_one_replayed_response(self):
        self.assertEqual(self.st, S.DELIVERED)
        calls = self.svc.ledger.calls(task_id=self.tid)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.rt.provider._open.requests), 1)

    def test_replay_is_labelled_and_is_not_a_model_call(self):
        ai, run = self.rc["body"]["ai_execution"], self.art["model_runs"][0]
        self.assertEqual(ai["execution_class"], "MOCK_EXECUTION")
        self.assertFalse(ai["real_model_inference"])
        self.assertTrue(run["transport"].startswith("RECORDED_REPLAY"))
        self.assertIn("req_011Cg7ZywMA4FMtZQC9G2QbX", run["transport"])
        self.assertTrue(str(run["request_id"]).startswith("REPLAY-"))
        self.assertTrue(str(run["response_id"]).startswith("REPLAY-"))
        self.assertEqual(ai["cost"]["estimated_charge_usd"], "0")

    def test_verification_of_the_recorded_output(self):
        c = self.art["delivery_verification"]["counts"]
        self.assertEqual((c["claim_supported"], c["claim_unconfirmed"], c["withheld"]), (5, 0, 0))
        self.assertEqual(self.art["delivery_verification"]["research_completeness"], "PASS")

    def test_receipt_verifies_offline_with_pinned_key_and_corpus(self):
        v = verify_any(self.rc, self.art, self.svc.store.events(self.tid), docset=DS,
                       trusted_public_key_hex=self.rc["body"]["issuer"]["public_key_hex"])
        self.assertTrue(v["valid"], v)

    def test_summary_separates_payment_and_ai(self):
        from autonomos.r1d.summary import execution_summary
        s = execution_summary(self.st, self.rc, self.art)
        self.assertEqual((s["payment_provider"], s["ai_provider"]), ("MOCK", "recorded_replay"))
        self.assertIn("not a model call", s["ai_note"])
        self.assertEqual(s["research_completeness"], "PASS")

    def test_payment_happened_before_the_model_request(self):
        ev = {e["to"]: e["ts"] for e in self.svc.store.events(self.tid)}
        call = self.svc.ledger.calls(task_id=self.tid)[0]
        self.assertLess(ev[S.PAYMENT_CAPTURED], ev[S.EXECUTION_AUTHORIZED])
        self.assertLess(ev[S.EXECUTION_AUTHORIZED], call["sent"])


class E02NoAiBeforeCapture(unittest.TestCase):
    def _task_without_capture(self, scenario=None):
        rt = recorded_runtime()
        svc = new_service(rt)
        tid = svc.request_task({"capability": "research_snapshot", "topic": Q, "corpus": SET, "buyer_ref": "e2e"})
        oid = svc.accept_offer(tid, svc.store.offer(task_id=tid)["_digest"]).rsplit("/", 1)[1]
        return svc, rt, tid, oid

    def _assert_no_ai(self, svc, rt, tid):
        with self.assertRaises(GateError):
            svc.execute(tid)
        self.assertEqual(svc.ledger.calls(task_id=tid), [])
        self.assertEqual(rt.provider._open.requests, [])
        self.assertIsNone(svc.store.execution(tid))

    def test_unapproved_order(self):
        svc, rt, tid, oid = self._task_without_capture()
        self._assert_no_ai(svc, rt, tid)

    def test_approved_but_not_captured(self):
        svc, rt, tid, oid = self._task_without_capture()
        svc.payments.buyer_action(oid, "approve")
        self._assert_no_ai(svc, rt, tid)

    def test_capture_pending_declined_or_unknown(self):
        for sc in ("pending", "declined", "timeout_never", "declined_instrument"):
            with self.subTest(scenario=sc):
                svc, rt, tid, oid = self._task_without_capture()
                svc.payments.buyer_action(oid, "approve")
                svc.payments.set_scenario(oid, sc)
                try:
                    svc.on_buyer_return(oid)
                except Exception:                        # noqa: BLE001 - provider errors are part of the scenario
                    pass
                self.assertNotEqual(svc.store.task(tid)["state"], S.EXECUTION_AUTHORIZED)
                self._assert_no_ai(svc, rt, tid)


    def test_forged_authorized_state_without_capture_is_refused(self):
        svc, rt, tid, oid = self._task_without_capture()
        svc.store.db.execute("UPDATE tasks SET state=? WHERE task_id=?", (S.EXECUTION_AUTHORIZED, tid))
        self._assert_no_ai(svc, rt, tid)


class R01RecordingGuards(unittest.TestCase):
    def _request(self, question=Q, pids=None):
        from autonomos.r1d.recorded import RecordedResponseTransport
        t = RecordedResponseTransport(REC)
        pids = pids or t.meta["passage_ids"]
        system, user, _ = engine.frame(DS, question, pids, "0123456789abcdef", 5)
        req = urllib.request.Request("https://example.invalid/", data=json.dumps(
            {"model": t.meta["model"], "messages": [{"role": "user", "content": user}]}).encode())
        return t, req

    def test_exact_request_is_answered_once(self):
        from autonomos.r1d.recorded import RecordingMismatch
        t, req = self._request()
        r = t(req)
        self.assertEqual(json.loads(r.read())["id"], "REPLAY-msg_011Cg7ZyxapdVetD9AJbjkgE")
        with self.assertRaisesRegex(RecordingMismatch, "ALREADY_REPLAYED"):
            t(req)

    def test_other_question_same_passages(self):
        from autonomos.r1d.recorded import RecordingMismatch
        t, req = self._request(question="What is still unverified about the IonQ plan?")
        with self.assertRaisesRegex(RecordingMismatch, "QUESTION_MISMATCH"):
            t(req)

    def test_same_question_other_passages(self):
        from autonomos.r1d.recorded import RecordedResponseTransport, RecordingMismatch
        pids = RecordedResponseTransport(REC).meta["passage_ids"][:-1]
        t, req = self._request(pids=pids)
        with self.assertRaisesRegex(RecordingMismatch, "PASSAGE_MISMATCH"):
            t(req)

    def test_corrupt_recording_refused(self):
        import shutil
        from autonomos.r1d.recorded import RecordedResponseTransport, RecordingMismatch
        d = tempfile.mkdtemp(prefix="r1e_rec_")
        shutil.copy(os.path.join(REC, "RECORDING.json"), d)
        with open(os.path.join(REC, "raw_output.txt"), "rb") as f:
            raw = f.read().replace(b"0.0048", b"0.0058")
        with open(os.path.join(d, "raw_output.txt"), "wb") as f:
            f.write(raw)
        with self.assertRaisesRegex(RecordingMismatch, "CORRUPT"):
            RecordedResponseTransport(d)


class P01PaymentEnvironmentPreflight(unittest.TestCase):
    def test_sandbox_payment_needs_a_sandbox_authorization(self):
        from autonomos.r1d.aiconfig import real_checks
        from r1e_launcher_support import owner_files
        from test_r1d_provider_budget import FAKE_USR_KEY
        for envs, ok in ((["MOCK"], False), (["SANDBOX"], True), (["MOCK", "SANDBOX"], True)):
            with self.subTest(envs=envs):
                d, le = owner_files(FAKE_USR_KEY, payment_envs=envs)
                e = {"AUTONOMOS_R1D_AI_PROVIDER": "anthropic_real", "AUTONOMOS_R1D_AI_AUTHORIZATION": le["AUTH"],
                     "AUTONOMOS_ANTHROPIC_KEY_FILE": le["KEY"], "AUTONOMOS_DATA_DIR": os.path.join(le["OUT"], "data"),
                     "AUTONOMOS_PAYMENT_PROVIDER": "paypal_sandbox",
                     "AUTONOMOS_R1D_REAL_AI_GO": "real-ai:AUTH-R1E-REHEARSAL"}
                checks, problems, env, _ = real_checks(e)
                self.assertEqual(checks["payment_environment_allowed"], ok, problems)


class E03NeverTwice(unittest.TestCase):
    def test_crash_mid_request_then_restart_never_resends(self):
        d = tempfile.mkdtemp(prefix="r1e_crash_")
        rt = recorded_runtime()
        svc = new_service(rt, data_dir=d)
        tid = paid_task(svc, Q)
        log = os.path.join(d, "transport.log")
        r = subprocess.run([sys.executable, "-B", os.path.join(APP, "tests", "r1e_child.py"),
                            json.dumps({"data_dir": d, "tid": tid, "log": log, "mode": "crash"})],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 9, r.stderr[-500:])
        self.assertEqual(len(open(log).read().splitlines()), 1)
        rt2 = recorded_runtime()
        svc2 = new_service(rt2, data_dir=d)
        svc2.recover()                                     # immediate restart: the write-ahead SENT mark survives
        self.assertEqual([c["state"] for c in svc2.ledger.calls(task_id=tid)], ["SENT"])
        self.assertRaises(GateError, svc2.execute, tid)
        svc2.recover(stale_execution_s=0)                  # later: stale execution and expired request lease
        svc2.ledger.recover(now=time.time() + 3600)
        self.assertEqual([c["state"] for c in svc2.ledger.calls(task_id=tid)], ["OUTCOME_UNKNOWN"])
        self.assertEqual(svc2.store.task(tid)["state"], S.REVIEW_REQUIRED)
        self.assertEqual(rt2.provider._open.requests, [])
        self.assertIsNone(svc2.store.receipt(tid))
        with self.assertRaises(GateError):
            svc2.execute(tid)
        self.assertEqual(len(open(log).read().splitlines()), 1)

    def test_uncertain_response_is_not_retried(self):
        rt = recorded_runtime()
        rt.provider._open.fail_with = ConnectionResetError("connection reset after send")
        svc = new_service(rt)
        tid = paid_task(svc, Q)
        self.assertEqual(svc.execute(tid), S.REVIEW_REQUIRED)
        self.assertEqual(len(rt.provider._open.requests), 1)
        self.assertEqual([c["state"] for c in svc.ledger.calls(task_id=tid)], ["OUTCOME_UNKNOWN"])
        self.assertIsNone(svc.store.receipt(tid))

    def test_concurrent_execute_sends_once(self):
        rt = recorded_runtime()
        svc = new_service(rt)
        tid = paid_task(svc, Q)
        out = []

        def go():
            try:
                out.append(svc.execute(tid))
            except GateError as e:
                out.append(f"refused: {e}")
        ts = [threading.Thread(target=go) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(out.count(S.DELIVERED), 1, out)
        self.assertEqual(len(rt.provider._open.requests), 1)

    def test_recording_refuses_a_different_prompt(self):
        rt = recorded_runtime()
        svc = new_service(rt)
        tid = paid_task(svc, "Which IonQ device parameters are confirmed by the documents?")
        self.assertEqual(svc.execute(tid), S.REVIEW_REQUIRED)
        self.assertIsNone(svc.store.receipt(tid))
        self.assertIn("RecordingMismatch", json.dumps(svc.view(tid)["ai"]))


# ====================================================================== 4. buyer view (items 4, 10)
class B01BuyerPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        from autonomos.server import make_handler
        cls.svc, cls.tid, cls.st, _ = run_recorded()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.svc))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.task = urllib.request.urlopen(f"{base}/tasks/{cls.tid}", timeout=10).read().decode()
        cls.home = urllib.request.urlopen(f"{base}/", timeout=10).read().decode()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def test_status_panel_separates_payment_ai_research_and_truth(self):
        t = self.task
        # R1e.1: labels renamed ("not a live model call", "Independent scientific truth")
        for s in ("Payment provider", "MOCK", "CAPTURE_VERIFIED", "AI provider", "recorded_replay",
                  "not a live model call", "Delivery", "DELIVERED", "Research completeness", "PASS", "Claim support",
                  "5/5", "Independent scientific truth", "NOT_INDEPENDENTLY_VERIFIED"):
            self.assertIn(s, t)

    def test_verified_claims_are_a_separate_section(self):
        self.assertIn("Document-supported findings", self.task)                   # R1e.1: renamed
        self.assertIn("Quote-verified excerpts whose claims are NOT confirmed", self.task)
        import re
        for m in re.finditer(r"verified as true", self.task):     # only negated uses are allowed
            self.assertRegex(self.task[max(0, m.start() - 40):m.start()], r"(No finding is independently|not) $")

    def test_home_shows_the_r1d_ai_runtime_not_the_legacy_selector(self):
        self.assertIn("MOCK_EXECUTION", self.home)
        self.assertIn("recorded_replay", self.home)
        self.assertNotIn('"ai_provider": "fixture"', self.home)


if __name__ == "__main__":
    unittest.main()
