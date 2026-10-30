"""MOCK payment provider: a local, deterministic imitation of the PayPal Orders v2 shapes this app uses.

Nothing here talks to PayPal. Every id is prefixed MOCK-, every response carries "_provider": "MOCK", and receipts
from a mock run say environment MOCK. It exists so the payment gates can be exercised without credentials; it is
never evidence of a PayPal integration.

R1: optional file-backed state (path=...) so several OS processes share one mock "provider" and tests can count how
many captures really happened. Test hooks simulate lost responses (timeout after capture), requests that never
arrived (timeout before capture), rate limiting (429) and INSTRUMENT_DECLINED. Like PayPal, a repeated
PayPal-Request-Id replays the first response - including an error.
"""
import contextlib
import copy
import hashlib
import hmac
import json
import sqlite3
import threading

MOCK_MERCHANT_ID = "MOCKMERCHANT01"
_MOCK_WEBHOOK_KEY = b"autonomos-mock-webhook-key (local test only)"
TRANSIENT_STATUSES = (0, 408, 429, 500, 502, 503, 504)  # 0 = timeout / network error: outcome unknown
# issues after which the BUYER must act again at PayPal (choose / re-confirm a funding source). Never auto-replaced.
BUYER_ACTION_ISSUES = ("INSTRUMENT_DECLINED", "PAYER_ACTION_REQUIRED")


class ProviderError(RuntimeError):
    def __init__(self, status, issue, detail="", debug_id=None, retry_after=None):
        super().__init__(f"{status} {issue} {detail}")
        self.status, self.issue, self.debug_id, self.retry_after = status, issue, debug_id, retry_after

    @property
    def transient(self):
        """Safe to retry a READ."""
        return self.status in TRANSIENT_STATUSES or self.issue == "INVALID_PROVIDER_RESPONSE"

    @property
    def definitive_refusal(self):
        """For a MUTATION: the provider answered with a business/validation refusal, so the request was not executed.
        Everything else (no response, 401 auth, 408/409, 429, 5xx, unparseable body) leaves the outcome UNKNOWN."""
        return 400 <= self.status < 500 and self.status not in (401, 408, 409, 429) \
            and self.issue != "INVALID_PROVIDER_RESPONSE"


class MockPayPal:
    environment = "MOCK"
    name = "mock"

    def __init__(self, base_url="http://127.0.0.1:8765", path=None):
        self.base_url = base_url.rstrip("/")
        self.lock = threading.RLock()
        self.path = path
        self._mem = {"orders": {}, "request_ids": {}, "replies": {}, "scenario": {}, "calls": [], "n": 0,
                     "captures_performed": 0, "token_valid": True, "hidden": {}, "get_scenario": {}}
        if path:
            db = sqlite3.connect(path, timeout=30)
            db.execute("PRAGMA busy_timeout = 30000")
            db.execute("CREATE TABLE IF NOT EXISTS mockstate (k INTEGER PRIMARY KEY CHECK (k = 1), v TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO mockstate VALUES (1, ?)", (json.dumps(self._mem),))
            db.commit()
            db.close()

    @contextlib.contextmanager
    def _state(self):
        with self.lock:
            if not self.path:
                yield self._mem
                return
            db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            db.execute("PRAGMA busy_timeout = 30000")
            db.execute("BEGIN IMMEDIATE")
            try:
                st = json.loads(db.execute("SELECT v FROM mockstate WHERE k=1").fetchone()[0])
                try:
                    yield st
                except ProviderError:
                    # a provider ERROR RESPONSE is still a provider event: persist call log / scenario / replies
                    db.execute("UPDATE mockstate SET v=? WHERE k=1", (json.dumps(st),))
                    db.execute("COMMIT")
                    raise
                db.execute("UPDATE mockstate SET v=? WHERE k=1", (json.dumps(st),))
                db.execute("COMMIT")
            except ProviderError:
                raise
            except BaseException:
                db.execute("ROLLBACK")
                raise
            finally:
                db.close()

    # convenience views used by tests (live dicts in memory mode, snapshots in file mode)
    def snapshot(self):
        with self._state() as st:
            return copy.deepcopy(st)

    @property
    def orders(self):
        return self._mem["orders"] if not self.path else self.snapshot()["orders"]

    @property
    def calls(self):
        return [tuple(c) for c in (self._mem["calls"] if not self.path else self.snapshot()["calls"])]

    @property
    def scenario(self):
        return self._mem["scenario"]

    @property
    def token_valid(self):
        return self._mem["token_valid"] if not self.path else self.snapshot()["token_valid"]

    @token_valid.setter
    def token_valid(self, v):
        with self._state() as st:
            st["token_valid"] = v

    def set_scenario(self, order_id, scenario):
        with self._state() as st:
            st["scenario"][order_id] = scenario

    def set_get_scenario(self, order_id, scenario):
        with self._state() as st:
            st.setdefault("get_scenario", {})[order_id] = scenario

    def mutate_order(self, order_id, fn):
        with self._state() as st:
            fn(st["orders"][order_id])

    @staticmethod
    def _auth(st):
        if not st["token_valid"]:
            raise ProviderError(401, "INVALID_TOKEN", "mock: access token expired or invalid", debug_id="mockdbg401")

    def create_order(self, body, request_id):
        with self._state() as st:
            self._auth(st)
            st["calls"].append(["create_order", request_id])
            if request_id in st["request_ids"]:                      # idempotent replay: same order back
                return copy.deepcopy(st["orders"][st["request_ids"][request_id]])
            st["n"] += 1
            oid = f"MOCK-ORDER-{st['n']:06d}"
            pu = copy.deepcopy(body["purchase_units"][0])
            pu["payee"] = pu.get("payee") or {"merchant_id": MOCK_MERCHANT_ID}
            o = {"id": oid, "intent": body["intent"], "status": "PAYER_ACTION_REQUIRED", "purchase_units": [pu],
                 "links": [{"rel": "payer-action", "href": f"{self.base_url}/mock-paypal/checkout/{oid}", "method": "GET"},
                           {"rel": "self", "href": f"mock://orders/{oid}", "method": "GET"}],
                 "_provider": "MOCK"}
            st["orders"][oid], st["request_ids"][request_id] = o, oid
            return copy.deepcopy(o)

    def buyer_action(self, order_id, action):
        """Simulates what the buyer does on the (mock) checkout page: approve or cancel."""
        with self._state() as st:
            o = st["orders"][order_id]
            if o["status"] in ("PAYER_ACTION_REQUIRED", "CREATED"):
                o["status"] = "APPROVED" if action == "approve" else o["status"]
                if action == "cancel":
                    o["_buyer_cancelled"] = True
            return o["status"]

    def get_order(self, order_id):
        with self._state() as st:
            self._auth(st)
            st["calls"].append(["get_order", order_id])
            gs = st.setdefault("get_scenario", {}).get(order_id, "")
            if gs.startswith("rate_limit:"):                         # reads throttled n times
                left = int(gs.split(":")[1])
                st["get_scenario"][order_id] = f"rate_limit:{left - 1}" if left > 1 else ""
                raise ProviderError(429, "RATE_LIMIT_REACHED", "mock get", debug_id="mockdbg429g", retry_after=0)
            if order_id not in st["orders"]:
                raise ProviderError(404, "RESOURCE_NOT_FOUND", order_id, debug_id="mockdbg404")
            o = copy.deepcopy(st["orders"][order_id])
            hid = st.setdefault("hidden", {})
            if hid.get(order_id, 0) > 0:                             # delayed processing / stale replica read:
                hid[order_id] -= 1                                   # the capture exists but is not visible yet
                o["status"] = "APPROVED"
                o["purchase_units"][0].pop("payments", None)
            return o

    def capture_order(self, order_id, request_id):
        with self._state() as st:
            self._auth(st)
            st["calls"].append(["capture_order", request_id])
            if request_id in st["replies"]:                          # idempotent replay of the FIRST response
                r = st["replies"][request_id]
                if r["error"]:
                    raise ProviderError(*r["error"])
                return copy.deepcopy(st["orders"][r["order_id"]])
            sc = st["scenario"].get(order_id, "")
            if sc.startswith("rate_limit:"):                         # 429 n times, nothing processed, nothing cached
                left = int(sc.split(":")[1])
                st["scenario"][order_id] = f"rate_limit:{left - 1}" if left > 1 else ""
                raise ProviderError(429, "RATE_LIMIT_REACHED", "mock", debug_id="mockdbg429")
            if sc == "timeout_never":                                 # in flight, never completes (sticky)
                raise ProviderError(0, "TIMEOUT", "mock: no response")
            if sc == "timeout_before":                                # request lost before reaching the provider
                st["scenario"][order_id] = ""
                raise ProviderError(0, "TIMEOUT", "mock: no response (request never processed)")
            o = st["orders"].get(order_id)
            err = None
            if o is None:
                err = (404, "RESOURCE_NOT_FOUND", order_id, "mockdbg404")
            elif o["status"] == "COMPLETED":
                err = (422, "ORDER_ALREADY_CAPTURED", "mock", "mockdbg422a")
            elif o["status"] != "APPROVED":
                err = (422, "ORDER_NOT_APPROVED", "mock: buyer has not approved", "mockdbg422b")
            elif sc == "declined_instrument":
                st["scenario"][order_id] = ""
                o["status"] = "PAYER_ACTION_REQUIRED"                 # buyer must choose another funding source
                err = (422, "INSTRUMENT_DECLINED", "mock", "mockdbg422c")
            if err:
                st["replies"][request_id] = {"error": list(err), "order_id": order_id}
                raise ProviderError(*err)
            pu = o["purchase_units"][0]
            cstatus = {"pending": "PENDING", "declined": "DECLINED"}.get(sc, "COMPLETED")
            cap = {"id": f"MOCK-CAPTURE-{order_id[-6:]}", "status": cstatus, "amount": copy.deepcopy(pu["amount"]),
                   "custom_id": pu.get("custom_id"), "invoice_id": pu.get("invoice_id"), "final_capture": True}
            if cstatus == "PENDING":
                cap["status_details"] = {"reason": "PENDING_REVIEW"}
            pu["payments"] = {"captures": [cap]}
            o["status"] = "COMPLETED" if cstatus in ("COMPLETED", "PENDING") else "APPROVED"
            st["captures_performed"] += 1
            st["replies"][request_id] = {"error": None, "order_id": order_id}
            if sc == "timeout_after":                                 # captured, but the response is lost
                st["scenario"][order_id] = ""
                raise ProviderError(0, "TIMEOUT", "mock: response lost after the capture was processed")
            if sc.startswith("delayed:"):                             # response lost AND invisible to the next n GETs
                st["scenario"][order_id] = ""
                st.setdefault("hidden", {})[order_id] = int(sc.split(":")[1])
                raise ProviderError(0, "TIMEOUT", "mock: capture still processing; not yet visible")
            if sc.startswith("success_hidden:"):                      # 2xx, but the next n GETs are stale
                st["scenario"][order_id] = ""
                st.setdefault("hidden", {})[order_id] = int(sc.split(":")[1])
            if sc == "server_error_after":                            # processed, but answered 500
                st["scenario"][order_id] = ""
                raise ProviderError(500, "INTERNAL_SERVER_ERROR", "mock", debug_id="mockdbg500")
            return copy.deepcopy(o)

    def captures_performed(self):
        return self.snapshot()["captures_performed"] if self.path else self._mem["captures_performed"]

    # ---- test hooks (never reachable from the HTTP app)
    def set_capture_status(self, order_id, status):
        def f(o):
            o["purchase_units"][0]["payments"]["captures"][0]["status"] = status
        self.mutate_order(order_id, f)

    # ---- webhooks: mock signature = HMAC over transmission id | time | webhook id | body sha256
    def sign_webhook(self, transmission_id, ts, raw_body, webhook_id="MOCK-WEBHOOK"):
        msg = f"{transmission_id}|{ts}|{webhook_id}|{hashlib.sha256(raw_body).hexdigest()}".encode()
        return hmac.new(_MOCK_WEBHOOK_KEY, msg, "sha256").hexdigest()

    def verify_webhook(self, headers, raw_body, webhook_id="MOCK-WEBHOOK"):
        h = {k.lower(): v for k, v in headers.items()}
        try:
            exp = self.sign_webhook(h["paypal-transmission-id"], h["paypal-transmission-time"], raw_body, webhook_id)
            ok = hmac.compare_digest(exp, h["paypal-transmission-sig"])
        except KeyError:
            ok = False
        return {"verification_status": "SUCCESS" if ok else "FAILURE", "_provider": "MOCK"}
