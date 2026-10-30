"""Independent payment verification against the stored offer.

Input is ONLY a provider response that this server fetched itself (GET /v2/checkout/orders/{id}) - never data
supplied by the browser, a webhook body, or a model. The verifier answers one question: does the provider's own
record show a completed capture of exactly the offered amount, in the offered currency, to the expected payee, for
this task and this offer, exactly once?

What it cannot establish: final settlement. A COMPLETED sandbox capture can still be refunded or reversed; that is
recorded later by reconciliation and never rewrites the delivery record.
"""
import time
from decimal import Decimal, InvalidOperation

VERDICTS = ("CAPTURE_VERIFIED", "CAPTURE_PENDING", "CAPTURE_DECLINED", "NOT_CAPTURED", "ORDER_VOIDED",
            "CAPTURE_REVERSED", "MISMATCH")


def _amount_eq(a, value, currency):
    try:
        return a["currency_code"] == currency and Decimal(a["value"]) == Decimal(value)
    except (KeyError, TypeError, InvalidOperation):
        return False


def verify_order(provider_order, offer, stored_order_id, expected_payee, environment, capture_response=None):
    checks = []

    def chk(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    o = provider_order or {}
    price = offer["price"]
    binding = all([
        chk("order_id_matches_record", o.get("id") == stored_order_id, f"{o.get('id')} vs {stored_order_id}"),
        chk("single_purchase_unit", len(o.get("purchase_units") or []) == 1),
    ])
    pu = (o.get("purchase_units") or [{}])[0]
    binding &= all([
        chk("reference_id_is_task", pu.get("reference_id") == offer["task_id"], str(pu.get("reference_id"))),
        chk("custom_id_is_offer", pu.get("custom_id") == offer["offer_id"], str(pu.get("custom_id"))),
        chk("order_amount_matches_offer", _amount_eq(pu.get("amount") or {}, price["value"], price["currency_code"]),
            f"{pu.get('amount')} vs {price}"),
        chk("payee_matches_configuration", bool(expected_payee) and (pu.get("payee") or {}).get("merchant_id") == expected_payee,
            f"{(pu.get('payee') or {}).get('merchant_id')} vs {expected_payee}"),
    ])
    caps = ((pu.get("payments") or {}).get("captures")) or []
    status = o.get("status")
    out = {"order_status": status, "capture_id": None, "capture_status": None, "checks": checks,
           "environment": environment, "verified_at": round(time.time(), 3),
           "source": f"server-side GET /v2/checkout/orders/{stored_order_id} ({environment})"}
    if not binding:
        return dict(out, verdict="MISMATCH")
    if status == "VOIDED":
        return dict(out, verdict="ORDER_VOIDED")
    if not caps:
        chk("capture_present", False, f"order status {status}")
        return dict(out, verdict="NOT_CAPTURED")
    chk("exactly_one_capture", len(caps) == 1, f"{len(caps)} captures")
    c = caps[0]
    out.update(capture_id=c.get("id"), capture_status=c.get("status"),
               capture_status_reason=(c.get("status_details") or {}).get("reason"))
    ok_amt = chk("capture_amount_matches_offer", _amount_eq(c.get("amount") or {}, price["value"], price["currency_code"]),
                 f"{c.get('amount')} vs {price}")
    if capture_response is not None:
        rc = ((((capture_response.get("purchase_units") or [{}])[0].get("payments") or {}).get("captures")) or [{}])[0]
        chk("capture_response_consistent_with_get", rc.get("id") == c.get("id"), f"{rc.get('id')} vs {c.get('id')}")
    if not all(x["ok"] for x in checks):
        return dict(out, verdict="MISMATCH")
    st = c.get("status")
    if st == "COMPLETED" and status == "COMPLETED" and ok_amt:
        return dict(out, verdict="CAPTURE_VERIFIED")
    if st == "PENDING":
        return dict(out, verdict="CAPTURE_PENDING")
    if st in ("DECLINED", "FAILED"):
        return dict(out, verdict="CAPTURE_DECLINED")
    if st in ("REFUNDED", "PARTIALLY_REFUNDED"):
        return dict(out, verdict="CAPTURE_REVERSED")
    return dict(out, verdict="MISMATCH")
