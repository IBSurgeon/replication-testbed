#!/usr/bin/env bash
# Module 91 (Linux): host actions for the tests of the node's state journal,
# replica file move and replication.log rotation (restart_replay, move_crash,
# logrotate_tail, reinit_stale_line).
#
#   91-journal.sh journal-info   --node-dir DIR
#                                (journal.jsonl: size, lines, first and last ts; its .bak files;
#                                 state.json: size, mtime, request replay entries; the host time)
#   91-journal.sh journal-events --node-dir DIR [--db-id ID] [--types alert,db_move] [--code CODE]
#                                [--from LINE]   (--from -1: the line count only)
#   91-journal.sh journal-append --node-dir DIR --line-b64 B64 [--force false]
#                                (one JSON event at the end of journal.jsonl; refused while the node runs)
#   91-journal.sh snapshot-block --node-dir DIR --state on|off
#                                (on: a folder named state.json.tmp, so every write of state.json fails)
#   91-journal.sh kill-when      --node-dir DIR --on witness|file|now [--db-id ID] [--path FILE]
#                                [--timeout 120] [--delay 0] [--action kill|stop]
#                                (witness: the node journals a db_move start of --db-id;
#                                 file: --path exists; now: at once)
#   91-journal.sh path-info      --paths 'P1;P2'   (exists, size, type, owner, group, mode of each)
#   91-journal.sh file-partial   --from FILE --to FILE [--fraction 0.5]
#                                (the first part of FILE; owner and mode of FILE; folders made)
#   91-journal.sh file-move      --from PATH --to PATH
#                                (one rename; folders made with the owner of the source folder)
#   91-journal.sh remove-path    --path PATH [--force false]
#                                (test artefacts only: "tb-" in the path, or *.hqc-move-tmp;
#                                 a folder that holds a *.fdb file needs --force true)
#   91-journal.sh replog-state   --path LOG --node-dir DIR
#                                (size, <log>.drained, <log>.rotate.lock, the apply watcher's
#                                 replication.log.offset, archives this module made)
#   91-journal.sh replog-rotate  --path LOG --mode drain|truncate|rename|copytruncate [--bytes N]
#                                (drain: the node's head drain under <log>.rotate.lock + <log>.drained;
#                                 truncate: the node's copy-then-truncate, same bookkeeping;
#                                 rename: logrotate "create"; copytruncate: logrotate "copytruncate")
#   91-journal.sh replog-cleanup --path LOG [--lock-created false] [--keep false]
#   91-journal.sh inject-while   --path LOG --db FILE --message-b64 B64 --watch 'P1;P2'
#                                [--role replica] [--level ERROR] [--burst 3] [--every 2]
#                                [--timeout 1800] [--max 100] [--gap 10] [--first-timeout 0]
#                                (replication.log blocks while one of the watched files exists;
#                                 ends when none has existed for --gap seconds, after --max
#                                 blocks, or when none appeared within --first-timeout)
#
# Results come as one "TBRESULT <json>" line.
source "$(dirname "$0")/common.sh"

CMD="${1:-}"; shift || true
parse_args "$@"

NODE_DIR="$(arg node_dir /opt/hqclusternode)"

node_running() { pgrep -f "$NODE_DIR/hqclusternode serve" >/dev/null 2>&1; }

case "$CMD" in
  journal-info)
    python3 - "$NODE_DIR" <<'PY'
import calendar, glob, json, os, re, sys, time

d = sys.argv[1]
TS = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)$")


def epoch(ts):
    m = TS.match(ts or "")
    if not m:
        return 0
    y, mo, dd, h, mi, s = (int(m.group(i)) for i in range(1, 7))
    t = calendar.timegm((y, mo, dd, h, mi, s, 0, 0, 0)) + float(m.group(7) or 0)
    z = m.group(8)
    if z != "Z":
        off = int(z[1:3]) * 3600 + int(z[4:6]) * 60
        t -= off if z[0] == "+" else -off
    return t


def finfo(p):
    try:
        st = os.stat(p)
        return {"path": p, "exists": True, "size": st.st_size, "mtime": st.st_mtime}
    except FileNotFoundError:
        return {"path": p, "exists": False, "size": 0, "mtime": 0}


j = finfo(os.path.join(d, "journal.jsonl"))
lines, first, last = 0, "", ""
if j["exists"]:
    with open(j["path"], "rb") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            lines += 1
            try:
                ts = json.loads(raw).get("ts", "")
            except ValueError:
                continue
            if not first:
                first = ts
            last = ts
j.update(lines=lines, first_ts=first, last_ts=last, first_epoch=epoch(first), last_epoch=epoch(last))
baks = [finfo(p) for p in sorted(glob.glob(os.path.join(d, "journal.jsonl.*.bak")))]

s = finfo(os.path.join(d, "state.json"))
replies = {"key": "", "count": 0, "oldest_age_sec": 0, "untimed": 0}
schema = None
if s["exists"]:
    try:
        with open(s["path"], encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        doc = {}
        s["error"] = str(e)[:200]
    schema = doc.get("schema_version")
    for key in ("request_replies", "request_replay"):
        if isinstance(doc.get(key), dict):
            rr = doc[key]
            ages = [time.time() - epoch(v.get("at")) for v in rr.values()
                    if isinstance(v, dict) and epoch(v.get("at"))]
            replies = {"key": key, "count": len(rr), "oldest_age_sec": int(max(ages)) if ages else 0,
                       "untimed": len(rr) - len(ages)}
            break
tmp = os.path.join(d, "state.json.tmp")
print("TBRESULT " + json.dumps({
    "journal": j, "baks": baks, "bak_count": len(baks), "state": s, "schema_version": schema,
    "replies": replies, "tmp_blocked": os.path.isdir(tmp), "now": time.time()}))
PY
    ;;

  journal-events)
    python3 - "$NODE_DIR/journal.jsonl" "$(arg from 0)" "$(arg db_id)" "$(arg types)" "$(arg code)" <<'PY'
import json, sys

path, start, dbid, types, code = sys.argv[1], int(sys.argv[2] or 0), sys.argv[3], sys.argv[4], sys.argv[5]
want = {t for t in types.split(",") if t}
try:
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
except FileNotFoundError:
    lines = []
if start < 0:
    print("TBRESULT " + json.dumps({"lines": len(lines), "events": []}))
    sys.exit(0)
compacted = start > len(lines)
if compacted:
    start = 0
ev = []
for i, line in enumerate(lines[start:], start):
    if dbid and dbid not in line:
        continue
    try:
        e = json.loads(line)
    except ValueError:
        continue
    if dbid and e.get("db_id") != dbid:
        continue
    if want and e.get("type") not in want:
        continue
    fields = e.get("fields") or {}
    if code and fields.get("code") != code:
        continue
    ev.append({"line": i, "ts": e.get("ts"), "type": e.get("type"), "db_id": e.get("db_id"), "fields": fields})
print("TBRESULT " + json.dumps({"lines": len(lines), "compacted": compacted, "events": ev[-500:]}))
PY
    ;;

  journal-append)
    if node_running && [[ "$(arg force false)" != true ]]; then
      die "the node runs: stop it first (or --force true)"
    fi
    python3 - "$NODE_DIR" "$(arg line_b64)" <<'PY'
import base64, datetime, json, os, sys

d, b64 = sys.argv[1], sys.argv[2]
try:
    ev = json.loads(base64.b64decode(b64).decode())
except ValueError as e:
    sys.exit(f"bad --line-b64: {e}")
if not isinstance(ev, dict) or not ev.get("type"):
    sys.exit("the event needs a type")
if not ev.get("ts"):
    ev["ts"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
p = os.path.join(d, "journal.jsonl")
new = not os.path.exists(p)
lead = ""
if not new and os.path.getsize(p) > 0:
    with open(p, "rb") as f:
        f.seek(-1, os.SEEK_END)
        if f.read(1) != b"\n":
            lead = "\n"         # a line cut by a crash must not swallow this one
with open(p, "a", encoding="utf-8") as f:
    f.write(lead + json.dumps(ev, separators=(",", ":")) + "\n")
    f.flush()
    os.fsync(f.fileno())
if new:
    os.chmod(p, 0o640)
    ref = os.path.join(d, "state.json")
    if os.path.exists(ref):
        st = os.stat(ref)
        os.chown(p, st.st_uid, st.st_gid)
print("TBRESULT " + json.dumps({"path": p, "created": new, "event": ev}))
PY
    ;;

  snapshot-block)
    T="$NODE_DIR/state.json.tmp"
    case "$(arg state)" in
      on)  [[ -f "$T" ]] && rm -f "$T"
           mkdir -p "$T" ;;
      off) [[ -d "$T" ]] && rmdir "$T" ;;
      *) die "--state on|off" ;;
    esac
    result "{\"blocked\":$([[ -d "$T" ]] && echo true || echo false)}"
    ;;

  kill-when)
    t0=$SECONDS
    python3 - "$NODE_DIR/journal.jsonl" "$(arg on witness)" "$(arg db_id)" "$(arg path)" "$(arg timeout 120)" <<'PY' || die "no $(arg on witness) event within $(arg timeout 120)s"
import os, sys, time

jpath, on, dbid, path, timeout = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], float(sys.argv[5])
end = time.time() + timeout
if on == "now":
    sys.exit(0)
if on == "file":
    if not path:
        sys.exit("--path is required")
    while time.time() < end:
        if os.path.exists(path):
            sys.exit(0)
        time.sleep(0.005)
    sys.exit(3)
if on != "witness" or not dbid:
    sys.exit("--on witness needs --db-id")
pos = os.path.getsize(jpath) if os.path.exists(jpath) else 0
buf = b""
while time.time() < end:
    try:
        size = os.path.getsize(jpath)
    except FileNotFoundError:
        size = 0
    if size < pos:
        pos, buf = 0, b""      # rotated: read the new journal from the top
    if size > pos:
        with open(jpath, "rb") as f:
            f.seek(pos)
            chunk = f.read(size - pos)
        pos += len(chunk)
        buf += chunk
        *done, buf = buf.split(b"\n")
        for line in done:
            if b'"db_move"' in line and b'"start"' in line and dbid.encode() in line:
                sys.exit(0)
    time.sleep(0.005)
sys.exit(3)
PY
    sleep "$(arg delay 0)"
    case "$(arg action kill)" in
      kill) pkill -9 -f "$NODE_DIR/hqclusternode serve" || true ;;
      stop) "$NODE_DIR/hqclusternode" svc stop -config "$NODE_DIR/node.json" || true ;;
      *) die "--action kill|stop" ;;
    esac
    result "{\"fired\":\"$(arg on witness)\",\"action\":\"$(arg action kill)\",\"sec\":$((SECONDS - t0))}"
    ;;

  path-info)
    python3 - "$(arg paths)" <<'PY'
import grp, json, os, pwd, stat, sys


def name(fn, i):
    try:
        return fn(i)[0]
    except KeyError:
        return str(i)


out = []
for p in [x for x in sys.argv[1].split(";") if x]:
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        out.append({"path": p, "exists": False})
        continue
    kind = "dir" if stat.S_ISDIR(st.st_mode) else "file" if stat.S_ISREG(st.st_mode) else \
        "link" if stat.S_ISLNK(st.st_mode) else "other"
    out.append({"path": p, "exists": True, "type": kind, "size": st.st_size, "mtime": st.st_mtime,
                "owner": name(pwd.getpwuid, st.st_uid), "group": name(grp.getgrgid, st.st_gid),
                "mode": format(st.st_mode & 0o7777, "o")})
print("TBRESULT " + json.dumps(out))
PY
    ;;

  file-partial|file-move)
    python3 - "$CMD" "$(arg from)" "$(arg to)" "$(arg fraction 0.5)" <<'PY'
import json, os, sys

cmd, src, dst, frac = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
if not src or not dst:
    sys.exit("--from and --to are required")
if not os.path.exists(src):
    sys.exit(f"no {src}")
if os.path.lexists(dst):
    sys.exit(f"{dst} exists")
# Missing folders of the destination get the owner and mode of the source's folder.
ref = os.stat(os.path.dirname(os.path.abspath(src)))
missing = []
d = os.path.dirname(os.path.abspath(dst))
while not os.path.isdir(d):
    missing.append(d)
    d = os.path.dirname(d)
for d in reversed(missing):
    os.mkdir(d, ref.st_mode & 0o7777)
    os.chown(d, ref.st_uid, ref.st_gid)
st = os.stat(src)
out = {"from": src, "to": dst, "made": list(reversed(missing))}
if cmd == "file-move":
    os.rename(src, dst)
else:
    n = int(st.st_size * frac)
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        left = n
        while left > 0:
            b = fi.read(min(left, 1 << 20))
            if not b:
                break
            fo.write(b)
            left -= len(b)
    os.chmod(dst, st.st_mode & 0o7777)
    os.chown(dst, st.st_uid, st.st_gid)
    out.update(bytes=n, size=st.st_size)
print("TBRESULT " + json.dumps(out))
PY
    ;;

  remove-path)
    python3 - "$(arg path)" "$(arg force false)" <<'PY'
import json, os, shutil, sys

p, force = sys.argv[1], sys.argv[2] == "true"
if not p or os.path.abspath(p) in ("/", os.path.abspath(os.sep)):
    sys.exit("bad --path")
if "tb-" not in p and not p.endswith(".hqc-move-tmp"):
    sys.exit(f"{p} is not a test artefact (no 'tb-' in the path, not *.hqc-move-tmp)")
if not os.path.lexists(p):
    print("TBRESULT " + json.dumps({"path": p, "removed": False}))
    sys.exit(0)
if os.path.isdir(p) and not os.path.islink(p):
    dbs = [os.path.join(r, f) for r, _, fs in os.walk(p) for f in fs if f.lower().endswith(".fdb")]
    if dbs and not force:
        sys.exit(f"{p} holds databases {dbs[:3]}: --force true to remove it")
    shutil.rmtree(p)
else:
    os.remove(p)
print("TBRESULT " + json.dumps({"path": p, "removed": True}))
PY
    ;;

  replog-state)
    python3 - "$(arg path)" "$NODE_DIR" <<'PY'
import glob, json, os, sys, time

log, nd = sys.argv[1], sys.argv[2]


def read_int(p):
    try:
        with open(p) as f:
            return int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


off = {"exists": False}
op = os.path.join(nd, "replication.log.offset")
if os.path.exists(op):
    with open(op) as f:
        parts = f.read().split()
    off = {"exists": True, "offset": int(parts[0]) if parts else None,
           "drained": int(parts[1]) if len(parts) > 1 else None, "mtime": os.path.getmtime(op)}
print("TBRESULT " + json.dumps({
    "path": log, "size": os.path.getsize(log) if os.path.exists(log) else -1,
    "drained": read_int(log + ".drained"), "lock": os.path.exists(log + ".rotate.lock"),
    "offset": off, "archives": sorted(glob.glob(log + ".tb-*")), "now": time.time()}))
PY
    ;;

  replog-rotate)
    python3 - "$(arg path)" "$(arg mode)" "$(arg bytes 0)" <<'PY'
import fcntl, json, os, shutil, sys, time

log, mode, nbytes = sys.argv[1], sys.argv[2], int(sys.argv[3] or 0)
if mode not in ("drain", "truncate", "rename", "copytruncate"):
    sys.exit("--mode drain|truncate|rename|copytruncate")
st = os.stat(log)
stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
arch = f"{log}.tb-{stamp}-{mode}"
i = 0
while os.path.exists(arch):
    i += 1
    arch = f"{log}.tb-{stamp}-{i}-{mode}"


def read_drained():
    try:
        with open(log + ".drained") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def add_drained(n):
    # The node's addDrained: one decimal number, replaced through tmp + rename.
    if n <= 0:
        return
    path = log + ".drained"
    new = not os.path.exists(path)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(f"{(read_drained() or 0) + n}\n")
    os.chmod(tmp, 0o644)
    os.rename(tmp, path)
    if new:
        os.chown(path, st.st_uid, st.st_gid)


def shift_left(drop):
    # The node's shiftLeft: move the rest down, copy until the size is stable
    # (Firebird may append meanwhile), truncate.
    with open(log, "r+b") as f:
        size = os.fstat(f.fileno()).st_size
        if size <= drop:
            f.truncate(0)
            return size
        dst, src, idle = 0, drop, 0
        while idle < 3:
            f.seek(src)
            b = f.read(256 * 1024)
            if b:
                f.seek(dst)
                f.write(b)
                dst += len(b)
                src += len(b)
                idle = 0
                continue
            if os.fstat(f.fileno()).st_size <= src:
                break
            idle += 1
        f.truncate(dst)
        return drop


res = {"mode": mode, "size_before": st.st_size, "drained_before": read_drained(), "archive": arch}
if mode in ("drain", "truncate"):
    lock = log + ".rotate.lock"
    created = not os.path.exists(lock)
    fd = os.open(lock, os.O_RDONLY | os.O_CREAT, 0o644)
    if created:
        os.chown(lock, st.st_uid, st.st_gid)
    end = time.time() + 30
    while True:                 # a tail holds it shared for one read
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() > end:
                sys.exit("the rotate lock stays busy")
            time.sleep(0.05)
    try:
        if mode == "drain":
            size = os.path.getsize(log)
            drop = min(nbytes if nbytes > 0 else size // 2, size)
            with open(log, "rb") as fi, open(arch, "wb") as fo:
                fo.write(fi.read(drop))
            removed = shift_left(drop)
        else:
            shutil.copyfile(log, arch)
            with open(log, "r+b") as f:
                removed = max(os.path.getsize(arch), os.fstat(f.fileno()).st_size)
                f.truncate(0)
        add_drained(removed)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    res.update(dropped=removed, lock_created=created)
elif mode == "rename":
    os.rename(log, arch)
    fd = os.open(log, os.O_CREAT | os.O_EXCL | os.O_WRONLY, st.st_mode & 0o7777)
    os.close(fd)
    os.chown(log, st.st_uid, st.st_gid)
    os.chmod(log, st.st_mode & 0o7777)
    res.update(dropped=st.st_size, lock_created=False)
else:
    shutil.copyfile(log, arch)
    with open(log, "r+b") as f:
        removed = os.fstat(f.fileno()).st_size
        f.truncate(0)
    res.update(dropped=removed, lock_created=False)
os.chmod(arch, 0o640)
res.update(size_after=os.path.getsize(log), drained_after=read_drained())
print("TBRESULT " + json.dumps(res))
PY
    ;;

  replog-cleanup)
    python3 - "$(arg path)" "$(arg lock_created false)" "$(arg keep false)" <<'PY'
import glob, json, os, sys

log, lock_created, keep = sys.argv[1], sys.argv[2] == "true", sys.argv[3] == "true"
gone = []
if not keep:
    for p in glob.glob(log + ".tb-*"):
        os.remove(p)
        gone.append(p)
if lock_created and os.path.exists(log + ".rotate.lock"):
    os.remove(log + ".rotate.lock")
    gone.append(log + ".rotate.lock")
print("TBRESULT " + json.dumps({"removed": gone}))
PY
    ;;

  inject-while)
    python3 - "$(arg path)" "$(arg db)" "$(arg role replica)" "$(arg level ERROR)" "$(arg message_b64)" \
      "$(arg watch)" "$(arg burst 3)" "$(arg every 2)" "$(arg timeout 1800)" "$(arg max 100)" "$(arg gap 10)" \
      "$(arg first_timeout 0)" <<'PY'
import base64, json, os, socket, sys, time

log, db, role, level, msg, watch = sys.argv[1:7]
burst, every, timeout, most = int(sys.argv[7]), float(sys.argv[8]), float(sys.argv[9]), int(sys.argv[10])
gap = float(sys.argv[11])       # the end: none of the files for this long (a short gap between them is not)
first_timeout = float(sys.argv[12]) or timeout      # no watched file within this long: give up
msg = base64.b64decode(msg).decode()
paths = [p for p in watch.split(";") if p]
if not paths:
    sys.exit("--watch is required")
host = socket.gethostname().split(".")[0] or "host"


def write(k):
    text = ""
    for _ in range(k):
        text += f"{host} ({role}) {time.strftime('%a %b %d %H:%M:%S %Y')}\n\tDatabase: {db}\n\t{level}: {msg}\n\n"
    with open(log, "a", encoding="utf-8") as f:
        f.write(text)


t0 = time.time()
end = t0 + timeout
seen, n, nxt, first, ex, last_ex, last_write = False, 0, 0.0, 0.0, False, 0.0, 0.0
while time.time() < end:
    ex = any(os.path.exists(p) for p in paths)
    now = time.time()
    if ex:
        last_ex = now
    # Blocks are written only while a watched file exists.
    if ex and not seen:
        seen, first = True, now
        k = min(burst, most)
        write(k)
        n += k
        nxt, last_write = now + every, now
    elif ex and now >= nxt and n < most:
        write(1)
        n += 1
        nxt, last_write = now + every, now
    if seen and not ex and now - last_ex >= gap:
        break
    if n >= most or (not seen and now - t0 >= first_timeout):
        break
    time.sleep(0.02)
print("TBRESULT " + json.dumps({"seen": seen, "injected": n, "gone": seen and not ex,
                                "watched_sec": round(last_ex - first, 1) if seen else 0,
                                "last_write_before_gone": round(last_ex - last_write, 2) if seen else None}))
PY
    ;;

  *) die "usage: 91-journal.sh journal-info|journal-events|journal-append|snapshot-block|kill-when|path-info|file-partial|file-move|remove-path|replog-state|replog-rotate|replog-cleanup|inject-while ..." ;;
esac
