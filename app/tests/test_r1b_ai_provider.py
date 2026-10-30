"""R1b: Anthropic API provider path, exercised ONLY with a fake HTTPS transport. No billed request is made.
Run from app/: python3 -B -m unittest tests.test_r1b_ai_provider -v
"""
import io
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from autonomos import receipt as R, states as S  # noqa: E402
from autonomos.ai import AIError, AnthropicAPIProvider, make_provider  # noqa: E402
from autonomos.config import Config  # noqa: E402
from autonomos.service import AutoNomos  # noqa: E402

KEY = "sk-ant-test-NOT-A-REAL-KEY"
Q = "What hardware evidence exists for the quantum-kernel experiment, and what is still unverified?"


class FakeAnthropic:
    """Answers like the Messages API. 'compose' gets findings that quote the passages it was sent (LOCAL FIXTURE)."""

    def __init__(self, fail=None):
        self.requests, self.fail = [], list(fail or [])

    def __call__(self, req, timeout=0):
        self.requests.append(req)
        if self.fail:
            code, body = self.fail.pop(0)
            if code >= 400:
                raise urllib.error.HTTPError(req.full_url, code, "err", {"request-id": "req_fail"},
                                             io.BytesIO(json.dumps(body).encode()))
            return self._resp(body)
        user = json.loads(req.data)["messages"][0]["content"]
        if "<passages>" not in user:
            text = json.dumps({"restated_question": "q", "search_terms": ["hardware", "evidence"],
                               "required_output": "findings", "out_of_scope_parts": [],
                               "instructions_found_in_input": False})
        else:
            blocks = re.findall(r'<passage id="([^"]+)">(.*?)</passage>', user, re.S)[:2]
            text = json.dumps({"summary": "mock-model summary", "uncertainties": [], "unanswered": [],
                               "confidence": "medium",
                               "findings": [{"claim": "The document states this.", "evidence": [
                                   {"passage_id": p, "quote": re.split(r"(?<=[.!?])\s+", b.strip())[0][:200]}]}
                                   for p, b in blocks]})
        return self._resp({"type": "message", "model": "claude-sonnet-5-5", "stop_reason": "end_turn",
                           "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": text}],
                           "usage": {"input_tokens": 10, "output_tokens": 20}})

    @staticmethod
    def _resp(body):
        class Resp(io.BytesIO):
            status = 200
            headers = {"request-id": "req_mock_123"}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return Resp(json.dumps(body).encode())


class TestAnthropicProvider(unittest.TestCase):
    def test_request_shape_and_no_key_leak(self):
        t = FakeAnthropic()
        p = AnthropicAPIProvider(KEY, model="claude-sonnet-5-5", opener=t)
        rec = p.complete("interpret", "SYS", "<question>x</question>")
        req = t.requests[0]
        h = {k.lower(): v for k, v in req.header_items()}
        self.assertEqual(req.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual((h["x-api-key"], h["anthropic-version"]), (KEY, "2023-06-01"))
        self.assertEqual(h["anthropic-beta"], AnthropicAPIProvider.FALLBACK_BETA)
        body = json.loads(req.data)
        self.assertEqual((body["model"], body["fallbacks"]), ("claude-sonnet-5-5", "default"))
        self.assertEqual((rec["provider"], rec["model"], rec["request_id"]), ("anthropic_api", "claude-sonnet-5-5", "req_mock_123"))
        self.assertNotIn(KEY, json.dumps(rec) + repr(p))
        p2 = AnthropicAPIProvider(KEY, opener=FakeAnthropic(), fallbacks=False)
        p2.complete("interpret", "S", "<question>x</question>")
        self.assertNotIn("fallbacks", json.loads(p2._open.requests[0].data))

    def test_errors_map_to_aierror_without_retry(self):
        for code, err in ((401, "authentication_error"), (429, "rate_limit_error"), (529, "overloaded_error"),
                          (500, "api_error"), (400, "invalid_request_error")):
            t = FakeAnthropic(fail=[(code, {"type": "error", "error": {"type": err}})])
            with self.assertRaises(AIError) as cm:
                AnthropicAPIProvider(KEY, opener=t).complete("x", "s", "u")
            self.assertIn(str(code), str(cm.exception))
            self.assertIn("req_fail", str(cm.exception))
            self.assertEqual(len(t.requests), 1)                    # never retried -> never double-billed
            self.assertNotIn(KEY, str(cm.exception))

    def test_refusal_truncation_and_empty_are_errors(self):
        for body in ({"type": "message", "stop_reason": "refusal", "stop_details": {"category": "cyber"}, "content": []},
                     {"type": "message", "stop_reason": "max_tokens", "content": [{"type": "text", "text": "{"}]},
                     {"type": "message", "stop_reason": "end_turn", "content": [{"type": "thinking", "thinking": ""}]}):
            with self.assertRaises(AIError):
                AnthropicAPIProvider(KEY, opener=FakeAnthropic(fail=[(200, body)])).complete("x", "s", "u")

    def test_missing_key_and_config_selection(self):
        with self.assertRaises(AIError):
            make_provider(Config.from_env(ai_provider="anthropic_api", anthropic_api_key="", data_dir=tempfile.mkdtemp()))
        cfg = Config.from_env(ai_provider="anthropic_api", anthropic_api_key=KEY, data_dir=tempfile.mkdtemp())
        self.assertIsInstance(make_provider(cfg), AnthropicAPIProvider)
        self.assertNotIn(KEY, json.dumps(cfg.public_view()))

    def test_full_paid_flow_with_mocked_api_is_labelled_genuine_path(self):
        d = tempfile.mkdtemp()
        svc = AutoNomos(Config.from_env(data_dir=d, payment_provider="mock", ai_provider="anthropic_api",
                                        anthropic_api_key=KEY), ai=AnthropicAPIProvider(KEY, opener=FakeAnthropic()))
        tid = svc.request_task({"capability": "research_snapshot", "topic": Q, "corpus": "agentnomos_quantum"})
        oid = svc.accept_offer(tid, svc.store.offer(task_id=tid)["_digest"]).rsplit("/", 1)[1]
        svc.payments.buyer_action(oid, "approve")
        svc.on_buyer_return(oid)
        self.assertEqual(svc.execute(tid), S.DELIVERED)
        rc = svc.store.receipt(tid)
        art = svc.store.artifact(rc["body"]["artifact"]["digest"])
        self.assertTrue(R.verify(rc, art, svc.store.events(tid))["valid"])
        runs = art["model_runs"]
        self.assertEqual({r["provider"] for r in runs}, {"anthropic_api"})
        self.assertEqual({r["request_id"] for r in runs}, {"req_mock_123"})
        self.assertEqual(rc["body"]["environment"], "MOCK")          # payment still MOCK: labelled

    def test_api_failure_after_payment_goes_to_review_not_rerun(self):
        d = tempfile.mkdtemp()
        t = FakeAnthropic(fail=[(529, {"type": "error", "error": {"type": "overloaded_error"}})])
        svc = AutoNomos(Config.from_env(data_dir=d, payment_provider="mock", ai_provider="anthropic_api",
                                        anthropic_api_key=KEY), ai=AnthropicAPIProvider(KEY, opener=t))
        tid = svc.request_task({"capability": "research_snapshot", "topic": Q, "corpus": "agentnomos_quantum"})
        oid = svc.accept_offer(tid, svc.store.offer(task_id=tid)["_digest"]).rsplit("/", 1)[1]
        svc.payments.buyer_action(oid, "approve")
        svc.on_buyer_return(oid)
        self.assertEqual(svc.execute(tid), S.REVIEW_REQUIRED)
        self.assertEqual(len(t.requests), 1)
        self.assertEqual(svc.store.execution(tid)["status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
