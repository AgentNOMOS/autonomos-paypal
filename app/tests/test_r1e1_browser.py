"""R1e.1 Part B: browser demo modes. Written BEFORE the R1e.1 browser code (record: evidence/test_first_part_b.txt).
DEMO_REPLAY = MOCK payment + RECORDED model output, no network; SUPERVISED_LIVE = PayPal Sandbox + one real request,
refused unless every owner gate is in place. No test here starts SUPERVISED_LIVE or touches a network.
Run from app/: python3 -B -m unittest tests/test_r1e1_browser.py -v
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r1d_support import Q, SET, new_service  # noqa: E402
from r1e_launcher_support import FAKE_PP_CLIENT, FAKE_PP_SECRET, free_port, owner_files  # noqa: E402
from autonomos import states as S  # noqa: E402
from autonomos.config import Config  # noqa: E402

APP = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REC = os.path.join(APP, "recordings", "real_ai_test1_opus55")


def replay_service():
    from autonomos.r1d.demo_mode import resolve
    from autonomos.r1d.recorded import recording_runtime
    rt = recording_runtime(REC)
    svc = new_service(rt)
    svc.demo_mode = resolve(svc.cfg, rt, {"AUTONOMOS_DEMO_MODE": "DEMO_REPLAY"})
    return svc


def sandbox_cfg(**over):
    kw = dict(payment_provider="paypal_sandbox", paypal_client_id=FAKE_PP_CLIENT, paypal_client_secret=FAKE_PP_SECRET,
              paypal_merchant_id="FAKEMERCHANT01", data_dir=tempfile.mkdtemp(), sandbox_owner_go="capture-at-most-1-order",
              sandbox_max_orders=1, sandbox_max_capture_requests=1)
    kw.update(over)
    return Config(**kw)


def real_runtime(payment_envs=("SANDBOX",), **auth_over):
    from autonomos.r1d.aiconfig import Runtime
    from autonomos.r1d.envelope import load_authorization
    from test_r1d_provider_budget import FAKE_USR_KEY
    d, le = owner_files(FAKE_USR_KEY, payment_envs=payment_envs, **auth_over)
    env = load_authorization(le["AUTH"], APP)          # repo root that never contains the temp owner files
    return Runtime("anthropic_real", object(), env), le


class M01ModeResolution(unittest.TestCase):
    def test_demo_replay_needs_mock_payment_and_recorded_ai(self):
        from autonomos.r1d.aiconfig import offline_runtime
        from autonomos.r1d.demo_mode import DemoModeError, resolve
        from autonomos.r1d.recorded import recording_runtime
        mock_cfg = Config(payment_provider="mock", data_dir=tempfile.mkdtemp())
        info = resolve(mock_cfg, recording_runtime(REC), {"AUTONOMOS_DEMO_MODE": "DEMO_REPLAY"})
        self.assertEqual(info["mode"], "DEMO_REPLAY")
        self.assertIn("replay", info["banner"].lower())
        with self.assertRaises(DemoModeError):
            resolve(mock_cfg, offline_runtime("mock"), {"AUTONOMOS_DEMO_MODE": "DEMO_REPLAY"})
        with self.assertRaises(DemoModeError):
            resolve(sandbox_cfg(), recording_runtime(REC), {"AUTONOMOS_DEMO_MODE": "DEMO_REPLAY"})

    def test_supervised_live_refused_without_every_gate(self):
        from autonomos.r1d.demo_mode import DemoModeError, resolve
        from autonomos.r1d.recorded import recording_runtime
        rt, le = real_runtime()
        marker = tempfile.mkdtemp(prefix="r1e1_marker_")
        ok_env = {"AUTONOMOS_DEMO_MODE": "SUPERVISED_LIVE", "AUTONOMOS_LIVE_LAUNCH_MARKER": marker}
        self.assertEqual(resolve(sandbox_cfg(), rt, ok_env)["mode"], "SUPERVISED_LIVE")
        bad = [("mock payment", Config(payment_provider="mock", data_dir=tempfile.mkdtemp()), rt, ok_env),
               ("replayed AI", sandbox_cfg(), recording_runtime(REC), ok_env),
               ("replayed AI with a SANDBOX-only, one-request envelope", sandbox_cfg(),
                recording_runtime(REC, allowed_payment_environments=("SANDBOX",), max_requests_total=1,
                                  max_executions=1), ok_env),
               ("no sandbox GO", sandbox_cfg(sandbox_owner_go=""), rt, ok_env),
               ("two orders allowed", sandbox_cfg(sandbox_max_orders=2), rt, ok_env),
               ("two capture POSTs allowed", sandbox_cfg(sandbox_max_capture_requests=2), rt, ok_env),
               ("no launcher marker", sandbox_cfg(), rt, {"AUTONOMOS_DEMO_MODE": "SUPERVISED_LIVE"}),
               ("authorization allows MOCK too", sandbox_cfg(), real_runtime(("MOCK", "SANDBOX"))[0], ok_env),
               ("two model requests", sandbox_cfg(), real_runtime(max_requests_total=2, max_usd_total="1.00")[0],
                ok_env)]
        for name, cfg, runtime, env in bad:
            with self.subTest(name=name), self.assertRaises(DemoModeError):
                resolve(cfg, runtime, env)

    def test_unset_is_development_and_unknown_is_refused(self):
        from autonomos.r1d.aiconfig import offline_runtime
        from autonomos.r1d.demo_mode import DemoModeError, resolve
        cfg = Config(payment_provider="mock", data_dir=tempfile.mkdtemp())
        self.assertEqual(resolve(cfg, offline_runtime("fixture"), {})["mode"], "DEVELOPMENT")
        with self.assertRaises(DemoModeError):
            resolve(cfg, offline_runtime("fixture"), {"AUTONOMOS_DEMO_MODE": "LIVE"})


class M02ServeCommand(unittest.TestCase):
    def _env(self, **kw):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("AUTONOMOS_", "ANTHROPIC_", "PAYPAL_"))}
        e.update(AUTONOMOS_DATA_DIR=tempfile.mkdtemp(), AUTONOMOS_PAYMENT_PROVIDER="mock", AUTONOMOS_AI_PROVIDER="fixture",
                 **kw)
        return e

    def test_demo_replay_server_starts_and_labels_every_page(self):
        port = free_port()
        e = self._env(AUTONOMOS_DEMO_MODE="DEMO_REPLAY", AUTONOMOS_R1D_AI_PROVIDER="recorded",
                      AUTONOMOS_LISTEN_PORT=str(port), AUTONOMOS_PUBLIC_BASE_URL=f"http://127.0.0.1:{port}")
        p = subprocess.Popen([sys.executable, "-B", "-u", "-m", "autonomos", "serve"], cwd=APP, env=e,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for _ in range(100):
                try:
                    body = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2).read().decode()
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                self.fail("server did not start")
            self.assertIn("DEMO_REPLAY", body)
            self.assertIn("not a live model call", body)
        finally:
            p.terminate()
            out = p.communicate(timeout=10)[0]
        self.assertIn("DEMO_REPLAY", out)

    def test_serve_refuses_inconsistent_modes_before_listening(self):
        for name, kw in (("live with mock payment", dict(AUTONOMOS_DEMO_MODE="SUPERVISED_LIVE",
                                                         AUTONOMOS_R1D_AI_PROVIDER="recorded")),
                         ("replay with the emulator", dict(AUTONOMOS_DEMO_MODE="DEMO_REPLAY",
                                                           AUTONOMOS_R1D_AI_PROVIDER="mock")),
                         ("unknown mode", dict(AUTONOMOS_DEMO_MODE="PRODUCTION", AUTONOMOS_R1D_AI_PROVIDER="mock"))):
            with self.subTest(name=name):
                port = free_port()
                r = subprocess.run([sys.executable, "-B", "-m", "autonomos", "serve"], cwd=APP,
                                   env=self._env(AUTONOMOS_LISTEN_PORT=str(port), **kw), capture_output=True,
                                   text=True, timeout=60)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("refused", (r.stdout + r.stderr).lower())
                s = socket.socket()
                self.assertNotEqual(s.connect_ex(("127.0.0.1", port)), 0)
                s.close()


class M05SupervisedLiveRehearsal(unittest.TestCase):
    """SUPERVISED_LIVE with FAKE credentials, offline: `serve` starts only when every gate is in place and labels the
    page; nothing is bought, so no provider is ever contacted (the suite runs inside a network namespace)."""

    def _env(self, le, port, **kw):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("AUTONOMOS_", "ANTHROPIC_", "PAYPAL_"))}
        e.update(AUTONOMOS_DEMO_MODE="SUPERVISED_LIVE", AUTONOMOS_PAYMENT_PROVIDER="paypal_sandbox",
                 AUTONOMOS_AI_PROVIDER="fixture", AUTONOMOS_R1D_AI_PROVIDER="anthropic_real",
                 AUTONOMOS_R1D_AI_AUTHORIZATION=le["AUTH"], AUTONOMOS_ANTHROPIC_KEY_FILE=le["KEY"],
                 AUTONOMOS_DATA_DIR=os.path.join(le["OUT"], "data"), AUTONOMOS_LISTEN_PORT=str(port),
                 AUTONOMOS_PUBLIC_BASE_URL=f"http://127.0.0.1:{port}", AUTONOMOS_SANDBOX_MAX_ORDERS="1",
                 AUTONOMOS_SANDBOX_MAX_CAPTURE_REQUESTS="1", AUTONOMOS_SANDBOX_OWNER_GO="capture-at-most-1-order",
                 AUTONOMOS_R1D_REAL_AI_GO="real-ai:AUTH-R1E-REHEARSAL", AUTONOMOS_LIVE_LAUNCH_MARKER=le["MARK"],
                 PAYPAL_CLIENT_ID=FAKE_PP_CLIENT, PAYPAL_CLIENT_SECRET=FAKE_PP_SECRET,
                 PAYPAL_MERCHANT_ID="FAKEMERCHANT01")
        e.update(kw)
        return e

    def test_live_mode_starts_only_with_every_gate(self):
        from test_r1d_provider_budget import FAKE_USR_KEY
        d, le = owner_files(FAKE_USR_KEY)
        os.makedirs(os.path.join(le["OUT"], "data"))
        os.mkdir(le["MARK"])
        for name, kw in (("no marker", {"AUTONOMOS_LIVE_LAUNCH_MARKER": os.path.join(d, "missing")}),
                         ("no AI GO", {"AUTONOMOS_R1D_REAL_AI_GO": ""}),
                         ("two orders", {"AUTONOMOS_SANDBOX_MAX_ORDERS": "2"})):
            with self.subTest(name=name):
                r = subprocess.run([sys.executable, "-B", "-m", "autonomos", "serve"], cwd=APP,
                                   env=self._env(le, free_port(), **kw), capture_output=True, text=True, timeout=60)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("refused", (r.stdout + r.stderr).lower())
                self.assertNotIn(FAKE_USR_KEY, r.stdout + r.stderr)
        port = free_port()
        p = subprocess.Popen([sys.executable, "-B", "-u", "-m", "autonomos", "serve"], cwd=APP,
                             env=self._env(le, port), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for _ in range(100):
                try:
                    body = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2).read().decode()
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                self.fail("live-mode server did not start")
            self.assertIn("MODE SUPERVISED_LIVE", body)
            self.assertIn("PayPal SANDBOX", body)
            self.assertIn("REAL_MODEL_EXECUTION", body)
        finally:
            p.terminate()
            out = p.communicate(timeout=10)[0]
        for secret in (FAKE_USR_KEY, FAKE_PP_SECRET):
            self.assertNotIn(secret, out + body)

    def test_launcher_starts_serve_in_supervised_live_with_its_marker(self):
        from r1e_launcher_support import LAUNCHER
        text = open(LAUNCHER).read()
        self.assertIn('AUTONOMOS_DEMO_MODE=SUPERVISED_LIVE AUTONOMOS_LIVE_LAUNCH_MARKER="$MARK"', text)
        self.assertLess(text.index('mkdir "$MARK"'), text.index("inenv python3 -B -u -m autonomos serve"))


class M06TamperedReceiptIsShownInvalid(unittest.TestCase):
    def test_page_shows_invalid_after_tampering(self):
        from r1d_support import paid_task
        svc = replay_service()
        tid = paid_task(svc, Q)
        self.assertEqual(svc.execute(tid), S.DELIVERED)
        b = _Browser(svc)
        try:
            self.assertIn(">VALID<", b.get(f"/tasks/{tid}").read().decode())
            rc = svc.store.receipt(tid)
            rc["body"]["payment"]["capture_status"] = "REFUNDED"           # body changed after signing
            svc.store.db.execute("UPDATE receipts SET receipt_json=? WHERE task_id=?", (json.dumps(rc), tid))
            page = b.get(f"/tasks/{tid}").read().decode()
            self.assertIn(">INVALID<", page)
            self.assertFalse(json.loads(b.get(f"/receipts/{tid}/verify").read())["valid"])
        finally:
            b.close()


class _Browser:
    def __init__(self, svc):
        import http.cookiejar
        from http.server import ThreadingHTTPServer
        from autonomos.server import make_handler
        self.svc = svc
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(svc))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        svc.payments.base_url = self.base
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.ua = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get(self, path):
        return self.ua.open(self.base + path)

    def post(self, path, data, csrf_from="/"):
        tok = re.search(r"name='csrf' value='([0-9a-f]+)'", self.get(csrf_from).read().decode()).group(1)
        return self.ua.open(urllib.request.Request(self.base + path, data=(data + "&csrf=" + tok).encode(),
                                                   method="POST", headers={"Origin": self.base}))

    def buy(self, question=Q):
        r = self.post("/tasks", f"capability=research_snapshot&corpus={SET}&topic={urllib.parse.quote(question)}")
        tid = r.geturl().rsplit("/", 1)[1]
        pending = self.get(f"/tasks/{tid}").read().decode()
        dg = self.svc.store.offer(task_id=tid)["_digest"]
        r = self.post(f"/tasks/{tid}/accept", f"offer_digest={dg}", csrf_from=f"/tasks/{tid}")
        oid = r.geturl().rsplit("/", 1)[1]
        self.post(f"/mock-paypal/{oid}/approve", "", csrf_from=f"/mock-paypal/checkout/{oid}")
        for _ in range(200):
            if self.svc.store.task(tid)["state"] in (S.DELIVERED, S.REVIEW_REQUIRED):
                break
            time.sleep(0.05)
        return tid, pending, self.get(f"/tasks/{tid}").read().decode()

    def close(self):
        self.httpd.shutdown()


class M03ReplayBrowserFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = _Browser(replay_service())
        cls.tid, cls.pending, cls.page = cls.b.buy()
        cls.svc = cls.b.svc

    @classmethod
    def tearDownClass(cls):
        cls.b.close()

    def test_page_reflects_the_stored_task(self):
        svc, page = self.svc, self.page
        rc = svc.store.receipt(self.tid)
        art = svc.store.artifact(rc["body"]["artifact"]["digest"])
        cap = svc.store.capture(svc.store.order_for_task(self.tid)["order_id"])
        self.assertEqual(svc.store.task(self.tid)["state"], S.DELIVERED)
        for s in (self.tid, rc["body"]["artifact"]["digest"], cap["capture_id"], rc["body"]["issuer"]["key_id"]):
            self.assertIn(s, page)
        for f in art["report"]["findings"]:
            self.assertIn(f["evidence"][0]["quote"].replace("&", "&amp;").replace("'", "&#x27;")[:40], page)

    def test_every_required_element_is_visible(self):
        p = self.page
        for s in ("DEMO_REPLAY", "Payment provider", "MOCK", "CAPTURE_VERIFIED", "Execution authorization",
                  "after the verified capture", "AI provider", "recorded_replay", "replayed, not a live model call",
                  "Document-supported findings", "Unconfirmed or withheld conclusions",
                  "Independent scientific truth", "NOT ESTABLISHED", "Delivery", "DELIVERED", "Signed receipt",
                  "Receipt verifier", "VALID"):
            self.assertIn(s, p)

    def test_no_misleading_success_indicators(self):
        p = self.page
        self.assertNotRegex(p, r"(?i)\bsuccess(ful)?\b|research (is )?verified|scientifically (proven|verified)")
        truth = re.search(r"Independent scientific truth</th><td>(.*?)</td>", p).group(1)
        self.assertNotIn("class='ok'", truth)
        self.assertIn("protocol", p.lower())
        self.assertNotRegex(p, r"Delivered report \((VERIFIED|PARTIAL)\)")      # status is qualified, not a headline
        self.assertIn("not a research-quality or truth verdict", p)

    def test_pending_page_shows_no_authorization_yet(self):
        self.assertIn("not authorized", self.pending)
        self.assertNotIn("Receipt verifier", self.pending)

    def test_receipt_verify_endpoint(self):
        v = json.loads(self.b.get(f"/receipts/{self.tid}/verify").read())
        self.assertTrue(v["valid"], v)
        self.assertTrue(v["checks"]["issuer_key_is_trusted"])
        self.assertTrue(v["checks"]["quotes_recheck_against_collection"])

    def test_a_second_buyer_task_is_also_replayed_and_labelled(self):
        tid2, _, page2 = self.b.buy()
        self.assertNotEqual(tid2, self.tid)
        self.assertEqual(self.svc.store.task(tid2)["state"], S.DELIVERED)
        self.assertIn("recorded_replay", page2)


class M04ReplayRefusesOtherQuestions(unittest.TestCase):
    def test_other_question_refused_before_any_task_or_order(self):
        b = _Browser(replay_service())
        try:
            home = b.get("/").read().decode()
            self.assertIn("readonly", home)
            n_tasks = b.svc.store.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            with self.assertRaises(urllib.error.HTTPError) as cm:
                b.post("/tasks", f"capability=research_snapshot&corpus={SET}&topic="
                                 f"{urllib.parse.quote('Is IonQ better than IBM?')}")
            self.assertEqual(cm.exception.code, 409)
            self.assertIn("DEMO_REPLAY", cm.exception.read().decode())
            self.assertEqual(b.svc.store.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], n_tasks)
            self.assertEqual(b.svc.store.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
        finally:
            b.close()


if __name__ == "__main__":
    unittest.main()
