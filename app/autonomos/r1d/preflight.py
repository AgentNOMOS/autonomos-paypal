"""Read-only AI preflight: can a REAL provider be enabled safely, and what could it cost at most? No network, no key
value printed, the database (if any) opened read-only. Exit status 0 only when the verdict is REAL_PROVIDER_READY or
the mode is an offline one."""
import os
import sqlite3
from decimal import Decimal

from .aiconfig import MODES, REPO_ROOT, real_checks
from ..corpus import load_sets
from .engine import PASSAGE_CAP, SCHEMA, SYSTEM_PROMPT, prompt_upper_bound_bytes
from .envelope import HARD_LIMITS, PRICE_SOURCE, REFERENCE_PRICES, reference_estimate


def _ledger_totals_ro(data_dir, authorization_id):
    path = os.path.join(data_dir or "", "autonomos.sqlite")
    if not data_dir or not os.path.exists(path):
        return {"database": "absent (fresh data directory)"}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='r1d_ai_calls'").fetchone():
            return {"database": "present, no R1d ledger yet"}
        rows = con.execute("SELECT state, COUNT(*) FROM r1d_ai_calls WHERE authorization_id=? GROUP BY state",
                           (authorization_id,)).fetchall()
        fr = con.execute("SELECT frozen, reason FROM r1d_authorizations WHERE authorization_id=?",
                         (authorization_id,)).fetchone()
        return {"database": "read-only", "calls_by_state": dict(rows), "frozen": bool(fr and fr[0]),
                "frozen_reason": fr and fr[1]}
    finally:
        con.close()


def preflight(environ=None, repo_root=REPO_ROOT):
    e = os.environ if environ is None else environ
    mode = e.get("AUTONOMOS_R1D_AI_PROVIDER", "fixture")
    typical_in = len(SYSTEM_PROMPT.encode()) + len(str(SCHEMA).encode()) + 14 * (PASSAGE_CAP + 120) + 600
    out = {"network_calls": 0, "mode": mode, "modes": list(MODES), "price_source": PRICE_SOURCE,
           "reference_prices_usd_per_mtok": REFERENCE_PRICES, "hard_limits": {k: str(v) for k, v in
                                                                              HARD_LIMITS.items()},
           "legacy_paths": {"claude_cli": "disabled in R1d (subscription credentials; inherited environment)",
                            "anthropic_api (R1b)": "disabled in R1d (no envelope; server-side fallbacks on)"}}
    if mode not in MODES:
        out.update(verdict="INVALID_MODE", blocking=[f"unknown mode {mode!r}"])
        return out, 2
    info = {}
    checks, problems, env, _ = real_checks(e, repo_root, read_key=True, info=info)   # format only; value discarded
    if "key_family" in info:
        out["key_family"] = info["key_family"]
    corpus_root = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                               "corpus")
    bounds = {sid: prompt_upper_bound_bytes(ds) for sid, ds in load_sets(corpus_root).items()}
    out["largest_prompt_input_bound_tokens_before_overhead"] = bounds
    if env is not None:
        need = env.overhead_tokens + max(bounds.values())
        checks["envelope_fits_largest_prompt"] = env.max_input_tokens_per_request >= need
        if not checks["envelope_fits_largest_prompt"]:
            problems.append(f"max_input_tokens_per_request {env.max_input_tokens_per_request} < largest possible "
                            f"prompt bound {need}: a paid task could be refused by its own envelope")
    else:
        checks["envelope_fits_largest_prompt"] = False
    out["real_checks"] = checks
    if env is not None:
        per_req = env.worst_case_usd(env.max_input_tokens_per_request, env.max_output_tokens_per_request)
        out["envelope"] = env.public_view()
        out["cost"] = {"estimation": "OWNER_CONFIRMED_PRICE" if env.price_known else "UNKNOWN",
                       "worst_case_per_request_usd": str(per_req),
                       "worst_case_authorization_usd": str(env.authorization_worst_case_usd()),
                       "typical_request_input_upper_bound_tokens": typical_in + env.overhead_tokens,
                       "typical_request_worst_case_usd": str(env.worst_case_usd(
                           typical_in + env.overhead_tokens, env.max_output_tokens_per_request))}
        out["ledger"] = _ledger_totals_ro(e.get("AUTONOMOS_DATA_DIR"), env.authorization_id)
    else:
        model = e.get("AUTONOMOS_R1D_MODEL", "claude-opus-5-5")
        ref = reference_estimate(model, typical_in + 4000, 12000)
        out["cost"] = {"estimation": "UNKNOWN (no owner authorization: reference price only, not confirmed)",
                       "reference_model": model,
                       "reference_typical_request_worst_case_usd": None if ref is None else str(ref.quantize(
                           Decimal("0.0001")))}
    ready = all(checks.values())
    if mode == "anthropic_real":
        out["verdict"] = "REAL_PROVIDER_READY" if ready else "REAL_PROVIDER_BLOCKED"
    else:
        out["verdict"] = f"OFFLINE_MODE_{mode.upper()}"
        out["real_provider_if_requested"] = "READY" if ready else "BLOCKED"
    out["blocking"] = problems
    return out, (0 if mode != "anthropic_real" or ready else 1)
