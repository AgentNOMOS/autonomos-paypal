"""R1e: RECORDED replay of one historical Messages API answer, for offline end-to-end runs and demos.

A replay is NOT a model call. RecordedResponseTransport is an injected transport for AnthropicMessagesProvider, so the
provider labels every record MOCK_EXECUTION (receipt: real_model_inference false) and its transport label starts with
"RECORDED_REPLAY of <historical request id>". Its ids are prefixed "REPLAY-" so they can never be mistaken for provider
ids, and the offline envelope prices it at zero (no request leaves the machine).

The recording answers only the request it was recorded for: the same question and exactly the same passages (ids and
excerpt text, checked against the recorded provided_digest); the per-execution delimiter nonce may differ. Anything
else raises RecordingMismatch inside the transport (the provider books it OUTCOME_UNKNOWN at the MOCK price of zero,
the task goes to REVIEW_REQUIRED, nothing is delivered). R1e.1: a transport answers each REQUEST at most once (keyed by
the per-execution delimiter nonce), so a re-sent request is refused while a demo server can replay for several tasks.
"""
import hashlib
import json
import os
import re

from . import MOCK_EXECUTION
from .aiconfig import Runtime
from .envelope import offline_envelope
from .providers import AnthropicMessagesProvider, _FakeResponse, parse_passages

REPLAY_PREFIX = "RECORDED_REPLAY"


class RecordingMismatch(RuntimeError):
    pass


class RecordedResponseTransport:
    def __init__(self, recording_dir):
        self.dir = recording_dir
        self.meta = json.load(open(os.path.join(recording_dir, "RECORDING.json")))
        self.raw = open(os.path.join(recording_dir, "raw_output.txt"), "rb").read()
        if hashlib.sha256(self.raw).hexdigest() != self.meta["raw_output_sha256"]:
            raise RecordingMismatch("RECORDING_CORRUPT: raw output digest differs from RECORDING.json")
        self.label = (f"{REPLAY_PREFIX} of {self.meta['historical_request_id']} ({self.meta['model']}, recorded "
                      f"{self.meta['recorded_utc']}, raw sha256 {self.meta['raw_output_sha256'][:12]}): not a model "
                      f"call")
        self.requests, self.fail_with, self.answered = [], None, set()

    def __call__(self, req, timeout=0):
        self.requests.append(req)
        body = json.loads(req.data)
        user = body["messages"][0]["content"]
        nonce = re.search(r"Random tag for this request: ([0-9a-f]{16})", user)
        if not nonce or nonce.group(1) in self.answered:
            raise RecordingMismatch("RECORDING_ALREADY_REPLAYED: this request was already answered once")
        self.answered.add(nonce.group(1))
        m = re.search(r'<question tag="[0-9a-f]{16}">\n(.*?)\n</question tag=', user, re.S)
        if not m or m.group(1) != self.meta["question"]:
            raise RecordingMismatch("RECORDING_QUESTION_MISMATCH: the recording was made for a different question")
        shown = {p["id"]: p["text"] for p in parse_passages(user)}
        if list(shown) != self.meta["passage_ids"] or hashlib.sha256(
                json.dumps(shown, sort_keys=True).encode()).hexdigest() != self.meta["provided_digest"]:
            raise RecordingMismatch("RECORDING_PASSAGE_MISMATCH: the recording was made for other passages")
        if body.get("model") != self.meta["model"]:
            raise RecordingMismatch("RECORDING_MODEL_MISMATCH")
        if self.fail_with is not None:                     # test hook: an interrupted response after sending
            raise self.fail_with
        out = {"type": "message", "id": f"REPLAY-{self.meta['historical_response_id']}", "model": self.meta["model"],
               "role": "assistant", "stop_reason": self.meta["stop_reason"],
               "content": [{"type": "text", "text": self.raw.decode("utf-8")}], "usage": dict(self.meta["usage"])}
        return _FakeResponse(json.dumps(out).encode(), {"request-id": f"REPLAY-{self.meta['historical_request_id']}"})


def recording_runtime(recording_dir, **envelope_overrides):
    t = RecordedResponseTransport(recording_dir)
    p = AnthropicMessagesProvider(t.meta["model"], envelope_overrides.get("effort", "medium"), transport=t,
                                  transport_label=t.label)
    return Runtime("recorded", p, offline_envelope(MOCK_EXECUTION, "anthropic_messages", t.meta["model"],
                                                   **envelope_overrides))
