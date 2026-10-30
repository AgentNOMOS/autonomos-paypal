"""Durable ledger of model requests. Every request is RESERVED, then marked SENT (write-ahead) before the transport is
touched, then finished. Reservation is the ONLY way to obtain permission for a request, and it runs inside one
BEGIN IMMEDIATE transaction (one writer across threads AND processes), where it re-checks from the database:

  * the execution exists, is STARTED, and its task is EXECUTING          (no model call without an authorized execution)
  * the execution's order has a CAPTURE_VERIFIED capture record           (no model call before a verified payment)
  * the payment environment is allowed by the authorization
  * the authorization is not expired and not frozen
  * per-execution request count, total request count, distinct executions, per-request input/output limits,
    total tokens and total USD (in-flight and unknown requests are charged at their worst case)

Request states:
  RESERVED          permission granted, nothing sent yet
  SENT              written BEFORE the network call: from here on the request may have reached the provider
  COMPLETED         the provider answered with a parsed body (usage recorded, charged at reported usage)
  REJECTED          the provider refused before processing (4xx incl. 429): charged 0
  OUTCOME_UNKNOWN   no usable answer (timeout, 5xx/529, broken or unparseable response, crash): charged worst case,
                    NEVER retried
  RELEASED_UNSENT   reservation abandoned before the write-ahead mark (crash): charged 0

The ledger writes only r1d_* tables. It never touches tasks, orders, captures, capture attempts or receipts.
"""
import json
import time
import uuid
from decimal import Decimal

from .. import states as S
from ..canon import canonical_json

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS r1d_ai_calls (call_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL,
      task_id TEXT NOT NULL, seq INTEGER NOT NULL, authorization_id TEXT NOT NULL, execution_class TEXT NOT NULL,
      provider TEXT NOT NULL, requested_model TEXT NOT NULL,
      state TEXT NOT NULL CHECK (state IN ('RESERVED','SENT','COMPLETED','REJECTED','OUTCOME_UNKNOWN',
                                           'RELEASED_UNSENT')),
      est_input_tokens_max INTEGER NOT NULL, max_output_tokens INTEGER NOT NULL, worst_case_usd TEXT NOT NULL,
      input_tokens INTEGER, output_tokens INTEGER, cache_write_tokens INTEGER, cache_read_tokens INTEGER,
      charged_usd TEXT, reported_model TEXT, request_id TEXT, response_id TEXT, stop_reason TEXT,
      request_digest TEXT, response_digest TEXT, error TEXT, owner TEXT NOT NULL, lease_until REAL NOT NULL,
      created REAL NOT NULL, sent REAL, finished REAL, UNIQUE (execution_id, seq))""",
    """CREATE TABLE IF NOT EXISTS r1d_ai_records (execution_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
      outcome TEXT NOT NULL, record_digest TEXT NOT NULL, record_json TEXT NOT NULL, raw_output TEXT, created REAL)""",
    """CREATE TABLE IF NOT EXISTS r1d_authorizations (authorization_id TEXT PRIMARY KEY, frozen INTEGER NOT NULL
      DEFAULT 0, reason TEXT, first_seen REAL, envelope_json TEXT)""",
)
COLS = ("call_id", "execution_id", "task_id", "seq", "authorization_id", "execution_class", "provider",
        "requested_model", "state", "est_input_tokens_max", "max_output_tokens", "worst_case_usd", "input_tokens",
        "output_tokens", "cache_write_tokens", "cache_read_tokens", "charged_usd", "reported_model", "request_id",
        "response_id", "stop_reason", "request_digest", "response_digest", "error", "owner", "lease_until",
        "created", "sent", "finished")
RESERVE_STALE_S = 60.0


class LedgerRefusal(RuntimeError):
    """No permission for a model request. Nothing was sent."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


class LedgerConflict(RuntimeError):
    """A ledger row changed under us (e.g. recovery marked it). Do not proceed."""


class Ledger:
    def __init__(self, store):
        self.store = store
        for stmt in SCHEMA:
            store.db.execute(stmt)

    # ------------------------------------------------------------------ reservation
    def reserve(self, *, execution_id, task_id, seq, env, est_input_tokens, payment_environment, now=None):
        now = time.time() if now is None else now
        worst = env.worst_case_usd(est_input_tokens, env.max_output_tokens_per_request)
        db = self.store.db
        with self.store.txn():
            r = db.execute("SELECT e.status, e.order_id, t.state FROM executions e JOIN tasks t ON t.task_id=e.task_id "
                           "WHERE e.execution_id=? AND e.task_id=?", (execution_id, task_id)).fetchone()
            if not r or r[0] != "STARTED" or r[2] != S.EXECUTING:
                raise LedgerRefusal("UNAUTHORIZED_CALL", "no STARTED execution for an EXECUTING task: no model request "
                                    f"is permitted (execution={r and r[0]}, task={r and r[2]})")
            cap = db.execute("SELECT verification_json FROM captures WHERE order_id=?", (r[1],)).fetchone()
            if not cap or json.loads(cap[0]).get("verdict") != "CAPTURE_VERIFIED":
                raise LedgerRefusal("PAYMENT_NOT_VERIFIED", "the execution's order has no CAPTURE_VERIFIED record")
            if payment_environment not in env.allowed_payment_environments:
                raise LedgerRefusal("PAYMENT_ENVIRONMENT_NOT_AUTHORIZED",
                                    f"{payment_environment} is not in {list(env.allowed_payment_environments)}")
            if env.not_after is not None and now > env.not_after:
                raise LedgerRefusal("AUTHORIZATION_EXPIRED", "the model-execution authorization has expired")
            a = db.execute("SELECT frozen, reason FROM r1d_authorizations WHERE authorization_id=?",
                           (env.authorization_id,)).fetchone()
            if a is None:
                db.execute("INSERT INTO r1d_authorizations VALUES (?,?,?,?,?)",
                           (env.authorization_id, 0, None, now, canonical_json(env.public_view())))
            elif a[0]:
                raise LedgerRefusal("AUTHORIZATION_FROZEN", f"authorization frozen: {a[1]}")
            if worst is None:
                raise LedgerRefusal("PRICE_UNKNOWN", "cost estimation is UNKNOWN; an owner-confirmed price is required")
            if est_input_tokens > env.max_input_tokens_per_request:
                raise LedgerRefusal("INPUT_TOKEN_LIMIT", f"input upper bound {est_input_tokens} > per-request limit "
                                    f"{env.max_input_tokens_per_request}")
            n_exec = db.execute("SELECT COUNT(*) FROM r1d_ai_calls WHERE execution_id=? AND state!='RELEASED_UNSENT'",
                                (execution_id,)).fetchone()[0]
            if n_exec >= env.max_requests_per_execution:
                raise LedgerRefusal("REQUEST_LIMIT_EXECUTION", f"{n_exec} request(s) already made for this execution "
                                    f"(limit {env.max_requests_per_execution})")
            t = self._totals(env.authorization_id)
            if t["requests"] + 1 > env.max_requests_total:
                raise LedgerRefusal("REQUEST_LIMIT_TOTAL", f"{t['requests']} request(s) already made under "
                                    f"{env.authorization_id} (limit {env.max_requests_total})")
            if execution_id not in t["execution_ids"] and len(t["execution_ids"]) + 1 > env.max_executions:
                raise LedgerRefusal("EXECUTION_LIMIT", f"authorization covers at most {env.max_executions} executions")
            if t["tokens"] + est_input_tokens + env.max_output_tokens_per_request > env.max_total_tokens:
                raise LedgerRefusal("TOKEN_BUDGET", f"tokens used/at risk {t['tokens']} + this request's bound "
                                    f"{est_input_tokens + env.max_output_tokens_per_request} > {env.max_total_tokens}")
            if env.max_usd_total is not None and t["usd"] + worst > env.max_usd_total:
                raise LedgerRefusal("USD_BUDGET", f"USD used/at risk {t['usd']} + this request's worst case {worst} > "
                                    f"{env.max_usd_total}")
            cid = "AICALL-" + uuid.uuid4().hex[:12].upper()
            db.execute("INSERT INTO r1d_ai_calls (call_id, execution_id, task_id, seq, authorization_id, "
                       "execution_class, provider, requested_model, state, est_input_tokens_max, max_output_tokens, "
                       "worst_case_usd, owner, lease_until, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (cid, execution_id, task_id, seq, env.authorization_id, env.execution_class, env.provider,
                        env.model, "RESERVED", est_input_tokens, env.max_output_tokens_per_request, str(worst),
                        self.store.owner, now + RESERVE_STALE_S, now))
        return cid

    def capacity(self, env, payment_environment, est_input_tokens, now=None):
        """Read-only: could one more execution's first request be reserved right now? (ok, reason). Used BEFORE an
        order is created, so a buyer is not charged for work the envelope can no longer pay for. Not a reservation:
        concurrent buyers can still race; the reservation inside the execution stays authoritative."""
        now = time.time() if now is None else now
        if payment_environment not in env.allowed_payment_environments:
            return False, "PAYMENT_ENVIRONMENT_NOT_AUTHORIZED"
        if env.not_after is not None and now > env.not_after:
            return False, "AUTHORIZATION_EXPIRED"
        if self.authorization_state(env.authorization_id)["frozen"]:
            return False, "AUTHORIZATION_FROZEN"
        worst = env.worst_case_usd(est_input_tokens, env.max_output_tokens_per_request)
        if worst is None:
            return False, "PRICE_UNKNOWN"
        if est_input_tokens > env.max_input_tokens_per_request:
            return False, "INPUT_TOKEN_LIMIT"
        t = self._totals(env.authorization_id)
        if t["requests"] + 1 > env.max_requests_total:
            return False, "REQUEST_LIMIT_TOTAL"
        if len(t["execution_ids"]) + 1 > env.max_executions:
            return False, "EXECUTION_LIMIT"
        if t["tokens"] + est_input_tokens + env.max_output_tokens_per_request > env.max_total_tokens:
            return False, "TOKEN_BUDGET"
        if env.max_usd_total is not None and t["usd"] + worst > env.max_usd_total:
            return False, "USD_BUDGET"
        return True, None

    def mark_sent(self, call_id, request_digest, lease_s, now=None):
        """Write-ahead: after this commits, the request is treated as possibly delivered. The execution must still be
        STARTED, in the SAME statement (a recovery that abandoned it first makes this change 0 rows)."""
        now = time.time() if now is None else now
        n = self.store.db.execute(
            "UPDATE r1d_ai_calls SET state='SENT', sent=?, request_digest=?, lease_until=? WHERE call_id=? AND "
            "state='RESERVED' AND owner=? AND EXISTS (SELECT 1 FROM executions e WHERE "
            "e.execution_id=r1d_ai_calls.execution_id AND e.status='STARTED')",
            (now, request_digest, now + lease_s, call_id, self.store.owner)).rowcount
        if n != 1:
            raise LedgerConflict(f"{call_id}: could not mark SENT (reservation lost or execution closed)")

    def release_unsent(self, call_id):
        self.store.db.execute("UPDATE r1d_ai_calls SET state='RELEASED_UNSENT', finished=? WHERE call_id=? AND "
                              "state='RESERVED'", (time.time(), call_id))

    def finish(self, call_id, state, env, *, usage=None, reported_model=None, request_id=None, response_id=None,
               stop_reason=None, response_digest=None, error=None):
        """SENT -> COMPLETED | REJECTED | OUTCOME_UNKNOWN. Returns (charged_usd, anomalies)."""
        assert state in ("COMPLETED", "REJECTED", "OUTCOME_UNKNOWN")
        row = self.call(call_id)
        u = usage or {}
        if state == "COMPLETED":
            charged = env.cost_of(u)
        elif state == "REJECTED":
            charged = Decimal(0)
        else:
            charged = Decimal(row["worst_case_usd"])
        n = self.store.db.execute(
            "UPDATE r1d_ai_calls SET state=?, input_tokens=?, output_tokens=?, cache_write_tokens=?, "
            "cache_read_tokens=?, charged_usd=?, reported_model=?, request_id=?, response_id=?, stop_reason=?, "
            "response_digest=?, error=?, finished=? WHERE call_id=? AND state='SENT' AND owner=?",
            (state, u.get("input_tokens"), u.get("output_tokens"), u.get("cache_creation_input_tokens"),
             u.get("cache_read_input_tokens"), str(charged), reported_model, request_id, response_id, stop_reason,
             response_digest, (error or "")[:300] or None, time.time(), call_id, self.store.owner)).rowcount
        if n != 1:
            raise LedgerConflict(f"{call_id}: not SENT any more (recovered as unknown?); result discarded")
        anomalies = []
        if state == "COMPLETED":
            if int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0) + \
                    int(u.get("cache_read_input_tokens") or 0) > row["est_input_tokens_max"]:
                anomalies.append("INPUT_ESTIMATE_EXCEEDED")
            if int(u.get("output_tokens") or 0) > row["max_output_tokens"]:
                anomalies.append("OUTPUT_LIMIT_EXCEEDED")
            if charged is not None and charged > Decimal(row["worst_case_usd"]):
                anomalies.append("COST_ABOVE_WORST_CASE")
        if anomalies:
            self.freeze(row["authorization_id"], "post-hoc envelope check failed on " + call_id + ": " +
                        ",".join(anomalies))
        return charged, anomalies

    def freeze(self, authorization_id, reason):
        self.store.db.execute("INSERT INTO r1d_authorizations (authorization_id, frozen, reason, first_seen) "
                              "VALUES (?,1,?,?) ON CONFLICT(authorization_id) DO UPDATE SET frozen=1, "
                              "reason=excluded.reason", (authorization_id, reason, time.time()))

    # ------------------------------------------------------------------ recovery (never re-sends anything)
    def recover(self, now=None):
        now = time.time() if now is None else now
        out = []
        db = self.store.db
        with self.store.txn():
            for (cid,) in db.execute("SELECT call_id FROM r1d_ai_calls WHERE state='RESERVED' AND lease_until < ?",
                                     (now,)).fetchall():
                db.execute("UPDATE r1d_ai_calls SET state='RELEASED_UNSENT', finished=? WHERE call_id=? AND "
                           "state='RESERVED'", (now, cid))
                out.append((cid, "RELEASED_UNSENT"))
            for (cid, worst) in db.execute("SELECT call_id, worst_case_usd FROM r1d_ai_calls WHERE state='SENT' AND "
                                           "lease_until < ?", (now,)).fetchall():
                db.execute("UPDATE r1d_ai_calls SET state='OUTCOME_UNKNOWN', charged_usd=?, error=?, finished=? "
                           "WHERE call_id=? AND state='SENT'",
                           (worst, "interrupted: process ended or lease expired while the request may have been in "
                                   "flight; charged at worst case; never retried", now, cid))
                out.append((cid, "OUTCOME_UNKNOWN"))
        return out

    # ------------------------------------------------------------------ records and views
    def put_record(self, execution_id, task_id, outcome, record, raw_output=None):
        from ..canon import digest
        d = digest(record)
        self.store.db.execute("INSERT INTO r1d_ai_records VALUES (?,?,?,?,?,?,?)",
                              (execution_id, task_id, outcome, d, canonical_json(record), raw_output, time.time()))
        return d

    def record(self, execution_id):
        r = self.store.db.execute("SELECT outcome, record_digest, record_json, raw_output FROM r1d_ai_records WHERE "
                                  "execution_id=?", (execution_id,)).fetchone()
        return None if not r else {"outcome": r[0], "record_digest": r[1], "record": json.loads(r[2]),
                                   "raw_output": r[3]}

    def call(self, call_id):
        r = self.store.db.execute("SELECT " + ", ".join(COLS) + " FROM r1d_ai_calls WHERE call_id=?",
                                  (call_id,)).fetchone()
        return None if not r else dict(zip(COLS, r))

    def calls(self, execution_id=None, task_id=None):
        q, a = ("execution_id=?", execution_id) if execution_id else ("task_id=?", task_id)
        rows = self.store.db.execute("SELECT " + ", ".join(COLS) + f" FROM r1d_ai_calls WHERE {q} ORDER BY seq, created",
                                     (a,)).fetchall()
        return [dict(zip(COLS, r)) for r in rows]

    def authorization_state(self, authorization_id):
        r = self.store.db.execute("SELECT frozen, reason FROM r1d_authorizations WHERE authorization_id=?",
                                  (authorization_id,)).fetchone()
        return {"frozen": bool(r and r[0]), "reason": r and r[1]}

    def totals(self, authorization_id):
        t = self._totals(authorization_id)
        return dict(t, usd=str(t["usd"]), execution_ids=sorted(t["execution_ids"]))

    def _totals(self, authorization_id):
        rows = self.store.db.execute(
            "SELECT execution_id, state, est_input_tokens_max, max_output_tokens, worst_case_usd, input_tokens, "
            "output_tokens, cache_write_tokens, cache_read_tokens, charged_usd FROM r1d_ai_calls "
            "WHERE authorization_id=? AND state!='RELEASED_UNSENT'", (authorization_id,)).fetchall()
        req, tok, usd, ex = 0, 0, Decimal(0), set()
        for (eid, st, est_in, max_out, worst, i, o, cw, cr, charged) in rows:
            req += 1
            ex.add(eid)
            if st == "COMPLETED":
                tok += (i or 0) + (o or 0) + (cw or 0) + (cr or 0)
                usd += Decimal(charged or "0")
            elif st == "REJECTED":
                pass
            else:                                   # RESERVED / SENT (in flight) / OUTCOME_UNKNOWN: worst case
                tok += est_in + max_out
                usd += Decimal(worst)
        return {"requests": req, "tokens": tok, "usd": usd, "execution_ids": ex}
