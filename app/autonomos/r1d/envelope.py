"""R1d cost governance: model prices, the execution envelope and the owner's authorization for REAL execution.

Nothing here performs I/O beyond reading the owner's authorization file. Prices are a REFERENCE table that R1d could
not re-verify live (zero-network mission); a REAL execution therefore needs an owner-confirmed price in the
authorization file, which may not be lower than the reference. Without a price the estimate is UNKNOWN and REAL
execution is refused.

Token bounds:
  * output: `max_tokens` is enforced by the API on every request (thinking tokens are billed as output and count
    against it), so the output side of a request is hard-bounded by the envelope.
  * input: no network token count is made. The bound is the UTF-8 byte length of everything sent (system, user text,
    JSON schema) plus a fixed overhead allowance. ASSUMPTION: the tokenizer never emits more tokens than bytes, and the
    API's own framing / structured-output scaffolding fits in the allowance. Every completed request is checked
    against this bound afterwards; an excess freezes the authorization (fail closed).
"""
import json
import os
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from . import FIXTURE_EXECUTION, MOCK_EXECUTION, REAL_MODEL_EXECUTION

PRICE_SOURCE = ("Anthropic first-party API list prices as cached in the bundled claude-api reference (cached "
                "2026-10-06). Not re-verified against the live pricing page in R1d (zero-network mission).")
# USD per million tokens (input, output). Haiku 5.5: rate for prompts up to 100K tokens.
REFERENCE_PRICES = {
    "claude-fable-5-1": ("10.00", "50.00"),
    "claude-opus-5-5": ("4.00", "20.00"),
    "claude-opus-5": ("5.00", "25.00"),
    "claude-sonnet-5-5": ("2.00", "10.00"),
    "claude-sonnet-5": ("2.00", "10.00"),
    "claude-haiku-5-5": ("0.10", "0.50"),
}
# R1d sends no cache_control, so cache tokens should be 0. If any are reported they are charged conservatively.
CACHE_WRITE_MULT, CACHE_READ_MULT = Decimal("2"), Decimal("1")
EFFORTS = ("low", "medium", "high")
DEFAULT_REAL_MODEL = "claude-opus-5-5"

# Code-level ceilings: an authorization file above these is refused outright (typo / runaway protection).
HARD_LIMITS = {"max_requests_total": 20, "max_requests_per_execution": 2, "max_executions": 10,
               "max_input_tokens_per_request": 200_000, "max_output_tokens_per_request": 32_000,
               "max_total_tokens": 1_000_000, "max_usd_total": Decimal("10.00"), "request_timeout_s": 600}
REAL_GO_PREFIX = "real-ai:"          # AUTONOMOS_R1D_REAL_AI_GO must equal "real-ai:<authorization_id>"


class EnvelopeError(ValueError):
    pass


def _dec(x, what):
    try:
        d = Decimal(str(x))
    except (InvalidOperation, TypeError):
        raise EnvelopeError(f"{what} is not a decimal number") from None
    if not d.is_finite() or d < 0:
        raise EnvelopeError(f"{what} must be a finite non-negative number")
    return d


@dataclass(frozen=True)
class Envelope:
    """Limits for model requests made under one authorization. Enforced by the ledger, inside a write transaction."""
    authorization_id: str
    execution_class: str
    provider: str                       # "fixture" | "anthropic_messages"
    model: str
    effort: str = "medium"
    max_requests_per_execution: int = 1
    max_requests_total: int = 1000
    max_executions: int = 1000
    max_input_tokens_per_request: int = 60_000
    max_output_tokens_per_request: int = 12_000
    max_total_tokens: int = 100_000_000
    max_usd_total: Decimal = None       # None: no USD limit (only allowed when the price is exactly zero)
    price_in: Decimal = None            # USD per million tokens; None = UNKNOWN
    price_out: Decimal = None
    price_basis: str = "UNKNOWN"
    request_timeout_s: float = 120.0
    overhead_tokens: int = 4000
    allowed_payment_environments: tuple = ("MOCK", "SANDBOX")
    not_after: float = None
    bound_data_dir: str = None          # REAL: the one database whose ledger enforces this authorization
    owner_statement: str = field(default="", repr=False)

    # -------------------------------------------------------------- estimates
    @property
    def price_known(self):
        return self.price_in is not None and self.price_out is not None

    def input_upper_bound(self, *texts):
        return sum(len(t.encode("utf-8")) for t in texts) + self.overhead_tokens

    def worst_case_usd(self, est_input_tokens, max_output_tokens):
        if not self.price_known:
            return None
        return (Decimal(est_input_tokens) * self.price_in + Decimal(max_output_tokens) * self.price_out) / 1_000_000

    def cost_of(self, usage):
        """USD for reported usage (input, output, cache write/read)."""
        if not self.price_known:
            return None
        u = usage or {}
        i, o = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
        cw, cr = int(u.get("cache_creation_input_tokens") or 0), int(u.get("cache_read_input_tokens") or 0)
        return (Decimal(i) * self.price_in + Decimal(o) * self.price_out + Decimal(cw) * self.price_in * CACHE_WRITE_MULT
                + Decimal(cr) * self.price_in * CACHE_READ_MULT) / 1_000_000

    def authorization_worst_case_usd(self):
        w = self.worst_case_usd(self.max_input_tokens_per_request, self.max_output_tokens_per_request)
        return None if w is None else w * self.max_requests_total

    def public_view(self):
        return {"authorization_id": self.authorization_id, "execution_class": self.execution_class,
                "provider": self.provider, "model": self.model, "effort": self.effort,
                "max_requests_per_execution": self.max_requests_per_execution,
                "max_requests_total": self.max_requests_total, "max_executions": self.max_executions,
                "max_input_tokens_per_request": self.max_input_tokens_per_request,
                "max_output_tokens_per_request": self.max_output_tokens_per_request,
                "max_total_tokens": self.max_total_tokens,
                "max_usd_total": None if self.max_usd_total is None else str(self.max_usd_total),
                "price_usd_per_mtok": None if not self.price_known else {"input": str(self.price_in),
                                                                          "output": str(self.price_out)},
                "price_basis": self.price_basis, "request_timeout_s": self.request_timeout_s,
                "input_estimate_overhead_tokens": self.overhead_tokens,
                "allowed_payment_environments": list(self.allowed_payment_environments),
                "not_after": self.not_after, "bound_data_dir": self.bound_data_dir}


def offline_envelope(execution_class, provider, model, **overrides):
    """Envelope for FIXTURE / MOCK runs: zero price (no paid request can be made), generous counts, same checks."""
    assert execution_class in (FIXTURE_EXECUTION, MOCK_EXECUTION)
    base = dict(authorization_id=f"OFFLINE-{execution_class.split('_')[0]}", execution_class=execution_class,
                provider=provider, model=model, price_in=Decimal(0), price_out=Decimal(0),
                price_basis="ZERO: offline provider, no paid request possible", max_usd_total=Decimal(0))
    base.update(overrides)
    return Envelope(**base)


def _parse_time(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        raise EnvelopeError("not_after must be an ISO-8601 UTC time like 2026-10-31T00:00:00Z") from None


REQUIRED_FIELDS = ("authorization_id", "mode", "provider", "model", "effort", "max_executions", "max_requests_total",
                   "max_requests_per_execution", "max_input_tokens_per_request", "max_output_tokens_per_request",
                   "max_total_tokens", "max_usd_total", "price_usd_per_mtok", "allowed_payment_environments",
                   "request_timeout_s", "not_after", "bound_data_dir", "owner_statement")


def file_is_private(path, repo_root):
    """(ok, problems) for a secret-bearing or authority-bearing file: regular, 0600/0400, owned by us, outside repo."""
    problems = []
    try:
        st = os.lstat(path)
    except OSError:
        return False, ["file not found"]
    if not stat.S_ISREG(st.st_mode):
        problems.append("not a regular file (symlinks are refused)")
    if stat.S_IMODE(st.st_mode) not in (0o600, 0o400):
        problems.append(f"mode {oct(stat.S_IMODE(st.st_mode))} (needs 0600 or 0400)")
    if st.st_uid != os.geteuid():
        problems.append("not owned by the current user")
    if os.path.realpath(path).startswith(os.path.realpath(repo_root) + os.sep):
        problems.append("inside the project repository (keep it outside, e.g. ~/.config/autonomos-paypal/)")
    return not problems, problems


def load_authorization(path, repo_root, now=None):
    """Parse and validate the owner's REAL_MODEL_EXECUTION authorization. Returns an Envelope or raises
    EnvelopeError listing every problem. Never makes a network call."""
    ok, problems = file_is_private(path, repo_root)
    if not ok:
        raise EnvelopeError("authorization file refused: " + "; ".join(problems))
    try:
        a = json.load(open(path))
    except ValueError:
        raise EnvelopeError("authorization file is not valid JSON") from None
    if not isinstance(a, dict):
        raise EnvelopeError("authorization file must hold one JSON object")
    missing = [k for k in REQUIRED_FIELDS if k not in a]
    if missing:
        raise EnvelopeError(f"authorization file misses fields: {missing}")
    errs = []
    if a["mode"] != REAL_MODEL_EXECUTION:
        errs.append("mode must be REAL_MODEL_EXECUTION")
    if a["provider"] != "anthropic_messages":
        errs.append("provider must be anthropic_messages")
    if not isinstance(a["authorization_id"], str) or not 6 <= len(a["authorization_id"]) <= 64 \
            or not all(c.isalnum() or c in "-_." for c in a["authorization_id"]):
        errs.append("authorization_id must be 6-64 characters [A-Za-z0-9-_.]")
    if not isinstance(a["model"], str) or not a["model"].startswith("claude-"):
        errs.append("model must be a claude-* model id")
    if a["effort"] not in EFFORTS:
        errs.append(f"effort must be one of {EFFORTS}")
    ints = {}
    for k in ("max_executions", "max_requests_total", "max_requests_per_execution", "max_input_tokens_per_request",
              "max_output_tokens_per_request", "max_total_tokens", "request_timeout_s"):
        v = a[k]
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            errs.append(f"{k} must be a positive integer")
        elif v > HARD_LIMITS[k]:
            errs.append(f"{k}={v} exceeds the code-level ceiling {HARD_LIMITS[k]}")
        ints[k] = v
    try:
        usd = _dec(a["max_usd_total"], "max_usd_total")
        if usd > HARD_LIMITS["max_usd_total"]:
            errs.append(f"max_usd_total exceeds the code-level ceiling {HARD_LIMITS['max_usd_total']}")
    except EnvelopeError as e:
        errs.append(str(e))
        usd = None
    price_in = price_out = None
    basis = "UNKNOWN"
    p = a["price_usd_per_mtok"]
    if not isinstance(p, dict) or set(p) != {"input", "output"}:
        errs.append("price_usd_per_mtok must be {\"input\": \"...\", \"output\": \"...\"} (owner-confirmed list price)")
    else:
        try:
            price_in, price_out = _dec(p["input"], "input price"), _dec(p["output"], "output price")
            if price_in == 0 or price_out == 0:
                errs.append("a zero price is not credible for a paid model")
            ref = REFERENCE_PRICES.get(a["model"])
            if ref and (price_in < Decimal(ref[0]) or price_out < Decimal(ref[1])):
                errs.append(f"owner price {price_in}/{price_out} is below the reference {ref[0]}/{ref[1]} USD/MTok; "
                            "a lower price would understate the cost envelope")
            basis = ("OWNER_CONFIRMED (>= reference table)" if ref else
                     "OWNER_CONFIRMED (model not in the reference table)")
        except EnvelopeError as e:
            errs.append(str(e))
    envs = a["allowed_payment_environments"]
    if not isinstance(envs, list) or not envs or any(e not in ("MOCK", "SANDBOX") for e in envs):
        errs.append("allowed_payment_environments must be a non-empty subset of [\"MOCK\", \"SANDBOX\"]")
    try:
        not_after = _parse_time(a["not_after"])
        if not_after <= (now or time.time()):
            errs.append("authorization has expired (not_after is in the past)")
    except EnvelopeError as e:
        errs.append(str(e))
        not_after = None
    if not isinstance(a["bound_data_dir"], str) or not os.path.isabs(a["bound_data_dir"]):
        errs.append("bound_data_dir must be the absolute path of the data directory whose ledger enforces this "
                    "authorization (budgets are counted per database)")
    if not isinstance(a["owner_statement"], str) or len(a["owner_statement"].strip()) < 20:
        errs.append("owner_statement must be a sentence written by the owner")
    if not errs and ints["max_requests_per_execution"] > ints["max_requests_total"]:
        errs.append("max_requests_per_execution exceeds max_requests_total")
    if errs:
        raise EnvelopeError("authorization refused: " + "; ".join(errs))
    env = Envelope(authorization_id=a["authorization_id"], execution_class=REAL_MODEL_EXECUTION,
                   provider="anthropic_messages", model=a["model"], effort=a["effort"],
                   max_requests_per_execution=ints["max_requests_per_execution"],
                   max_requests_total=ints["max_requests_total"], max_executions=ints["max_executions"],
                   max_input_tokens_per_request=ints["max_input_tokens_per_request"],
                   max_output_tokens_per_request=ints["max_output_tokens_per_request"],
                   max_total_tokens=ints["max_total_tokens"], max_usd_total=usd, price_in=price_in,
                   price_out=price_out, price_basis=basis, request_timeout_s=float(ints["request_timeout_s"]),
                   allowed_payment_environments=tuple(envs), not_after=not_after,
                   bound_data_dir=os.path.realpath(a["bound_data_dir"]), owner_statement=a["owner_statement"])
    worst = env.authorization_worst_case_usd()
    if worst > usd:
        raise EnvelopeError(f"authorization refused: the worst case of the envelope ({worst:.4f} USD = "
                            f"{env.max_requests_total} requests x full input and output limits) exceeds "
                            f"max_usd_total {usd}. Lower the token limits or raise max_usd_total.")
    if env.max_total_tokens < env.max_input_tokens_per_request + env.max_output_tokens_per_request:
        raise EnvelopeError("authorization refused: max_total_tokens is smaller than one request's limits")
    return env


def reference_estimate(model, est_input_tokens, max_output_tokens):
    """Read-only helper for reports: worst case at the reference price, or None when the price is UNKNOWN."""
    ref = REFERENCE_PRICES.get(model)
    if not ref:
        return None
    return (Decimal(est_input_tokens) * Decimal(ref[0]) + Decimal(max_output_tokens) * Decimal(ref[1])) / 1_000_000
