"""AI providers. A provider turns (system, user) text into text. It has no tools, no network of its own (except the
Anthropic API transport itself), and never sees secrets, prices it could change, or payment state.

fixture        deterministic stand-in for tests and offline demos. NOT an AI model; outputs are labelled FIXTURE.
claude_cli     headless Claude Code (`claude -p`, tools disabled, no MCP, no session persistence).
anthropic_api  Anthropic Messages API over HTTPS (needs ANTHROPIC_API_KEY; billed to that key).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from .canon import sha256_text

DEFAULT_MODEL = os.environ.get("AUTONOMOS_MODEL", "claude-sonnet-5-5")


class AIError(RuntimeError):
    pass


def extract_json(text):
    """First top-level JSON object in a model reply (tolerates ```json fences)."""
    if text is None:
        raise AIError("empty model output")
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    s = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    try:
        return json.loads(s)
    except Exception as e:
        raise AIError(f"model output is not valid JSON: {e}") from e


def _record(provider, model, system, user, text, usage, wall, extra=None):
    return dict({"provider": provider, "model": model, "system_sha256": sha256_text(system),
                 "user_sha256": sha256_text(user), "output_sha256": sha256_text(text or ""), "output": text,
                 "usage": usage, "wall_s": round(wall, 3)}, **(extra or {}))


class FixtureProvider:
    """Deterministic, rule-based stand-in. Labelled so it can never be mistaken for model output."""
    name = "FIXTURE"

    def __init__(self, scripted=None):
        self.scripted = scripted or {}     # role -> text (tests can script hostile outputs)
        self.calls = 0

    def complete(self, role, system, user, max_seconds=60):
        self.calls += 1
        t0 = time.time()
        if role in self.scripted:
            text = self.scripted[role]
        elif role == "interpret":
            q = user.split("<question>")[1].split("</question>")[0]
            words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}", q)][:8]
            text = json.dumps({"restated_question": q.strip(), "search_terms": words or ["overview"],
                               "required_output": "findings with quotes", "out_of_scope_parts": [],
                               "instructions_found_in_input": False})
        else:
            blocks = re.findall(r'<passage id="([^"]+)">(.*?)</passage>', user, re.S)
            findings = []
            for pid, body in blocks[:3]:
                sent = re.split(r"(?<=[.!?])\s+", body.strip())[0][:200]
                findings.append({"claim": f"[FIXTURE] The document states: {sent[:120]}",
                                 "evidence": [{"passage_id": pid, "quote": sent}]})
            text = json.dumps({"summary": "[FIXTURE OUTPUT - not produced by an AI model]", "findings": findings,
                               "uncertainties": ["Fixture provider: no reasoning was performed."],
                               "unanswered": [], "confidence": "low"})
        return _record("FIXTURE", "fixture-rules-v1", system, user, text, {"output_tokens": 0}, time.time() - t0)


class ClaudeCLIProvider:
    """Headless Claude Code. The executable is pinned by SHA-256 at start; a change aborts (no silent retry)."""
    name = "claude_cli"

    def __init__(self, model=DEFAULT_MODEL, exe="/usr/bin/claude", max_budget_usd=0.40):
        real = os.path.realpath(shutil.which(exe) or exe)
        if not os.path.exists(real):
            raise AIError(f"claude executable not found at {exe}")
        self.exe, self.model, self.max_budget = real, model, max_budget_usd
        self.exe_sha = hashlib.sha256(open(real, "rb").read()).hexdigest()
        self.calls = 0

    def complete(self, role, system, user, max_seconds=180):
        if not os.path.exists(self.exe) or hashlib.sha256(open(self.exe, "rb").read()).hexdigest() != self.exe_sha:
            raise AIError("claude executable changed or missing since start; refusing to call")
        self.calls += 1
        env = dict(os.environ, DISABLE_AUTOUPDATER="1")
        t0 = time.time()
        with tempfile.TemporaryDirectory(prefix="autonomos_ai_") as cwd:
            try:
                p = subprocess.run([self.exe, "-p", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
                                    "--setting-sources", "", "--output-format", "json", "--model", self.model,
                                    "--max-budget-usd", str(self.max_budget), "--system-prompt", system],
                                   input=user, capture_output=True, text=True, timeout=max_seconds, cwd=cwd, env=env)
            except subprocess.TimeoutExpired as e:
                raise AIError(f"model call exceeded {max_seconds}s") from e
        try:
            envl = json.loads(p.stdout)
        except Exception as e:
            raise AIError(f"claude CLI rc={p.returncode}: unparseable envelope") from e
        if p.returncode != 0 or envl.get("is_error"):
            raise AIError(f"claude CLI error rc={p.returncode}: {str(envl.get('result'))[:200]}")
        u = envl.get("usage") or {}
        usage = {k: u.get(k) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                        "cache_read_input_tokens")}
        return _record("claude_cli", self.model, system, user, envl.get("result"), usage, time.time() - t0,
                       {"model_usage_keys": sorted((envl.get("modelUsage") or {}).keys()),
                        "notional_usd": envl.get("total_cost_usd"), "num_turns": envl.get("num_turns"),
                        "exe_sha256": self.exe_sha})


class AnthropicAPIProvider:
    """Anthropic Messages API over stdlib HTTPS (the project is dependency-free by design, so no SDK).

    Billed to the key owner. R1b: exercised ONLY against a fake transport (no billed request has been made).
    * No automatic retries: a failed call raises AIError and the paid task goes to REVIEW_REQUIRED (never re-run
      blindly, never double-billed). 401/403/404/400/429/529/5xx/timeouts are all mapped to AIError with the
      Anthropic `request-id` when present.
    * `stop_reason` is checked before content is used: `refusal` and `max_tokens` raise AIError.
    * Thinking blocks (adaptive thinking is on by default on current models) are ignored; only `text` is used.
    * Refusal fallback (server-side, beta) is ON by default and can be disabled with AUTONOMOS_AI_FALLBACKS=0.
    * The record keeps the model the API reports and the request id, so a judge can tell a genuine run from
      a FIXTURE run (provider "FIXTURE", model "fixture-rules-v1", output prefixed "[FIXTURE").
    """
    name = "anthropic_api"
    URL = "https://api.anthropic.com/v1/messages"
    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, api_key, model=DEFAULT_MODEL, max_tokens=16000, opener=None, fallbacks=None):
        if not api_key:
            raise AIError("ANTHROPIC_API_KEY is not set")
        self._key, self.model, self.max_tokens, self.calls = api_key, model, max_tokens, 0
        self._open = opener or urllib.request.urlopen
        self.fallbacks = (os.environ.get("AUTONOMOS_AI_FALLBACKS", "1") != "0") if fallbacks is None else fallbacks

    def __repr__(self):
        return f"AnthropicAPIProvider(model={self.model!r}, key=[REDACTED])"

    def complete(self, role, system, user, max_seconds=300):
        self.calls += 1
        req_body = {"model": self.model, "max_tokens": self.max_tokens, "system": system,
                    "messages": [{"role": "user", "content": user}]}
        headers = {"x-api-key": self._key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        if self.fallbacks:
            req_body["fallbacks"] = "default"
            headers["anthropic-beta"] = self.FALLBACK_BETA
        req = urllib.request.Request(self.URL, data=json.dumps(req_body).encode(), method="POST", headers=headers)
        t0 = time.time()
        try:
            with self._open(req, timeout=max_seconds) as r:
                rid = (getattr(r, "headers", None) or {}).get("request-id")
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            rid = (e.headers or {}).get("request-id") if e.headers else None
            try:
                err = (json.loads(e.read()).get("error") or {}).get("type", "")
            except Exception:
                err = ""
            raise AIError(f"Anthropic API HTTP {e.code} {err} request-id={rid}; not retried") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise AIError(f"Anthropic API unreachable or timed out ({type(e).__name__}); not retried") from None
        except ValueError:
            raise AIError("Anthropic API returned an unparseable body") from None
        if not isinstance(d, dict) or d.get("type") == "error":
            raise AIError("Anthropic API returned an error object")
        stop = d.get("stop_reason")
        if stop == "refusal":
            raise AIError(f"model declined (refusal, category {(d.get('stop_details') or {}).get('category')})")
        if stop == "max_tokens":
            raise AIError("model output truncated at max_tokens")
        text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        if not text:
            raise AIError(f"no text content (stop_reason {stop})")
        return _record("anthropic_api", d.get("model", self.model), system, user, text, d.get("usage"),
                       time.time() - t0, {"request_id": rid, "stop_reason": stop, "requested_model": self.model})


def make_provider(cfg, scripted=None):
    if cfg.ai_provider == "claude_cli":
        return ClaudeCLIProvider()
    if cfg.ai_provider == "anthropic_api":
        return AnthropicAPIProvider(cfg.anthropic_api_key)
    return FixtureProvider(scripted)
