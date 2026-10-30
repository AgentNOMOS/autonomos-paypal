"""PayPal SANDBOX adapter for Orders v2 and webhook verification (stdlib HTTPS only).

Endpoints used (OpenAPI schemas retrieved 2026-10-15, Orders v2 schema 2.36):
  POST /v1/oauth2/token                              client_credentials (Basic auth with client id + secret)
  POST /v2/checkout/orders                           create (PayPal-Request-Id for idempotency, Prefer: return=representation)
  GET  /v2/checkout/orders/{id}                      server-side truth for verification and reconciliation
  POST /v2/checkout/orders/{id}/capture              capture (PayPal-Request-Id)
  POST /v1/notifications/verify-webhook-signature    postback verification -> verification_status SUCCESS|FAILURE

The base URL is fixed to https://api-m.sandbox.paypal.com; live hosts are refused by Config.validate() and again here.
The client secret is only ever placed in the Authorization header of the token request; it is never logged.
STATUS: implemented, not yet executed against PayPal (no sandbox credentials were provided in R0/R1).
R1: timeouts and network errors raise ProviderError(status=0) = OUTCOME UNKNOWN; the service reconciles with GET
before any retry, and retries reuse the same PayPal-Request-Id.
R1b: MUTATIONS (create order, capture) are never re-sent by this adapter - not even after a 401. Reads (GET order,
token) may be refreshed/retried. The token is refreshed proactively before a mutation if it has < 5 min left.
A 2xx whose body is not a JSON object raises ProviderError(status, "INVALID_PROVIDER_RESPONSE"): for a capture
that means OUTCOME UNKNOWN, never success. PayPal-Debug-Id (body or header) and Retry-After are preserved.
"""
import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from ..config import LIVE_HOSTS, SANDBOX_API_BASE
from .mock import ProviderError


def _hdr(resp, name):
    try:
        return (getattr(resp, "headers", None) or {}).get(name)
    except Exception:
        return None


class PayPalSandbox:
    environment = "SANDBOX"
    name = "paypal_sandbox"

    def __init__(self, cfg, opener=None, timeout=30):
        if cfg.paypal_api_base != SANDBOX_API_BASE or any(h in cfg.paypal_api_base for h in LIVE_HOSTS):
            raise ProviderError(0, "LIVE_ENVIRONMENT_REFUSED", "only the sandbox API is permitted")
        self.base, self._id, self._secret = SANDBOX_API_BASE, cfg.paypal_client_id, cfg.paypal_client_secret
        self.webhook_id = cfg.paypal_webhook_id
        self._open = opener or urllib.request.urlopen     # tests inject a fake transport
        self.timeout, self._tok, self._exp, self.lock = timeout, None, 0, threading.Lock()
        self.calls = []
        self.last_debug_id = None

    # ------------------------------------------------------------------ transport
    def _http(self, method, path, body=None, headers=None, form=False, raw=None):
        url = self.base + path
        assert url.startswith(SANDBOX_API_BASE + "/")
        data = raw
        if body is not None:
            data = urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        self.calls.append((method, path, (headers or {}).get("PayPal-Request-Id")))
        try:
            with self._open(req, timeout=self.timeout) as r:
                raw = r.read()
                status = r.status
                self.last_debug_id = _hdr(r, "Paypal-Debug-Id")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                err = json.loads(raw)
                if not isinstance(err, dict):
                    raise ValueError
            except Exception:
                err = {"name": "HTTP_ERROR"}
            issue = ((err.get("details") or [{}])[0].get("issue") or err.get("name") or err.get("error") or "HTTP_ERROR")
            ra = _hdr(e, "Retry-After")
            raise ProviderError(e.code, issue, "", debug_id=err.get("debug_id") or _hdr(e, "Paypal-Debug-Id"),
                                retry_after=float(ra) if ra and ra.strip().isdigit() else None) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # no HTTP status: the request may or may not have been processed -> outcome UNKNOWN (status 0)
            raise ProviderError(0, "NETWORK_ERROR_OR_TIMEOUT", type(e).__name__) from None
        try:
            body = json.loads(raw) if raw else {}
            if not isinstance(body, dict):
                raise ValueError("not an object")
        except ValueError:
            raise ProviderError(status, "INVALID_PROVIDER_RESPONSE", "unparseable body",
                                debug_id=self.last_debug_id) from None
        return status, body

    def _token(self, min_ttl=60):
        with self.lock:
            if self._tok and time.time() < self._exp - min_ttl:
                return self._tok
            basic = base64.b64encode(f"{self._id}:{self._secret}".encode()).decode()
            st, d = self._http("POST", "/v1/oauth2/token", {"grant_type": "client_credentials"},
                               {"Authorization": f"Basic {basic}",
                                "Content-Type": "application/x-www-form-urlencoded"}, form=True)
            if not isinstance(d.get("access_token"), str):
                raise ProviderError(st, "INVALID_PROVIDER_RESPONSE", "token response without access_token")
            self._tok, self._exp = d["access_token"], time.time() + int(d.get("expires_in", 300))
            return self._tok

    def _auth_headers(self, request_id=None, min_ttl=60):
        h = {"Authorization": f"Bearer {self._token(min_ttl)}", "Content-Type": "application/json",
             "Prefer": "return=representation"}
        if request_id:
            h["PayPal-Request-Id"] = request_id
        return h

    def _call(self, method, path, body=None, request_id=None, raw=None, mutation=False):
        try:
            return self._http(method, path, body, self._auth_headers(request_id, 300 if mutation else 60), raw=raw)[1]
        except ProviderError as e:
            if e.status == 401:
                with self.lock:
                    self._tok = None                 # drop the token either way
                if mutation:                         # a payment mutation is NEVER re-sent here; the service reconciles
                    raise
                return self._http(method, path, body, self._auth_headers(request_id), raw=raw)[1]   # reads: once
            raise

    # ------------------------------------------------------------------ Orders v2
    def create_order(self, body, request_id):
        return self._call("POST", "/v2/checkout/orders", body, request_id, mutation=True)

    def get_order(self, order_id):
        return self._call("GET", "/v2/checkout/orders/" + urllib.parse.quote(order_id, safe=""))

    def capture_order(self, order_id, request_id):
        return self._call("POST", "/v2/checkout/orders/" + urllib.parse.quote(order_id, safe="") + "/capture", {},
                          request_id, mutation=True)

    # ------------------------------------------------------------------ webhooks
    def verify_webhook(self, headers, raw_body, webhook_id=None):
        """Postback verification. PayPal's docs require the event to be posted back exactly as received, so the
        raw body bytes are spliced into the request instead of being parsed and re-serialised."""
        h = {k.lower(): v for k, v in headers.items()}
        head = {"auth_algo": h.get("paypal-auth-algo"), "cert_url": h.get("paypal-cert-url"),
                "transmission_id": h.get("paypal-transmission-id"), "transmission_sig": h.get("paypal-transmission-sig"),
                "transmission_time": h.get("paypal-transmission-time"), "webhook_id": webhook_id or self.webhook_id}
        if not all(head.values()):
            return {"verification_status": "FAILURE", "reason": "missing PayPal transmission headers or webhook id"}
        json.loads(raw_body)                          # must be JSON; raises otherwise
        raw = json.dumps(head)[:-1].encode() + b',"webhook_event":' + raw_body.strip() + b"}"
        return self._call("POST", "/v1/notifications/verify-webhook-signature", raw=raw)
