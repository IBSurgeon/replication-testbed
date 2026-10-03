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
#                             --action stop|kill|kill-stay [--timeout 900] [--delay SEC]
#                             (kill-stay: killed, and systemd does not start it again)
#   90-hostctl.sh nbackup-unlock --db FILE [--fb-root /opt/firebird] [--port 3050]
#   90-hostctl.sh nbackup-lock   --db FILE [--fb-root /opt/firebird] [--port 3050]
#   90-hostctl.sh fb-tool     --name nbackup --state off|on [--fb-root /opt/firebird]
#                             (off: the tool renamed away, so every call fails)
#                             (an operator's backup lock, nbackup -L)
#   90-hostctl.sh db-new-guid --db FILE [--fb-root /opt/firebird] [--port 3050] [--fb-service UNIT]
#   90-hostctl.sh rcm-api     --method GET --path /v1/alerts [--body-b64 B64] [--then-restart false]
#                             (RCM operator API on this host, Digest login from the secrets)
#   90-hostctl.sh rcm-web     --path '/partials/db-table?tab=m3'
#                             (an RCM web page part: login form, session cookie, HX-Request)
#   90-hostctl.sh write-probe --db FILE [--fb-root /opt/firebird] [--port 3050]
#                             (one committed row in TB_PROBE: fails on a read-only replica)
#   90-hostctl.sh hold-tx    --db FILE --seconds N [--fb-root /opt/firebird] [--port 3050]
#                             (a writing transaction left open N seconds in the background, then rolled back)
#   90-hostctl.sh node-conf-set --node-dir DIR --key firebird.replconf_valid_till --value-b64 B64
#                             (one key of node.json; "" removes it; restart the node to apply)
#   90-hostctl.sh file-put    --path FILE --content-b64 B64   (keeps FILE.tb-bak once)
#   90-hostctl.sh file-restore --path FILE                    (FILE.tb-bak back)
#   90-hostctl.sh file-copy   --from FILE --to FILE           (owner and mode kept; folders made)
#   90-hostctl.sh db-header   --db FILE [--fb-root /opt/firebird]
#                             (gstat -h: GUIDs, replication sequence, attributes)
#   90-hostctl.sh db-copy-locked --db FILE --to FILE [--fixup seq|noseq|none] [--replica read_only|{GUID}]
#                             (a copy as reinit makes one: nbackup -L, copy, -N, -F on the copy)
#   90-hostctl.sh guid-promote --db FILE [--mode shutdown|stop] [--legacy false] [--fb-service UNIT]
#                             (replica mode off, nbackup -L, -F without -SEQUENCE, delta removed)
#   90-hostctl.sh segment-guids --glob 'PATTERN[;PATTERN]'  (journal segments: GUID and number)
#   90-hostctl.sh replace-db  --db FILE --with FILE [--fb-service UNIT]
#                             (Firebird stopped, FILE replaced, Firebird started)
#   90-hostctl.sh attach      --db FILE [--fb-root /opt/firebird] [--port 3050]
#                             (one attach through the server: ok, or the error)
#   90-hostctl.sh clock       --shift-days N | --epoch E
#                             (N days from now with time sync off; E with time sync on)
#   90-hostctl.sh sql         --db FILE --sql-b64 B64 [--fb-root /opt/firebird] [--port 3050]
#                             (one isql script through the server: ok and the output)
#   90-hostctl.sh stat        --path PATH   (owner, group, mode; exists false when missing)
#   90-hostctl.sh remove-dir  --path DIR    (an empty folder only)
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

# db_header_json FILE: gstat -h as one JSON object. GUIDs as gstat prints
# them (HQbird 2.5/3.0 print another word order), without braces. No
# "Replication sequence" line means 0. master_guid is the "Replication
# master GUID" of an HQbird 2.5/3.0 replica.
db_header_json() {
  local out
  out="$("$(fb_tool "$FB_ROOT" gstat)" -h "$1" 2>&1)" || true
  python3 -c '
import json, re, sys
t = sys.stdin.read()
def f(rx):
    m = re.search(rx, t, re.I | re.M)
    return m.group(1).strip() if m else ""
def g(rx):
    return f(rx).strip("{}").upper()
seq = f(r"^\s*Replication sequence:?\s+(\d+)")
ok = bool(re.search(r"Database GUID", t, re.I))
print(json.dumps({"guid": g(r"^\s*Database GUID:?\s*(\S+)"),
                  "master_guid": g(r"^\s*Replication master GUID:?\s*(\S+)"),
                  "backup_guid": g(r"^\s*Database backup GUID:?\s*(\S+)"),
                  "repl_seq": int(seq) if seq else 0,
                  "attributes": f(r"^\s*Attributes\s+(.*)$"),
                  "ods": f(r"^\s*ODS version\s+(\S+)"),
                  "ok": ok, "tail": "" if ok else t[-400:]}))' <<<"$out"
}

# step NAME CMD...: run CMD, append {step, rc, sec, out} to $STEPS_F.
step() {
  local name="$1"; shift
  local t0=$SECONDS rc=0 out
  out="$("$@" 2>&1)" || rc=$?
  python3 -c 'import json, sys; print(json.dumps({"step": sys.argv[1], "rc": int(sys.argv[2]), "sec": int(sys.argv[3]), "out": sys.argv[4][-400:]}))' \
    "$name" "$rc" "$((SECONDS - t0))" "$out" >>"$STEPS_F"
  return $rc
}

fb_pid() { systemctl show -p MainPID --value "$1" 2>/dev/null || echo 0; }

wait_fb_port() { wait_until "${1:-120}" bash -c "exec 3<>/dev/tcp/127.0.0.1/$PORT" || true; }

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
    sleep "$(arg delay 0)"
    case "$(arg action stop)" in
      stop) "$NODE_DIR/hqclusternode" svc stop -config "$NODE_DIR/node.json" ;;
      kill) pkill -9 -f "$NODE_DIR/hqclusternode serve" || true ;;
      # A crash the node does not come back from by itself: systemd would
      # start it again 5 s later (Restart=on-failure); svc stop cancels that.
      kill-stay) pkill -9 -f "$NODE_DIR/hqclusternode serve" || true
                 sleep 1
                 "$NODE_DIR/hqclusternode" svc stop -config "$NODE_DIR/node.json" || true ;;
      *) die "--action stop|kill|kill-stay" ;;
    esac
    result "{\"fired\":\"$(arg event locked)\",\"action\":\"$(arg action stop)\",\"delta_exists\":$([[ -f "$F" ]] && echo true || echo false)}"
    ;;

  nbackup-unlock)
    load_secrets
    DB="$(arg db)"
    "$(fb_tool "$FB_ROOT" nbackup)" -N "localhost/$PORT:$DB"
    result "{\"unlocked\":\"$DB\",\"delta_left\":$([[ -f "$DB.delta" ]] && echo true || echo false)}"
    ;;

  fb-tool)
    N="$(arg name)"; [[ -n "$N" ]] || die "--name is required"
    T="$FB_ROOT/bin/$N"; [[ -e "$T" || -e "$T.tb-off" ]] || T="$FB_ROOT/$N"
    case "$(arg state)" in
      off) [[ -e "$T" ]] && mv -f "$T" "$T.tb-off" ;;
      on)  [[ -e "$T.tb-off" ]] && mv -f "$T.tb-off" "$T" ;;
      *) die "--state off|on" ;;
    esac
    result "{\"tool\":\"$T\",\"present\":$([[ -e "$T" ]] && echo true || echo false)}"
    ;;

  nbackup-lock)
    load_secrets
    DB="$(arg db)"
    "$(fb_tool "$FB_ROOT" nbackup)" -L "localhost/$PORT:$DB"
    result "{\"locked\":\"$DB\",\"delta\":$([[ -f "$DB.delta" ]] && echo true || echo false)}"
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

  hold-tx)
    # A transaction that has written and stays open: isql inserts a row, then
    # waits in "shell sleep" without committing, then rolls back and exits.
    load_secrets
    DB="$(arg db)"; [[ -f "$DB" ]] || die "no database $DB"
    SECS="$(arg seconds 120)"
    isql_q "$DB" "recreate table TB_HOLD (ID integer not null primary key); commit;" >/dev/null 2>&1 || true
    ISQL="$(fb_tool "$FB_ROOT" isql)"
    printf 'set autoddl off;\ninsert into TB_HOLD values (%s);\nshell sleep %s;\nrollback;\n' "$RANDOM" "$SECS" |
      setsid nohup "$ISQL" -q "localhost/$PORT:$DB" >/tmp/tb-hold-tx.log 2>&1 &
    echo $! >/tmp/tb-hold-tx.pid
    result "{\"holding\":\"$DB\",\"seconds\":$SECS,\"pid\":$!}"
    ;;

  node-conf-set)
    F="$NODE_DIR/node.json"; [[ -f "$F" ]] || die "no $F"
    K="$(arg key)"; V="$(printf '%s' "$(arg value_b64)" | base64 -d)"
    python3 - "$F" "$K" "$V" <<'PY'
import json, os, sys
path, key, value = sys.argv[1:4]
c = json.load(open(path, encoding="utf-8"))
d = c
parts = key.split(".")
for p in parts[:-1]:
    d = d.setdefault(p, {})
if value == "":
    d.pop(parts[-1], None)
else:
    d[parts[-1]] = value
tmp = path + ".tbtmp"
st = os.stat(path)
json.dump(c, open(tmp, "w", encoding="utf-8"), indent=2)
os.chmod(tmp, st.st_mode & 0o777)
os.chown(tmp, st.st_uid, st.st_gid)
os.replace(tmp, path)
PY
    result "{\"key\":\"$K\"}"
    ;;

  file-put)
    P="$(arg path)"; [[ -n "$P" ]] || die "--path is required"
    [[ ! -e "$P" || -e "$P.tb-bak" ]] || cp -p "$P" "$P.tb-bak"
    printf '%s' "$(arg content_b64)" | base64 -d >"$P.tbtmp"
    [[ ! -e "$P" ]] || { chown --reference="$P" "$P.tbtmp"; chmod --reference="$P" "$P.tbtmp"; }
    mv -f "$P.tbtmp" "$P"
    result "{\"put\":\"$P\"}"
    ;;

  file-restore)
    P="$(arg path)"; [[ -e "$P.tb-bak" ]] || die "no $P.tb-bak"
    mv -f "$P.tb-bak" "$P"
    result "{\"restored\":\"$P\"}"
    ;;

  file-copy)
    D="$(dirname "$(arg to)")"
    if [[ ! -d "$D" ]]; then
      mkdir -p "$D"; chown --reference="$(dirname "$(arg from)")" "$D" 2>/dev/null || true
    fi
    cp -p "$(arg from)" "$(arg to)"
    result "{\"copied\":\"$(arg to)\"}"
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

  write-probe)
    # Does the database take writes? A replica refuses them; a database that
    # became a master (promote) or a normal one takes them.
    load_secrets
    DB="$(arg db)"; [[ -f "$DB" ]] || die "no database $DB"
    set +e
    out="$(isql_q "$DB" "set bail on;
recreate table TB_PROBE (ID integer not null primary key);
commit;
insert into TB_PROBE (ID) values ($RANDOM);
commit;" 2>&1)"; ec=$?
    set -e
    python3 -c 'import json,sys; print("TBRESULT " + json.dumps({"written": sys.argv[1] == "0", "error": sys.argv[2][-400:]}))' "$ec" "$out"
    ;;

  rcm-web)
    # The page parts (/partials/*) take only a web session, not Digest: log
    # in with the form, then ask as the page does (HX-Request: true).
    read_secrets
    python3 - "$(arg path /partials/db-table?tab=m3)" <<'PY'
import http.cookiejar, json, os, sys, urllib.error, urllib.parse, urllib.request
path = sys.argv[1]
user, pw = os.environ.get("TB_RCM_USER", ""), os.environ.get("TB_RCM_PASSWORD", "")
if not user or not pw:
    sys.exit("TB_RCM_USER / TB_RCM_PASSWORD are not set (secrets rcm_user, rcm_password)")
base = "http://127.0.0.1:7444"
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
form = urllib.parse.urlencode({"username": user, "password": pw}).encode()
try:
    r = op.open(urllib.request.Request(base + "/login", data=form, method="POST"), timeout=30)
    landed = r.geturl()
except urllib.error.HTTPError as e:
    landed = e.geturl() or ""
if not any(c.name == "rcm_session" for c in jar):
    sys.exit(f"RCM login failed (landed on {landed})")
req = urllib.request.Request(base + path, headers={"HX-Request": "true"})
try:
    r = op.open(req, timeout=60)
    st, raw, ctype = r.status, r.read(), r.headers.get("Content-Type", "")
except urllib.error.HTTPError as e:
    st, raw, ctype = e.code, e.read(), e.headers.get("Content-Type", "")
text = raw.decode("utf-8", errors="replace")
body = json.loads(text) if "json" in ctype and text.strip() else text
print("TBRESULT " + json.dumps({"status": st, "body": body}))
PY
    ;;

  db-header)
    load_secrets
    DB="$(arg db)"; [[ -f "$DB" ]] || die "no database $DB"
    result "$(db_header_json "$DB")"
    ;;

  db-copy-locked)
    # A copy of a live database made the way reinit makes one: nbackup -L,
    # file copy, nbackup -N; then nbackup -F on the copy with -SEQUENCE
    # (seq: the GUID and sequence of the source), without it (noseq: a new
    # GUID, sequence 0) or not at all (none: left stalled). --replica puts
    # the copy in replica mode (read_only; {GUID} on HQbird 2.5/3.0).
    load_secrets
    DB="$(arg db)"; TO="$(arg to)"
    [[ -f "$DB" && -n "$TO" ]] || die "--db (an existing file) and --to are required"
    NB="$(fb_tool "$FB_ROOT" nbackup)"; GFIX="$(fb_tool "$FB_ROOT" gfix)"
    mkdir -p "$(dirname "$TO")"; rm -f "$TO" "$TO.delta"
    "$NB" -L "localhost/$PORT:$DB"
    ec=0; cp -f "$DB" "$TO" || ec=1
    "$NB" -N "localhost/$PORT:$DB"
    (( ec == 0 )) || { rm -f "$TO"; die "copy under nbackup lock failed"; }
    chown --reference="$DB" "$TO"; chmod --reference="$DB" "$TO"
    case "$(arg fixup seq)" in
      seq)   "$NB" -F "localhost/$PORT:$TO" -SEQUENCE ;;
      noseq) "$NB" -F "localhost/$PORT:$TO" ;;
      none)  ;;
      *) die "--fixup seq|noseq|none" ;;
    esac
    R="$(arg replica)"
    [[ -z "$R" ]] || "$GFIX" -replica "$R" "localhost/$PORT:$TO"
    result "{\"copy\":\"$TO\",\"header\":$(db_header_json "$TO")}"
    ;;

  guid-promote)
    # A replica database becomes a master with a GUID of its own, in place
    # and without a copy: replica mode off; the database in single shutdown
    # through the running server (--mode shutdown) or Firebird stopped
    # (--mode stop); nbackup -L; nbackup -F without -SEQUENCE (new GUID,
    # replication sequence 0); the delta removed; validated; online again.
    # Between -L and -F nothing may write: -F does not carry the delta over.
    # A failure after -L and before -F unlocks with -N (the delta merged).
    load_secrets
    DB="$(arg db)"; [[ -f "$DB" ]] || die "no database $DB"
    MODE="$(arg mode shutdown)"; LEG="$(arg legacy false)"
    UNIT="$(arg fb_service)"; [[ -n "$UNIT" ]] || UNIT="$(detect_fb_unit)"
    [[ -n "$UNIT" ]] || die "cannot find the Firebird unit"
    GFIX="$(fb_tool "$FB_ROOT" gfix)"; NB="$(fb_tool "$FB_ROOT" nbackup)"
    REMOTE="localhost/$PORT:$DB"; SPEC="$REMOTE"; [[ "$MODE" == stop ]] && SPEC="$DB"
    STEPS_F="$(mktemp)"; ok=true; locked=false; fixed=false
    before="$(db_header_json "$DB")"; pid0="$(fb_pid "$UNIT")"
    off=none; [[ "$LEG" == true ]] && off='{}'
    step replica_off "$GFIX" -replica "$off" "$REMOTE" || ok=false
    if $ok; then
      if [[ "$MODE" == stop ]]; then step stop systemctl stop "$UNIT" || ok=false
      else step shutdown "$GFIX" -shut single -force 0 "$SPEC" || ok=false; fi
    fi
    if $ok; then if step lock "$NB" -L "$SPEC"; then locked=true; else ok=false; fi; fi
    if $ok; then if step fixup "$NB" -F "$SPEC"; then fixed=true; else ok=false; fi; fi
    middle="$(db_header_json "$DB")"
    delta_existed=false; [[ -f "$DB.delta" ]] && delta_existed=true
    if $fixed; then rm -f "$DB.delta"
    elif $locked; then step unlock "$NB" -N "$SPEC" || true; fi
    if $ok; then step validate "$GFIX" -v -full "$SPEC" || ok=false; fi
    if [[ "$MODE" == stop ]]; then step start systemctl start "$UNIT" || ok=false; wait_fb_port 120
    else step online "$GFIX" -online "$SPEC" || ok=false; fi
    after="$(db_header_json "$DB")"; pid1="$(fb_pid "$UNIT")"
    delta_left=false; [[ -f "$DB.delta" ]] && delta_left=true
    python3 - "$STEPS_F" "$ok" "$MODE" "$before" "$middle" "$after" "$pid0" "$pid1" \
      "$delta_existed" "$delta_left" <<'PY'
import json, sys
a = sys.argv
steps = [json.loads(l) for l in open(a[1]) if l.strip()]
print("TBRESULT " + json.dumps({"ok": a[2] == "true", "mode": a[3], "steps": steps,
    "before": json.loads(a[4]), "middle": json.loads(a[5]), "after": json.loads(a[6]),
    "pid_before": a[7], "pid_after": a[8],
    "delta_existed": a[9] == "true", "delta_left": a[10] == "true"}))
PY
    rm -f "$STEPS_F"
    ;;

  segment-guids)
    # Journal segment headers (FBCHANGELOG: Firebird 4/5, FBREPLLOG: HQbird
    # 3.0): the GUID of the database that wrote it and its number.
    python3 - "$(arg glob)" <<'PY'
import glob, json, os, struct, sys, uuid
out = []
for pat in sys.argv[1].split(";"):
    for f in sorted(glob.glob(pat)):
        if not os.path.isfile(f):
            continue
        try:
            with open(f, "rb") as fh:
                b = fh.read(64)
        except OSError as e:
            out.append({"file": f, "error": str(e)})
            continue
        sig = b[:12].split(b"\0")[0].decode("ascii", "replace")
        if sig not in ("FBCHANGELOG", "FBREPLLOG") or len(b) < 40:
            continue
        out.append({"file": f, "signature": sig,
                    "guid": str(uuid.UUID(bytes_le=bytes(b[16:32]))).upper(),
                    "sequence": struct.unpack_from("<Q", b, 32)[0]})
print("TBRESULT " + json.dumps(out))
PY
    ;;

  replace-db)
    # What an operator does to recreate a replica by hand: Firebird stopped,
    # the file replaced by --with (owner and mode of the old one), started.
    load_secrets
    DB="$(arg db)"; W="$(arg with)"
    [[ -f "$DB" && -f "$W" ]] || die "--db and --with must be existing files"
    UNIT="$(arg fb_service)"; [[ -n "$UNIT" ]] || UNIT="$(detect_fb_unit)"
    [[ -n "$UNIT" ]] || die "cannot find the Firebird unit"
    chown --reference="$DB" "$W"; chmod --reference="$DB" "$W"
    systemctl stop "$UNIT"
    mv -f "$W" "$DB"; rm -f "$DB.delta"
    systemctl start "$UNIT"
    wait_fb_port 120
    result "{\"replaced\":\"$DB\",\"header\":$(db_header_json "$DB")}"
    ;;

  attach)
    # One attach through the server, as a client makes it: HQbird 2.5/3.0
    # ask the replconf plugin on every attach, so it fails while the plugin
    # refuses its file.
    load_secrets
    set +e; out="$(isql_q "$(arg db)" "set heading off; select 'TB_ATTACH_OK' from rdb\$database;" 2>&1)"; ec=$?; set -e
    python3 -c 'import json, sys; t = sys.argv[2]; print("TBRESULT " + json.dumps({"ok": sys.argv[1] == "0" and "TB_ATTACH_OK" in t, "out": "" if "TB_ATTACH_OK" in t else t[-300:]}))' "$ec" "$out"
    ;;

  sql)
    # One isql script against a database through the server, for tests that
    # write or read rows of their own. ok is isql's exit code.
    load_secrets
    DB="$(arg db)"; [[ -f "$DB" ]] || die "no database $DB"
    SQL="$(base64 -d <<<"$(arg sql_b64)")" || die "bad --sql-b64"
    set +e; out="$(isql_q "$DB" "$SQL" 2>&1)"; ec=$?; set -e
    python3 -c 'import json,sys; print("TBRESULT " + json.dumps({"ok": sys.argv[1] == "0", "out": sys.argv[2][-4000:]}))' "$ec" "$out"
    ;;

  stat)
    P="$(arg path)"
    if [[ -e "$P" || -L "$P" ]]; then
      result "$(stat -c '{"exists":true,"owner":"%U","group":"%G","mode":"%a","type":"%F"}' "$P")"
    else
      result '{"exists":false}'
    fi
    ;;

  remove-dir)
    P="$(arg path)"; [[ -n "$P" && "$P" != "/" ]] || die "bad --path"
    if [[ -d "$P" ]]; then rmdir "$P" || die "cannot remove $P (not empty?)"; fi
    result '{"removed":true}'
    ;;

  clock)
    # The host clock: --shift-days N moves it N days from now (time sync
    # off); --epoch E sets it to E and turns time sync on again.
    if [[ -n "$(arg epoch)" ]]; then
      date -s "@$(arg epoch)" >/dev/null
      timedatectl set-ntp true 2>/dev/null || true
    else
      N="$(arg shift_days)"; [[ "$N" =~ ^-?[0-9]+$ ]] || die "--shift-days N or --epoch E"
      timedatectl set-ntp false 2>/dev/null || true
      date -s "@$(( $(date +%s) + N * 86400 ))" >/dev/null
    fi
    result "{\"date\":\"$(date +%F)\",\"epoch\":$(date +%s),\"ntp\":\"$(timedatectl show -p NTP --value 2>/dev/null || echo unknown)\"}"
    ;;

  *) die "usage: 90-hostctl.sh secure-file|node-api|node-svc|fb-svc|counts|limbo|files|remove-file|block-peer|unblock-peer|tail|replctl|statelog|replog-inject|peer-push|node-on-file|nbackup-unlock|nbackup-lock|fb-tool|db-new-guid|rcm-api|rcm-web|write-probe|hold-tx|node-conf-set|file-put|file-restore|file-copy|db-header|db-copy-locked|guid-promote|segment-guids|replace-db|attach|clock" ;;
esac
