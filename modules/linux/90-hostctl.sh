#!/usr/bin/env bash
# Module 90 (Linux): small host actions for tb.py and the tests.
#
#   90-hostctl.sh secure-file PATH
#   90-hostctl.sh node-api    --node-dir DIR --method GET --path /v1/status [--body-b64 B64] [--timeout 60]
#                             [--addr HOST:PORT]
#   90-hostctl.sh node-svc    --node-dir DIR --action stop|start|restart|kill
#   90-hostctl.sh fb-svc      --action stop|start|restart|status [--fb-service UNIT]
#   90-hostctl.sh counts      --db FILE [--fb-root /opt/firebird] [--port 3050]
#   90-hostctl.sh limbo       --db FILE [--fb-root /opt/firebird] [--port 3050]
#   90-hostctl.sh files       --glob 'PATTERN'
#   90-hostctl.sh remove-file --path FILE
#   90-hostctl.sh block-peer  --addr ADDR [--port PORT]      (iptables, both ways)
#   90-hostctl.sh unblock-peer --addr ADDR [--port PORT]
#   90-hostctl.sh tail        --path FILE [--lines 50]
#   90-hostctl.sh replctl     --dir JOURNAL_SOURCE_DIR   (replica control files)
#   90-hostctl.sh statelog    --node-dir DIR --db-id ID [--from LINE]
#                             (state changes the node journaled, from its journal.jsonl)
#   90-hostctl.sh replog-inject --path REPLICATION_LOG --db FILE --message-b64 B64
#                             [--role replica|master] [--level ERROR] [--count 1]
#   90-hostctl.sh peer-push   --node-dir DIR --addr HOST:PORT --meta-b64 B64 [--file F | --random N]
#                             (POST /v1/peer/segments to another node, as this node)
#   90-hostctl.sh node-on-file --node-dir DIR --path FILE --event locked|unlocked
#                             --action stop|kill [--timeout 900]
#   90-hostctl.sh nbackup-unlock --db FILE [--fb-root /opt/firebird] [--port 3050]
#   90-hostctl.sh db-new-guid --db FILE [--fb-root /opt/firebird] [--port 3050] [--fb-service UNIT]
#   90-hostctl.sh rcm-api     --method GET --path /v1/alerts [--body-b64 B64] [--then-restart false]
#                             (RCM operator API on this host, Digest login from the secrets)
#
# Results come as one "TBRESULT <json>" line.
source "$(dirname "$0")/common.sh"

CMD="${1:-}"; shift || true
if [[ "$CMD" == secure-file ]]; then chmod 600 "$1"; chown root:root "$1" 2>/dev/null || true; exit 0; fi
parse_args "$@"

NODE_DIR="$(arg node_dir /opt/hqclusternode)"
FB_ROOT="$(arg fb_root /opt/firebird)"
PORT="$(arg port 3050)"

isql_q() { # DB SQL -> stdout
  printf '%s\n' "$2" | "$(fb_tool "$FB_ROOT" isql)" -q -pag 0 "localhost/$PORT:$1"
}

case "$CMD" in
  node-api)
    args=(api "$(arg method GET)" "$(arg path /v1/status)" -i -timeout-sec "$(arg timeout 60)"
          -config "$NODE_DIR/node.json" -certs "$NODE_DIR/certs")
    # --addr: another node's API, called with this node's certificate.
    [[ -z "$(arg addr)" ]] || args+=(-addr "$(arg addr)")
    tmp=""
    if [[ -n "$(arg body_b64)" ]]; then
      tmp="$(mktemp)"; base64 -d <<<"$(arg body_b64)" >"$tmp"; args+=(-body-file "$tmp")
    fi
    set +e; out="$("$NODE_DIR/hqclusternode" "${args[@]}" 2>&1)"; ec=$?; set -e
    [[ -z "$tmp" ]] || rm -f "$tmp"
    if [[ $ec -ne 0 ]]; then echo "$out" >&2; die "api call failed (exit $ec)"; fi
    python3 -c 'import json,sys; print("TBRESULT " + json.dumps(json.loads(sys.stdin.read())))' <<<"$out"
    ;;

  node-svc)
    case "$(arg action)" in
      stop|start) "$NODE_DIR/hqclusternode" svc "$(arg action)" -config "$NODE_DIR/node.json" ;;
      restart) "$NODE_DIR/hqclusternode" svc stop -config "$NODE_DIR/node.json" || true
               "$NODE_DIR/hqclusternode" svc start -config "$NODE_DIR/node.json" ;;
      # A crash: systemd restarts the node by itself (Restart=on-failure).
      kill) pkill -9 -f "$NODE_DIR/hqclusternode serve" || true ;;
      *) die "--action stop|start|restart|kill" ;;
    esac
    result "{\"node_svc\":\"$(arg action)\"}"
    ;;

  fb-svc)
    UNIT="$(arg fb_service)"; [[ -n "$UNIT" ]] || UNIT="$(detect_fb_unit)"
    [[ -n "$UNIT" ]] || die "cannot find the Firebird unit"
    case "$(arg action)" in
      stop|start|restart) systemctl "$(arg action)" "$UNIT" ;;
      status) ;;
      *) die "--action stop|start|restart|status" ;;
    esac
    result "{\"unit\":\"$UNIT\",\"active\":\"$(systemctl is-active "$UNIT" || true)\"}"
    ;;

  counts)
    # Row count of every user table. Keyed tables (with a primary key or a
    # unique index) must match between master and replica.
    load_secrets
    DB="$(arg db)"
    tables="$(isql_q "$DB" "set heading off;
select trim(r.rdb\$relation_name) || '|' ||
  iif(exists(select 1 from rdb\$indices i where i.rdb\$relation_name = r.rdb\$relation_name
             and i.rdb\$unique_flag = 1 and coalesce(i.rdb\$index_inactive, 0) = 0), 'K', 'N')
from rdb\$relations r
where coalesce(r.rdb\$system_flag, 0) = 0 and r.rdb\$view_blr is null
order by 1;")" || die "isql failed on $DB"
    sql="set heading off; set transaction read only ignore limbo;"
    first=1
    while IFS='|' read -r t k; do
      # isql pads the column with spaces: strip both fields.
      t="$(echo "$t" | tr -d '[:space:]')"; k="$(echo "$k" | tr -d '[:space:]')"; [[ -n "$t" ]] || continue
      [[ $first -eq 1 ]] && sql+=$'\nselect ' || sql+=$'\nunion all select '
      sql+="'RC|$t|$k|' || count(*) from \"$t\""
      first=0
    done <<<"$tables"
    [[ $first -eq 0 ]] || die "no user tables in $DB"
    out="$(isql_q "$DB" "$sql;
commit;")" || die "count query failed on $DB"
    python3 -c '
import json, sys
rows = {}
for line in sys.stdin.read().splitlines():
    line = line.strip()
    if line.startswith("RC|"):
        _, t, k, n = [f.strip() for f in line.split("|")]
        rows[t] = {"keyed": k == "K", "rows": int(n)}
print("TBRESULT " + json.dumps(rows))' <<<"$out"
    ;;

  limbo)
    load_secrets
    out="$("$(fb_tool "$FB_ROOT" gfix)" -list "localhost/$PORT:$(arg db)" 2>&1 || true)"
    n="$(grep -ciE 'transaction [0-9]+' <<<"$out" || true)"
    result "{\"limbo\":${n:-0}}"
    ;;

  files)
    # shellcheck disable=SC2086
    python3 -c 'import glob,json,sys,os; fs=sorted(glob.glob(sys.argv[1])); print("TBRESULT "+json.dumps([{"path":f,"size":os.path.getsize(f),"mtime":os.path.getmtime(f)} for f in fs]))' "$(arg glob)"
    ;;

  remove-file)
    P="$(arg path)"; [[ -f "$P" ]] || die "no file $P"
    rm -f "$P"; result "{\"removed\":\"$P\"}"
    ;;

  block-peer|unblock-peer)
    need_cmd iptables
    A="$(arg addr)"; [[ -n "$A" ]] || die "--addr is required"
    op=-I; [[ "$CMD" == unblock-peer ]] && op=-D
    if [[ -n "$(arg port)" ]]; then
      iptables $op INPUT -s "$A" -p tcp --dport "$(arg port)" -j DROP -m comment --comment tb-block 2>/dev/null || true
      iptables $op OUTPUT -d "$A" -p tcp --dport "$(arg port)" -j DROP -m comment --comment tb-block 2>/dev/null || true
    else
      iptables $op INPUT -s "$A" -j DROP -m comment --comment tb-block 2>/dev/null || true
      iptables $op OUTPUT -d "$A" -j DROP -m comment --comment tb-block 2>/dev/null || true
    fi
    result "{\"action\":\"$CMD\",\"addr\":\"$A\"}"
    ;;

  tail) tail -n "$(arg lines 50)" "$(arg path)" ;;

  replctl)
    # Firebird's replica control files ({GUID}) in a journal source folder:
    # the position applied so far and the transactions held as active.
    python3 - "$(arg dir)" <<'PY'
import glob, json, os, struct, sys
out = []
for f in sorted(glob.glob(os.path.join(sys.argv[1], "{*}"))):
    b = open(f, "rb").read()
    if len(b) < 40 or not b.startswith(b"FBREPLCTL"):
        out.append({"file": f, "error": "not a control file"})
        continue
    n, = struct.unpack_from("<I", b, 12)
    seq, = struct.unpack_from("<Q", b, 16)
    off, = struct.unpack_from("<I", b, 24)
    dbs, = struct.unpack_from("<Q", b, 32)
    act = [dict(zip(("tra", "seq"), struct.unpack_from("<QQ", b, 40 + 16 * i))) for i in range(n)]
    out.append({"file": f, "sequence": seq, "offset": off, "db_sequence": dbs, "active": act})
print("TBRESULT " + json.dumps(out))
PY
    ;;

  statelog)
    # db_state and reinit_step events of one database, from line --from of the
    # node's journal on. The line count comes back too: the next call starts
    # there. A journal shorter than --from was compacted: read it all.
    python3 - "$NODE_DIR/journal.jsonl" "$(arg from 0)" "$(arg db_id)" <<'PY'
import json, sys
path, start, dbid = sys.argv[1], int(sys.argv[2] or 0), sys.argv[3]
if start < 0:                       # the line count only: where the next call starts
    try:
        with open(path, "rb") as f:
            n = sum(1 for _ in f)
    except FileNotFoundError:
        n = 0
    print("TBRESULT " + json.dumps({"lines": n, "events": []}))
    sys.exit(0)
try:
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
except FileNotFoundError:
    lines = []
if start > len(lines):
    start = 0
ev = []
for line in lines[start:]:
    if dbid not in line or ('"db_state"' not in line and '"reinit_step"' not in line):
        continue
    try:
        e = json.loads(line)
    except ValueError:
        continue
    if e.get("db_id") != dbid:
        continue
    f = e.get("fields") or {}
    ev.append({"ts": e.get("ts"), "type": e.get("type"), "state": f.get("state"), "reason": f.get("reason"),
               "generation": f.get("generation"), "phase": f.get("phase"), "error": f.get("error")})
print("TBRESULT " + json.dumps({"lines": len(lines), "events": ev}))
PY
    ;;

  replog-inject)
    # Firebird's replication.log block format, appended in one write. The node
    # reads this file for what Firebird did; a test that cannot make Firebird
    # log a line puts it there itself.
    python3 - "$(arg path)" "$(arg db)" "$(arg role replica)" "$(arg level ERROR)" "$(arg count 1)" \
      "$(arg message_b64)" <<'PY'
import base64, json, socket, sys, time
path, db, role, level, count, msg = sys.argv[1:7]
msg = base64.b64decode(msg).decode()
host = socket.gethostname().split(".")[0] or "host"
text = ""
for _ in range(int(count)):
    text += f"{host} ({role}) {time.strftime('%a %b %d %H:%M:%S %Y')}\n\tDatabase: {db}\n\t{level}: {msg}\n\n"
with open(path, "a", encoding="utf-8") as f:
    f.write(text)
print("TBRESULT " + json.dumps({"path": path, "blocks": int(count)}))
PY
    ;;

  peer-push)
    # A segment sent to another node's peer API with this node's certificate:
    # the receiver takes it for one from this node. sha256 "auto" (or none)
    # is filled in from the body.
    python3 - "$NODE_DIR" "$(arg addr)" "$(arg meta_b64)" "$(arg file)" "$(arg random 0)" <<'PY'
import base64, hashlib, json, os, subprocess, sys, tempfile
nd, addr, meta, src, rnd = sys.argv[1:6]
meta = json.loads(base64.b64decode(meta))
body = open(src, "rb").read() if src else os.urandom(int(rnd) or 4096)
if meta.get("sha256") in (None, "", "auto"):
    meta["sha256"] = hashlib.sha256(body).hexdigest()
meta.setdefault("uncompressed_size", len(body))
meta.setdefault("compress", "none")
fd, tmp = tempfile.mkstemp()
os.write(fd, body)
os.close(fd)
try:
    r = subprocess.run([os.path.join(nd, "hqclusternode"), "api", "POST", "/v1/peer/segments", "-i",
                        "-addr", addr, "-config", os.path.join(nd, "node.json"), "-certs", os.path.join(nd, "certs"),
                        "-body-file", tmp, "-content-type", "application/octet-stream",
                        "-H", "X-HQCluster-Segment: " + json.dumps(meta)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
finally:
    os.unlink(tmp)
if r.returncode != 0:
    sys.stderr.write(r.stdout + r.stderr)
    sys.exit(1)
out = json.loads(r.stdout)
out["meta"] = meta
print("TBRESULT " + json.dumps(out))
PY
    ;;

  node-on-file)
    # Wait for an nbackup lock file: "locked" fires when it appears,
    # "unlocked" when it appeared and is gone again. Then stop or kill the
    # node at once — the moment a reinit is in that phase.
    F="$(arg path)"; [[ -n "$F" ]] || die "--path is required"
    python3 - "$F" "$(arg event locked)" "$(arg timeout 900)" <<'PY' || die "no $(arg event locked) event for $F"
import os, sys, time
f, evt, end = sys.argv[1], sys.argv[2], time.time() + float(sys.argv[3])
seen = False
while time.time() < end:
    ex = os.path.exists(f)
    if ex and not seen:
        seen = True
        if evt == "locked":
            sys.exit(0)
    if seen and not ex:
        sys.exit(0)
    time.sleep(0.02)
sys.exit(3)
PY
    case "$(arg action stop)" in
      stop) "$NODE_DIR/hqclusternode" svc stop -config "$NODE_DIR/node.json" ;;
      kill) pkill -9 -f "$NODE_DIR/hqclusternode serve" || true ;;
      *) die "--action stop|kill" ;;
    esac
    result "{\"fired\":\"$(arg event locked)\",\"action\":\"$(arg action stop)\",\"delta_exists\":$([[ -f "$F" ]] && echo true || echo false)}"
    ;;

  nbackup-unlock)
    load_secrets
    DB="$(arg db)"
    "$(fb_tool "$FB_ROOT" nbackup)" -N "localhost/$PORT:$DB"
    result "{\"unlocked\":\"$DB\",\"delta_left\":$([[ -f "$DB.delta" ]] && echo true || echo false)}"
    ;;

  db-new-guid)
    # The same data under a new GUID in the same file: what a restore or a
    # copy put in place of a master database looks like. A copy under an
    # nbackup lock, -F on the copy (new GUID), then Firebird is stopped while
    # the copy replaces the file.
    load_secrets
    DB="$(arg db)"; [[ -f "$DB" ]] || die "no database $DB"
    NB="$(fb_tool "$FB_ROOT" nbackup)"
    UNIT="$(arg fb_service)"; [[ -n "$UNIT" ]] || UNIT="$(detect_fb_unit)"
    [[ -n "$UNIT" ]] || die "cannot find the Firebird unit"
    tmp="$DB.tbnewguid"
    "$NB" -L "localhost/$PORT:$DB"
    ec=0; cp -f "$DB" "$tmp" || ec=1
    "$NB" -N "localhost/$PORT:$DB"
    (( ec == 0 )) || { rm -f "$tmp"; die "copy under nbackup lock failed"; }
    "$NB" -F "$tmp"
    chown --reference="$DB" "$tmp"; chmod --reference="$DB" "$tmp"
    systemctl stop "$UNIT"
    mv -f "$tmp" "$DB"
    systemctl start "$UNIT"
    result "{\"replaced\":\"$DB\"}"
    ;;

  rcm-api)
    read_secrets
    python3 - "$(arg method GET)" "$(arg path /v1/alerts)" "$(arg body_b64)" "$(arg then_restart false)" <<'PY'
import base64, json, os, subprocess, sys, urllib.error, urllib.request
method, path, body, restart = sys.argv[1:5]
user, pw = os.environ.get("TB_RCM_USER", ""), os.environ.get("TB_RCM_PASSWORD", "")
if not user or not pw:
    sys.exit("TB_RCM_USER / TB_RCM_PASSWORD are not set (secrets rcm_user, rcm_password)")
base = "http://127.0.0.1:7444"
mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
mgr.add_password(None, base, user, pw)
op = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))
data = base64.b64decode(body) if body else None
req = urllib.request.Request(base + path, data=data, method=method,
                             headers={"Content-Type": "application/json", "Accept": "application/json"})
try:
    r = op.open(req, timeout=60)
    st, raw = r.status, r.read()
except urllib.error.HTTPError as e:
    st, raw = e.code, e.read()
# Restart right after the answer: a job RCM runs in the background is then
# cut off while it runs.
if restart == "true":
    subprocess.run(["systemctl", "restart", "hqbirdrcm"], check=False)
try:
    b = json.loads(raw or b"null")
except ValueError:
    b = raw.decode(errors="replace")[:2000]
print("TBRESULT " + json.dumps({"status": st, "body": b}))
PY
    ;;

  *) die "usage: 90-hostctl.sh secure-file|node-api|node-svc|fb-svc|counts|limbo|files|remove-file|block-peer|unblock-peer|tail|replctl|statelog|replog-inject|peer-push|node-on-file|nbackup-unlock|db-new-guid|rcm-api" ;;
esac
