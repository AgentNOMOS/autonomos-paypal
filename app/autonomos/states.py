"""Task / payment state machine. The ONLY way to change a task's state is Store.transition(), which checks this
table and requires an evidence reference for every gated state."""

TASK_REQUESTED = "TASK_REQUESTED"
OFFER_ISSUED = "OFFER_ISSUED"
BUYER_APPROVAL_REQUIRED = "BUYER_APPROVAL_REQUIRED"
ORDER_CREATED = "ORDER_CREATED"
PAYMENT_PENDING = "PAYMENT_PENDING"
PAYMENT_CAPTURED = "PAYMENT_CAPTURED"
EXECUTION_AUTHORIZED = "EXECUTION_AUTHORIZED"
EXECUTING = "EXECUTING"
DELIVERY_VERIFIED = "DELIVERY_VERIFIED"
DELIVERED = "DELIVERED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
REVIEW_REQUIRED = "REVIEW_REQUIRED"

STATES = (TASK_REQUESTED, OFFER_ISSUED, BUYER_APPROVAL_REQUIRED, ORDER_CREATED, PAYMENT_PENDING, PAYMENT_CAPTURED,
          EXECUTION_AUTHORIZED, EXECUTING, DELIVERY_VERIFIED, DELIVERED, FAILED, CANCELLED, REVIEW_REQUIRED)
TERMINAL = (DELIVERED, FAILED, CANCELLED)
OPEN_FOR_CAPTURE = (PAYMENT_PENDING, REVIEW_REQUIRED)   # the only task states in which a capture may be reserved/sent

# from -> allowed targets
TRANSITIONS = {
    TASK_REQUESTED: {OFFER_ISSUED, FAILED},                       # FAILED = capability refused (nothing paid)
    OFFER_ISSUED: {BUYER_APPROVAL_REQUIRED, CANCELLED},
    BUYER_APPROVAL_REQUIRED: {ORDER_CREATED, CANCELLED},           # buyer accepted the offer -> create order
    ORDER_CREATED: {PAYMENT_PENDING, CANCELLED, FAILED},           # PAYMENT_PENDING = checkout/approval underway
    PAYMENT_PENDING: {PAYMENT_CAPTURED, CANCELLED, FAILED, REVIEW_REQUIRED},
    PAYMENT_CAPTURED: {EXECUTION_AUTHORIZED, REVIEW_REQUIRED},
    EXECUTION_AUTHORIZED: {EXECUTING},
    EXECUTING: {DELIVERY_VERIFIED, REVIEW_REQUIRED},               # failed / partial work after payment -> review
    DELIVERY_VERIFIED: {DELIVERED},
    REVIEW_REQUIRED: {PAYMENT_CAPTURED, CANCELLED, FAILED},        # only after re-verification against the provider
    DELIVERED: set(), FAILED: set(),
    CANCELLED: {REVIEW_REQUIRED},     # R1c: ONLY for a provider capture found later on a cancelled task (EDGE_RULES)
}
# states that may only be entered with a specific kind of evidence attached
REQUIRED_EVIDENCE = {
    OFFER_ISSUED: "offer",
    BUYER_APPROVAL_REQUIRED: "offer",
    ORDER_CREATED: "provider_order",
    PAYMENT_CAPTURED: "payment_verification",           # server-side provider response, verified against the offer
    EXECUTION_AUTHORIZED: "payment_verification",
    DELIVERY_VERIFIED: "artifact_verification",
    DELIVERED: "receipt",
}


# states only one component may enter (an operator can move a task to review / cancel / fail, never forward)
REQUIRED_ACTOR = {
    PAYMENT_CAPTURED: {"payment_verifier"},
    EXECUTION_AUTHORIZED: {"payment_verifier"},
    EXECUTING: {"executor"},
    DELIVERY_VERIFIED: {"artifact_verifier"},
    DELIVERED: {"receipt_issuer"},
}


# R1c: rules for one specific edge, checked by Store.transition inside the transaction (actor, evidence kind).
# Reopening a cancelled task is an explicit operator reconciliation backed by a server-side provider read; it never
# goes further than REVIEW_REQUIRED (a task that was ever CANCELLED is never executed automatically).
EDGE_RULES = {
    (CANCELLED, REVIEW_REQUIRED): ({"operator"}, "payment_verification"),
}


def allowed(src, dst, actor=None):
    """An edge listed in EDGE_RULES is closed unless the caller names an authorised actor."""
    if dst not in TRANSITIONS.get(src, set()):
        return False
    rule = EDGE_RULES.get((src, dst))
    return rule is None or actor in rule[0]
