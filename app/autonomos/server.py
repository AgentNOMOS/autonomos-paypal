"""Minimal local web UI (stdlib). Binds to 127.0.0.1 only. Six screens: request, offer review, checkout, status,
report, receipt. Secrets never reach the browser; the page only shows Config.public_view().

R1b hardening: every state-changing browser POST needs (1) a same-origin Origin/Referer, (2) a CSRF token bound to
the HttpOnly SameSite=Lax session cookie, and (3) for task actions, the session that created the task. Errors are
generic (no stack traces, no provider bodies); logs pass through redact(). The webhook endpoint is exempt from CSRF
(it is authenticated by signature verification instead) and is reachable only on loopback - no public listener."""
import hashlib
import hmac
import html
import json
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import states as S
from .canon import canonical_json
from .payments import ProviderError
from .r1d.summary import PAYMENT, execution_summary, runtime_view
from .service import GateError

_SID = re.compile(r"^[0-9a-f]{32}$")


def redact(text, cfg=None):
    """Mask secrets and bearer/basic credentials in anything that may reach a log."""
    t = str(text)
    for v in ((cfg.paypal_client_secret, cfg.anthropic_api_key) if cfg else ()):
        if v:
            t = t.replace(v, "[REDACTED]")
    return re.sub(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+", r"\1 [REDACTED]", t)

CSS = """body{font:15px/1.5 system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1b1f24}
main{max-width:860px;margin:0 auto;padding:16px}header{background:#0b2a4a;color:#fff;padding:10px 16px}
.banner{background:#fff4c2;border:1px solid #e5c100;padding:6px 10px;border-radius:6px;font-size:13px}
.card{background:#fff;border:1px solid #dde1e6;border-radius:10px;padding:14px 16px;margin:12px 0}
.state{display:inline-block;padding:2px 8px;border-radius:12px;background:#e8eefc;font-weight:600}
.ok{color:#0a7a2f}.bad{color:#b00020}button{font-size:15px;padding:8px 14px;border-radius:8px;border:0;
background:#0070ba;color:#fff;cursor:pointer}button.secondary{background:#6b7280}textarea,select{width:100%;font:inherit}
code,pre{font-size:12px;background:#f1f3f5;padding:2px 4px;border-radius:4px;overflow-x:auto}pre{padding:8px;white-space:pre-wrap}
blockquote{margin:4px 0 4px 12px;border-left:3px solid #9bb7d4;padding-left:8px;color:#333}
ol.tl{padding-left:18px}ol.tl li{margin:2px 0}.warn{color:#8a5a00}.neutral{color:#37474f}
.mode{padding:8px 12px;border-radius:8px;margin:8px 0;font-weight:600}.mode-DEMO_REPLAY{background:#efe6ff;border:2px solid #7c4dff}
.mode-SUPERVISED_LIVE{background:#ffe3e0;border:2px solid #c62828}.mode-DEVELOPMENT{background:#eceff1;border:2px solid #607d8b}
table.status th{text-align:left;padding:3px 12px 3px 0;vertical-align:top;white-space:nowrap}table.status td{padding:3px 0}"""
STEPS = [S.TASK_REQUESTED, S.OFFER_ISSUED, S.BUYER_APPROVAL_REQUIRED, S.ORDER_CREATED, S.PAYMENT_PENDING,
         S.PAYMENT_CAPTURED, S.EXECUTION_AUTHORIZED, S.EXECUTING, S.DELIVERY_VERIFIED, S.DELIVERED]


def esc(x):
    return html.escape(str(x), quote=True)


def page(title, body, refresh=False, env="MOCK", mode=None):
    r = '<meta http-equiv="refresh" content="2">' if refresh else ""
    mb = (f"<p class='mode mode-{esc(mode['mode'])}'>MODE {esc(mode['mode'])} &middot; {esc(mode['banner'])}</p>"
          if mode else "")
    note = ("MOCK PAYMENT PROVIDER: no PayPal call is made; nothing here is a PayPal transaction." if env == "MOCK" else
            "PayPal SANDBOX: test money only. Not a live payment.")
    return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,"
            f"initial-scale=1'>{r}<title>{esc(title)}</title><style>{CSS}</style></head><body><header><b>AutoNOMOS</b>"
            f" &middot; Verified Agent Commerce</header><main>{mb}<p class='banner'>{esc(note)}</p>{body}</main></body></html>")


def make_handler(svc):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
            b = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; "
                             "form-action 'self' https://www.sandbox.paypal.com; frame-ancestors 'none'")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Cache-Control", "no-store")
            if getattr(self, "_new_sid", None):
                self.send_header("Set-Cookie", f"autonomos_sid={self._new_sid}; Path=/; HttpOnly; SameSite=Lax")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)

        def _redirect(self, to):
            self._send(303, "", headers={"Location": to})

        def _form(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            return raw, dict(urllib.parse.parse_qsl(raw.decode("utf-8", "replace")))

        # -------------------------------------------------------------- session / CSRF / origin
        def _sid(self):
            if getattr(self, "_cached_sid", None):
                return self._cached_sid
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "autonomos_sid" and _SID.match(v):
                    self._cached_sid = v
                    return v
            self._cached_sid = self._new_sid = secrets.token_hex(16)
            return self._cached_sid

        def _csrf(self):
            return hmac.new(_SECRET, self._sid().encode(), hashlib.sha256).hexdigest()[:40]

        def _csrf_field(self):
            return f"<input type='hidden' name='csrf' value='{esc(self._csrf())}'>"

        def _buyer_ref(self):
            return "sess-" + hashlib.sha256(_SECRET + self._sid().encode()).hexdigest()[:16]

        def _origin_ok(self):
            port = self.server.server_address[1]
            allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
            pb = urllib.parse.urlparse(svc.cfg.public_base_url)
            allowed.add(f"{pb.scheme}://{pb.netloc}")
            o = self.headers.get("Origin")
            if o:
                return o in allowed
            ref = self.headers.get("Referer") or ""
            return any(ref == a or ref.startswith(a + "/") for a in allowed)

        def _check_post(self, f, tid=None):
            if not self._origin_ok():
                raise _Forbidden("cross-origin request refused")
            had_cookie = not getattr(self, "_new_sid", None) and self._sid()
            if not had_cookie or not hmac.compare_digest(f.get("csrf", ""), self._csrf()):
                raise _Forbidden("missing or invalid CSRF token; reload the page")
            if tid is not None:
                t = svc.store.task(tid)
                if t is None or t["request"].get("buyer_ref") != self._buyer_ref():
                    raise _Forbidden("this task belongs to another browser session")

        def _start_execution(self, tid):
            if svc.store.task(tid)["state"] == S.EXECUTION_AUTHORIZED:
                threading.Thread(target=lambda: _safe(svc.execute, tid), daemon=True).start()

        def _page(self, title, body, refresh=False, env=None):
            return page(title, body, refresh, svc.payments.environment, getattr(svc, "demo_mode", None))

        def _verify_receipt(self, tid):
            """R1e.1: the offline receipt verifier run on the stored receipt, artifact, events and the pinned
            collection, with this server's own public key as the trusted issuer key."""
            from .r1d.receipt2 import verify_any
            rc = svc.store.receipt(tid)
            if not rc:
                return None
            art = svc.store.artifact(rc["body"]["artifact"]["digest"])
            ds = svc.docsets.get(rc["body"]["offer"]["document_set"]["id"]) if hasattr(svc, "docsets") else None
            return verify_any(rc, art, svc.store.events(tid), docset=ds, trusted_public_key_hex=svc.signer.public_hex)

        # -------------------------------------------------------------- GET
        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            parts = [p for p in u.path.split("/") if p]
            env = svc.payments.environment
            try:
                if not parts:
                    return self._send(200, self._page("AutoNOMOS", self.home(), env=env))
                if parts[0] == "tasks" and len(parts) == 2:
                    v = svc.view(parts[1])
                    if not v:
                        return self._send(404, self._page("not found", "<p>Unknown task.</p>", env=env))
                    busy = v["task"]["state"] in (S.EXECUTION_AUTHORIZED, S.EXECUTING, S.PAYMENT_CAPTURED,
                                                  S.DELIVERY_VERIFIED)          # R1e: Run 2 display gap closed
                    return self._send(200, self._page("Task", self.task_page(v), refresh=busy, env=env))
                if parts[0] == "receipts" and len(parts) == 3 and parts[2] == "verify":
                    return self._send(200, json.dumps(self._verify_receipt(parts[1]), indent=1), "application/json")
                if parts[0] == "receipts" and len(parts) == 2:
                    rc = svc.store.receipt(parts[1].removesuffix(".json"))
                    return self._send(200 if rc else 404, json.dumps(rc, indent=1), "application/json")
                if parts[0] == "artifacts" and len(parts) == 3:
                    a = svc.deliver_artifact(parts[1], parts[2].removesuffix(".json"))
                    return self._send(200, canonical_json(a), "application/json")
                if parts[0] == "events" and len(parts) == 2:
                    return self._send(200, json.dumps(svc.store.events(parts[1]), indent=1), "application/json")
                if parts[0] == "api" and parts[1:2] == ["tasks"] and len(parts) == 3:
                    return self._send(200, json.dumps(svc.view(parts[2]), indent=1, default=str), "application/json")
                if parts[0] == "paypal" and parts[1:2] == ["return"]:
                    st = svc.on_buyer_return(q.get("token", ""))
                    tid = svc.store.task_for_order(q.get("token", ""))
                    self._start_execution(tid)
                    return self._redirect(f"/tasks/{tid}")
                if parts[0] == "paypal" and parts[1:2] == ["cancel"]:
                    svc.on_buyer_cancel(q.get("token", ""))
                    return self._redirect(f"/tasks/{svc.store.task_for_order(q.get('token', ''))}")
                if parts[0] == "mock-paypal" and parts[1:2] == ["checkout"] and env == "MOCK":
                    return self._send(200, self._page("Mock checkout", self.mock_checkout(parts[2]), env=env))
                return self._send(404, self._page("not found", "<p>Not found.</p>", env=env))
            except GateError as e:
                return self._send(409, self._page("Refused", f"<div class='card bad'>{esc(e)}</div>", env=env))
            except ProviderError as e:
                return self._provider_error(e, env)
            except Exception as e:
                return self._internal_error(e, env)

        def _provider_error(self, e, env):
            print("provider error:", redact(f"{e.status} {e.issue} debug_id={e.debug_id}", svc.cfg))
            return self._send(503, self._page("Payment provider unavailable", "<div class='card bad'>The payment provider "
                              "could not be reached. No payment state was changed and nothing will be charged twice. "
                              f"Try again later. Provider reference: <code>{esc(e.debug_id or 'none')}</code></div>",
                              env=env))

        def _internal_error(self, e, env):
            ref = secrets.token_hex(4)
            print(f"internal error {ref}:", type(e).__name__, redact(str(e)[:200], svc.cfg))
            return self._send(500, self._page("Error", f"<div class='card bad'>Internal error (reference {ref}). No payment "
                              "state was changed by this error.</div>", env=env))

        # -------------------------------------------------------------- POST
        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            raw, f = self._form()
            env = svc.payments.environment
            try:
                if parts == ["webhooks", "paypal"]:
                    res = svc.handle_webhook(dict(self.headers.items()), raw, reconcile=False)   # record, then ack
                    if res.get("queued"):
                        tid, tx = res["task_id"], res["transmission_id"]

                        def later():
                            svc.process_webhook(tx, tid)
                            self._start_execution(tid)
                        threading.Thread(target=lambda: _safe(later), daemon=True).start()
                    body = {k: v for k, v in res.items() if k in ("accepted", "reason", "queued", "duplicate")}
                    return self._send(res.get("http", 200), json.dumps(body), "application/json")
                if parts == ["tasks"]:
                    self._check_post(f)
                    dm = getattr(svc, "demo_mode", None) or {}
                    if dm.get("mode") == "DEMO_REPLAY" and (f.get("topic") != dm["question"] or
                                                            f.get("corpus") != dm["document_set"]):
                        raise GateError("DEMO_REPLAY can only answer the recorded question about the recorded document "
                                        "set (it replays one historical model answer). No task or order was created.")
                    tid = svc.request_task({"capability": f.get("capability"), "topic": f.get("topic"),
                                            "corpus": f.get("corpus"), "buyer_ref": self._buyer_ref()})
                    return self._redirect(f"/tasks/{tid}")
                if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "accept":
                    self._check_post(f, parts[1])
                    return self._redirect(svc.accept_offer(parts[1], f.get("offer_digest", "")))
                if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "cancel":
                    self._check_post(f, parts[1])
                    svc.cancel(parts[1], "buyer cancelled explicitly on the AutoNOMOS task page")
                    return self._redirect(f"/tasks/{parts[1]}")
                if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "reapproved":
                    self._check_post(f, parts[1])
                    order = svc.store.order_for_task(parts[1])
                    if not order:
                        raise GateError("no order for this task")
                    svc.on_buyer_reapproved(order["order_id"])
                    self._start_execution(parts[1])
                    return self._redirect(f"/tasks/{parts[1]}")
                if len(parts) == 3 and parts[0] == "mock-paypal" and env == "MOCK":
                    self._check_post(f)
                    oid = parts[1]
                    svc.payments.buyer_action(oid, "approve" if parts[2] == "approve" else "cancel")
                    if parts[2] == "approve":
                        return self._redirect(f"/paypal/return?token={urllib.parse.quote(oid)}")
                    return self._redirect(f"/paypal/cancel?token={urllib.parse.quote(oid)}")
                return self._send(404, "not found", "text/plain")
            except _Forbidden as e:
                return self._send(403, self._page("Refused", f"<div class='card bad'>{esc(e)}</div>", env=env))
            except GateError as e:
                return self._send(409, self._page("Refused", f"<div class='card bad'>{esc(e)}</div>", env=env))
            except ProviderError as e:
                return self._provider_error(e, env)
            except Exception as e:
                return self._internal_error(e, env)

        # -------------------------------------------------------------- views
        def home(self):
            sets = "".join(f"<option value='{esc(k)}'>{esc(v.sources.get('title', k))} ({len(v.passages)} passages)"
                           f"</option>" for k, v in svc.docsets.items())
            cfg = {k: v for k, v in svc.cfg.public_view().items() if k not in ("ai_provider", "anthropic_api_key_set")}
            if getattr(svc, "runtime", None) is not None:  # R1e: the R1d runtime, not the legacy R1b AI selector
                cfg["ai"] = runtime_view(svc.runtime)
            else:
                cfg["ai_provider"] = svc.cfg.ai_provider
            dm = getattr(svc, "demo_mode", None) or {}
            replay = dm.get("mode") == "DEMO_REPLAY"
            q = dm["question"] if replay else ("What hardware evidence exists for the quantum-kernel experiment, and "
                                                "what is still unverified?")
            if replay:
                sets = "".join(f"<option value='{esc(k)}'>{esc(v.sources.get('title', k))}</option>"
                               for k, v in svc.docsets.items() if k == dm["document_set"])
            note = ("<p class='warn'>DEMO_REPLAY: the question is fixed, because the answer is a REPLAY of one recorded "
                    "Claude Opus 5.5 answer to exactly this question. Payment is MOCK.</p>" if replay else "")
            return (f"<div class='card'><h2>Order a Verified Research Snapshot</h2>{note}<p>Ask a question about an approved "
                    f"document set. You get a fixed-price offer first; work starts only after your PayPal payment is "
                    f"captured and verified; every finding in the report quotes the documents verbatim, and a signed "
                    f"receipt ties payment, work and delivery together.</p><form method='post' action='/tasks'>{self._csrf_field()}"
                    f"<input type='hidden' name='capability' value='research_snapshot'><label>Your question</label>"
                    f"<textarea name='topic' rows='3' required{' readonly' if replay else ''}>{esc(q)}</textarea><label>Document set</label><select name='corpus'>"
                    f"{sets}</select><p><button>Get a price</button></p></form></div><div class='card'><b>Configuration"
                    f"</b> <code>{esc(json.dumps(cfg))}</code></div>")

        def task_page(self, v):
            t, o = v["task"], v["offer"]
            st = t["state"]
            out = [f"<div class='card'><h2>Task {esc(t['task_id'])}</h2><p>Status: <span class='state'>{esc(st)}</span>"
                   f"{' &mdash; ' + esc(t['reason']) if t['reason'] else ''}</p>"
                   + self.progress(st) + "</div>"] + ([self.status_panel(v)] if getattr(svc, "runtime", None) else [])
            if o:
                out.append(f"<div class='card'><h3>Offer {esc(o['offer_id'])}</h3><p><b>{esc(o['title'])}</b>: "
                           f"{esc(o['question'])}</p><p>Price: <b>{esc(o['price']['value'])} {esc(o['price']['currency_code'])}"
                           f"</b> &middot; payee <code>{esc(o['payee']['merchant_id'])}</code> &middot; documents "
                           f"<code>{esc(o['document_set']['id'])}</code></p><p>{esc(o['deliverable'])}</p><ul>"
                           + "".join(f"<li>{esc(x)}</li>" for x in o["terms"]) +
                           f"</ul><p>Offer digest <code>{esc(o['_digest'])}</code></p>")
                if st == S.BUYER_APPROVAL_REQUIRED:
                    out.append(f"<form method='post' action='/tasks/{esc(t['task_id'])}/accept'>{self._csrf_field()}<input type='hidden' "
                               f"name='offer_digest' value='{esc(o['_digest'])}'><button>Approve offer and pay with "
                               f"PayPal ({'mock' if v['environment'] == 'MOCK' else 'sandbox'})</button></form>"
                               f"<form method='post' action='/tasks/{esc(t['task_id'])}/cancel'>{self._csrf_field()}<p><button class='secondary'>"
                               f"Cancel</button></p></form>")
                out.append("</div>")
            att = svc.store.capture_attempt(v["order"]["order_id"]) if v["order"] else None
            declined = att and att["state"] == "RELEASED" and att["last_error"] in ("INSTRUMENT_DECLINED",
                                                                                    "PAYER_ACTION_REQUIRED")
            if st == S.PAYMENT_PENDING and not declined and not svc.store.cancel_blocker(v["order"]["order_id"]):
                # R1c: leaving PayPal is navigation, not a decision. Offer both explicit choices.
                left = any(e["evidence_kind"] == "checkout_navigation" for e in v["events"])
                link = svc._approve_link(svc.store.order_for_task(t["task_id"])["created"])
                out.append(f"<div class='card'><h3>Payment not confirmed</h3><p>"
                           + ("You came back from PayPal without completing the payment. " if left else "")
                           + f"Leaving PayPal does not cancel anything and nothing is charged until PayPal confirms "
                           f"a capture. <a href='{esc(link)}'>Continue at PayPal</a></p><form method='post' action="
                           f"'/tasks/{esc(t['task_id'])}/cancel'>{self._csrf_field()}<button class='secondary'>Cancel "
                           f"this order</button></form></div>")
            if att and st == S.PAYMENT_PENDING and att["state"] == "RELEASED" and \
                    att["last_error"] in ("INSTRUMENT_DECLINED", "PAYER_ACTION_REQUIRED"):
                link = svc._approve_link(svc.store.order_for_task(t["task_id"])["created"])
                out.append(f"<div class='card bad'><h3>Payment method declined</h3><p>PayPal declined the funding source. "
                           f"Nothing was charged. <a href='{esc(link)}'>Approve again at PayPal</a> with another funding "
                           f"source, then confirm here. We never switch your funding source for you.</p><form method='post' "
                           f"action='/tasks/{esc(t['task_id'])}/reapproved'>{self._csrf_field()}<button>I approved again"
                           f" &mdash; retry payment</button></form></div>")
            if att and att["state"] == "OUTCOME_UNKNOWN":
                out.append("<div class='card bad'><h3>Payment outcome being reconciled</h3><p>The payment provider did "
                           "not confirm the capture. Work has NOT started. We check the provider's own record and never "
                           "send a second capture automatically; the operator resolves anything that stays unclear.</p></div>")
            if v["capture"]:
                c = v["capture"]["verification"]
                out.append(f"<div class='card'><h3>Payment verification</h3><p>Verdict <b class='{'ok' if c['verdict'] == 'CAPTURE_VERIFIED' else 'bad'}'>"
                           f"{esc(c['verdict'])}</b> &middot; order <code>{esc(v['order']['order_id'])}</code> &middot; capture "
                           f"<code>{esc(c.get('capture_id'))}</code> ({esc(c.get('capture_status'))})</p><p>Source: {esc(c['source'])}</p><ul>"
                           + "".join(f"<li class='{'ok' if x['ok'] else 'bad'}'>{esc(x['check'])}</li>" for x in c["checks"])
                           + "</ul></div>")
            if v["receipt"]:
                b = v["receipt"]["body"]
                a = svc.store.artifact(b["artifact"]["digest"])
                out.append(self.report(a, b))
                out.append(f"<div class='card'><h3>Receipt</h3><p>Artifact digest <code>{esc(b['artifact']['digest'])}</code>"
                           f"<br>Signed by <code>{esc(b['issuer']['key_id'])}</code> (demo key) &middot; "
                           f"<a href='/receipts/{esc(t['task_id'])}.json'>receipt.json</a> &middot; "
                           f"<a href='/artifacts/{esc(t['task_id'])}/{esc(b['artifact']['digest'])}.json'>artifact.json</a> &middot; "
                           f"<a href='/events/{esc(t['task_id'])}'>events.json</a></p><ul>"
                           + "".join(f"<li>{esc(x)}</li>" for x in b["limits"]) + "</ul></div>")
            out.append("<div class='card'><h3>Evidence trail</h3><ol class='tl'>" + "".join(
                f"<li><code>{esc(e['to'])}</code> by {esc(e['actor'])}"
                f"{' &middot; ' + esc(e['evidence_kind']) if e['evidence_kind'] else ''} <code>{esc((e['hash'] or '')[:12])}</code></li>"
                for e in v["events"]) + "</ol></div>")
            return "".join(out)

        def status_panel(self, v):
            """R1e.1: every field the buyer needs, each from its own record; green only for a verified capture and a
            valid receipt. Nothing here is a research-quality or truth verdict."""
            st, rc, ev = v["task"]["state"], v["receipt"], v["events"]
            dm = getattr(svc, "demo_mode", None) or {"mode": "DEVELOPMENT", "banner": ""}
            art = svc.store.artifact(rc["body"]["artifact"]["digest"]) if rc else None
            rv, env = runtime_view(svc.runtime), svc.runtime.envelope
            capv = (v["capture"] or {}).get("verification") if v["capture"] else None
            pay = PAYMENT.get(svc.payments.environment, svc.payments.environment)
            money = "test money, no PayPal" if pay == "MOCK" else "PayPal Sandbox test money"
            rows = [("Mode", f"{esc(dm['mode'])}"),
                    ("Payment provider", f"{esc(pay)} ({money}) &middot; status " + (
                        f"<b class='{'ok' if capv['verdict'] == 'CAPTURE_VERIFIED' else 'bad'}'>{esc(capv['verdict'])}"
                        f"</b>, capture <code>{esc(capv.get('capture_id'))}</code> ({esc(capv.get('capture_status'))}), "
                        f"{len([c for c in capv['checks'] if c['ok']])}/{len(capv['checks'])} server-side checks"
                        if capv else f"<b>{esc(st)}</b> (no verified capture)"))]
            ts = {e["to"]: e["ts"] for e in ev}

            def utc(t):
                return time.strftime("%H:%M:%S", time.gmtime(t)) + "Z"
            if S.EXECUTION_AUTHORIZED in ts:
                rows.append(("Execution authorization",
                             f"authorized at {utc(ts[S.EXECUTION_AUTHORIZED])}, after the verified capture "
                             f"(PAYMENT_CAPTURED {utc(ts[S.PAYMENT_CAPTURED])}) &middot; AI authorization "
                             f"<code>{esc(env.authorization_id)}</code>: {esc(env.model)}, at most "
                             f"{env.max_requests_total} model request(s)"))
            else:
                rows.append(("Execution authorization", "not authorized: no verified capture yet, so no AI work can "
                                                        "start"))
            calls = svc.ledger.calls(task_id=v["task"]["task_id"]) if hasattr(svc, "ledger") else []
            kind = ("replayed, not a live model call" if rv["ai_provider"] == "recorded_replay" else
                    "REAL model request" if rv["ai_execution_class"] == "REAL_MODEL_EXECUTION" else
                    "offline stand-in, not a model call")
            rows.append(("AI provider", f"<b>{esc(rv['ai_provider'])}</b> ({esc(rv['ai_execution_class'])}): "
                                        f"{kind} &middot; {len(calls)} request(s) in the ledger" + "".join(
                f" &middot; <code>{esc(c['request_id'] or c['state'])}</code> {esc(c['state'])}" for c in calls)))
            rows.append(("Delivery", f"<b>{esc(st)}</b> &middot; protocol outcome only (payment verified, artifact "
                                     f"checks, receipt); not a research or truth verdict"))
            if art:
                es = execution_summary(st, rc, art)
                cs, rcmp = es["claim_support"], es["research_completeness"]
                cls = "neutral" if rcmp == "PASS" else "warn" if rcmp == "PARTIAL" else "bad"
                rows += [("Research completeness", f"<b class='{cls}'>{esc(rcmp)}</b> &middot; whether every model "
                                                   f"finding passed the document checks "
                                                   f"({esc('; '.join(es['research_completeness_reasons']))})"),
                         ("Claim support", f"<b>{cs['supported']}/{cs['model_findings']}</b> document-supported "
                                           f"(rule {esc(cs['rule'])}) &middot; {cs['unconfirmed']} unconfirmed &middot; "
                                           f"{cs['withheld']} withheld &middot; {cs['rejected']} rejected")]
            rows.append(("Independent scientific truth", "<b class='neutral'>NOT ESTABLISHED</b> &middot; AutoNOMOS "
                                                         "checks what the documents say, not whether it is true "
                                                         "(NOT_INDEPENDENTLY_VERIFIED)"))
            if rc:
                b = rc["body"]
                rows.append(("Signed receipt", f"<code>{esc(b['issuer']['key_id'])}</code> (local DEMO Ed25519 key) "
                                               f"&middot; artifact <code>{esc(b['artifact']['digest'])}</code> &middot; "
                                               f"<a href='/receipts/{esc(v['task']['task_id'])}.json'>receipt.json</a>"))
                vr = self._verify_receipt(v["task"]["task_id"])
                n_ok = sum(1 for x in vr["checks"].values() if x)
                rows.append(("Receipt verifier", f"<b class='{'ok' if vr['valid'] else 'bad'}'>"
                                                 f"{'VALID' if vr['valid'] else 'INVALID'}</b> ({n_ok}/{len(vr['checks'])} "
                                                 f"checks: signature, digests, event chain, quotes re-checked against "
                                                 f"the collection, issuer key pinned to this server) &middot; "
                                                 f"<a href='/receipts/{esc(v['task']['task_id'])}/verify'>details</a> "
                                                 f"&middot; anyone can re-run <code>python -m autonomos verify-receipt"
                                                 f"</code> offline"))
            return ("<div class='card' id='status-panel'><h3>Status</h3><table class='status'>" + "".join(
                f"<tr><th>{k}</th><td>{val}</td></tr>" for k, val in rows) + "</table></div>")

        @staticmethod
        def progress(st):
            if st in (S.FAILED, S.CANCELLED, S.REVIEW_REQUIRED):
                return ""
            i = STEPS.index(st) if st in STEPS else 0
            return "<p><small>Protocol progress (payment and delivery steps only; not a research verdict)</small><br>" + " &rarr; ".join(f"<b>{esc(s)}</b>" if j == i else (esc(s) if j > i else f"<span class='ok'>{esc(s)}</span>")
                                           for j, s in enumerate(STEPS)) + "</p>"

        @staticmethod
        def report(a, b):
            r = a["report"]

            def items(xs):
                return "".join(f"<li><p>{esc(x['claim'])}</p>" + "".join(
                    f"<blockquote>&ldquo;{esc(e['quote'])}&rdquo; <code>{esc(e['passage_id'])}</code></blockquote>"
                    for e in x["evidence"]) + "</li>" for x in xs)
            f, u = items(r["findings"]), items(r.get("unconfirmed_findings", []))
            rm = "".join(f"<li>{esc(x['claim'])}: {esc('; '.join(x['reasons']))}</li>" for x in r["removed_findings"])
            runs = a["model_runs"]
            return (f"<div class='card'><h3>Delivered report</h3><p><small>Artifact check status: "
                    f"{esc(a['delivery_verification']['status'])} (whether every model finding passed the document "
                    f"checks; not a research-quality or truth verdict)</small></p>"
                    f"<p>{esc(r['summary'])}</p>"
                    f"<p><b>Document-supported findings</b> (each quotation is verbatim in the named document and "
                    f"the claim passed the deterministic support rule; independent scientific truth: NOT ESTABLISHED)"
                    f"</p><ol>{f or '<li>none</li>'}</ol>"
                    f"<p><b>Unconfirmed or withheld conclusions</b> (not delivered as findings)</p>"
                    + (f"<p>Quote-verified excerpts whose claims are NOT confirmed (read the quotation, not the claim)</p>"
                       f"<ol>{u}</ol>" if u else "")
                    + (f"<p>Withheld or rejected by the verifier</p><ul>{rm}</ul>" if rm else "")
                    + ("<p>none</p>" if not u and not rm else "")
                    + (f"<p><b>Uncertainties</b></p><ul>" + "".join(f"<li>{esc(x)}</li>" for x in r["uncertainties"]) + "</ul>")
                    + (f"<p><b>Not answerable from these documents</b></p><ul>" + "".join(f"<li>{esc(x)}</li>" for x in r["unanswered"]) + "</ul>" if r["unanswered"] else "")
                    + f"<p>Produced by <code>{esc(runs[-1]['provider'])}/{esc(runs[-1]['model'])}</code> in {len(runs)} calls; "
                      f"{esc(a['delivery_verification']['quotes_checked'])} quotes machine-checked.</p></div>")

        def mock_checkout(self, oid):
            o = svc.payments.orders.get(oid)
            if not o:
                return "<p>Unknown mock order.</p>"
            amt = o["purchase_units"][0]["amount"]
            return (f"<div class='card'><h2>MOCK checkout</h2><p>This page imitates PayPal's buyer approval step. "
                    f"<b>No PayPal system is involved.</b></p><p>Order <code>{esc(oid)}</code>: {esc(amt['value'])} "
                    f"{esc(amt['currency_code'])}</p><form method='post' action='/mock-paypal/{esc(oid)}/approve'>{self._csrf_field()}"
                    f"<button>Approve (mock)</button></form><form method='post' action='/mock-paypal/{esc(oid)}/cancel'>{self._csrf_field()}"
                    f"<p><button class='secondary'>Cancel (mock)</button></p></form></div>")
    return H


class _Forbidden(Exception):
    pass


_SECRET = secrets.token_bytes(32)     # per-process CSRF/session key; never persisted, never rendered


def _safe(fn, *a):
    try:
        fn(*a)
    except Exception as e:     # recorded by the service; never crash the server
        print("background execution error:", type(e).__name__, redact(str(e)[:200]))


def serve(svc, host="127.0.0.1", port=8765):
    if host not in ("127.0.0.1", "localhost"):
        raise SystemExit("refusing to bind a non-loopback address in the R0 demo")
    httpd = ThreadingHTTPServer((host, port), make_handler(svc))
    rv = runtime_view(svc.runtime) if getattr(svc, "runtime", None) else {"ai_provider": svc.cfg.ai_provider,
                                                                          "ai_execution_class": "legacy",
                                                                          "authorization": None}
    print(f"AutoNOMOS on http://{host}:{port}  payment={svc.payments.environment}  ai={rv['ai_provider']} "
          f"({rv['ai_execution_class']}, authorization {rv['authorization']})")
    httpd.serve_forever()
