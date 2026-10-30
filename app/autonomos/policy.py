"""Capability policy, fixed pricing and offers. Deterministic: nothing here reads model output.

The price, currency, payee and capability come only from this module's tables and the server configuration.
A model can neither propose nor change them; the offer is digested, and every later step is checked against that
digest."""
import re
import time
import uuid

from .canon import digest

POLICY_VERSION = "AUTONOMOS-POLICY-0.1.0"
OFFER_TTL_S = 30 * 60
CAPABILITIES = {
    "research_snapshot": {
        "title": "Verified Research Snapshot",
        "deliverable": "Up to 5 findings answering the buyer's question. Every finding cites verbatim quotes from the "
                       "approved document set; quotes are machine-checked before delivery. Unanswerable parts are "
                       "declared, not invented.",
        "max_findings": 5,
        "model_budget": {"max_model_calls": 2, "max_seconds": 240},
    },
}
# Requests that must never become paid work in this demo, whatever capability is named.
REFUSED_INTENTS = [
    (r"\b(send|transfer|wire|pay|refund|payout)\b.{0,40}\b(money|funds|usd|\$|payment|account)", "moves money"),
    (r"\b(password|credential|api[_ -]?key|secret|private key|seed phrase)\b", "asks for credentials or secrets"),
    (r"\b(browse|scrape|crawl|search the web|google)\b", "requires unrestricted web access"),
    (r"\b(medical|legal|tax|investment) advice\b", "regulated advice"),
]
MIN_TOPIC, MAX_TOPIC = 12, 600


class PolicyRefusal(ValueError):
    pass


def check_request(request, corpora):
    """Return the normalised capability spec or raise PolicyRefusal with a buyer-readable reason."""
    cap = request.get("capability")
    if cap not in CAPABILITIES:
        raise PolicyRefusal(f"capability {cap!r} is not offered (offered: {sorted(CAPABILITIES)})")
    topic = (request.get("topic") or "").strip()
    if not (MIN_TOPIC <= len(topic) <= MAX_TOPIC):
        raise PolicyRefusal(f"the question must be {MIN_TOPIC}-{MAX_TOPIC} characters")
    for pat, why in REFUSED_INTENTS:
        if re.search(pat, topic, re.I):
            raise PolicyRefusal(f"request refused: it {why}, which this service does not do")
    corpus = request.get("corpus")
    if corpus not in corpora:
        raise PolicyRefusal(f"document set {corpus!r} is not approved (approved: {sorted(corpora)})")
    return {"capability": cap, "topic": topic, "corpus": corpus}


def make_offer(task_id, spec, corpus_manifest_digest, cfg, now=None):
    now = now or time.time()
    cap = CAPABILITIES[spec["capability"]]
    offer = {
        "offer_id": "OFFER-" + uuid.uuid4().hex[:12].upper(),
        "task_id": task_id,
        "capability": spec["capability"],
        "title": cap["title"],
        "question": spec["topic"],
        "document_set": {"id": spec["corpus"], "manifest_digest": corpus_manifest_digest},
        "deliverable": cap["deliverable"],
        "max_findings": cap["max_findings"],
        "price": {"value": cfg.price_value, "currency_code": cfg.price_currency},
        "payee": {"merchant_id": cfg.paypal_merchant_id or None, "environment": "SANDBOX"},
        "payment_provider": cfg.payment_provider,
        "terms": ["PayPal Sandbox only: test money, not real revenue.",
                  "Work starts only after the payment is captured and verified server-side.",
                  "This service does not hold funds in escrow and makes no refund or buyer-protection promise; "
                  "if paid work fails, the task is marked REVIEW_REQUIRED for a human decision."],
        "created": round(now, 3), "expires": round(now + OFFER_TTL_S, 3),
        "policy_version": POLICY_VERSION,
    }
    return offer, digest(offer)


def offer_is_live(offer, now=None):
    return (now or time.time()) <= offer["expires"]
