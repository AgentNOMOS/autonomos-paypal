"""Receipt v2: the R1c v1 body, unchanged, plus an `ai_execution` section and R1d limits, signed the same way.

* receipt.build() (R1c) produces the v1 body; R1d copies it, sets receipt_version "2", adds ai_execution, re-signs.
  The R1c verifier (receipt.verify) therefore validates a v2 receipt's signature, artifact digest, offer binding, event
  chain and payment verdict exactly as before.
* verify_any() dispatches on receipt_version: "1" -> the R1c checks only (old Run 2 receipts); "2" -> R1c checks plus
  the AI-execution checks below. A receipt may claim REAL_MODEL_EXECUTION only with per-request provider evidence.
* Receipts are built only after DELIVERY_VERIFIED; a failed AI execution has no receipt (REVIEW_REQUIRED instead).
"""
import copy

from .. import receipt as R
from ..canon import digest
from . import EXECUTION_CLASSES, FIXTURE_EXECUTION, MOCK_EXECUTION, REAL_MODEL_EXECUTION
from .providers import API_URL

R1D_LIMITS = [
    "AI execution class is stated in ai_execution.execution_class. Only REAL_MODEL_EXECUTION means a language model "
    "was called; FIXTURE_EXECUTION and MOCK_EXECUTION outputs are not model inference.",
    "QUOTE_VERIFIED means the quotation occurs verbatim (whitespace-normalized) in the named pinned document. "
    "CLAIM_SUPPORTED (rule R1E-SUPPORT-1) is deterministic document support over the quotation and its bounded "
    "same-passage context unit, not semantic entailment. Quote-verified excerpts whose claims are NOT confirmed are "
    "delivered separately (report.unconfirmed_findings). No finding is independently verified as true.",
    "DELIVERED is the protocol outcome. ai_execution.research_completeness (PASS / PARTIAL / FAIL) states whether every "
    "model finding and statement passed verification; it does not judge whether the question was answered fully.",
    "Token usage and cost are the provider's reported usage for completed requests; requests with an unknown outcome "
    "are charged at their worst case. USD figures are estimates at the authorization's price, not an invoice.",
    "A REAL_MODEL_EXECUTION label is the issuer's record. Confirming it independently needs the provider's own logs "
    "for the listed request ids.",
]


def build(task, offer, order, capture, execution, artifact_digest, delivery_verification, events, signer,
          environment, ai_execution):
    v1 = R.build(task, offer, order, capture, execution, artifact_digest, delivery_verification, events, signer,
                 environment)
    body = copy.deepcopy(v1["body"])
    body["receipt_version"] = "2"
    body["ai_execution"] = ai_execution
    body["limits"] = list(body["limits"]) + R1D_LIMITS
    return {"body": body, "signature_hex": signer.sign(body)}


def ai_execution_section(artifact):
    """What the receipt states about the AI work: copied from the artifact's ai_execution record, plus its digest."""
    a = artifact["ai_execution"]
    v = artifact["delivery_verification"]
    return {
        "execution_class": a["execution_class"],
        "real_model_inference": a["execution_class"] == REAL_MODEL_EXECUTION,
        "provider": a["provider"], "requested_model": a["requested_model"], "reported_models": a["reported_models"],
        "authorization_ref": a["authorization_ref"], "execution_policy": a["execution_policy"],
        "source_collection": a["source_collection"], "model_requests": a["model_requests"],
        "request_digests": [r["request_digest"] for r in a["requests"]],
        "response_digests": [r["response_digest"] for r in a["requests"]],
        "provider_request_ids": [r["request_id"] for r in a["requests"]],
        "token_usage": a["token_usage"], "cost": a["cost"],
        "validation": {"status": v["status"], **v["counts"], "claim_rule": v.get("claim_rule", "R1D-LEX-1"),
                       "truth_independently_verified": 0},
        "research_completeness": v.get("research_completeness"),
        "unsupported_claim_decisions": [d for d in v["decisions"] if d["decision"] != "DELIVERED"
                                        or d.get("claim_status") != "CLAIM_SUPPORTED"][:20],
        "outcome": f"DELIVERED_{v['status']}",
        "ai_record_digest": digest(a),
    }


def _label_consistent(ai, artifact):
    """A label must match the evidence recorded for every request."""
    cls, reqs = ai.get("execution_class"), (artifact or {}).get("ai_execution", {}).get("requests", [])
    if cls not in EXECUTION_CLASSES or ai.get("real_model_inference") != (cls == REAL_MODEL_EXECUTION):
        return False
    if not reqs or len(reqs) != ai.get("model_requests"):
        return False
    if cls == FIXTURE_EXECUTION:
        return all(r["provider"] == "FIXTURE" and r["request_id"] is None for r in reqs)
    if cls == MOCK_EXECUTION:
        return all(r["provider"] == "anthropic_messages" and r["transport"] != API_URL for r in reqs)
    return bool(ai.get("authorization_ref")) and not str(ai["authorization_ref"]).startswith("OFFLINE") and all(
        r["provider"] == "anthropic_messages" and r["transport"] == API_URL and r["request_id"]
        and str(r["response_id"] or "").startswith("msg_") and isinstance((r.get("usage") or {}).get("input_tokens"), int)
        and str(r["model"] or "").startswith(ai["requested_model"]) for r in reqs)


def verify_any(receipt, artifact=None, events=None, docset=None, trusted_public_key_hex=None):
    """Offline verification for v1 (R1c) and v2 (R1d) receipts. The signature check uses the public key embedded in
    the receipt, so anyone can re-sign a forged body with their own key; pass the issuer's key obtained out of band
    (trusted_public_key_hex) to bind the receipt to that issuer."""
    base = R.verify(receipt, artifact, events)
    b = receipt["body"]
    if trusted_public_key_hex is not None:
        base["checks"]["issuer_key_is_trusted"] = b["issuer"]["public_key_hex"] == trusted_public_key_hex.lower()
        base["valid"] = all(base["checks"].values())
    if b.get("receipt_version", "1") == "1":
        return dict(base, receipt_version="1")
    checks = dict(base["checks"])
    ai = b.get("ai_execution") or {}
    checks["ai_section_present"] = bool(ai)
    checks["execution_label_wellformed"] = ai.get("execution_class") in EXECUTION_CLASSES and \
        ai.get("real_model_inference") == (ai.get("execution_class") == REAL_MODEL_EXECUTION)
    if artifact is not None:
        checks["ai_record_matches_artifact"] = digest(artifact.get("ai_execution")) == ai.get("ai_record_digest")
        checks["execution_label_consistent_with_evidence"] = _label_consistent(ai, artifact)
        v = artifact.get("delivery_verification", {})
        checks["validation_counts_match_artifact"] = (
            ai.get("validation", {}).get("status") == v.get("status") ==
            b["artifact"]["status"] and ai["validation"].get("delivered") == v.get("counts", {}).get("delivered"))
        checks["artifact_not_failed"] = v.get("status") in ("VERIFIED", "PARTIAL")
        checks["no_truth_verification_claimed"] = all(
            f.get("truth_status") == "NOT_INDEPENDENTLY_VERIFIED" for f in artifact.get("report", {}).get("findings", [])
            + artifact.get("report", {}).get("unconfirmed_findings", []))
        if docset is not None:
            from .verifier import recheck_delivered
            ok, _ = recheck_delivered(artifact, docset)
            checks["quotes_recheck_against_collection"] = ok
    return {"valid": all(checks.values()), "checks": checks, "receipt_version": "2"}
