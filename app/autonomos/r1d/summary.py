"""R1d.2 execution summary: one record that never lets DELIVERED stand for "the research is complete".

Six separate fields, each with its own source:
  payment_provider       MOCK | PAYPAL_SANDBOX          receipt environment (what took the payment)
  ai_provider            anthropic_messages | recorded_replay | mock | fixture, with ai_execution_class (what
                         produced the text; recorded_replay = a historical answer replayed offline, not a model call)
  delivery_status        the task state (DELIVERED, REVIEW_REQUIRED, ...): the protocol outcome only
  research_completeness  PASS | PARTIAL | FAIL            verification completeness of the model's answer
  claim_support          document support counts (rule named)
  independent_truth      always NOT_INDEPENDENTLY_VERIFIED (AutoNOMOS verifies documents, not the world)

research_completeness is PASS only if no finding was withheld or rejected, every delivered claim is document-supported,
and neither the model summary nor a not-established statement was withheld; PARTIAL if at least one supported claim was
delivered; FAIL otherwise. It does not judge whether the question was answered exhaustively. For artifacts written
before R1d.2 it is derived from the recorded counts with the same rule and labelled as derived.
Pure function; reads nothing, writes nothing.
"""
from . import FIXTURE_EXECUTION, MOCK_EXECUTION, REAL_MODEL_EXECUTION

PAYMENT = {"MOCK": "MOCK", "SANDBOX": "PAYPAL_SANDBOX"}
AI_PROVIDER = {REAL_MODEL_EXECUTION: "anthropic_messages", MOCK_EXECUTION: "mock", FIXTURE_EXECUTION: "fixture"}


def research_completeness(artifact):
    """(value, basis, reasons) from an artifact of any R1d version."""
    v = (artifact or {}).get("delivery_verification") or {}
    if v.get("research_completeness"):
        return v["research_completeness"], f"recorded by {v.get('verifier_version', 'the verifier')}", \
            v.get("research_completeness_reasons", [])
    c, rep = v.get("counts") or {}, (artifact or {}).get("report") or {}
    reasons = ([f"{c.get('withheld', 0) + c.get('rejected', 0)} finding(s) removed"]
               if c.get("withheld", 0) + c.get("rejected", 0) else []) + (
        [f"{c['claim_unconfirmed']} delivered claim(s) not confirmed"] if c.get("claim_unconfirmed") else []) + (
        ["model summary withheld"] if rep.get("model_summary_withheld") else []) + (
        [f"{len(rep['not_established_withheld'])} not-established statement(s) withheld"]
        if rep.get("not_established_withheld") else [])
    value = "FAIL" if v.get("status") == "FAILED" or not c.get("claim_supported") else (
        "PARTIAL" if reasons else "PASS")
    return value, "derived from the recorded counts by the R1d.2 rule (artifact predates R1d.2)", reasons or ["none"]


def execution_summary(task_state, receipt, artifact):
    b = (receipt or {}).get("body") or {}
    ai = b.get("ai_execution") or {}
    a = (artifact or {}).get("ai_execution") or {}
    v = (artifact or {}).get("delivery_verification") or {}
    c = v.get("counts") or {}
    cls = ai.get("execution_class") or a.get("execution_class")
    transport = str((((artifact or {}).get("model_runs") or [{}])[0]).get("transport") or "")
    replay = transport.startswith("RECORDED_REPLAY")
    research, basis, why = research_completeness(artifact) if artifact else ("FAIL", "no artifact", ["no artifact"])
    rule = v.get("claim_rule") or ("R1D-LEX-1" if artifact else None)
    n = c.get("model_findings", 0)
    return {
        "payment_provider": PAYMENT.get(b.get("environment"), b.get("environment") or "NONE"),
        "ai_provider": "recorded_replay" if replay else AI_PROVIDER.get(cls, "none"),
        "ai_execution_class": cls,
        "ai_note": (f"{transport}" if replay else
                    "one real model request through the governed ledger" if cls == REAL_MODEL_EXECUTION else
                    "offline stand-in; not a model call" if cls else None),
        "real_model_inference": cls == REAL_MODEL_EXECUTION,
        "delivery_status": task_state,
        "delivery_note": "protocol outcome only: payment verified, artifact checks passed, receipt issued; it does "
                         "not mean the research is complete" if task_state == "DELIVERED" else None,
        "artifact_status": v.get("status"),
        "research_completeness": research,
        "research_completeness_basis": basis,
        "research_completeness_reasons": why,
        "claim_support": {"rule": rule, "model_findings": n, "supported": c.get("claim_supported", 0),
                          "unconfirmed": c.get("claim_unconfirmed", 0), "withheld": c.get("withheld", 0),
                          "rejected": c.get("rejected", 0)},
        "claim_support_verdict": ("NONE" if not c.get("claim_supported") else
                                  "ALL_SUPPORTED" if c.get("claim_supported") == n else "PARTIAL"),
        "independent_truth": f"NOT_INDEPENDENTLY_VERIFIED ({c.get('truth_independently_verified', 0)}/{n})",
        "headline": f"{task_state} · payment {PAYMENT.get(b.get('environment'), b.get('environment') or 'NONE')} · "
                    f"AI {'recorded_replay' if replay else AI_PROVIDER.get(cls, 'none')} · research {research} · claims "
                    f"{c.get('claim_supported', 0)}/{n} document-supported · truth not independently verified",
    }


def runtime_view(runtime):
    """The AI side of a running service, for pages shown before any artifact exists. No secrets."""
    env, prov = runtime.envelope, runtime.provider
    label = str(getattr(prov, "transport_label", "") or "")
    cls = env.execution_class
    return {"ai_provider": "recorded_replay" if label.startswith("RECORDED_REPLAY") else AI_PROVIDER.get(cls, "none"),
            "ai_execution_class": cls, "model": env.model, "authorization": env.authorization_id,
            "max_model_requests_total": env.max_requests_total,
            "ai_note": label if label.startswith("RECORDED_REPLAY") else (
                "real model requests through the governed ledger" if cls == REAL_MODEL_EXECUTION else
                "offline stand-in; not a model call")}
