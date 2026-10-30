"""R1d AI provider selection. Environment variables:

  AUTONOMOS_R1D_AI_PROVIDER         fixture (default) | mock | anthropic_real
  AUTONOMOS_R1D_MODEL               model id (mock: label only; anthropic_real: must equal the authorization's model)
  AUTONOMOS_R1D_AI_AUTHORIZATION    path to the owner's authorization JSON (anthropic_real; 0600/0400, outside repo)
  AUTONOMOS_R1D_REAL_AI_GO          must equal "real-ai:<authorization_id>" (anthropic_real)
  AUTONOMOS_ANTHROPIC_KEY_FILE      path to a file holding only the API key (anthropic_real; 0600/0400, outside repo)
  AUTONOMOS_DATA_DIR                anthropic_real: must equal the authorization's bound_data_dir

anthropic_real additionally requires ANTHROPIC_API_KEY to be UNSET in the server's environment: the key lives only in
the key file and in the provider object, so no subprocess can inherit it. The legacy AUTONOMOS_AI_PROVIDER must stay
"fixture" (checked by R1dService). Selecting anthropic_real never makes a request by itself.
"""
import os
import re
from dataclasses import dataclass

from . import FIXTURE_EXECUTION, MOCK_EXECUTION
from .envelope import (DEFAULT_REAL_MODEL, EFFORTS, REAL_GO_PREFIX, EnvelopeError, file_is_private,
                       load_authorization, offline_envelope)
from .providers import AnthropicMessagesProvider, FixtureProviderR1d, OfflineMessagesEmulator

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
MODES = ("fixture", "mock", "recorded", "anthropic_real")
EMULATOR_CUES = {
    "HARDWARE_EXECUTION": r"\bwe ran\b|\bran one\b",
    "REPORTED_HARDWARE_RESULT": r"self-overlap|surviving contrast|dynamic range retained",
    "RAW_EVIDENCE_AVAILABILITY": r"withheld",
    "CLASSICAL_SIMULATION_OR_EXACT": r"exact classical|statevector|noise-free",
    "COMPILED_ONLY": r"only been compiled|never submitted|compiled and verified locally",
    "PLANNED_NOT_PERFORMED": r"no ionq (job|hardware result)|plans a controlled|proposed",
    "UNKNOWN_OR_UNSUPPORTED": r"not yet confirmed|unconfirmed|no quantum advantage|not documented",
}


class AIConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Runtime:
    mode: str
    provider: object
    envelope: object


def offline_runtime(mode="fixture", model=None, transport=None, transport_label=None, **envelope_overrides):
    if mode == "fixture":
        p = FixtureProviderR1d()
        return Runtime("fixture", p, offline_envelope(FIXTURE_EXECUTION, "fixture", p.model, **envelope_overrides))
    if mode == "mock":
        model = model or DEFAULT_REAL_MODEL
        effort = envelope_overrides.get("effort", "medium")
        if transport is None:
            transport, transport_label = OfflineMessagesEmulator(EMULATOR_CUES), OfflineMessagesEmulator.label
        p = AnthropicMessagesProvider(model, effort, transport=transport, transport_label=transport_label)
        return Runtime("mock", p, offline_envelope(MOCK_EXECUTION, "anthropic_messages", model, **envelope_overrides))
    raise AIConfigError(f"offline_runtime does not build {mode!r}")


# R1d.1: a CLOSED list of Console API key families, all sent as `x-api-key` (no other auth path exists in R1d).
#   sk-ant-apiNN-...  Console API key (documented examples: sk-ant-api03-...)
#   sk-ant-usr-...    Console personal API key (owner-confirmed 2026-10-17; the docs only say keys start with sk-ant-)
# Credentials that are not API keys are refused by name; anything else is refused as unknown.
API_KEY_FAMILIES = (("console_api_key", re.compile(r"sk-ant-api\d{2}-[A-Za-z0-9_-]{20,}")),
                    ("console_personal_key_usr", re.compile(r"sk-ant-usr-[A-Za-z0-9_-]{20,}")))
REFUSED_KEY_PREFIXES = (("sk-ant-oat", "OAuth access token (Claude subscription / Claude Code login), not an API key"),
                        ("sk-ant-ort", "OAuth refresh token, not an API key"),
                        ("sk-ant-admin", "Admin API key, cannot call the Messages API"),
                        ("sk-ant-sid", "claude.ai session key, not an API key"))


def classify_api_key(k):
    """(ok, label) for a candidate key string (already stripped). The label never contains the value."""
    if not isinstance(k, str) or not k:
        return False, "empty"
    if any(c.isspace() for c in k) or not k.isascii() or len(k) > 512:
        return False, "not one ASCII token without whitespace"
    for prefix, why in REFUSED_KEY_PREFIXES:
        if k.startswith(prefix):
            return False, why
    for label, rx in API_KEY_FAMILIES:
        if rx.fullmatch(k):
            return True, label
    if k.startswith(("sk-ant-api", "sk-ant-usr-")):
        return False, "malformed key of an accepted family"
    return False, "not an accepted Console API key family (sk-ant-apiNN-, sk-ant-usr-)"


def real_checks(environ=None, repo_root=REPO_ROOT, read_key=False, info=None):
    """All preconditions for REAL execution, as booleans plus problems. No network. Returns (checks, problems,
    envelope or None, key or None). With read_key=True the key file is format-checked whenever it is private (its
    value is never returned in problems or info); the key itself is returned only when every check passed.
    `info`, if given, receives the key family label."""
    e = os.environ if environ is None else environ
    checks, problems, env, key = {}, [], None, None
    checks["legacy_ai_provider_is_fixture"] = e.get("AUTONOMOS_AI_PROVIDER", "fixture") == "fixture"
    checks["anthropic_api_key_env_unset"] = "ANTHROPIC_API_KEY" not in e
    if not checks["anthropic_api_key_env_unset"]:
        problems.append("ANTHROPIC_API_KEY is set in the environment: unset it; the key belongs in the key file only")
    apath = e.get("AUTONOMOS_R1D_AI_AUTHORIZATION", "")
    checks["authorization_file_set"] = bool(apath)
    try:
        env = load_authorization(apath, repo_root) if apath else None
        checks["authorization_valid"] = env is not None
        if env is None:
            problems.append("AUTONOMOS_R1D_AI_AUTHORIZATION is not set")
    except EnvelopeError as ex:
        checks["authorization_valid"] = False
        problems.append(str(ex))
    checks["owner_go_matches_authorization"] = bool(env) and e.get("AUTONOMOS_R1D_REAL_AI_GO") == \
        REAL_GO_PREFIX + env.authorization_id
    if env and not checks["owner_go_matches_authorization"]:
        problems.append(f"AUTONOMOS_R1D_REAL_AI_GO must equal {REAL_GO_PREFIX}<authorization_id>")
    m = e.get("AUTONOMOS_R1D_MODEL")
    checks["model_matches_authorization"] = bool(env) and (m is None or m == env.model)
    checks["effort_valid"] = bool(env) and env.effort in EFFORTS
    checks["price_known"] = bool(env) and env.price_known
    dd = e.get("AUTONOMOS_DATA_DIR", "")
    checks["data_dir_matches_authorization"] = bool(env) and bool(dd) and \
        os.path.realpath(dd) == env.bound_data_dir
    if env and not checks["data_dir_matches_authorization"]:
        problems.append(f"AUTONOMOS_DATA_DIR must be the authorization's bound_data_dir ({env.bound_data_dir}): "
                        "budgets are enforced per database")
    # R1e: the configured payment environment must be one the authorization allows (checked before any launch; the
    # ledger and the pre-order capacity gate enforce it again at run time)
    penv = {"mock": "MOCK", "paypal_sandbox": "SANDBOX"}.get(e.get("AUTONOMOS_PAYMENT_PROVIDER", "mock"), "UNKNOWN")
    checks["payment_environment_allowed"] = bool(env) and penv in env.allowed_payment_environments
    if env and not checks["payment_environment_allowed"]:
        problems.append(f"payment environment {penv} is not in the authorization's allowed_payment_environments "
                        f"{list(env.allowed_payment_environments)}")
    kpath = e.get("AUTONOMOS_ANTHROPIC_KEY_FILE", "")
    kok, kprob = file_is_private(kpath, repo_root) if kpath else (False, ["AUTONOMOS_ANTHROPIC_KEY_FILE is not set"])
    checks["key_file_private_and_outside_repo"] = kok
    problems += [f"key file: {p}" for p in kprob]
    if read_key and kok:
        k = open(kpath).read().strip()
        ok, label = classify_api_key(k)
        checks["key_file_format"] = ok
        if info is not None:
            info["key_family"] = label
        if not ok:
            problems.append(f"key file refused: {label} (value not shown)")
        elif all(checks.values()):
            key = k
    return checks, problems, env, key


def runtime_from_env(environ=None, repo_root=REPO_ROOT):
    e = os.environ if environ is None else environ
    mode = e.get("AUTONOMOS_R1D_AI_PROVIDER", "fixture")
    if mode not in MODES:
        raise AIConfigError(f"AUTONOMOS_R1D_AI_PROVIDER must be one of {MODES}")
    if mode in ("fixture", "mock"):
        return offline_runtime(mode, model=e.get("AUTONOMOS_R1D_MODEL"))
    if mode == "recorded":                         # R1e.1: labelled replay of one recorded answer (DEMO_REPLAY)
        from .recorded import recording_runtime
        return recording_runtime(e.get("AUTONOMOS_R1D_RECORDING") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "recordings",
            "real_ai_test1_opus55"))
    checks, problems, env, key = real_checks(e, repo_root, read_key=True)
    if not all(checks.values()) or key is None:
        raise AIConfigError("anthropic_real refused: " + "; ".join(problems or
                                                                    [k for k, v in checks.items() if not v]))
    return Runtime("anthropic_real", AnthropicMessagesProvider(env.model, env.effort, api_key=key), env)
