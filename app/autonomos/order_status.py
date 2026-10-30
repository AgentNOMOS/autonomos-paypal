"""R1c: READ-ONLY order diagnosis (CLI `python -m autonomos order-status TASK_ID|OFFER_ID|ORDER_ID`).

Guarantees (each covered by tests/test_r1c_cancel_integrity.py):
  * The local database is opened with SQLite `mode=ro` (plus `immutable=1` when no -wal exists), so a write is
    impossible at the driver level and no file is created. Store is never constructed: its constructor writes
    (journal mode, schema, migrations).
  * Provider access goes through ReadOnlyOrders, which exposes get_order() and nothing else. With PayPal Sandbox that
    is one OAuth token request plus GET /v2/checkout/orders/{id}. No create, capture, void or refund is reachable.
  * Approval, capture and merchant identity are reported separately, from the provider's own record; a browser
    callback is reported as navigation only. IDs are masked; links, tokens and secrets are never printed.
  * It never changes task state and never triggers execution. State-changing recovery is a separate, explicitly
    authorized command: `reconcile ORDER_ID --owner-confirm`.
"""
import json
import os
import sqlite3
import time
import urllib.parse

from . import states as S
from .canon import digest
from .payment_verifier import verify_order
from .payments import ProviderError
from .payments.mock import MOCK_MERCHANT_ID

PHASES = ("BUYER_NAVIGATED_AWAY", "AWAITING_PAYER_ACTION", "BUYER_APPROVED", "CAPTURE_IN_PROGRESS",
          "CAPTURE_OUTCOME_UNKNOWN", "CAPTURE_VERIFIED_COMPLETED", "CAPTURE_NOT_VERIFIED", "ORDER_VOIDED",
          "TERMINATED_UNPAID", "PROVIDER_UNREADABLE", "UNKNOWN_PROVIDER_STATUS")
LEGACY_REDIRECT_REASON = "buyer cancelled at PayPal checkout"     # pre-R1c: cancel redirect cancelled the task


class ReadOnlyOrders:
    """The only provider surface of the diagnosis path."""
    __slots__ = ("_provider",)

    def __init__(self, provider):
        self._provider = provider

    @property
    def environment(self):
        return self._provider.environment

    def get_order(self, order_id):
        return self._provider.get_order(order_id)

    def calls(self):
        c = getattr(self._provider, "calls", None)
        c = c() if callable(c) else c
        return [(x[0], x[1]) for x in (c or [])]


def mask(x):
    return (x[:4] + "…" + x[-4:]) if isinstance(x, str) and len(x) > 10 else x


def open_ro(path):
    """Read-only connection that creates no file. AutoNOMOS databases are WAL-mode: with a -wal file present (live
    server or uncheckpointed data) `mode=ro` shares it and sees every committed transaction; without one, the main file
    holds everything committed and changes only at a checkpoint, so `immutable=1` reads it without creating -wal/-shm."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no AutoNOMOS database at {path}")
    q = "?mode=ro" if os.path.exists(path + "-wal") else "?mode=ro&immutable=1"
    return sqlite3.connect("file:" + urllib.parse.quote(os.path.abspath(path)) + q, uri=True)


def _locate(db, ref):
    for col in ("order_id", "task_id", "offer_id"):
        r = db.execute(f"SELECT order_id, task_id, offer_id, created_json FROM orders WHERE {col}=?", (ref,)).fetchone()
        if r:
            return r
    return None


def _captures(o):
    return [c for pu in (o or {}).get("purchase_units") or [] for c in ((pu.get("payments") or {}).get("captures") or [])]


def classify(task_state, attempt, verdict, provider_order, navigated, now=None):
    """Payment phase from durable local state + the provider record (None = provider unreadable). Pure function."""
    now = time.time() if now is None else now
    ok = provider_order is not None
    status = (provider_order or {}).get("status")
    a = attempt or {}
    if ok and _captures(provider_order):
        return "CAPTURE_VERIFIED_COMPLETED" if verdict == "CAPTURE_VERIFIED" else "CAPTURE_NOT_VERIFIED"
    if ok and status == "VOIDED":
        return "ORDER_VOIDED"
    if a.get("state") == "OUTCOME_UNKNOWN":
        return "CAPTURE_OUTCOME_UNKNOWN"
    if a.get("state") == "SENT" or (a.get("state") == "RESERVED" and (a.get("lease_until") or 0) > now):
        return "CAPTURE_IN_PROGRESS"
    if not ok:
        return "PROVIDER_UNREADABLE"
    if task_state in (S.CANCELLED, S.FAILED):
        return "TERMINATED_UNPAID"
    if status == "APPROVED":
        return "BUYER_APPROVED"
    if status in ("CREATED", "PAYER_ACTION_REQUIRED"):
        return "BUYER_NAVIGATED_AWAY" if navigated else "AWAITING_PAYER_ACTION"
    return "UNKNOWN_PROVIDER_STATUS"


def order_status(cfg, ref, provider, db_path=None, now=None):
    """Diagnose one order. `provider` is any object with get_order(); it is wrapped in ReadOnlyOrders here."""
    ro = provider if isinstance(provider, ReadOnlyOrders) else ReadOnlyOrders(provider)
    db = open_ro(db_path or os.path.join(cfg.data_dir, "autonomos.sqlite"))
    try:
        hit = _locate(db, ref)
        if hit is None:
            raise LookupError("no provider order is recorded for this task, offer or order reference")
        oid, tid, offer_id, created_json = hit
        st, reason = db.execute("SELECT state, reason FROM tasks WHERE task_id=?", (tid,)).fetchone()
        ojson, odig = db.execute("SELECT offer_json, offer_digest FROM offers WHERE offer_id=?", (offer_id,)).fetchone()
        offer = json.loads(ojson)
        cols = ("state", "generation", "sends", "lease_until", "last_error", "first_sent")
        r = db.execute("SELECT " + ", ".join(cols) + " FROM capture_attempts WHERE order_id=?", (oid,)).fetchone()
        attempt = dict(zip(cols, r)) if r else None
        r = db.execute("SELECT status, verification_json FROM captures WHERE order_id=?", (oid,)).fetchone()
        local_cap = {"status": r[0], "verdict": json.loads(r[1]).get("verdict")} if r else None
        nav = [{"ts": e[0], "route": json.loads(e[1]).get("route")} for e in db.execute(
            "SELECT ts, detail_json FROM events WHERE task_id=? AND evidence_kind='checkout_navigation' ORDER BY seq",
            (tid,)).fetchall()]
        cancel_ev = db.execute("SELECT detail_json FROM events WHERE task_id=? AND to_state=? AND from_state!=? "
                               "ORDER BY seq DESC LIMIT 1", (tid, S.CANCELLED, S.CANCELLED)).fetchone()
        ever_cancelled = cancel_ev is not None
    finally:
        db.close()

    expected = cfg.paypal_merchant_id if ro.environment == "SANDBOX" else MOCK_MERCHANT_ID
    out = {"read_only": True, "local_db": "opened read-only (mode=ro); Store not constructed",
           "task": {"task_id": tid, "state": st, "reason": reason, "ever_cancelled": ever_cancelled},
           "offer_id": offer_id, "offer_digest_intact": digest(offer) == odig, "order_id": mask(oid),
           "environment": ro.environment,
           "local": {"order_status_at_creation": json.loads(created_json).get("status"),
                     "capture_attempt": attempt and {k: attempt[k] for k in ("state", "generation", "sends",
                                                                              "last_error", "first_sent")},
                     "capture_record": local_cap, "checkout_navigation": nav}}
    if ever_cancelled:
        d = json.loads(cancel_ev[0])
        out["task"]["cancellation"] = {"explicit": bool(d.get("explicit")),
                                       "legacy_redirect_cancellation": reason == LEGACY_REDIRECT_REASON
                                       and not d.get("explicit")}
    try:
        o = ro.get_order(oid)
        out["provider"] = {"readable": True, "id_matches_local": o.get("id") == oid, "status": o.get("status"),
                           "create_time": o.get("create_time"), "update_time": o.get("update_time"),
                           "links": [l.get("rel") for l in o.get("links") or []]}
    except ProviderError as e:
        o = None
        out["provider"] = {"readable": False, "status_code": e.status, "issue": e.issue, "debug_id": e.debug_id}
    out["provider_calls"] = [(m, p.replace(oid, mask(oid))) for m, p in ro.calls()]

    pstatus = (o or {}).get("status")
    caps = _captures(o)
    pu = ((o or {}).get("purchase_units") or [{}])[0]
    v = verify_order(o, offer, oid, expected, ro.environment) if o else None
    out["approval"] = {"provider_status": pstatus,
                       "buyer_approved": None if o is None or pstatus == "VOIDED" else
                       (pstatus in ("APPROVED", "COMPLETED") or bool(caps)),
                       "payer_recorded": None if o is None else bool(o.get("payer"))}
    out["capture"] = {"present": None if o is None else bool(caps), "count": None if o is None else len(caps),
                      "items": [{"id": mask(c.get("id")), "status": c.get("status"), "amount": c.get("amount"),
                                 "final_capture": c.get("final_capture")} for c in caps],
                      "verifier_verdict": v and v["verdict"]}
    payee = (pu.get("payee") or {}).get("merchant_id") if o else None
    out["merchant"] = {"expected": expected, "provider_payee": payee, "match": None if o is None else payee == expected}
    out["amount"] = {"offer": offer["price"], "provider": pu.get("amount") if o else None}

    phase = classify(st, attempt, v and v["verdict"], o, bool(nav), now)
    alerts = []
    if o is not None and not out["provider"]["id_matches_local"]:
        alerts.append("PROVIDER_ID_MISMATCH")
    if o is not None and payee != expected:
        alerts.append("PAYEE_MISMATCH")
    if st in (S.CANCELLED, S.FAILED) and caps:
        alerts.append("CAPTURE_ON_TERMINAL_TASK")
    if st == S.CANCELLED and attempt and (attempt["sends"] or attempt["state"] in ("SENT", "OUTCOME_UNKNOWN")):
        alerts.append("CAPTURE_ATTEMPT_ON_CANCELLED_TASK")
    if st == S.CANCELLED and pstatus == "APPROVED":
        alerts.append("APPROVED_AT_PROVIDER_AFTER_LOCAL_CANCEL")
    if out["task"].get("cancellation", {}).get("legacy_redirect_cancellation"):
        alerts.append("LEGACY_REDIRECT_CANCELLATION")
    if st in (S.PAYMENT_CAPTURED, S.EXECUTION_AUTHORIZED, S.EXECUTING, S.DELIVERY_VERIFIED, S.DELIVERED) \
            and o is not None and not caps:
        alerts.append("LOCAL_CAPTURE_NOT_AT_PROVIDER")
    out["phase"], out["alerts"] = phase, alerts
    out["next_step"] = _next_step(st, phase, alerts)
    return out


def _next_step(st, phase, alerts):
    if "CAPTURE_ON_TERMINAL_TASK" in alerts and st == S.CANCELLED:
        return "owner: `reconcile ORDER_ID --owner-confirm` records the capture and moves the task to REVIEW_REQUIRED"
    if alerts and set(alerts) - {"LEGACY_REDIRECT_CANCELLATION", "APPROVED_AT_PROVIDER_AFTER_LOCAL_CANCEL"}:
        return "owner review: provider and local records disagree; nothing is changed automatically"
    if phase in ("CAPTURE_OUTCOME_UNKNOWN", "CAPTURE_IN_PROGRESS"):
        return "wait; never re-send. Run order-status again later (R1-08 runbook for unknown outcomes)"
    if phase in ("BUYER_NAVIGATED_AWAY", "AWAITING_PAYER_ACTION", "BUYER_APPROVED"):
        return "buyer: continue at PayPal, or cancel explicitly on the task page"
    if phase == "PROVIDER_UNREADABLE":
        return "provider not readable now; nothing concluded. Retry order-status later"
    return "none: local and provider records are consistent"
