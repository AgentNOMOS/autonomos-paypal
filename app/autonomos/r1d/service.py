"""R1dService: the R1c AutoNomos governor with the R1d execute() and recover(). Every payment method (offer, order,
capture, verification, cancellation, reconciliation, webhooks) is inherited unchanged from autonomos/service.py.

execute() keeps the R1c gate prefix byte-for-byte in meaning (EXECUTION_AUTHORIZED state, CAPTURE_VERIFIED record,
UNIQUE execution claim, EXECUTING by the executor) and then:
  * re-loads and re-hashes the paid-for collection from disk (missing or changed => no model request, REVIEW_REQUIRED)
  * runs the research worker; its single model request goes through the ledger (authorization + envelope)
  * verifies deterministically; delivers with a v2 receipt only if at least one finding survives
  * any failure after the claim => execution FAILED, task REVIEW_REQUIRED, an AI record with the reason and usage;
    never a receipt, never an automatic re-run, never a payment-table write
"""
import os
import time
from decimal import Decimal

from .. import policy, states as S
from ..canon import digest
from ..corpus import DocumentSet, load_sets
from ..service import AutoNomos, GateError
from . import aiconfig, engine, receipt2, verifier
from .governed import GovernedModelClient
from .ledger import Ledger

ARTIFACT_VERSION = "2"


class R1dService(AutoNomos):
    def __init__(self, cfg, payments=None, runtime=None, store_path=None, corpus_root=None):
        if cfg.ai_provider != "fixture":
            raise GateError("R1d: the legacy AUTONOMOS_AI_PROVIDER paths (claude_cli uses subscription credentials; "
                            "anthropic_api has no envelope and server-side fallbacks on) are disabled. Leave "
                            "AUTONOMOS_AI_PROVIDER=fixture and select the R1d provider with AUTONOMOS_R1D_AI_PROVIDER.")
        self.runtime = runtime or aiconfig.runtime_from_env()
        bound = self.runtime.envelope.bound_data_dir
        if self.runtime.envelope.execution_class == "REAL_MODEL_EXECUTION" and \
                (not bound or os.path.realpath(cfg.data_dir) != bound):
            raise GateError(f"R1d: this REAL authorization is bound to {bound}; the data directory is "
                            f"{os.path.realpath(cfg.data_dir)} (budgets are enforced per database)")
        super().__init__(cfg, payments=payments, ai=self.runtime.provider, store_path=store_path)
        self.corpus_root = corpus_root or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "corpus")
        self.docsets = load_sets(self.corpus_root)
        self.ledger = Ledger(self.store)
        self.prompt_bounds = {sid: self.runtime.envelope.overhead_tokens + engine.prompt_upper_bound_bytes(ds)
                              for sid, ds in self.docsets.items()}

    # ------------------------------------------------------------------ 5: AI capacity before the buyer pays
    def accept_offer(self, tid, offer_digest_seen):
        """R1d gate in front of the inherited accept_offer: if the AI envelope cannot pay for one more full request
        for this collection, refuse BEFORE any order is created (nothing charged). Repeat clicks on an already
        accepted offer go straight to the inherited code (same order, same link)."""
        t, offer = self.store.task(tid), self.store.offer(task_id=tid)
        if t is not None and offer is not None and t["state"] == S.BUYER_APPROVAL_REQUIRED:
            bound = self.prompt_bounds.get(offer["document_set"]["id"])
            ok, why = (False, "UNKNOWN_COLLECTION") if bound is None else \
                self.ledger.capacity(self.runtime.envelope, self.payments.environment, bound)
            if not ok:
                self.store.note(tid, "policy", "ai_capacity_refusal", {"reason": why, "input_bound": bound},
                                {"note": "AI execution envelope cannot cover this task; no order was created"})
                raise GateError(f"this service cannot run the paid AI work right now ({why}); no order was "
                                "created and nothing was charged")
        return super().accept_offer(tid, offer_digest_seen)

    def ai_client(self):
        return GovernedModelClient(self.runtime.provider, self.runtime.envelope, self.ledger,
                                   self.payments.environment)

    # ------------------------------------------------------------------ 9-12: execute, verify, receipt
    def execute(self, tid):
        t = self.store.task(tid)
        if t is None or t["state"] != S.EXECUTION_AUTHORIZED:
            raise GateError(f"execution not authorized (state {t and t['state']})")
        order = self.store.order_for_task(tid)
        cap = self.store.capture(order["order_id"])
        if not cap or cap["verification"].get("verdict") != "CAPTURE_VERIFIED":
            raise GateError("no verified capture on record")
        eid = self.store.claim_execution(order["order_id"], tid)
        if eid is None:
            raise GateError("this paid order has already been executed")
        offer = self.store.offer(task_id=tid)
        self.store.transition(tid, S.EXECUTING, "executor", "execution_claim", {"execution_id": eid},
                              {"execution_id": eid})
        started = time.time()
        try:
            docset = self._load_collection(offer)
            budget = policy.CAPABILITIES[offer["capability"]]["model_budget"]
            raw, rec, out, provided = engine.run(offer, docset, self.ai_client(), execution_id=eid, task_id=tid,
                                                 deadline_s=budget["max_seconds"])
            deliverable, ver = verifier.verify(raw, out, docset, offer, provided, self.runtime.envelope.execution_class)
        except Exception as e:                      # noqa: BLE001 - every failure after the claim goes to review
            return self._fail(tid, eid, offer, e, started)
        art = self._artifact(raw, rec, deliverable, ver, docset)
        adig = self.store.put_artifact(tid, art)
        self.ledger.put_record(eid, tid, f"VERIFICATION_{ver['status']}", art["ai_execution"], rec.get("output"))
        self.store.finish_execution(eid, ver["status"], adig)
        if ver["status"] == "FAILED":
            self.store.transition(tid, S.REVIEW_REQUIRED, "artifact_verifier", "artifact_verification", ver,
                                  reason="no finding survived R1d evidence verification")
            return S.REVIEW_REQUIRED
        self.store.transition(tid, S.DELIVERY_VERIFIED, "artifact_verifier", "artifact_verification",
                              dict(ver, artifact_digest=adig), {"artifact_digest": adig, "status": ver["status"]})
        rc = receipt2.build(self.store.task(tid), offer, order, cap, self.store.execution(tid), adig, ver,
                            self.store.events(tid), self.signer, self.payments.environment,
                            receipt2.ai_execution_section(art))
        self.store.put_receipt(tid, rc)
        self.store.transition(tid, S.DELIVERED, "receipt_issuer", "receipt", rc, {"receipt_digest": digest(rc)})
        return S.DELIVERED

    def _load_collection(self, offer):
        sid = offer["document_set"]["id"]
        if sid not in self.docsets or "/" in sid or sid.startswith("."):
            raise engine.EngineFailure("COLLECTION_NOT_APPROVED", f"{sid!r} is not an approved collection")
        root = os.path.join(self.corpus_root, sid)
        try:
            ds = DocumentSet(root, sid)
        except (OSError, ValueError) as e:
            raise engine.EngineFailure("COLLECTION_UNAVAILABLE", f"{sid}: {type(e).__name__}") from None
        if ds.manifest_digest != offer["document_set"]["manifest_digest"]:
            raise engine.EngineFailure("COLLECTION_CHANGED", f"{sid}: bytes differ from the collection that was "
                                       "priced (manifest digest mismatch)")
        return ds

    def _artifact(self, raw, rec, deliverable, ver, docset):
        env = self.runtime.envelope
        calls = self.ledger.calls(execution_id=self.store.execution(raw["task_id"])["execution_id"])
        usage = {k: sum(int((c.get(k) or 0)) for c in calls) for k in
                 ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens")}
        ai = {
            "execution_class": env.execution_class, "provider": rec["provider"], "requested_model": env.model,
            "reported_models": sorted({c["reported_model"] for c in calls if c["reported_model"]}),
            "authorization_ref": env.authorization_id, "execution_policy": env.public_view(),
            "source_collection": {"set_id": docset.id, "manifest_digest": docset.manifest_digest,
                                  "files": docset.files},
            "model_requests": len(calls),
            "requests": [{"call_id": c["call_id"], "seq": c["seq"], "state": c["state"], "provider": rec["provider"],
                          "transport": rec["transport"], "model": c["reported_model"], "request_id": c["request_id"],
                          "response_id": c["response_id"], "stop_reason": c["stop_reason"],
                          "request_digest": c["request_digest"], "response_digest": c["response_digest"],
                          "usage": {"input_tokens": c["input_tokens"], "output_tokens": c["output_tokens"]},
                          "est_input_tokens_max": c["est_input_tokens_max"],
                          "max_output_tokens": c["max_output_tokens"], "charged_usd": c["charged_usd"]}
                         for c in calls],
            "token_usage": usage,
            "cost": {"price_basis": env.price_basis,
                     "estimated_charge_usd": str(sum((Decimal(c["charged_usd"] or "0") for c in calls), Decimal(0))),
                     "worst_case_usd": str(sum((Decimal(c["worst_case_usd"]) for c in calls), Decimal(0))),
                     "authorization_totals_after": self.ledger.totals(env.authorization_id)},
            "outcome": f"VERIFICATION_{ver['status']}",
        }
        return {
            "artifact_version": ARTIFACT_VERSION, "engine": raw["engine"], "task_id": raw["task_id"],
            "offer_id": raw["offer_id"], "question": raw["question"], "document_set": raw["document_set"],
            "retrieval": raw["retrieval"], "framing": raw["framing"], "input_flags": raw["input_flags"],
            "flagged_passages": raw["flagged_passages"],
            "model_runs": [{k: rec.get(k) for k in ("provider", "execution_class", "transport", "requested_model",
                                                    "model", "system_sha256", "user_sha256", "schema_digest",
                                                    "output_sha256", "usage", "wall_s", "request_id", "response_id",
                                                    "stop_reason", "ledger_call_id", "request_digest",
                                                    "response_digest")}],
            "ai_execution": ai,
            "raw_report_digest": rec["output_sha256"],
            "report": deliverable, "delivery_verification": ver,
        }

    def _fail(self, tid, eid, offer, e, started):
        kind = getattr(e, "kind", type(e).__name__)
        calls = self.ledger.calls(execution_id=eid)
        env = self.runtime.envelope
        rec = {"execution_class": env.execution_class, "authorization_ref": env.authorization_id,
               "requested_model": env.model, "outcome": f"FAILED_{kind}", "error": str(e)[:500],
               "detail": getattr(e, "detail", {}), "wall_s": round(time.time() - started, 3),
               "model_requests": len(calls),
               "requests": [{k: c[k] for k in ("call_id", "state", "request_id", "response_id", "stop_reason",
                                               "input_tokens", "output_tokens", "charged_usd", "error")}
                            for c in calls],
               "authorization_totals_after": self.ledger.totals(env.authorization_id)}
        raw_out = getattr(e, "record", {}).get("output") if hasattr(e, "record") else None
        rdig = self.ledger.put_record(eid, tid, rec["outcome"], rec, raw_out)
        self.store.finish_execution(eid, "FAILED", None)
        self.store.transition(tid, S.REVIEW_REQUIRED, "executor", "execution_failure",
                              {"error": str(e)[:300], "ai_record_digest": rdig},
                              {"ai_failure": kind, "model_requests": len(calls),
                               "ledger_states": [c["state"] for c in calls]},
                              reason=f"paid task failed after capture (R1d {kind}); needs a human decision (no "
                                     "automatic refund, no automatic re-run)")
        return S.REVIEW_REQUIRED

    # ------------------------------------------------------------------ recovery
    def recover(self, stale_execution_s=900.0, orphan_offer_s=120.0):
        report = super().recover(stale_execution_s, orphan_offer_s)
        report["ai_calls"] = self.ledger.recover()
        return report

    def view(self, tid):
        v = super().view(tid)
        if v is not None:
            ex = v.get("execution")
            rec = self.ledger.record(ex["execution_id"]) if ex else None
            v["ai"] = {"calls": self.ledger.calls(task_id=tid),          # raw model output is never served
                       "record": rec and {k: rec[k] for k in ("outcome", "record_digest", "record")}}
        return v
