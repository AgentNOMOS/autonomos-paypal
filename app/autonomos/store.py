"""SQLite store. Idempotency and "at most once" guarantees are enforced by UNIQUE constraints, not by convention:
one offer per task, one order per offer, one capture record per order, one execution per order, one receipt per
task, one processing per webhook transmission/event id. Every state change is an append-only, hash-chained event."""
import contextlib
import json
import os
import sqlite3
import threading
import time
import uuid

from . import states as S
from .canon import canonical_json, digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, created REAL, request_json TEXT NOT NULL,
  state TEXT NOT NULL, capability TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS offers (offer_id TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL REFERENCES tasks,
  offer_json TEXT NOT NULL, offer_digest TEXT NOT NULL, amount TEXT NOT NULL, currency TEXT NOT NULL,
  accepted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS orders (order_id TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL REFERENCES tasks,
  offer_id TEXT UNIQUE NOT NULL REFERENCES offers, provider TEXT NOT NULL, request_id TEXT UNIQUE NOT NULL,
  created_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS captures (order_id TEXT PRIMARY KEY REFERENCES orders, capture_id TEXT UNIQUE,
  status TEXT NOT NULL, verification_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS executions (execution_id TEXT PRIMARY KEY, order_id TEXT UNIQUE NOT NULL REFERENCES orders,
  task_id TEXT UNIQUE NOT NULL REFERENCES tasks, started REAL, status TEXT, artifact_digest TEXT);
CREATE TABLE IF NOT EXISTS artifacts (digest TEXT PRIMARY KEY, task_id TEXT NOT NULL, content_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipts (task_id TEXT PRIMARY KEY REFERENCES tasks, receipt_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS webhooks (transmission_id TEXT PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, received REAL,
  verified TEXT, event_type TEXT, resource_id TEXT, outcome TEXT);
CREATE TABLE IF NOT EXISTS capture_attempts (order_id TEXT PRIMARY KEY REFERENCES orders,
  generation INTEGER NOT NULL, request_id TEXT UNIQUE NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('RESERVED','SENT','OUTCOME_UNKNOWN','RESOLVED','RELEASED')),
  owner TEXT NOT NULL, lease_until REAL NOT NULL, sends INTEGER NOT NULL DEFAULT 0, created REAL, updated REAL,
  last_error TEXT, debug_id TEXT, first_sent REAL, reapproval INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, ts REAL, from_state TEXT,
  to_state TEXT, actor TEXT, evidence_kind TEXT, evidence_digest TEXT, detail_json TEXT, prev_hash TEXT, hash TEXT);
"""
# Components allowed to move a task. The AI model is deliberately absent.
ACTORS = {"policy", "buyer", "payment_gateway", "payment_verifier", "executor", "artifact_verifier", "receipt_issuer",
          "webhook_reconciler", "operator"}


class TransitionError(RuntimeError):
    pass


class ConcurrencyError(RuntimeError):
    """An update that had to change exactly one row changed none: someone else owns the record. Do not proceed."""


MAX_CAPTURE_GENERATIONS = 3      # a new logical capture attempt only after a DEFINITIVE provider refusal + re-approval
_ATTEMPT_COLS = ("order_id", "generation", "request_id", "state", "owner", "lease_until", "sends", "last_error",
                 "debug_id", "first_sent", "reapproval")


class _Locked:
    """Serialises access to one sqlite connection shared by the web server's threads (results fetched under lock)."""

    def __init__(self, db, lock):
        self._d, self._l = db, lock

    def execute(self, sql, args=()):
        with self._l:
            cur = self._d.execute(sql, args)
            return _Result(cur.fetchall(), cur.rowcount)


class _Result:
    def __init__(self, rows, rowcount):
        self._rows, self.rowcount = rows, rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class Store:
    def __init__(self, path):
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA busy_timeout = 30000")       # other PROCESSES wait for the write lock
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(SCHEMA)
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(capture_attempts)")}
        for col, ddl in (("first_sent", "REAL"), ("reapproval", "INTEGER NOT NULL DEFAULT 0")):
            if col not in cols:                                # R1 checkpoint databases: additive migration only
                self._db.execute(f"ALTER TABLE capture_attempts ADD COLUMN {col} {ddl}")
        self.lock = threading.RLock()
        self.db = _Locked(self._db, self.lock)          # every statement, read or write, is serialised
        self.owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"  # identity of this process for leases

    @contextlib.contextmanager
    def txn(self):
        """BEGIN IMMEDIATE: one writer across threads AND processes for the whole block. Never put a network call
        inside a txn() block."""
        with self.lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _one(self, sql, args, what):
        n = self.db.execute(sql, args).rowcount
        if n != 1:
            raise ConcurrencyError(f"{what}: expected 1 row changed, got {n}")

    def set_capability(self, tid, cap):
        self.db.execute("UPDATE tasks SET capability=? WHERE task_id=?", (cap, tid))

    # ------------------------------------------------------------------ tasks and events
    def new_task(self, request):
        tid = "TASK-" + uuid.uuid4().hex[:12].upper()
        with self.txn():
            self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                            (tid, time.time(), canonical_json(request), S.TASK_REQUESTED, None, None))
            self._event(tid, None, S.TASK_REQUESTED, "buyer", "request", request, {})
        return tid

    def task(self, tid):
        r = self.db.execute("SELECT task_id, created, request_json, state, capability, reason FROM tasks WHERE task_id=?",
                            (tid,)).fetchone()
        if not r:
            return None
        return {"task_id": r[0], "created": r[1], "request": json.loads(r[2]), "state": r[3], "capability": r[4],
                "reason": r[5]}

    def _event(self, tid, src, dst, actor, kind, evidence, detail):
        prev = self.db.execute("SELECT hash FROM events WHERE task_id=? ORDER BY seq DESC LIMIT 1", (tid,)).fetchone()
        prev = prev[0] if prev else "GENESIS"
        ev = {"task_id": tid, "ts": round(time.time(), 6), "from": src, "to": dst, "actor": actor,
              "evidence_kind": kind, "evidence_digest": digest(evidence) if evidence is not None else None,
              "detail": detail, "prev_hash": prev}
        h = digest(ev)
        self.db.execute("INSERT INTO events (task_id, ts, from_state, to_state, actor, evidence_kind, evidence_digest, "
                        "detail_json, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (tid, ev["ts"], src, dst, actor, kind, ev["evidence_digest"], canonical_json(detail), prev, h))
        return h

    def transition(self, tid, dst, actor, evidence_kind=None, evidence=None, detail=None, reason=None, guard=None):
        """Atomic, table-checked state change. Raises TransitionError instead of guessing.
        `guard(src)` (R1c) runs INSIDE the same write transaction and may raise to refuse: it is how a check on other
        tables (e.g. "no capture may be in flight") is made atomic with the state change."""
        if actor not in ACTORS:
            raise TransitionError(f"actor {actor!r} may not change task state")
        if dst in S.REQUIRED_ACTOR and actor not in S.REQUIRED_ACTOR[dst]:
            raise TransitionError(f"{dst} may only be entered by {sorted(S.REQUIRED_ACTOR[dst])}, not {actor!r}")
        need = S.REQUIRED_EVIDENCE.get(dst)
        if need and (evidence_kind != need or evidence is None):
            raise TransitionError(f"{dst} requires evidence of kind {need!r}, got {evidence_kind!r}")
        with self.txn():
            cur = self.task(tid)
            if cur is None:
                raise TransitionError(f"unknown task {tid}")
            src = cur["state"]
            if not S.allowed(src, dst, actor):
                raise TransitionError(f"{tid}: transition {src} -> {dst} is not permitted for {actor!r}")
            rule = S.EDGE_RULES.get((src, dst))
            if rule and (evidence_kind != rule[1] or evidence is None):
                raise TransitionError(f"{src} -> {dst} requires evidence of kind {rule[1]!r}, got {evidence_kind!r}")
            if guard is not None:
                guard(src)
            n = self.db.execute("UPDATE tasks SET state=?, reason=COALESCE(?, reason) WHERE task_id=? AND state=?",
                                (dst, reason, tid, src)).rowcount
            if n != 1:
                raise TransitionError(f"{tid}: concurrent state change detected")
            self._event(tid, src, dst, actor, evidence_kind, evidence, detail or {})
        return dst

    def note(self, tid, actor, kind, evidence, detail):
        """Evidence event without a state change (e.g. pending capture, post-delivery reconciliation)."""
        with self.txn():
            st = self.task(tid)["state"]
            self._event(tid, st, st, actor, kind, evidence, detail)

    def events(self, tid):
        rows = self.db.execute("SELECT seq, ts, from_state, to_state, actor, evidence_kind, evidence_digest, detail_json, "
                               "prev_hash, hash FROM events WHERE task_id=? ORDER BY seq", (tid,)).fetchall()
        return [{"seq": r[0], "ts": r[1], "from": r[2], "to": r[3], "actor": r[4], "evidence_kind": r[5],
                 "evidence_digest": r[6], "detail": json.loads(r[7]), "prev_hash": r[8], "hash": r[9]} for r in rows]

    # ------------------------------------------------------------------ offers / orders / captures / executions
    def put_offer(self, offer):
        with self.lock:
            self.db.execute("INSERT INTO offers VALUES (?,?,?,?,?,?,0)",
                            (offer["offer_id"], offer["task_id"], canonical_json(offer), digest(offer),
                             offer["price"]["value"], offer["price"]["currency_code"]))

    def offer(self, offer_id=None, task_id=None):
        q = "SELECT offer_json, offer_digest, accepted FROM offers WHERE " + ("offer_id=?" if offer_id else "task_id=?")
        r = self.db.execute(q, (offer_id or task_id,)).fetchone()
        if not r:
            return None
        o = json.loads(r[0])
        if digest(o) != r[1]:
            raise TransitionError("stored offer digest mismatch")
        return dict(o, _digest=r[1], _accepted=bool(r[2]))

    def accept_offer(self, offer_id):
        """Claim the offer for order creation. Exactly one caller (thread or process) gets True."""
        with self.lock:
            return self.db.execute("UPDATE offers SET accepted=1 WHERE offer_id=? AND accepted=0", (offer_id,)).rowcount == 1

    def put_order(self, order_id, task_id, offer_id, provider, request_id, created):
        with self.lock:
            self.db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?)",
                            (order_id, task_id, offer_id, provider, request_id, canonical_json(created)))

    def order_for_task(self, task_id):
        r = self.db.execute("SELECT order_id, offer_id, provider, request_id, created_json FROM orders WHERE task_id=?",
                            (task_id,)).fetchone()
        return None if not r else {"order_id": r[0], "offer_id": r[1], "provider": r[2], "request_id": r[3],
                                   "created": json.loads(r[4])}

    def task_for_order(self, order_id):
        r = self.db.execute("SELECT task_id FROM orders WHERE order_id=?", (order_id,)).fetchone()
        return r[0] if r else None

    def put_capture(self, order_id, capture_id, status, verification):
        with self.lock:
            self.db.execute("INSERT INTO captures VALUES (?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET "
                            "capture_id=excluded.capture_id, status=excluded.status, "
                            "verification_json=excluded.verification_json WHERE captures.status != 'COMPLETED'",
                            (order_id, capture_id, status, canonical_json(verification)))

    def capture(self, order_id):
        r = self.db.execute("SELECT capture_id, status, verification_json FROM captures WHERE order_id=?",
                            (order_id,)).fetchone()
        return None if not r else {"capture_id": r[0], "status": r[1], "verification": json.loads(r[2])}

    def claim_execution(self, order_id, task_id):
        """At most one execution per paid order: the UNIQUE constraints make a second claim fail."""
        eid = "EXEC-" + uuid.uuid4().hex[:12].upper()
        try:
            with self.lock:
                self.db.execute("INSERT INTO executions VALUES (?,?,?,?,?,?)",
                                (eid, order_id, task_id, time.time(), "STARTED", None))
            return eid
        except sqlite3.IntegrityError:
            return None

    def finish_execution(self, eid, status, artifact_digest):
        self._one("UPDATE executions SET status=?, artifact_digest=? WHERE execution_id=? AND status='STARTED'",
                  (status, artifact_digest, eid), f"finish execution {eid}")

    def stale_executions(self, older_than_s):
        rows = self.db.execute("SELECT execution_id, task_id FROM executions WHERE status='STARTED' AND started < ?",
                               (time.time() - older_than_s,)).fetchall()
        return [{"execution_id": r[0], "task_id": r[1]} for r in rows]

    def abandon_execution(self, eid):
        self._one("UPDATE executions SET status='ABANDONED' WHERE execution_id=? AND status='STARTED'", (eid,),
                  f"abandon execution {eid}")

    def execution(self, task_id):
        r = self.db.execute("SELECT execution_id, order_id, started, status, artifact_digest FROM executions "
                            "WHERE task_id=?", (task_id,)).fetchone()
        return None if not r else {"execution_id": r[0], "order_id": r[1], "started": r[2], "status": r[3],
                                   "artifact_digest": r[4]}

    def put_artifact(self, task_id, artifact):
        d = digest(artifact)
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO artifacts VALUES (?,?,?)", (d, task_id, canonical_json(artifact)))
        return d

    def artifact(self, d):
        r = self.db.execute("SELECT content_json FROM artifacts WHERE digest=?", (d,)).fetchone()
        return json.loads(r[0]) if r else None

    def put_receipt(self, task_id, receipt):
        with self.lock:
            self.db.execute("INSERT INTO receipts VALUES (?,?)", (task_id, canonical_json(receipt)))

    def receipt(self, task_id):
        r = self.db.execute("SELECT receipt_json FROM receipts WHERE task_id=?", (task_id,)).fetchone()
        return json.loads(r[0]) if r else None

    def record_webhook(self, transmission_id, event_id, verified, event_type, resource_id):
        """Durably record a verified event BEFORE it is acknowledged. Returns False if this transmission or event id
        was already recorded (replay / duplicate delivery). DB errors propagate -> the caller answers 503 so PayPal
        retries the delivery."""
        try:
            with self.lock:
                self.db.execute("INSERT INTO webhooks VALUES (?,?,?,?,?,?,?)",
                                (transmission_id, event_id, time.time(), verified, event_type, resource_id, None))
            return True
        except sqlite3.IntegrityError:
            return False

    def pending_webhooks(self):
        """Recorded (= acknowledged) events whose asynchronous reconciliation never finished (e.g. crash after ack)."""
        return self.db.execute("SELECT transmission_id, resource_id, event_type FROM webhooks WHERE outcome IS NULL "
                               "OR outcome='queued' ORDER BY received").fetchall()

    def set_webhook_outcome(self, transmission_id, outcome):
        self._one("UPDATE webhooks SET outcome=? WHERE transmission_id=?", (outcome, transmission_id),
                  f"webhook outcome {transmission_id}")

    # ------------------------------------------------------------------ durable capture reservation
    @staticmethod
    def capture_request_id(order_id, generation):
        """Stable per LOGICAL capture attempt: every retry of the same attempt reuses it (PayPal idempotency)."""
        return f"autonomos-capture-{order_id}" + ("" if generation == 1 else f"-g{generation}")

    def capture_attempt(self, order_id):
        r = self.db.execute("SELECT " + ", ".join(_ATTEMPT_COLS) + " FROM capture_attempts WHERE order_id=?",
                            (order_id,)).fetchone()
        return None if not r else dict(zip(_ATTEMPT_COLS, r))

    def reserve_capture(self, order_id, lease_s=60.0, new_generation=False):
        """Atomically decide which process owns the capture attempt for this order.

        Returns (verdict, attempt):
          OWNER          we own a fresh attempt (nothing was ever sent)       -> may capture
          OWNER_RECOVER  we took over an expired lease after something MAY have been sent -> reconcile (GET) first
          BUSY           another live owner holds the lease                   -> do nothing
          RESOLVED       the attempt already finished                         -> nothing to capture
          EXHAUSTED      too many definitive refusals                         -> review
          CLOSED         the task is not PAYMENT_PENDING / REVIEW_REQUIRED     -> nothing may be sent (R1c)
        """
        now = time.time()
        with self.txn():
            a = self.capture_attempt(order_id)
            if self._order_task_state(order_id) not in S.OPEN_FOR_CAPTURE:
                return "CLOSED", a                               # R1c: a cancelled/terminal task never reserves
            if a is None:
                rid = self.capture_request_id(order_id, 1)
                self.db.execute("INSERT INTO capture_attempts (order_id, generation, request_id, state, owner, "
                                "lease_until, sends, created, updated) VALUES (?,?,?,?,?,?,?,?,?)",
                                (order_id, 1, rid, "RESERVED", self.owner, now + lease_s, 0, now, now))
                return "OWNER", self.capture_attempt(order_id)
            if a["state"] == "RESOLVED":
                return "RESOLVED", a
            if a["state"] == "RELEASED" and a["sends"] == 0:     # released before anything was sent: re-own
                self._one("UPDATE capture_attempts SET state='RESERVED', owner=?, lease_until=?, updated=? "
                          "WHERE order_id=? AND state='RELEASED' AND sends=0", (self.owner, now + lease_s, now, order_id),
                          "re-own unsent capture attempt")
                return "OWNER", self.capture_attempt(order_id)
            if a["state"] == "RELEASED":
                if not new_generation:
                    return "RESOLVED", a
                if a["generation"] >= MAX_CAPTURE_GENERATIONS:
                    return "EXHAUSTED", a
                if not a["reapproval"]:                          # only an explicit buyer re-approval opens a generation
                    return "RESOLVED", a
                g = a["generation"] + 1
                self._one("UPDATE capture_attempts SET generation=?, request_id=?, state='RESERVED', owner=?, "
                          "lease_until=?, updated=?, sends=0, first_sent=NULL, reapproval=0 "
                          "WHERE order_id=? AND state='RELEASED' AND reapproval=1 AND generation=?",
                          (g, self.capture_request_id(order_id, g), self.owner, now + lease_s, now, order_id,
                           a["generation"]), "new capture generation")
                return "OWNER", self.capture_attempt(order_id)
            if a["owner"] != self.owner and a["lease_until"] > now:
                return "BUSY", a
            self._one("UPDATE capture_attempts SET owner=?, lease_until=?, updated=? WHERE order_id=? AND owner=? "
                      "AND state=?", (self.owner, now + lease_s, now, order_id, a["owner"], a["state"]),
                      "take over capture lease")
            took = self.capture_attempt(order_id)
            return ("OWNER" if a["state"] == "RESERVED" and a["sends"] == 0 else "OWNER_RECOVER"), took

    def capture_mark(self, order_id, frm, to, error=None, debug_id=None, sent=False, release_lease=False):
        """Owner-checked state change of the attempt; exactly one row must change or we stop. `sent=True` is written
        BEFORE the network call (write-ahead): after a crash, sends>0 means "a capture may be in flight".
        R1c: the write-ahead mark also requires the task to be open for capture, in the SAME statement. A cancellation
        commits either before it (this changes 0 rows -> ConcurrencyError -> nothing is sent) or after it (the
        cancellation then sees SENT and refuses)."""
        now = time.time()
        self._one("UPDATE capture_attempts SET state=?, sends=sends+?, last_error=COALESCE(?, last_error), "
                  "debug_id=COALESCE(?, debug_id), updated=?, first_sent=CASE WHEN ?=1 THEN COALESCE(first_sent, ?) "
                  "ELSE first_sent END, lease_until=CASE WHEN ?=1 THEN 0 ELSE lease_until END "
                  "WHERE order_id=? AND owner=? AND state IN (%s) AND (?=0 OR EXISTS (SELECT 1 FROM orders o JOIN "
                  "tasks t ON t.task_id=o.task_id WHERE o.order_id=capture_attempts.order_id AND t.state IN (%s)))"
                  % (",".join("?" * len(frm)), ",".join("?" * len(S.OPEN_FOR_CAPTURE))),
                  (to, 1 if sent else 0, error, debug_id, now, 1 if sent else 0, now, 1 if release_lease else 0,
                   order_id, self.owner, *frm, 1 if sent else 0, *S.OPEN_FOR_CAPTURE),
                  f"capture attempt {order_id} {frm}->{to}")

    def request_reapproval(self, order_id):
        """The buyer explicitly says they re-approved at PayPal after a decline. Only valid on a released, declined
        attempt; it permits ONE new generation (new PayPal-Request-Id). It never selects a funding source."""
        n = self.db.execute("UPDATE capture_attempts SET reapproval=1, updated=? WHERE order_id=? AND state='RELEASED' "
                            "AND last_error IN ('INSTRUMENT_DECLINED','PAYER_ACTION_REQUIRED')",
                            (time.time(), order_id)).rowcount
        return n == 1

    # ------------------------------------------------------------------ R1c: cancellation integrity
    def _order_task_state(self, order_id):
        r = self.db.execute("SELECT t.state FROM orders o JOIN tasks t ON t.task_id=o.task_id WHERE o.order_id=?",
                            (order_id,)).fetchone()
        return r[0] if r else None

    def cancel_blocker(self, order_id, now=None):
        """Why this order's task must NOT be cancelled, from the durable capture attempt (None = no local blocker).
        A capture reserved by a live worker, sent, of unknown outcome, or resolved blocks it. (The provider GET in
        AutoNomos.cancel() is the authority for captures made outside this attempt.)"""
        now = time.time() if now is None else now
        a = self.capture_attempt(order_id)
        if a:
            if a["state"] in ("SENT", "OUTCOME_UNKNOWN"):
                return f"a capture request may be in flight or its outcome is unknown (attempt {a['state']})"
            if a["state"] == "RESOLVED":
                return "a capture is recorded for this order (attempt RESOLVED)"
            if a["state"] == "RESERVED" and a["lease_until"] > now:
                return "a capture is being processed right now (attempt RESERVED)"
        return None

    def was_cancelled(self, tid):
        """True if this task was EVER cancelled (append-only event log)."""
        return self.db.execute("SELECT 1 FROM events WHERE task_id=? AND to_state=? LIMIT 1",
                               (tid, S.CANCELLED)).fetchone() is not None

    def resolve_attempt_on_closed_task(self, order_id, note):
        """Operator reconciliation of a CANCELLED task whose provider record shows a capture: close any sticky attempt
        regardless of lease owner (no worker may send for a closed task: reserve -> CLOSED, SENT mark guarded)."""
        self.db.execute("UPDATE capture_attempts SET state='RESOLVED', last_error=?, lease_until=0, updated=? "
                        "WHERE order_id=? AND state IN ('RESERVED','SENT','OUTCOME_UNKNOWN')",
                        (note, time.time(), order_id))
