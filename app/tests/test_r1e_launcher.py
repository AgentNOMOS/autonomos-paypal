"""R1e unified-demo launcher (items 11 and 16), offline rehearsals only: fake key, fake PayPal env, fake authorization.
The launcher never starts a server or sends anything in these tests: --dry-run stops before, --launch is refused
before the marker (no GO, or no interactive terminal), and --collect reads a local recorded-replay run.
Run from app/: python3 -B -m unittest tests/test_r1e_launcher.py -v
"""
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r1d_support import Q, new_service, paid_task  # noqa: E402
from r1e_launcher_support import FAKE_PP_CLIENT, FAKE_PP_SECRET, LAUNCHER, owner_files  # noqa: E402
from test_r1d_provider_budget import FAKE_USR_KEY, REFUSED_TOKENS  # noqa: E402


def run(mode, env, extra=None, stdin=subprocess.DEVNULL):
    e = dict(env, **(extra or {}))
    r = subprocess.run(["bash", LAUNCHER, mode], env=e, capture_output=True, text=True, timeout=180, stdin=stdin)
    return r.returncode, r.stdout + r.stderr


class L01LauncherPresent(unittest.TestCase):
    def test_launcher_exists_no_skip(self):
        self.assertTrue(os.path.isfile(LAUNCHER), f"R1e launcher missing: {LAUNCHER}")


class L02DryRun(unittest.TestCase):
    def assert_nothing_created(self, env):
        for k in ("OUT", "MARK", "EVD"):
            self.assertFalse(os.path.exists(env[k]), k)

    def test_dry_run_ok_and_silent_about_secrets(self):
        d, env = owner_files(FAKE_USR_KEY)
        rc, text = run("--dry-run", env)
        self.assertEqual(rc, 0, text)
        for s in ("format OK (console_personal_key_usr)", "PayPal sandbox env file: present, private",
                  "sandbox-preflight exit 0", "key family (app preflight): console_personal_key_usr",
                  "ai-preflight verdict: REAL_PROVIDER_BLOCKED", "DRY RUN COMPLETE"):
            self.assertIn(s, text)
        for secret in (FAKE_USR_KEY, FAKE_PP_SECRET, FAKE_PP_CLIENT):
            self.assertNotIn(secret, text)
        self.assert_nothing_created(env)

    def test_refusals(self):
        cases = [("oauth token", REFUSED_TOKENS["oauth_access"], {}, {}, "format NOT OK (OAuth access token"),
                 ("MOCK-only authorization", FAKE_USR_KEY, {"allowed_payment_environments": ["MOCK"]}, {},
                  "does not match the UNIFIED DEMO 1 envelope"),
                 ("two requests", FAKE_USR_KEY, {"max_requests_total": 2}, {}, "does not match"),
                 ("cheap price", FAKE_USR_KEY, {"price_usd_per_mtok": {"input": "4.00", "output": "20.00"}}, {},
                  "below the conservative 4.40/22.00")]
        for name, key, over, extra, msg in cases:
            with self.subTest(name=name):
                d, env = owner_files(key, **over)
                rc, text = run("--dry-run", env, extra)
                self.assertEqual(rc, 2, text)
                self.assertIn(msg, text)
                self.assertNotIn(key, text)
                self.assert_nothing_created(env)

    def test_second_launch_refused(self):
        d, env = owner_files(FAKE_USR_KEY)
        os.mkdir(env["MARK"])
        rc, text = run("--dry-run", env)
        self.assertEqual(rc, 2)
        self.assertIn("already launched", text)

    def test_live_paypal_host_refused(self):
        d, env = owner_files(FAKE_USR_KEY)
        os.chmod(env["PPENV"], 0o600)
        with open(env["PPENV"], "a") as f:
            f.write("PAYPAL_API_BASE=https://api-m.paypal.com\n")
        rc, text = run("--dry-run", env)
        self.assertEqual(rc, 2)
        self.assertIn("LIVE host", text)


class L03LaunchGuards(unittest.TestCase):
    def test_launch_without_go_is_refused_before_the_marker(self):
        d, env = owner_files(FAKE_USR_KEY)
        rc, text = run("--launch", env)
        self.assertEqual(rc, 2, text)
        self.assertIn("not READY", text)
        self.assertFalse(os.path.exists(env["MARK"]))

    def test_launch_with_go_but_no_terminal_is_refused_before_the_marker(self):
        d, env = owner_files(FAKE_USR_KEY)
        go = {"AUTONOMOS_R1D_REAL_AI_GO": "real-ai:AUTH-R1E-REHEARSAL",
              "AUTONOMOS_SANDBOX_OWNER_GO": "capture-at-most-1-order"}
        rc, text = run("--launch", env, go, stdin=subprocess.PIPE)
        self.assertEqual(rc, 2, text)
        self.assertIn("ai-preflight verdict: REAL_PROVIDER_READY", text)
        self.assertIn("interactive terminal", text)
        self.assertFalse(os.path.exists(env["MARK"]))
        self.assertFalse(os.path.exists(env["OUT"]))


class L04Collect(unittest.TestCase):
    def test_collect_refused_without_launch(self):
        d, env = owner_files(FAKE_USR_KEY)
        rc, text = run("--collect", env)
        self.assertEqual(rc, 2)
        self.assertIn("no launch marker", text)

    def test_collect_is_offline_and_verifies_a_recorded_run(self):
        from autonomos.r1d.recorded import recording_runtime
        d, env = owner_files(FAKE_USR_KEY)
        os.makedirs(os.path.join(env["OUT"], "data"))
        app = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        svc = new_service(recording_runtime(os.path.join(app, "recordings", "real_ai_test1_opus55")),
                          data_dir=os.path.join(env["OUT"], "data"))
        tid = paid_task(svc, Q)
        self.assertEqual(svc.execute(tid), "DELIVERED")
        os.mkdir(env["MARK"])
        with open(os.path.join(env["MARK"], "launched_at"), "w") as f:
            f.write("rehearsal\n")
        rc, text = run("--collect", env)
        self.assertEqual(rc, 0, text)
        self.assertIn("credential material in evidence: NONE", text)
        self.assertIn("evidence seal failures: 0", text)
        v = json.load(open(os.path.join(env["EVD"], tid, "verify_receipt.json")))
        self.assertTrue(v["valid"], v)
        s = json.load(open(os.path.join(env["EVD"], tid, "execution_summary.json")))
        self.assertEqual((s["payment_provider"], s["ai_provider"], s["research_completeness"]),
                         ("MOCK", "recorded_replay", "PASS"))


if __name__ == "__main__":
    unittest.main()
