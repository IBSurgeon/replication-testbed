#!/usr/bin/env bash
# Module 93 (Linux): host actions for the tests of the hqcluster-node review
# 2026-10-22, stage 1 (pubtables_utf8, peer_removed_retention,
# reinit_concurrent, newguid_fail_repeat, firebirdlog_flood).
#
#   93-stage1.sh sql-utf8     --db FILE --sql-b64 B64 [--fb-root /opt/firebird] [--port 3050]
#                             (one isql script, connection charset UTF8: ok and the output)
#   93-stage1.sh count-tables --db FILE --tables-b64 B64 [--fb-root /opt/firebird] [--port 3050]
#                             (B64 of a JSON list of exact table names; each name is
#                             quoted, so a space or a non-Latin letter works; UTF8)
#   93-stage1.sh journal      --node-dir DIR [--file journal.jsonl] [--from 0] [--types T1,T2]
#                             [--db-id ID] [--grep TEXT] [--limit 500]
#                             (events of a node's JSON-lines file, as they are: journal.jsonl
#                             or db-file-ops.jsonl; --from -1: the line count only)
#   93-stage1.sh age-files    --glob 'PATTERN' --days N
#                             (file times N days back; the content is not touched)
#   93-stage1.sh fblog-inject --path FIREBIRD_LOG --count N [--tag tbflood]
#                             (N error entries in Firebird's firebird.log format, appended;
#                             the log is never cut and never created)
#
# Results come as one "TBRESULT <json>" line.
source "$(dirname "$0")/common.sh"

CMD="${1:-}"; shift || true
parse_args "$@"

FB_ROOT="$(arg fb_root /opt/firebird)"
PORT="$(arg port 3050)"

isql_utf8() { # DB SQL -> stdout
  printf '%s\n' "$2" | "$(fb_tool "$FB_ROOT" isql)" -q -pag 0 -ch UTF8 "localhost/$PORT:$1"
}

case "$CMD" in
  sql-utf8)
    # hostctl sql without a charset (NONE) mangles a non-Latin name; this
    # one connects with UTF8. ok: isql's exit code, and no error text.
    load_secrets
    DB="$(arg db)"; [[ -f "$DB" ]] || die "no database $DB"
    SQL="$(base64 -d <<<"$(arg sql_b64)")" || die "bad --sql-b64"
    set +e; out="$(isql_utf8 "$DB" "$SQL" 2>&1)"; ec=$?; set -e
    python3 -c '
import json, sys
t = sys.argv[2]
bad = "Statement failed" in t or "SQLSTATE =" in t
print("TBRESULT " + json.dumps({"ok": sys.argv[1] == "0" and not bad, "out": t[-4000:]}))' "$ec" "$out"
    ;;

  count-tables)
    # hostctl counts strips every space from a table name and then fails on
    # the whole database; this counts the named tables one by one.
    load_secrets
    DB="$(arg db)"; [[ -f "$DB" ]] || die "no database $DB"
    python3 - "$(fb_tool "$FB_ROOT" isql)" "localhost/$PORT:$DB" "$(arg tables_b64)" <<'PY'
import base64, json, subprocess, sys
isql, dsn, b64 = sys.argv[1:4]
names = json.loads(base64.b64decode(b64).decode("utf-8"))
out = []
for n in names:
    q = '"' + n.replace('"', '""') + '"'
    script = ("set heading off;\nset transaction read only;\n"
              "select 'RC|' || count(*) from " + q + ";\ncommit;\n")
    p = subprocess.run([isql, "-q", "-pag", "0", "-ch", "UTF8", dsn], input=script.encode("utf-8"),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    text = p.stdout.decode("utf-8", "replace")
    rows = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("RC|"):
            try:
                rows = int(line[3:].strip())
            except ValueError:
                pass
    out.append({"name": n, "rows": rows, "error": "" if rows is not None else text.strip()[-300:]})
print("TBRESULT " + json.dumps({"tables": out}))
PY
    ;;

  journal)
    # Raw events, unlike hostctl statelog (db_state and reinit_step of one
    # database only): delivery_remove_peer has no db_id, and the peer of a
    # reinit is in the fields.
    python3 - "$(arg node_dir /opt/hqclusternode)/$(arg file journal.jsonl)" "$(arg from 0)" "$(arg types)" \
      "$(arg db_id)" "$(arg grep)" "$(arg limit 500)" <<'PY'
import json, sys
path, start, types, dbid, grep, limit = sys.argv[1], int(sys.argv[2] or 0), sys.argv[3], sys.argv[4], sys.argv[5], int(sys.argv[6] or 500)
want = {t for t in types.split(",") if t}
try:
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
except FileNotFoundError:
    lines = []
if start < 0:                       # the line count only: where the next call starts
    print("TBRESULT " + json.dumps({"lines": len(lines), "events": []}))
    sys.exit(0)
if start > len(lines):              # compacted or rotated since: read it all
    start = 0
ev = []
for line in lines[start:]:
    if grep and grep not in line:
        continue
    try:
        e = json.loads(line)
    except ValueError:
        continue
    if not isinstance(e, dict):
        continue
    if want and e.get("type", e.get("op")) not in want:
        continue
    if dbid and e.get("db_id") != dbid:
        continue
    e.pop("before", None)
    e.pop("after", None)
    ev.append(e)
print("TBRESULT " + json.dumps({"lines": len(lines), "events": ev[-limit:]}))
PY
    ;;

  age-files)
    # Retention deletes a segment only when it is older than retention_days.
    G="$(arg glob)"; [[ -n "$G" ]] || die "--glob is required"
    D="$(arg days)"; [[ "$D" =~ ^[0-9]+$ ]] || die "--days N (whole days)"
    python3 - "$G" "$D" <<'PY'
import glob, json, os, sys, time
t = time.time() - int(sys.argv[2]) * 86400
fs = sorted(f for f in glob.glob(sys.argv[1]) if os.path.isfile(f))
for f in fs:
    os.utime(f, (t, t))
print("TBRESULT " + json.dumps({"aged": len(fs), "mtime": t, "files": fs}))
PY
    ;;

  fblog-inject)
    # Firebird writes "\n<host>\t<ctime>\n\t<message>\n\n" per entry. The
    # client addresses come from the documentation ranges (RFC 5737).
    P="$(arg path)"; [[ -n "$P" ]] || die "--path is required"
    [[ -f "$P" ]] || die "no file $P (the test appends to Firebird's own log, it never creates one)"
    python3 - "$P" "$(arg count 1)" "$(arg tag tbflood)" <<'PY'
import json, os, random, socket, sys, time
path, count, tag = sys.argv[1], int(sys.argv[2]), sys.argv[3]
host = socket.gethostname().split(".")[0] or "host"
nets = ("192.0.2.", "198.51.100.", "203.0.113.")
kinds = ("read errno = 104", "send errno = 32", "read errno = 110")
before = os.path.getsize(path)
chunks, last = [], ""
for i in range(count):
    client = tag + "-last" if i == count - 1 else "%s-ws%d" % (tag, i % 97)
    addr = "%s%d/%d" % (nets[i % 3], 1 + (i * 7) % 254, 1024 + random.randint(0, 64000))
    last = "INET/inet_error: %s, client host = %s, address = %s, user = TB_FLOOD" % (kinds[i % 3], client, addr)
    chunks.append("\n%s\t%s\n\t%s\n\n" % (host, time.ctime(), last))
with open(path, "a", encoding="utf-8") as f:   # append only: the log is never cut
    f.write("".join(chunks))
print("TBRESULT " + json.dumps({"path": path, "entries": count, "size_before": before,
                                "size_after": os.path.getsize(path), "last_line": last}))
PY
    ;;

  *) die "usage: 93-stage1.sh sql-utf8|count-tables|journal|age-files|fblog-inject" ;;
esac
