"""Delivery receipts: built only by server code from stored, verified records; signed with a DEMO test key.

What a receipt proves when it verifies:
  * the issuer holding the demo key wrote exactly this receipt (Ed25519 signature);
  * the delivered artifact has exactly this digest; the offer has exactly this digest;
  * the event chain head commits to the full INTENT -> OFFER -> AUTHORIZATION -> PAYMENT -> EXECUTION -> VERIFICATION
    -> RECEIPT history stored by the server.
What it does NOT prove: that PayPal produced the payment record (re-query PayPal server-side for that), that the
payment is settled and irreversible, or that the findings are true beyond "quoted verbatim from the named documents".
"""
import json
import os
import secrets
import time

from . import ed25519
from .canon import canonical_json, digest

LIMITS = [
    "A sandbox capture is test money. It is not revenue and not settlement.",
    "A COMPLETED capture can still be refunded or reversed; delivery does not make the payment final.",
    "Payment fields come from a server-side provider query at verification time; to check PayPal's origin, query "
    "the order again with the provider (the receipt's own signature cannot prove it).",
    "The signature uses a locally generated DEMO key with no identity binding beyond this deployment.",
    "Findings are verified as verbatim quotes from the named document set, not as true statements about the world.",
]


class DemoSigner:
    def __init__(self, path):
        if not os.path.exists(path):
            sk = secrets.token_bytes(32)
            d = {"label": "AUTONOMOS DEMO RECEIPT KEY - test only, generated locally, never use for value",
                 "created": round(time.time()), "secret_hex": sk.hex(),
                 "public_hex": ed25519.public_key(sk).hex()}
            tmp = f"{path}.{os.getpid()}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(d, f)
            try:
                os.link(tmp, path)            # atomic publish; if another process won, use its key
            except FileExistsError:
                pass
            finally:
                os.unlink(tmp)
        d = json.load(open(path))
        self._sk, self.public_hex = bytes.fromhex(d["secret_hex"]), d["public_hex"]
        self.key_id = "demo-ed25519-" + self.public_hex[:16]

    def sign(self, body):
        return ed25519.sign(self._sk, canonical_json(body).encode()).hex()


def build(task, offer, order, capture, execution, artifact_digest, delivery_verification, events, signer, environment):
    body = {
        "receipt_version": "1",
        "environment": environment,
        "task": {"task_id": task["task_id"], "capability": offer["capability"], "question": offer["question"]},
        "offer": {"offer_id": offer["offer_id"], "offer_digest": offer["_digest"], "amount": offer["price"]["value"],
                  "currency": offer["price"]["currency_code"], "document_set": offer["document_set"]},
        "buyer_approval": {"accepted": True, "offer_digest_confirmed_by_buyer": offer["_digest"]},
        "payment": {"provider": order["provider"], "order_id": order["order_id"],
                    "capture_id": capture["capture_id"], "capture_status": capture["status"],
                    "verification_verdict": capture["verification"]["verdict"],
                    "verified_at": capture["verification"]["verified_at"],
                    "verification_source": capture["verification"]["source"],
                    "checks_passed": sum(c["ok"] for c in capture["verification"]["checks"]),
                    "checks_total": len(capture["verification"]["checks"])},
        "execution": {"execution_id": execution["execution_id"], "started": execution["started"]},
        "artifact": {"digest": artifact_digest, "status": delivery_verification["status"],
                     "verified_findings": delivery_verification["verified_findings"],
                     "rejected_findings": delivery_verification["rejected_findings"],
                     "quotes_checked": delivery_verification["quotes_checked"]},
        "evidence_manifest": [{"seq": e["seq"], "to": e["to"], "actor": e["actor"], "evidence_kind": e["evidence_kind"],
                               "evidence_digest": e["evidence_digest"], "hash": e["hash"]} for e in events],
        "event_chain_head": events[-1]["hash"],
        "completed_at": round(time.time(), 3),
        "limits": LIMITS,
        "issuer": {"key_id": signer.key_id, "public_key_hex": signer.public_hex, "algorithm": "Ed25519 (RFC 8032)"},
    }
    return {"body": body, "signature_hex": signer.sign(body)}


def verify(receipt, artifact=None, events=None):
    """Offline verification anyone can run with the receipt (+ artifact, + exported events)."""
    out = []
    b = receipt["body"]
    pk = bytes.fromhex(b["issuer"]["public_key_hex"])
    out.append(("signature", ed25519.verify(pk, canonical_json(b).encode(), bytes.fromhex(receipt["signature_hex"]))))
    if artifact is not None:
        out.append(("artifact_digest", digest(artifact) == b["artifact"]["digest"]))
        out.append(("artifact_bound_to_offer", artifact.get("offer_id") == b["offer"]["offer_id"]))
    if events is not None:
        prev, ok = "GENESIS", True
        for e in events:
            ev = {"task_id": b["task"]["task_id"], "ts": e["ts"], "from": e["from"], "to": e["to"], "actor": e["actor"],
                  "evidence_kind": e["evidence_kind"], "evidence_digest": e["evidence_digest"], "detail": e["detail"],
                  "prev_hash": prev}
            ok &= e["prev_hash"] == prev and digest(ev) == e["hash"]
            prev = e["hash"]
        out.append(("event_chain_recomputes", ok))
        out.append(("event_chain_head_matches_receipt",
                    any(e["hash"] == b["event_chain_head"] for e in events)))
    out.append(("payment_verdict_is_verified", b["payment"]["verification_verdict"] == "CAPTURE_VERIFIED"))
    return {"valid": all(v for _, v in out), "checks": dict(out)}
