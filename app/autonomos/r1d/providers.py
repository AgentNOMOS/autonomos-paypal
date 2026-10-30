"""R1d model providers. A provider turns (system, user, JSON schema) into one model answer. It has no tools, no files,
no database, no payment object and no subprocess: it receives strings and returns a record. Budget and authorization
are NOT checked here; that is the ledger's job (governed.py calls the ledger before any provider).

FixtureProviderR1d        FIXTURE_EXECUTION. Deterministic local rules. Output is labelled FIXTURE. Not a model.
AnthropicMessagesProvider REAL_MODEL_EXECUTION only when the transport is the standard-library HTTPS opener and the
                          owner's key file was loaded. With ANY injected transport (tests, the offline emulator) it is
                          MOCK_EXECUTION, refuses a real key, and labels every record MOCK.

Anthropic Messages API (stdlib HTTPS; the project is dependency-free by design):
  POST https://api.anthropic.com/v1/messages, headers x-api-key / anthropic-version: 2023-06-01 / content-type.
  Body: model, max_tokens, system, one user message, output_config {format: json_schema, effort}. No `tools`, no
  `thinking` (adaptive by default on current models), no `fallbacks` and no `anthropic-beta` header: a refusal is a
  failure, never a silent re-run on another (more expensive) model.
One complete() = at most one HTTP request. Nothing here retries.
"""
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request

from . import FIXTURE_EXECUTION, MOCK_EXECUTION, REAL_MODEL_EXECUTION
from ..canon import digest, sha256_text

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
# HTTP statuses the API documents as refusals before processing (no output generated): charged 0 in the ledger.
REJECTED_STATUSES = {400: "INVALID_REQUEST", 401: "AUTHENTICATION", 402: "BILLING", 403: "PERMISSION",
                     404: "NOT_FOUND", 413: "REQUEST_TOO_LARGE", 429: "RATE_LIMITED"}


class ProviderFailure(Exception):
    """billing: NO (refused before processing), UNKNOWN (may have been processed), YES (a parsed response with usage)."""

    def __init__(self, kind, message, billing="UNKNOWN", http_status=None, request_id=None, response=None,
                 retry_after=None):
        super().__init__(f"{kind}: {message}")
        self.kind, self.billing, self.http_status = kind, billing, http_status
        self.request_id, self.response, self.retry_after = request_id, response, retry_after


def _record(provider, execution_class, transport, requested_model, model, system, user, schema, text, usage, wall,
            **extra):
    return dict({"provider": provider, "execution_class": execution_class, "transport": transport,
                 "requested_model": requested_model, "model": model, "system_sha256": sha256_text(system),
                 "user_sha256": sha256_text(user), "schema_digest": digest(schema),
                 "output_sha256": sha256_text(text or ""), "output": text, "usage": usage, "wall_s": round(wall, 3)},
                **extra)


class FixtureProviderR1d:
    """Deterministic stand-in: quotes the first sentence of the first passages. Labelled FIXTURE everywhere."""
    name = "fixture"
    execution_class = FIXTURE_EXECUTION
    model = "fixture-rules-r1d-v1"
    transport_label = "none (local deterministic rules)"

    def __init__(self):
        self.calls = 0

    def complete(self, system, user, schema, max_tokens, timeout_s, request_digest=None):
        self.calls += 1
        t0 = time.time()
        findings = []
        for i, p in enumerate(parse_passages(user)[:3]):
            sent = re.split(r"(?<=[.!?])\s+", p["text"].strip())[0][:200]
            if len(sent) < 12:
                continue
            findings.append({"finding_id": f"F{i + 1}", "claim": f"[FIXTURE] The document states: {sent[:120]}",
                             "source_document": p["source"], "anchor": p["id"], "quote": sent,
                             "evidence_class": "UNKNOWN_OR_UNSUPPORTED",
                             "limitation": "Fixture provider: no reasoning was performed; this is not model output."})
        text = json.dumps({"answer_summary": "[FIXTURE OUTPUT - not produced by an AI model]", "findings": findings,
                           "not_established": [{"topic": "everything", "statement":
                                                "Fixture provider: the question was not analysed."}],
                           "instructions_found_in_sources": False, "instructions_found_in_question": False})
        return _record("FIXTURE", FIXTURE_EXECUTION, self.transport_label, self.model, self.model, system, user,
                       schema, text, {"input_tokens": 0, "output_tokens": 0}, time.time() - t0,
                       request_id=None, response_id=None, stop_reason="end_turn", response_digest=sha256_text(text))


class AnthropicMessagesProvider:
    name = "anthropic_messages"
    DEADLINE_GRACE_S = 5.0                 # wall-clock allowance beyond the socket timeout before giving up

    def __init__(self, model, effort="medium", api_key=None, transport=None, transport_label=None):
        if transport is None:
            if not api_key:
                raise ProviderFailure("NO_CREDENTIAL", "REAL provider needs the owner's API key file", billing="NO")
            self._open, self.execution_class = urllib.request.urlopen, REAL_MODEL_EXECUTION
            self.transport_label = API_URL
        else:
            if api_key:
                raise ProviderFailure("KEY_WITH_TEST_TRANSPORT", "a real API key is never given to a test or emulated "
                                      "transport", billing="NO")
            self._open, self.execution_class = transport, MOCK_EXECUTION
            self.transport_label = transport_label or "injected-test-transport"
            api_key = "MOCK-TRANSPORT-NO-KEY"
        self._key, self.model, self.effort, self.calls = api_key, model, effort, 0

    def __repr__(self):
        return (f"AnthropicMessagesProvider(model={self.model!r}, class={self.execution_class}, "
                f"transport={self.transport_label!r}, key=[REDACTED])")

    def request_body(self, system, user, schema, max_tokens):
        return {"model": self.model, "max_tokens": int(max_tokens), "system": system,
                "messages": [{"role": "user", "content": user}],
                "output_config": {"format": {"type": "json_schema", "schema": schema}, "effort": self.effort}}

    def complete(self, system, user, schema, max_tokens, timeout_s, request_digest=None):
        """Exactly one HTTP request, bounded by a wall-clock deadline. Raises ProviderFailure; never retries."""
        self.calls += 1
        body = self.request_body(system, user, schema, max_tokens)
        req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST",
                                     headers={"x-api-key": self._key, "anthropic-version": API_VERSION,
                                              "content-type": "application/json"})
        t0 = time.time()
        status, headers, raw = self._send_with_deadline(req, timeout_s)
        rid = headers.get("request-id")
        try:
            d = json.loads(raw)
        except ValueError:
            raise ProviderFailure("UNPARSEABLE_RESPONSE", "2xx body is not JSON", request_id=rid) from None
        if not isinstance(d, dict) or d.get("type") != "message":
            raise ProviderFailure("UNEXPECTED_RESPONSE", "2xx body is not a message object", request_id=rid)
        usage = d.get("usage") if isinstance(d.get("usage"), dict) else None
        meta = {"request_id": rid, "response_id": d.get("id"), "model": d.get("model"), "usage": usage,
                "stop_reason": d.get("stop_reason"), "response_digest": hashlib.sha256(raw).hexdigest()}
        if usage is None:
            raise ProviderFailure("NO_USAGE", "response carries no usage; cannot account for it", request_id=rid,
                                  response=meta)
        if not str(d.get("model", "")).startswith(self.model):
            raise ProviderFailure("MODEL_MISMATCH", f"requested {self.model}, provider reported {d.get('model')!r}",
                                  billing="YES", request_id=rid, response=meta)
        stop = d.get("stop_reason")
        if stop == "refusal":
            cat = (d.get("stop_details") or {}).get("category")
            raise ProviderFailure("REFUSAL", f"model declined (category {cat}); not re-sent", billing="YES",
                                  request_id=rid, response=meta)
        if stop == "max_tokens":
            raise ProviderFailure("TRUNCATED", "output truncated at max_tokens", billing="YES", request_id=rid,
                                  response=meta)
        if stop != "end_turn":
            raise ProviderFailure("UNEXPECTED_STOP", f"stop_reason {stop!r}", billing="YES", request_id=rid,
                                  response=meta)
        blocks = d.get("content") if isinstance(d.get("content"), list) else []
        odd = sorted({str(b.get("type")) for b in blocks if isinstance(b, dict)} -
                     {"text", "thinking", "redacted_thinking"})
        if odd:                                    # no tools were offered: any tool block is an anomaly
            raise ProviderFailure("UNEXPECTED_CONTENT", f"content block types {odd}", billing="YES", request_id=rid,
                                  response=meta)
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        if not text.strip():
            raise ProviderFailure("NO_TEXT", "no text content", billing="YES", request_id=rid, response=meta)
        return _record(self.name, self.execution_class, self.transport_label, self.model, d.get("model"), system,
                       user, schema, text, usage, time.time() - t0, request_id=rid, response_id=d.get("id"),
                       stop_reason=stop, response_digest=meta["response_digest"])

    def _send_with_deadline(self, req, timeout_s):
        """Run the request in a worker thread and stop waiting at the deadline: a slow-drip response cannot hold the
        execution past timeout_s (+5 s grace). A late answer is discarded (the ledger already counts it UNKNOWN)."""
        box = {}

        def work():
            try:
                with self._open(req, timeout=timeout_s) as r:
                    box["ok"] = (getattr(r, "status", 200), dict(getattr(r, "headers", {}) or {}), r.read())
            except urllib.error.HTTPError as e:
                box["http"] = e
            except BaseException as e:            # noqa: BLE001 - classified below, never re-raised raw
                box["exc"] = e
        th = threading.Thread(target=work, daemon=True)
        th.start()
        th.join(timeout_s + self.DEADLINE_GRACE_S)
        if th.is_alive():
            raise ProviderFailure("TIMEOUT", f"no complete response within {timeout_s:.0f}s; outcome unknown")
        if "http" in box:
            e = box["http"]
            hdrs = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
            rid = hdrs.get("request-id")
            try:
                etype = (json.loads(e.read()).get("error") or {}).get("type", "")
            except Exception:                      # noqa: BLE001
                etype = ""
            if e.code in REJECTED_STATUSES:
                raise ProviderFailure(REJECTED_STATUSES[e.code], f"HTTP {e.code} {etype}; not retried", billing="NO",
                                      http_status=e.code, request_id=rid, retry_after=hdrs.get("retry-after"))
            kind = "OVERLOADED" if e.code == 529 else ("TIMEOUT" if e.code in (408, 504) else "SERVER_ERROR")
            raise ProviderFailure(kind, f"HTTP {e.code} {etype}; outcome treated as unknown; not retried",
                                  http_status=e.code, request_id=rid)
        if "exc" in box:
            e = box["exc"]
            kind = "TIMEOUT" if isinstance(e, TimeoutError) or "timed out" in str(e).lower() else "TRANSPORT_ERROR"
            raise ProviderFailure(kind, f"{type(e).__name__} during the request; outcome unknown; not retried")
        status, headers, raw = box["ok"]
        headers = {k.lower(): v for k, v in headers.items()}
        if not 200 <= int(status) < 300:
            raise ProviderFailure("SERVER_ERROR", f"unexpected HTTP status {status}", request_id=headers.get("request-id"))
        return status, headers, raw


# ---------------------------------------------------------------------- prompt framing shared with the emulator
PASSAGE_RE = r'\[\[SOURCE (?P<nonce>[0-9a-f]{16}) id="(?P<id>[^"]+)" source="(?P<source>[^"]+)"\]\]\n(?P<text>.*?)\n\[\[END (?P=nonce)\]\]'


def parse_passages(user):
    return [m.groupdict() for m in re.finditer(PASSAGE_RE, user, re.S)]


class OfflineMessagesEmulator:
    """Offline stand-in for the Messages API used by the MOCK demo. It answers in the R1d schema by picking, for each
    evidence class, a sentence from the supplied passages that contains that class's cue words. It is a transport for
    AnthropicMessagesProvider, so the provider labels everything MOCK_EXECUTION. NOT a model; no reasoning."""
    label = "offline-emulator (no network, no model)"

    def __init__(self, class_cues):
        self.class_cues, self.requests = class_cues, []

    def __call__(self, req, timeout=0):
        self.requests.append(req)
        body = json.loads(req.data)
        user = body["messages"][0]["content"]
        passages = parse_passages(user)
        findings, missing, used = [], [], set()
        for cls, cue in self.class_cues.items():
            hit = None
            for p in passages:
                for s in re.split(r"(?<=[.!?])\s+|\s+\*\s+", p["text"]):
                    s = s.strip(" *")
                    if 24 <= len(s) <= 300 and re.search(cue, s, re.I) and (p["id"], s) not in used \
                            and s in p["text"]:
                        hit = (p, s)
                        break
                if hit:
                    break
            if hit and len(findings) < 5:
                used.add((hit[0]["id"], hit[1]))
                findings.append({"finding_id": f"F{len(findings) + 1}", "claim": f"The documents state that {hit[1]}",
                                 "source_document": hit[0]["source"], "anchor": hit[0]["id"], "quote": hit[1],
                                 "evidence_class": cls,
                                 "limitation": "Offline emulator selection by cue words; not model reasoning."})
            else:
                missing.append({"topic": cls, "statement": "Not established by the supplied evidence."})
        text = json.dumps({"answer_summary": "[MOCK EXECUTION - offline emulator, not produced by an AI model]",
                           "findings": findings, "not_established": missing,
                           "instructions_found_in_sources": False, "instructions_found_in_question": False})
        out = {"type": "message", "id": f"msg_mock_{len(self.requests):04d}", "model": body["model"],
               "role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": text}],
               "usage": {"input_tokens": len(req.data) // 4, "output_tokens": len(text) // 4}}
        return _FakeResponse(json.dumps(out).encode(), {"request-id": f"mock-req-{len(self.requests):04d}"})


class _FakeResponse:
    def __init__(self, raw, headers, status=200):
        self._raw, self.headers, self.status = raw, headers, status

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
