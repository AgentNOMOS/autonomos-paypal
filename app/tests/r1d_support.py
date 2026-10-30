"""Shared helpers for the R1d tests. Everything is offline:

* install_network_guard(): any socket connection or name lookup to a non-loopback address raises NetworkBlocked
  (the R1c tests' own loopback HTTP servers keep working). Installed on import.
* FakeMessagesAPI: a scriptable stand-in for POST /v1/messages. It is an INJECTED transport, so the provider labels
  every record MOCK_EXECUTION. It never sees a real key.
* honest_answer(): an evidence-backed answer whose quotations are copied from the real agentnomos_quantum passages.
"""
import io
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error

APP = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, APP)

from autonomos.config import Config  # noqa: E402
from autonomos.r1d import aiconfig  # noqa: E402
from autonomos.r1d.providers import parse_passages  # noqa: E402
from autonomos.r1d.service import R1dService  # noqa: E402

Q = "What hardware evidence exists for the quantum-kernel experiment, and what is still unverified?"
CORPUS = os.path.join(APP, "corpus")
SET = "agentnomos_quantum"
FAKE_KEY = "sk-ant-api03-TEST-ONLY-NOT-A-REAL-KEY-0000000000"
LOOPBACK = ("127.0.0.1", "::1", "localhost")


class NetworkBlocked(RuntimeError):
    pass


NETWORK_ATTEMPTS = []


def install_network_guard():
    if getattr(socket, "_r1d_guard", False):
        return
    real_connect, real_connect_ex, real_gai = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def _ok(addr):
        return isinstance(addr, tuple) and str(addr[0]) in LOOPBACK or isinstance(addr, (str, bytes))   # AF_UNIX ok

    def connect(self, addr):
        if not _ok(addr):
            NETWORK_ATTEMPTS.append(addr)
            raise NetworkBlocked(f"offline test attempted a network connection to {addr!r}")
        return real_connect(self, addr)

    def connect_ex(self, addr):
        if not _ok(addr):
            NETWORK_ATTEMPTS.append(addr)
            raise NetworkBlocked(f"offline test attempted a network connection to {addr!r}")
        return real_connect_ex(self, addr)

    def getaddrinfo(host, *a, **k):
        if host not in LOOPBACK and host is not None:
            NETWORK_ATTEMPTS.append(host)
            raise NetworkBlocked(f"offline test attempted a DNS lookup of {host!r}")
        return real_gai(host, *a, **k)
    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = connect, connect_ex, getaddrinfo
    socket._r1d_guard = True


install_network_guard()


# ---------------------------------------------------------------------- fake Messages API
class _Resp:
    def __init__(self, raw, headers, status=200, read_exc=None):
        self._raw, self.headers, self.status, self._exc = raw, headers, status, read_exc

    def read(self):
        if self._exc:
            raise self._exc
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def message(text, model="claude-opus-5-5", stop="end_turn", usage=None, mid="msg_test_0001", extra_blocks=()):
    return {"type": "message", "id": mid, "model": model, "role": "assistant", "stop_reason": stop,
            "content": [{"type": "thinking", "thinking": ""}, *extra_blocks, {"type": "text", "text": text}],
            "usage": usage or {"input_tokens": 1200, "output_tokens": 800}}


class FakeMessagesAPI:
    """script items (consumed in order; the last one repeats):
      ("answer", obj | callable(user, body) -> obj)   200 with the JSON text
      ("text", str)                                   200 with that text
      ("message", dict | callable(body) -> dict)      200 with this message object
      ("http", code, error_type[, headers])           HTTPError
      ("raise", exception)                            transport exception before any response
      ("read_raise", exception)                       2xx headers, then the body read fails (interrupted response)
      ("raw", bytes)                                  200 with raw bytes
      ("sleep", seconds, next_item)                   wait, then behave like next_item
      ("exit", code)                                  os._exit in the middle of the request (process crash)
    """

    def __init__(self, *script, log_path=None):
        self.script, self.requests, self.log_path = list(script) or [("answer", honest_answer)], [], log_path

    def __call__(self, req, timeout=0):
        self.requests.append(req)
        if self.log_path:
            with open(self.log_path, "a") as f:
                f.write(f"{os.getpid()} {time.time()}\n")
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return self._do(step, req)

    def _do(self, step, req):
        body = json.loads(req.data)
        user = body["messages"][0]["content"]
        kind = step[0]
        hdr = {"request-id": f"req_test_{len(self.requests):04d}"}
        if kind == "sleep":
            time.sleep(step[1])
            return self._do(step[2], req)
        if kind == "exit":
            os._exit(step[1])
        if kind == "raise":
            raise step[1]
        if kind == "http":
            h = dict(hdr, **(step[3] if len(step) > 3 else {}))
            raise urllib.error.HTTPError(req.full_url, step[1], "error", h,
                                         io.BytesIO(json.dumps({"type": "error", "error": {"type": step[2]}}).encode()))
        if kind == "raw":
            return _Resp(step[1], hdr)
        if kind == "read_raise":
            return _Resp(b"", hdr, read_exc=step[1])
        if kind == "answer":
            obj = step[1](user, body) if callable(step[1]) else step[1]
            return _Resp(json.dumps(message(json.dumps(obj), model=body["model"])).encode(), hdr)
        if kind == "text":
            return _Resp(json.dumps(message(step[1], model=body["model"])).encode(), hdr)
        if kind == "message":
            m = step[1](body) if callable(step[1]) else step[1]
            return _Resp(json.dumps(m).encode(), hdr)
        raise AssertionError(f"unknown script step {kind}")


# ---------------------------------------------------------------------- answers
HONEST_FINDINGS = [
    ("HARDWARE_EXECUTION", "qkrl_README.md#p008",
     "We ran one job on `ibm_marrakesh` (9 qubits, 5 circuits × 4,096 shots = 20,480 shots, 660 CZ per submitted "
     "circuit).",
     "The documents report that one job was run on `ibm_marrakesh` with 5 circuits × 4,096 shots = 20,480 shots.",
     "This is the project's own account; the provider's job record was not supplied."),
    ("REPORTED_HARDWARE_RESULT", "qkrl_README.md#p004",
     "| surviving contrast f, global-depolarising fit | 1 | **0.0048** (95 % CI 0.0033 – 0.0065) |",
     "The documents report a surviving contrast f of 0.0048 (95 % CI 0.0033 – 0.0065) from the global-depolarising "
     "fit.",
     "The documents report this result, but raw hardware measurements were not provided."),
    ("RAW_EVIDENCE_AVAILABILITY", "qkrl_README.md#p003",
     "The raw IBM job files are withheld from this public release until redistribution rights are confirmed "
     "(`WITHHELD_EVIDENCE.md`), so these values cannot be recomputed from this repository",
     "The raw IBM job files are withheld from this public release, so these values cannot be recomputed from this "
     "repository.",
     "Independent verification unavailable."),
    ("COMPILED_ONLY", "qkrl_IONQ_HARDWARE_PLAN.md#p020",
     "Payloads are in `experiments/ionq_native_payloads/` and were **never submitted**.",
     "The IonQ native payloads were never submitted.",
     "Compiled artifacts only; says nothing about hardware behaviour."),
    ("PLANNED_NOT_PERFORMED", "qkrl_IONQ_HARDWARE_PLAN.md#p000",
     "IonQ hardware experiment plan (PROPOSED — no IonQ job has been run)",
     "The IonQ hardware experiment is a proposed plan; no IonQ job has been run.",
     "A plan, not a result."),
]


def finding(i, cls, anchor, quote, claim, lim, src=None):
    return {"finding_id": f"F{i}", "claim": claim, "source_document": src or anchor.split("#")[0],
            "anchor": f"{SET}/{anchor}", "quote": quote, "evidence_class": cls, "limitation": lim}


def honest_answer(user=None, body=None):
    shown = {p["id"] for p in parse_passages(user)} if user else None
    fs = [finding(i + 1, *f) for i, f in enumerate(HONEST_FINDINGS)]
    if shown is not None:
        fs = [f for f in fs if f["anchor"] in shown]
    return {"answer_summary": "The documents describe one IBM hardware job with reported, non-recomputable results; "
                              "IonQ work is compiled or planned only.",
            "findings": fs,
            "not_established": [{"topic": "Independent verification of the IBM hardware numbers",
                                 "statement": "Independent verification unavailable: the raw job files are withheld."},
                                {"topic": "IonQ hardware results",
                                 "statement": "Not established by the supplied evidence; the documents state that no "
                                              "IonQ hardware result exists yet."}],
            "instructions_found_in_sources": False, "instructions_found_in_question": False}


def answer_with(*replacements, keep=None):
    """honest_answer with some findings replaced: replacements = (index, dict-of-field-overrides)."""
    def build(user=None, body=None):
        a = honest_answer(user, body)
        for idx, over in replacements:
            a["findings"][idx] = dict(a["findings"][idx], **over)
        if keep is not None:
            a["findings"] = [a["findings"][i] for i in keep]
        return a
    return build


# ---------------------------------------------------------------------- service helpers
def new_service(runtime=None, data_dir=None, corpus_root=None, payments=None):
    d = data_dir or tempfile.mkdtemp(prefix="r1d_test_")
    cfg = Config.from_env(data_dir=d, payment_provider="mock", ai_provider="fixture")
    return R1dService(cfg, payments=payments, runtime=runtime or aiconfig.offline_runtime("fixture"),
                      corpus_root=corpus_root)


def mock_runtime(api=None, **envelope_overrides):
    api = api or FakeMessagesAPI()
    rt = aiconfig.offline_runtime("mock", transport=api, transport_label="test-fake-messages-api", **envelope_overrides)
    return rt, api


def paid_task(svc, question=Q, corpus=SET):
    """request -> offer -> accept -> mock approval -> capture verified. Returns the task id (EXECUTION_AUTHORIZED)."""
    tid = svc.request_task({"capability": "research_snapshot", "topic": question, "corpus": corpus,
                            "buyer_ref": "r1d-test"})
    oid = svc.accept_offer(tid, svc.store.offer(task_id=tid)["_digest"]).rsplit("/", 1)[1]
    svc.payments.buyer_action(oid, "approve")
    svc.on_buyer_return(oid)
    return tid


def payment_snapshot(svc):
    """Every payment-side table, for before/after comparisons."""
    out = {}
    for t in ("offers", "orders", "captures", "capture_attempts", "webhooks"):
        out[t] = sorted(svc.store.db.execute(f"SELECT * FROM {t}").fetchall())
    return out


def copy_corpus(extra_files=None):
    import shutil
    root = tempfile.mkdtemp(prefix="r1d_corpus_")
    shutil.copytree(os.path.join(CORPUS, SET), os.path.join(root, SET))
    for name, text in (extra_files or {}).items():
        with open(os.path.join(root, SET, name), "w") as f:
            f.write(text)
    return root
