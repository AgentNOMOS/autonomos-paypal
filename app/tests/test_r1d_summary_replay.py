"""R1d.2 execution summaries and offline replay. Offline; the demo below uses MOCK payment and the FIXTURE model.
Run from app/: python3 -B -m unittest tests/test_r1d_summary_replay.py -v
"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r1d_support  # noqa: E402,F401  (network guard)
from autonomos.r1d.replay import ReplayRefused, replay, replay_dir  # noqa: E402
from autonomos.r1d.summary import execution_summary  # noqa: E402

APP = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FIX = os.path.join(APP, "tests", "fixtures", "real_ai_test1")
CORPUS = os.path.join(APP, "corpus")


def L(n):
    return json.load(open(os.path.join(FIX, n)))


class S01HistoricalSummary(unittest.TestCase):
    def test_test1_delivered_but_research_partial(self):
        s = execution_summary("DELIVERED", L("receipt.json"), L("artifact.json"))
        self.assertEqual((s["payment_provider"], s["ai_provider"], s["ai_execution_class"]),
                         ("MOCK", "anthropic_messages", "REAL_MODEL_EXECUTION"))
        self.assertEqual(s["delivery_status"], "DELIVERED")
        self.assertEqual(s["research_completeness"], "PARTIAL")
        self.assertIn("derived", s["research_completeness_basis"])
        self.assertEqual((s["claim_support"]["supported"], s["claim_support"]["model_findings"],
                          s["claim_support"]["rule"]), (3, 5, "R1D-LEX-1"))
        self.assertEqual(s["claim_support_verdict"], "PARTIAL")
        self.assertEqual(s["independent_truth"], "NOT_INDEPENDENTLY_VERIFIED (0/5)")
        self.assertIn("research PARTIAL", s["headline"])
        self.assertIn("does not mean the research is complete", s["delivery_note"])

    def test_delivered_never_implies_complete(self):
        art = copy.deepcopy(L("artifact.json"))
        for change in ({"withheld": 1, "claim_unconfirmed": 0, "claim_supported": 4},
                       {"withheld": 0, "claim_unconfirmed": 1, "claim_supported": 4}):
            art["delivery_verification"]["counts"].update(change)
            art["report"]["model_summary_withheld"] = None
            self.assertEqual(execution_summary("DELIVERED", L("receipt.json"), art)["research_completeness"],
                             "PARTIAL", change)
        art["delivery_verification"]["counts"].update(withheld=0, claim_unconfirmed=0, claim_supported=5)
        art["report"]["model_summary_withheld"] = {"reasons": ["x"]}
        self.assertEqual(execution_summary("DELIVERED", L("receipt.json"), art)["research_completeness"], "PARTIAL")

    def test_sandbox_payment_label(self):
        rc = copy.deepcopy(L("receipt.json"))
        rc["body"]["environment"] = "SANDBOX"
        self.assertEqual(execution_summary("DELIVERED", rc, L("artifact.json"))["payment_provider"], "PAYPAL_SANDBOX")


class S02DemoOutput(unittest.TestCase):
    def test_fixture_demo_prints_separate_fields(self):
        out = os.path.join(tempfile.mkdtemp(prefix="r1d2_demo_"), "run")          # demo refuses an existing OUT
        env = dict(os.environ, AUTONOMOS_DATA_DIR=os.path.join(out, "var"), AUTONOMOS_PAYMENT_PROVIDER="mock",
                   AUTONOMOS_AI_PROVIDER="fixture", AUTONOMOS_R1D_AI_PROVIDER="fixture")
        r = subprocess.run([sys.executable, "-B", "-m", "autonomos", "demo", out, "--ai", "fixture"], cwd=APP, env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        d = json.loads(r.stdout[r.stdout.index("{"):])
        self.assertNotIn("provider", d)                                       # the ambiguous R1d.1 key is gone
        self.assertEqual((d["payment_provider"], d["ai_provider"], d["ai_execution_class"]),
                         ("MOCK", "fixture", "FIXTURE_EXECUTION"))
        for k in ("delivery_status", "research_completeness", "claim_support", "independent_truth"):
            self.assertIn(k, d)
        self.assertIn(d["research_completeness"], ("PASS", "PARTIAL", "FAIL"))
        self.assertTrue(d["independent_truth"].startswith("NOT_INDEPENDENTLY_VERIFIED"))
        rc = json.load(open(os.path.join(out, "receipt.json")))
        self.assertEqual(rc["body"]["ai_execution"]["research_completeness"], d["research_completeness"])
        self.assertEqual(rc["body"]["ai_execution"]["validation"]["claim_rule"], "R1E1-SUPPORT-1")    # R1e.1: renamed


class S03Replay(unittest.TestCase):
    def test_replay_is_a_new_offline_reassessment(self):
        r = replay_dir(FIX, CORPUS)
        c = r["comparison"]
        self.assertIn("NEW OFFLINE REASSESSMENT", c["label"])
        rows = {x["finding_id"]: x for x in c["findings"]}
        self.assertEqual(rows["F1"]["original_verdict"], "WITHHELD")
        self.assertEqual(rows["F1"]["r1d2_verdict"], "DELIVERED / CLAIM_SUPPORTED")
        self.assertEqual(rows["F2"]["original_verdict"], "DELIVERED / CLAIM_UNCONFIRMED")
        self.assertEqual(rows["F2"]["r1d2_verdict"], "DELIVERED / CLAIM_SUPPORTED")
        self.assertEqual([k for k, v in rows.items() if v["changed"]], ["F1", "F2"])
        self.assertTrue(all(v["truth_status"] == "NOT_INDEPENDENTLY_VERIFIED" for v in rows.values()))
        self.assertEqual(c["summary"]["original"]["counts"]["withheld"], 1)       # history is reported, not rewritten

    def test_replay_refuses_altered_inputs(self):
        art, raw, offer = L("artifact.json"), open(os.path.join(FIX, "raw_output.txt"), "rb").read(), L("offer.json")
        with self.assertRaises(ReplayRefused):
            replay(art, raw.replace(b"0.0048", b"0.0058"), offer, CORPUS)
        bad = copy.deepcopy(art)
        bad["retrieval"]["passage_ids"] = bad["retrieval"]["passage_ids"][:-1]
        with self.assertRaises(ReplayRefused):
            replay(bad, raw, offer, CORPUS)

    def test_replay_cli_writes_nothing(self):
        before = {n: os.path.getmtime(os.path.join(FIX, n)) for n in os.listdir(FIX)}
        r = subprocess.run([sys.executable, "-B", "-m", "autonomos", "replay-verify", FIX], cwd=APP,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertIn("NEW OFFLINE REASSESSMENT", r.stdout)
        self.assertEqual(before, {n: os.path.getmtime(os.path.join(FIX, n)) for n in os.listdir(FIX)})


if __name__ == "__main__":
    unittest.main()
