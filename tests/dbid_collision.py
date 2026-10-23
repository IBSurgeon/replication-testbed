"""A second file with the same db_id (hqcluster-node review 2026-10-22, E-01).

The node makes a db_id from the path: lower case, "/" becomes "__". So on
Linux <dir>/EMPLOYEE.fdb has the same db_id as the tracked <dir>/employee.fdb,
and it sorts before it. The old scansync kept the first path in sort order:
the copy took the tracked database's record and its conf section. Now the
tracked path wins, the copy is refused, and the alert db_id_collision names
it.

Steps, on the master, with one test database:
  1. copy the database file to a colliding name (hostctl file-copy) and run
     POST /v1/scansync: the answer lists the collision (kept = the tracked
     path, dropped = the copy); the record keeps its path and is not
     ORPHANED; no record and no conf line has the copy; the alert
     db_id_collision is raised and names the copy;
  2. restart the node (unless --no-restart): the same after the start-up scan;
  3. a short load: the replicas match the master;
  4. remove the copy, scansync again: no collision, the record is unchanged.
     The node does not withdraw this alert by itself; the test notes whether
     it stays (PASS either way).

The colliding name: on Linux the file name in upper case, in the same
folder; on Windows (no case on the file system) <parent>/<folder>__<file>.
The Windows name sorts after the tracked path, so there the old code passes
too: the run notes it.

The copy is removed and the alert cleared at the end.
"""
import time
import urllib.parse

from tblib import ops
from tblib.cluster import TbError, log
from tblib.results import Results

from ._common import converge_and_record, norm
from .disasters import node_svc, wait_node

HELP = "a second file with the same db_id: the tracked path wins, alert db_id_collision (node E-01)"

TAG = "dbidcoll"
CODE = "db_id_collision"


def add_args(p):
    p.add_argument("--db", default="", help="test database (db_id or folder name); default: the first")
    p.add_argument("--load-seconds", type=int, default=45, help="seconds of write load before the compare")
    p.add_argument("--timeout", type=int, default=600, help="seconds for the replicas to match")
    p.add_argument("--no-restart", action="store_true", help="skip the node restart step")


def windows(cl):
    return cl.h(cl.cfg.master).get("os") == "windows"


def copy_name(cl, path):
    """The colliding name for path and a note on its sort order."""
    sep = "\\" if "\\" in path else "/"
    folder, fname = path.rsplit(sep, 1)
    stem, dot, ext = fname.rpartition(".")
    if not dot:
        stem, ext = fname, ""
    variant = stem.upper() if stem != stem.upper() else stem.lower()
    if not windows(cl) and variant != stem:
        out = folder + sep + variant + dot + ext
    else:
        parent, sub = folder.rsplit(sep, 1)
        out = parent + sep + sub + "__" + fname
    first = out < path
    note = ("the copy sorts before the tracked path (the old code kept the copy)" if first else
            "the copy sorts after the tracked path (the old code kept the tracked path too)")
    return out, first, note


def same(cl, a, b):
    if windows(cl):
        return norm(a) == norm(b)
    return (a or "") == (b or "")


def exists(cl, path):
    return bool(cl.hostctl(cl.cfg.master, "files", {"glob": path}, check=False))


def scansync(cl):
    st, out = cl.api(cl.cfg.master, "POST", "/v1/scansync", {})
    return out if isinstance(out, dict) else {}


def collisions(cl, out, db_id):
    return [c for c in out.get("collisions") or [] if c.get("db_id") == db_id]


def record(cl, db_id):
    for d in cl.databases(cl.cfg.master):
        if d.get("db_id") == db_id:
            return d
    return None


def copy_records(cl, copy):
    return [d.get("db_id") for d in cl.databases(cl.cfg.master) if same(cl, d.get("path"), copy)]


def conf_text(cl):
    """The replication conf the node writes, as text ("" when unknown)."""
    try:
        _, doc = cl.api(cl.cfg.master, "GET", "/v1/config")
        doc = (doc or {}).get("config", doc) or {}
        p = (doc.get("firebird") or {}).get("replication_conf")
        if not p:
            return ""
        rc, _, _, out = cl.module(cl.cfg.master, "90-hostctl", "tail", {"path": p, "lines": 20000}, check=False)
        return out if rc == 0 else ""
    except TbError:
        return ""


def alert_for(cl, db_id):
    return [x for x in ops.node_alerts(cl, cl.cfg.master, code=CODE) if x.get("database") == db_id]


def wait_alert(cl, db_id, copy, timeout=60):
    end = time.time() + timeout
    while True:
        al = alert_for(cl, db_id)
        if al or time.time() >= end:
            return al
        time.sleep(5)


def check_state(cl, res, case, d, rec0, copy):
    """Record, conf and alert after a scan that saw the copy."""
    rec = record(cl, d["db_id"]) or {}
    ok = same(cl, rec.get("path"), d["path"]) and rec.get("state") not in ("ORPHANED", None)
    others = copy_records(cl, copy)
    res.record(f"{case}: the record keeps its path", "PASS" if ok and not others else "FAIL",
               note=f"path={rec.get('path')} state={rec.get('state')} records of the copy={others}")
    gen_ok = rec.get("generation") == rec0.get("generation") and rec.get("guid") == rec0.get("guid")
    res.record(f"{case}: generation and GUID unchanged", "PASS" if gen_ok else "FAIL",
               note=f"generation {rec0.get('generation')} -> {rec.get('generation')}, "
                    f"guid {rec0.get('guid')} -> {rec.get('guid')}")
    text = conf_text(cl)
    if not text:
        res.record(f"{case}: conf has no section of the copy", "SKIP", note="the conf file could not be read")
    else:
        bad = copy in text if not windows(cl) else norm(copy) in norm(text)
        res.record(f"{case}: conf has no section of the copy", "FAIL" if bad else "PASS",
                   note="the copy is in the conf" if bad else "only the tracked path")


def check_alert(cl, res, case, d, copy, timeout=60):
    al = wait_alert(cl, d["db_id"], copy, timeout)
    msg = " ".join(str(x.get("message", "")) for x in al)
    named = (copy in msg) if not windows(cl) else (norm(copy) in norm(msg))
    res.record(f"{case}: alert {CODE} names the copy", "PASS" if al and named else "FAIL",
               note=(msg[:300] if al else "no alert"))
    return bool(al)


def run(cl, a):
    res = Results("dbid_collision", vars(a).copy())
    m = cl.cfg.master
    copy = None
    made = False
    pending_seen = False
    d = None
    try:
        dbs = cl.test_dbs(which=a.db or "all")
        if not dbs:
            raise TbError("no test database on the master (run 'dbs' first)")
        d = dbs[0]
        copy, _, order = copy_name(cl, d["path"])
        log(f"tracked {d['path']}, copy {copy}; {order}")
        if exists(cl, copy):
            raise TbError(f"a file is already at {copy}; remove it first")
        cl.load_stop(TAG)
        base = scansync(cl)
        rec0 = record(cl, d["db_id"]) or {}

        # 1. the copy and a scan
        cl.hostctl(m, "file-copy", {"from": d["path"], "to": copy})
        made = True
        out = scansync(cl)
        col = collisions(cl, out, d["db_id"])
        ok = (len(col) == 1 and same(cl, col[0].get("kept"), d["path"])
              and same(cl, col[0].get("dropped"), copy))
        res.record("scansync reports the collision", "PASS" if ok else "FAIL",
                   note=f"{col} ({order})")
        if out.get("pending_restart") and not base.get("pending_restart"):
            pending_seen = True
            res.record("scansync: no conf change", "FAIL", note="pending_restart is true after the copy")
        else:
            res.record("scansync: no conf change", "PASS",
                       note=f"pending_restart {base.get('pending_restart')} -> {out.get('pending_restart')}")
        check_state(cl, res, "after scansync", d, rec0, copy)
        check_alert(cl, res, "after scansync", d, copy, timeout=30)

        # 2. a node restart: the start-up scan sees the copy again
        if not a.no_restart:
            node_svc(cl, m, "restart")
            if not wait_node(cl, m, 180):
                raise TbError("the master node did not come back after the restart")
            check_state(cl, res, "after node restart", d, rec0, copy)
            how = "the start-up scan raised it again"
            if not wait_alert(cl, d["db_id"], copy, timeout=60):
                log("no alert after the restart yet: run scansync")
                scansync(cl)
                how = "the start-up scan did not raise it; the next scansync did"
            check_alert(cl, res, "after node restart", d, copy, timeout=30)
            res.record("after node restart: who raised the alert", "PASS", note=how + " (note only)")

        # 3. replication of the tracked file still works
        cl.load_start([d["path"]], mode="write", tx="off", conns="1:2", tag=TAG)
        time.sleep(a.load_seconds)
        cl.load_stop(TAG)
        converge_and_record(cl, res, "replication of the tracked file", [d["path"]], a.timeout)

        # 4. the copy goes
        cl.hostctl(m, "remove-file", {"path": copy})
        made = False
        out = scansync(cl)
        col = collisions(cl, out, d["db_id"])
        rec = record(cl, d["db_id"]) or {}
        ok = (not col and same(cl, rec.get("path"), d["path"]) and rec.get("state") != "ORPHANED"
              and rec.get("generation") == rec0.get("generation"))
        res.record("copy removed: the record is unchanged", "PASS" if ok else "FAIL",
                   note=f"collisions={col} path={rec.get('path')} state={rec.get('state')}")
        left = alert_for(cl, d["db_id"])
        res.record("copy removed: the alert", "PASS",
                   note=("the alert stays (the node does not withdraw it; DELETE /v1/alerts clears it)"
                         if left else "the alert is withdrawn"))
    except TbError as e:
        res.record("dbid_collision", "FAIL", note=str(e)[:400])
    finally:
        cl.load_stop(TAG)
        if copy and (made or exists(cl, copy)):
            try:
                if exists(cl, copy):
                    cl.hostctl(m, "remove-file", {"path": copy})
                scansync(cl)
            except TbError as e:
                log(f"remove {copy}: {e}")
        if d:
            aid = urllib.parse.quote(f"warn:{CODE}:{d['db_id']}", safe=":")
            try:
                cl.api(m, "DELETE", f"/v1/alerts/{aid}", check_status=False)
            except TbError as e:
                log(f"clear the alert: {e}")
            try:
                rec = record(cl, d["db_id"]) or {}
                if pending_seen or rec.get("state") == "PENDING_RESTART":
                    ops.restart_firebird(cl, m, "test bed: dbid_collision clean-up")
            except TbError as e:
                log(f"Firebird restart: {e}")
    return res.finish()
