"""R1e.1: the operating mode of the web demo, checked once at start-up and shown on every page.

  DEMO_REPLAY      MOCK payment (no PayPal) + RECORDED model output (no model call). No network at all.
  SUPERVISED_LIVE  PayPal SANDBOX + ONE real Anthropic request, only when EVERY owner gate is in place: the Sandbox
                   owner GO, caps of 1 order / 1 capture request, an authorization that allows SANDBOX only and at
                   most one model request, a REAL runtime (which itself needs the AI GO, the key file and a READY
                   preflight), and the single-use marker that only the owner launcher creates.
  DEVELOPMENT      AUTONOMOS_DEMO_MODE unset: the test and development configuration, labelled as such.

resolve() refuses any mismatch (DemoModeError); `serve` then exits before it listens.
"""
import os

from . import MOCK_EXECUTION, REAL_MODEL_EXECUTION

MODES = ("DEMO_REPLAY", "SUPERVISED_LIVE")


class DemoModeError(RuntimeError):
    pass


def _is_replay(runtime):
    return str(getattr(runtime.provider, "transport_label", "") or "").startswith("RECORDED_REPLAY")


def resolve(cfg, runtime, environ=None):
    e = os.environ if environ is None else environ
    mode = e.get("AUTONOMOS_DEMO_MODE", "")
    env = runtime.envelope
    if not mode:
        return {"mode": "DEVELOPMENT", "banner": "DEVELOPMENT configuration (not a demo mode): "
                f"payment {cfg.payment_provider}, AI {env.execution_class}", "live": False}
    if mode not in MODES:
        raise DemoModeError(f"AUTONOMOS_DEMO_MODE must be one of {MODES}, not {mode!r}")
    if mode == "DEMO_REPLAY":
        problems = []
        if cfg.payment_provider != "mock":
            problems.append("DEMO_REPLAY needs the MOCK payment provider (no PayPal)")
        if env.execution_class != MOCK_EXECUTION or not _is_replay(runtime):
            problems.append("DEMO_REPLAY needs the RECORDED replay runtime (AUTONOMOS_R1D_AI_PROVIDER=recorded)")
        if problems:
            raise DemoModeError("; ".join(problems))
        meta = runtime.provider._open.meta
        return {"mode": "DEMO_REPLAY", "live": False,
                "banner": "DEMO_REPLAY: MOCK payment (no PayPal) and a REPLAYED Claude Opus 5.5 answer recorded on "
                          f"{meta['recorded_utc'][:10]} (request {meta['historical_request_id']}); not a live model "
                          "call. No network.",
                "question": meta["question"], "document_set": meta["document_set"]["id"]}
    problems = []
    if cfg.payment_provider != "paypal_sandbox":
        problems.append("SUPERVISED_LIVE needs the PayPal SANDBOX provider")
    if cfg.sandbox_owner_go != "capture-at-most-1-order":
        problems.append("SUPERVISED_LIVE needs AUTONOMOS_SANDBOX_OWNER_GO")
    if cfg.sandbox_max_orders != 1 or cfg.sandbox_max_capture_requests != 1:
        problems.append("SUPERVISED_LIVE needs caps of exactly 1 order and 1 capture request")
    if env.execution_class != REAL_MODEL_EXECUTION:
        problems.append("SUPERVISED_LIVE needs the REAL Anthropic runtime (anthropic_real with the owner's GO)")
    else:
        if tuple(env.allowed_payment_environments) != ("SANDBOX",):
            problems.append("the AI authorization must allow the SANDBOX payment environment only")
        if env.max_requests_total != 1 or env.max_executions != 1:
            problems.append("the AI authorization must allow exactly one model request and one execution")
    marker = e.get("AUTONOMOS_LIVE_LAUNCH_MARKER", "")
    if not marker or not os.path.isdir(marker):
        problems.append("SUPERVISED_LIVE is started only by the owner launcher (single-use marker missing)")
    if problems:
        raise DemoModeError("; ".join(problems))
    return {"mode": "SUPERVISED_LIVE", "live": True,
            "banner": f"SUPERVISED_LIVE: PayPal SANDBOX (test money, 1 order, 1 capture request) and ONE real "
                      f"{env.model} request under authorization {env.authorization_id}. Owner-supervised."}
