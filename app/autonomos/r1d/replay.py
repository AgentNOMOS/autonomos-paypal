"""R1d.2 offline replay: re-verify a historical model output with the current verifier. No model, no network, no
payment, no database, no receipt: it reads an evidence directory and returns a comparison. It never changes or
re-signs the historical artifact or receipt; a different verdict is a NEW OFFLINE REASSESSMENT, not a correction of
the historical transaction.

Inputs (all from the sealed evidence, read-only): artifact.json (what was delivered and the exact retrieval), the raw
model output (bytes whose sha256 must equal artifact.raw_report_digest), offer.json, and the pinned collection.
The verifier is shown exactly the excerpts the model was shown: their digest must equal the recorded provided_digest.
"""
import hashlib
import json
import os

from ..corpus import DocumentSet
from . import engine, verifier


class ReplayRefused(RuntimeError):
    pass


def replay(artifact, raw_output_bytes, offer, corpus_root):
    set_id = artifact["document_set"]["id"]
    ds = DocumentSet(os.path.join(corpus_root, set_id), set_id)
    if ds.manifest_digest != artifact["document_set"]["manifest_digest"]:
        raise ReplayRefused("collection manifest digest differs from the historical artifact")
    if hashlib.sha256(raw_output_bytes).hexdigest() != artifact["raw_report_digest"]:
        raise ReplayRefused("raw model output digest differs from artifact.raw_report_digest")
    r = artifact["retrieval"]
    provided = {p: ds.passages[p][:r["passage_cap_chars"]] for p in r["passage_ids"]}
    if hashlib.sha256(json.dumps(provided, sort_keys=True).encode()).hexdigest() != r["provided_digest"]:
        raise ReplayRefused("reconstructed excerpts differ from what the model was shown (provided_digest)")
    if offer.get("offer_id") != artifact["offer_id"] or offer.get("question") != artifact["question"]:
        raise ReplayRefused("offer does not match the artifact")
    out = engine.parse_output(raw_output_bytes.decode("utf-8"))
    raw = {"offer_id": artifact["offer_id"], "task_id": artifact["task_id"], "question": artifact["question"],
           "document_set": artifact["document_set"], "flagged_passages": artifact.get("flagged_passages", []),
           "input_flags": artifact.get("input_flags", [])}
    exec_class = artifact["model_runs"][0]["execution_class"] if artifact.get("model_runs") else "UNKNOWN"
    rep, ver = verifier.verify(raw, out, ds, offer, provided, exec_class)
    return {"inputs": {"raw_output_sha256": artifact["raw_report_digest"], "provided_digest": r["provided_digest"],
                       "manifest_digest": ds.manifest_digest, "passages": len(provided)},
            "model_output": out, "report": rep, "verification": ver, "comparison": compare(artifact, out, rep, ver)}


def _orig(artifact):
    v, rep = artifact["delivery_verification"], artifact["report"]
    by = {}
    for d in v["decisions"]:
        by[d["finding_id"]] = {"decision": d["decision"], "claim_status": d.get("claim_status"),
                               "reasons": d.get("reasons", [])}
    for f in rep["findings"]:
        by[f["finding_id"]]["support_detail"] = f.get("support_detail")
    return by


def compare(artifact, out, rep, ver):
    old = _orig(artifact)
    new = {f["finding_id"]: f for f in rep["findings"]}
    removed = {r["finding_id"]: r for r in rep["removed_findings"]}
    rows = []
    for f in out["findings"]:
        fid = f["finding_id"]
        o = old.get(fid, {})
        n = new.get(fid) or removed.get(fid) or {}
        sup = n.get("support") or {}
        ctx = (sup.get("evidence_span") or {}).get("context")
        o_v = f"{o.get('decision')}" + (f" / {o['claim_status']}" if o.get("claim_status") else "")
        n_v = ("DELIVERED / " + n["claim_status"]) if fid in new else (n.get("decision") or "?")
        rows.append({
            "finding_id": fid, "evidence_class": f["evidence_class"], "model_claim": f["claim"],
            "anchor": f["anchor"], "quote": f["quote"],
            "original_verdict": o_v, "original_reasons": o.get("reasons", []),
            "r1d2_verdict": n_v,
            "r1d2_reasons": n.get("reasons", []) if fid not in new else n.get("claim_unconfirmed_reasons", []),
            "supporting_span": {"quote_passage": f["anchor"], "context_kind": ctx and ctx["kind"],
                                "context_text": ctx and ctx["text"], "context_header": ctx and ctx["header"]},
            "accepted_because": sup.get("accepted_because", []),
            "changed": o_v != n_v,
            "truth_status": n.get("truth_status", "NOT_INDEPENDENTLY_VERIFIED"),
            "independent_evidence": n.get("independent_evidence", "NOT_ESTABLISHED"),
            "remaining_uncertainty": n.get("verifier_note") or "not delivered",
        })
    hist = artifact["delivery_verification"]
    return {"label": "NEW OFFLINE REASSESSMENT (the historical artifact and signed receipt are unchanged)",
            "findings": rows,
            "summary": {"original": {"status": hist["status"], "counts": hist["counts"],
                                     "model_summary_withheld": artifact["report"].get("model_summary_withheld")},
                        "r1d2": {"status": ver["status"], "counts": ver["counts"],
                                 "research_completeness": ver["research_completeness"],
                                 "model_summary_withheld": rep.get("model_summary_withheld")}}}


def replay_dir(evidence_dir, corpus_root, raw_output_name="raw_output.txt", offer_name="offer.json"):
    art = json.load(open(os.path.join(evidence_dir, "artifact.json")))
    raw = open(os.path.join(evidence_dir, raw_output_name), "rb").read()
    offer = json.load(open(os.path.join(evidence_dir, offer_name)))
    return replay(art, raw, offer, corpus_root)
