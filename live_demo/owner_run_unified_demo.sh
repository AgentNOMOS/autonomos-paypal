#!/bin/bash
# AutoNOMOS R1e.1 LIVE DEMO (UNIFIED DEMO 1, mode SUPERVISED_LIVE) - owner-run launcher: ONE PayPal SANDBOX order (1.00 USD test money, at most 1 capture
# request) and ONE real Claude Opus 5.5 request, in one supervised loopback web session.
#
#   owner_run_unified_demo.sh --dry-run   all checks + offline preflights; starts nothing, sends nothing
#   owner_run_unified_demo.sh --launch    after the owner's written GO: checks, typed confirmation in a terminal,
#                                         single-use marker, then the loopback server in the foreground (Ctrl-C ends it)
#   owner_run_unified_demo.sh --collect   after the server has stopped: OFFLINE evidence capture (no PayPal, no Anthropic)
#
# Never prints, logs or copies the API key or the PayPal secret. Never retries. Refuses a second launch.
# Paths can be overridden by env (OWN, AUTH, KEY, PPENV, OUT, MARK, EVD, PORT) for offline rehearsal.
set -uo pipefail
umask 077
MODE=${1:-}
OWN=${OWN:-/root/.config/autonomos-paypal}
AUTH=${AUTH:-$OWN/ai_authorization_r1e1_live1.json}
KEY=${KEY:-$OWN/anthropic.key}
PPENV=${PPENV:-$OWN/sandbox.env}
OUT=${OUT:-$OWN/r1e1-live-demo-1}
MARK=${MARK:-$OWN/r1e1-live-demo-1.LAUNCHED}
PORT=${PORT:-8766}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
APP=$(cd "$HERE/../app" && pwd)               # the R1e.1 release candidate next to this script
EVD=${EVD:-$(cd "$HERE/.." && pwd)/evidence_live_demo_1}
die() { echo "REFUSED: $*" >&2; exit 2; }
case "$MODE" in --dry-run|--launch|--collect) ;; *) die "usage: $0 --dry-run | --launch | --collect";; esac

private_file() {   # path label
  [ -f "$1" ] && [ ! -L "$1" ] || die "$2 $1 missing (or a symlink)"
  local m; m=$(stat -c %a "$1"); [ "$m" = 600 ] || [ "$m" = 400 ] || die "$2 mode $m (needs 600 or 400)"
  [ "$(stat -c %u "$1")" = "$(id -u)" ] || die "$2 not owned by $(id -un)"
}

if [ "$MODE" = "--collect" ]; then
  echo "== collect (offline): no PayPal call, no Anthropic call"
  [ -d "$MARK" ] || die "no launch marker: nothing to collect"
  [ -e "$EVD" ] && die "$EVD already exists"
  python3 -I -c "import socket,sys;s=socket.socket();s.bind(('127.0.0.1',int(sys.argv[1])));s.close()" "$PORT" \
    2>/dev/null || die "port $PORT is still in use: stop the server (Ctrl-C) first"
  mkdir -p "$EVD"
  python3 -I -B - "$OUT/data/autonomos.sqlite" "$EVD" <<'EOF' || die "collect failed"
import json, os, sqlite3, sys
src, evd = sys.argv[1], sys.argv[2]
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
d = sqlite3.connect(os.path.join(evd, "autonomos.sqlite")); s.backup(d); d.close()      # consistent read-only snapshot
db = sqlite3.connect(f"file:{os.path.join(evd, 'autonomos.sqlite')}?mode=ro", uri=True)
tasks = [r[0] for r in db.execute("SELECT task_id FROM tasks ORDER BY created")]
summary = {"tasks": tasks, "orders": db.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
           "capture_attempts": [list(r) for r in db.execute("SELECT order_id, state, sends FROM capture_attempts")],
           "ai_calls": [list(r) for r in db.execute("SELECT call_id, state, requested_model, reported_model, request_id, "
                                                     "input_tokens, output_tokens, charged_usd, worst_case_usd FROM r1d_ai_calls")]}
for tid in tasks:
    t = os.path.join(evd, tid); os.makedirs(t)
    rc = db.execute("SELECT receipt_json FROM receipts WHERE task_id=?", (tid,)).fetchone()
    if rc:
        rc = json.loads(rc[0]); json.dump(rc, open(os.path.join(t, "receipt.json"), "w"), indent=1)
        a = db.execute("SELECT content_json FROM artifacts WHERE digest=?", (rc["body"]["artifact"]["digest"],)).fetchone()
        json.dump(json.loads(a[0]), open(os.path.join(t, "artifact.json"), "w"), indent=1)
    cols = ["seq", "task_id", "ts", "from", "to", "actor", "evidence_kind", "evidence_digest", "detail", "prev_hash", "hash"]
    ev = [dict(zip(cols, r)) for r in db.execute("SELECT seq, task_id, ts, from_state, to_state, actor, evidence_kind, "
                                                 "evidence_digest, detail_json, prev_hash, hash FROM events WHERE task_id=? "
                                                 "ORDER BY seq", (tid,))]
    for e in ev:
        e["detail"] = json.loads(e["detail"]); e.pop("task_id")
    json.dump(ev, open(os.path.join(t, "events.json"), "w"), indent=1)
    st = db.execute("SELECT state FROM tasks WHERE task_id=?", (tid,)).fetchone()[0]
    json.dump({"task": {"task_id": tid, "state": st}}, open(os.path.join(t, "task_view.json"), "w"), indent=1)
json.dump(summary, open(os.path.join(evd, "DB_SUMMARY.json"), "w"), indent=1, default=str)
print(json.dumps(summary, indent=1, default=str))
EOF
  [ -f "$OUT/data/demo_receipt_key.json" ] && python3 -I -c "import json,sys;print(json.load(open(sys.argv[1]))['public_hex'])" \
     "$OUT/data/demo_receipt_key.json" > "$EVD/issuer_public_key.hex"       # public half only
  cp "$AUTH" "$EVD/ai_authorization_used.json"; cp "$MARK/launched_at" "$EVD/launched_at" 2>/dev/null
  cd "$APP" || die "no app"
  for t in "$EVD"/TASK-*; do
    [ -f "$t/receipt.json" ] || continue
    python3 -B -m autonomos verify-receipt "$t/receipt.json" "$t/artifact.json" "$t/events.json" \
      --corpus corpus/agentnomos_quantum --issuer-key "$(cat "$EVD/issuer_public_key.hex")" > "$t/verify_receipt.json"
    echo "$(basename "$t"): verify-receipt exit=$?"
    python3 -B -m autonomos execution-summary "$t" > "$t/execution_summary.json"; python3 -I -c \
      "import json,sys;print(json.load(open(sys.argv[1]))['headline'])" "$t/execution_summary.json"
  done
  python3 -I - "$KEY" "$PPENV" "$EVD" <<'EOF'
import os, sys
secrets = [open(sys.argv[1]).read().strip().encode()]
for line in open(sys.argv[2]):
    k, _, v = line.strip().partition("=")
    if k.strip().endswith(("SECRET", "CLIENT_ID")) and v.strip().strip('"\''):
        secrets.append(v.strip().strip('"\'').encode())
hits = [os.path.join(d, f) for d, _, fs in os.walk(sys.argv[3]) for f in fs
        if any(s and s in open(os.path.join(d, f), "rb").read() for s in secrets)]
print("credential material in evidence:", "NONE" if not hits else f"FOUND IN {len(hits)} FILE(S) - DELETE THEM")
sys.exit(1 if hits else 0)
EOF
  (cd "$EVD" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS && \
   sha256sum -c SHA256SUMS | grep -vc ': OK$' | sed 's/^/evidence seal failures: /')
  echo "COLLECT DONE (offline). The PayPal-side read-only check (order-status) is a separate owner decision."
  exit 0
fi

echo "== 1. credential and authorization files (metadata only; no secret is printed)"
private_file "$KEY" "key file"
python3 -I -B - "$KEY" "$APP" <<'EOF' || die "key file does not hold one accepted Console API key (sk-ant-apiNN-..., sk-ant-usr-...)"
import sys
sys.path.insert(0, sys.argv[2])
from autonomos.r1d.aiconfig import classify_api_key       # the same rule the app enforces; value never printed
ok, label = classify_api_key(open(sys.argv[1]).read().strip())
print("key file: present, private, format", "OK" if ok else "NOT OK", f"({label})")
sys.exit(0 if ok else 1)
EOF
private_file "$PPENV" "PayPal sandbox env file"
for v in PAYPAL_CLIENT_ID PAYPAL_CLIENT_SECRET PAYPAL_MERCHANT_ID; do
  grep -q "^[[:space:]]*\(export[[:space:]]\+\)\?$v=..*" "$PPENV" || die "PayPal sandbox env file has no $v"
done
grep -qi "api-m.paypal.com\|api.paypal.com" "$PPENV" && die "PayPal env file mentions a LIVE host"
echo "PayPal sandbox env file: present, private, names set (values not shown)"
private_file "$AUTH" "authorization file"
AID=$(python3 -I - "$AUTH" "$OUT" <<'EOF'
import json, sys, datetime
a, out = json.load(open(sys.argv[1])), sys.argv[2]
want = {"model": "claude-opus-5-5", "effort": "medium", "max_executions": 1, "max_requests_total": 1,
        "max_requests_per_execution": 1, "max_input_tokens_per_request": 34000,
        "max_output_tokens_per_request": 12000, "max_total_tokens": 46000,
        "allowed_payment_environments": ["SANDBOX"], "bound_data_dir": out + "/data"}
bad = [f"{k}={a.get(k)!r} (expected {v!r})" for k, v in want.items() if a.get(k) != v]
try:
    if float(a["max_usd_total"]) > 0.50: bad.append("max_usd_total above 0.50")
    if float(a["price_usd_per_mtok"]["input"]) < 4.40 or float(a["price_usd_per_mtok"]["output"]) < 22.00:
        bad.append("price below the conservative 4.40/22.00 USD per MTok (US-only 1.1x)")
    na = datetime.datetime.strptime(a["not_after"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    if not datetime.timedelta(0) < na - datetime.datetime.now(datetime.timezone.utc) <= datetime.timedelta(hours=24):
        bad.append("not_after must be in the future and at most 24 h ahead")
except Exception as e:
    bad.append(f"unreadable field: {type(e).__name__} {e}")
if bad:
    print("; ".join(bad), file=sys.stderr); sys.exit(1)
print(a["authorization_id"])
EOF
) || die "authorization file does not match the UNIFIED DEMO 1 envelope"
echo "authorization: $AID (envelope matches UNIFIED DEMO 1: 1 model request, SANDBOX payment only)"

echo "== 2. single-run guards"
[ ! -e "$MARK" ] || die "UNIFIED DEMO 1 was already launched ($MARK exists). Another run needs a new authorization and GO."
[ ! -e "$OUT" ] || die "$OUT already exists"
[ ! -e "$EVD" ] || die "$EVD already exists"
python3 -I -c "import socket,sys;s=socket.socket();s.bind(('127.0.0.1',int(sys.argv[1])));s.close()" "$PORT" 2>/dev/null \
  || die "port $PORT is in use (the R1c Run 2 server holds 8765; this demo uses $PORT)"
echo "no previous launch; OUT and evidence directory are new; port $PORT is free"

RUNENV=(env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_PROFILE -u ANTHROPIC_BASE_URL -u PAYPAL_WEBHOOK_ID
        -u HTTPS_PROXY -u https_proxy -u HTTP_PROXY -u http_proxy -u ALL_PROXY -u all_proxy
        AUTONOMOS_ENV_FILE="$PPENV" AUTONOMOS_PAYMENT_PROVIDER=paypal_sandbox AUTONOMOS_AI_PROVIDER=fixture
        AUTONOMOS_R1D_AI_PROVIDER=anthropic_real AUTONOMOS_R1D_AI_AUTHORIZATION="$AUTH"
        AUTONOMOS_ANTHROPIC_KEY_FILE="$KEY" AUTONOMOS_DATA_DIR="$OUT/data"
        AUTONOMOS_PUBLIC_BASE_URL="http://127.0.0.1:$PORT" AUTONOMOS_LISTEN_PORT="$PORT"
        AUTONOMOS_SANDBOX_MAX_ORDERS=1 AUTONOMOS_SANDBOX_MAX_CAPTURE_REQUESTS=1
        AUTONOMOS_DEMO_MODE=SUPERVISED_LIVE AUTONOMOS_LIVE_LAUNCH_MARKER="$MARK")   # R1e.1: serve refuses without both
[ -n "${AUTONOMOS_R1D_REAL_AI_GO:-}" ] && RUNENV+=(AUTONOMOS_R1D_REAL_AI_GO="$AUTONOMOS_R1D_REAL_AI_GO")
[ -n "${AUTONOMOS_SANDBOX_OWNER_GO:-}" ] && RUNENV+=(AUTONOMOS_SANDBOX_OWNER_GO="$AUTONOMOS_SANDBOX_OWNER_GO")
inenv() { ( set -a; . "$PPENV"; set +a; unset PAYPAL_WEBHOOK_ID; "${RUNENV[@]}" "$@" ); }

echo "== 3. offline preflights (no network)"
cd "$APP" || die "no app"
PP=$(mktemp); PF=$(mktemp)
inenv python3 -B -m autonomos sandbox-preflight > "$PP"; ppc=$?
python3 -I -c "import json,sys;d=json.load(open(sys.argv[1]));c=d['checks'];print('  sandbox checks failing:',[k for k,v in c.items() if not v] or 'none');print('  caps:',d['config']['sandbox_caps'],' api base:',d['config']['paypal_api_base'],' merchant set:',bool(d['config']['paypal_merchant_id']))" "$PP"
echo "sandbox-preflight exit $ppc (0 = everything but the owner GO is in place)"
[ "$ppc" = 0 ] || die "sandbox preflight failed"
inenv python3 -B -m autonomos ai-preflight > "$PF"; pfc=$?
VERDICT=$(python3 -I -c "import json,sys;d=json.load(open(sys.argv[1]));print(d['verdict']);print('  key family (app preflight):',d.get('key_family'),file=sys.stderr);[print('  blocking:',b,file=sys.stderr) for b in d['blocking']];print('  worst case USD:',d.get('cost',{}).get('worst_case_authorization_usd'),file=sys.stderr)" "$PF")
echo "ai-preflight verdict: $VERDICT (exit $pfc)"
rm -f "$PP" "$PF"

if [ "$MODE" = "--dry-run" ]; then
  echo "DRY RUN COMPLETE: nothing was started and nothing was sent. The launch needs, after the owner's written GO:"
  echo "  AUTONOMOS_R1D_REAL_AI_GO=real-ai:$AID  AUTONOMOS_SANDBOX_OWNER_GO=capture-at-most-1-order"
  exit 0
fi

[ "$VERDICT" = REAL_PROVIDER_READY ] || die "ai-preflight is not READY (see blocking lines above)"
[ "${AUTONOMOS_R1D_REAL_AI_GO:-}" = "real-ai:$AID" ] || die "AUTONOMOS_R1D_REAL_AI_GO does not match $AID"
[ "${AUTONOMOS_SANDBOX_OWNER_GO:-}" = "capture-at-most-1-order" ] || die "AUTONOMOS_SANDBOX_OWNER_GO is not set"
[ -t 0 ] || die "the confirmation must be typed in an interactive terminal (stdin is not a TTY)"
echo
echo "This starts the loopback server on 127.0.0.1:$PORT for ONE supervised task: one PayPal SANDBOX order (test money,"
echo "at most one capture request) and ONE paid request to api.anthropic.com (claude-opus-5-5, ledger ceiling from"
echo "$AID). Nothing is retried. Stop the server with Ctrl-C after the receipt is shown, then run --collect."
read -r -p "Type the authorization id to start: " ans
[ "$ans" = "$AID" ] || die "confirmation did not match; nothing started"
mkdir "$MARK" || die "could not create the single-launch marker; nothing started"
date -u +%FT%TZ > "$MARK/launched_at"
mkdir -p "$OUT/data"
echo "== 4. server in mode SUPERVISED_LIVE (foreground). Open http://127.0.0.1:$PORT/ in the owner's browser (SSH tunnel)."
inenv python3 -B -u -m autonomos serve
echo "server ended (exit $?). Next: $0 --collect"
