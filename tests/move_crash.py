"""A replica file move interrupted by a crash ends with one good file
(hqcluster-node review 2026-10-22: C-01).

POST /v1/databases/{db_id}/move journals a witness (db_move start) before the
first rename. A node that starts with a witness finishes the move (the new
file exists) or rolls it back (it does not). A cross-device copy writes
<new>.hqc-move-tmp and renames it when complete, so a temp left behind means
"not moved yet" and is deleted. When both final names exist and the new one
is smaller, the node keeps both and raises db_file_missing.

Cases, on one replica and one test database (default db1):
  race     (--kill-at, default witness,file) the move starts through the API
           and the node is killed -9 at that moment: when the witness is
           journaled, or when the new file appears (now: --kill-delay seconds
           after the request). systemd starts the node again.
  crafted  (--crafted, default backward,forward) with the node and Firebird
           stopped, the picture a crash leaves is made by hand, then the node
           starts:
             backward  a partial <new>.hqc-move-tmp + the witness: rolled back
             forward   the file renamed to <new> + the witness, the mailbox
                       still at the old place: finished
             conflict  (opt-in) a smaller file at <new> + the witness: both
                       kept, db_file_missing raised; then <new> is removed and
                       the next start rolls back
After each case:
  - the database file exists exactly once, at the old or the new path, and no
    *.hqc-move-tmp is left
  - the record follows the file, has no move witness and is not ORPHANED
  - the file is attachable and Firebird runs
  - no db_file_missing alert (except the conflict case)
  - a second move ends well (back to the old path, or there and back)
The database is put back at its old path in finally. At the end the load
stops and the replica must match the master.

Note: on one filesystem the node renames; it never copies. The race can only
interrupt renames; the temp copy of a cross-device move is covered by the
crafted backward case.
"""
import base64
import json
import threading
import time

from tblib.cluster import TbError, log
from tblib.results import Results

from ._common import add_load_args, converge_and_record, ensure_load, norm, pick_replicas, replica_record
from .disasters import fb_svc, node_svc, wait_node

HELP = "replica move killed midway (race and crafted crash states): one good file, record follows (C-01)"

TAG = "move_crash"
MOD = "91-journal"
TMP = ".hqc-move-tmp"
FOLDER = "tb-movecrash"


def add_args(p):
    p.add_argument("--db", default="db1", help="one test database")
    p.add_argument("--replica", default="", help="one replica; default: the first")
    p.add_argument("--kill-at", default="witness,file", help="race rounds: witness, file, now (empty: none)")
    p.add_argument("--kill-delay", type=float, default=0.3, help="now: seconds after the move request")
    p.add_argument("--crafted", default="backward,forward", help="crafted states: backward, forward, conflict")
    p.add_argument("--catchup-timeout", type=int, default=600)
    add_load_args(p)


# --- host calls --------------------------------------------------------------
def jcall(cl, host, cmd, args=None, timeout=None):
    """One 91-journal command on the host: its result, or None when it failed."""
    a = {"node_dir": cl.h(host)["paths"]["node"]}
    a.update(args or {})
    rc, res, _, out = cl.module(host, MOD, cmd, a, check=False, timeout=timeout)
    if rc != 0:
        log(f"[{host}] {MOD} {cmd}: exit {rc}: {(out or '')[-300:]}")
        return None
    return res


def _root(cl, rep):
    """The replica's databases.root, from its own config."""
    _, doc = cl.api(rep, "GET", "/v1/config")
    cfg = (doc or {}).get("config", doc) or {}
    root = (cfg.get("databases") or {}).get("root", "")
    if not root:
        raise TbError(f"[{rep}] no databases.root in config")
    return root


def _rel(path, root):
    p = path.replace("\\", "/").rstrip("/")
    r = root.replace("\\", "/").rstrip("/")
    if not p.lower().startswith(r.lower() + "/"):
        raise TbError(f"{path} is not under {root}")
    return p[len(r) + 1:]


class Ctx:
    """The database, its replica copy and the move target."""

    def __init__(self, cl, a, db, rep):
        self.cl, self.a, self.db, self.rep = cl, a, db, rep
        rec = replica_record(cl, rep, db["path"])
        if not rec:
            raise TbError(f"[{rep}] no record of {db['path']}")
        self.rid = rec["db_id"]
        self.p0 = rec["path"]
        self.root = _root(cl, rep)
        self.rel0 = _rel(self.p0, self.root)
        self.target = f"{FOLDER}/{self.rel0}"
        self.pn = cl.host(rep).join(self.root, *self.target.split("/"))
        self.folder = cl.host(rep).join(self.root, FOLDER)
        self.mailbox0 = rec.get("incoming") or self.p0 + ".Incoming"

    def rec(self):
        st, r = self.cl.api(self.rep, "GET", f"/v1/databases/{self.rid}", check_status=False)
        return r if st == 200 and isinstance(r, dict) else None

    def files(self):
        ps = [self.p0, self.pn, self.p0 + TMP, self.pn + TMP, self.mailbox0, self.pn + ".Incoming"]
        info = jcall(self.cl, self.rep, "path-info", {"paths": ";".join(ps)}) or []
        out = {p: {"exists": False} for p in ps}
        out.update({i["path"]: i for i in info})
        return out

    def move(self, target, timeout=180):
        """POST move; (status, body); the move is synchronous."""
        try:
            return self.cl.api(self.rep, "POST", f"/v1/databases/{self.rid}/move", {"target": target},
                               timeout=timeout, check_status=False)
        except TbError as e:
            return 0, {"error": str(e)[:300]}

    def alerts(self, code):
        st, al = self.cl.api(self.rep, "GET", f"/v1/alerts?code={code}&database={self.rid}", check_status=False)
        return [x for x in (al or []) if isinstance(x, dict)] if st == 200 else []

    def fb_active(self, wait=0):
        """(running, systemd state); waits up to `wait` seconds for running."""
        end = time.time() + wait
        while True:
            r = self.cl.hostctl(self.rep, "fb-svc", {"action": "status", "fb_service": self.cl.fb_service(self.rep)},
                                check=False) or {}
            if r.get("active") == "active" or time.time() >= end:
                return r.get("active") == "active", r.get("active")
            time.sleep(3)

    def witness(self):
        """The db_move start event the mover journals before its first rename."""
        ev = {"type": "db_move", "db_id": self.rid,
              "fields": {"action": "start", "path": self.pn, "incoming": self.pn + ".Incoming"}}
        return base64.b64encode(json.dumps(ev).encode()).decode()


def ensure_up(cl, rep, timeout=120):
    if wait_node(cl, rep, timeout):
        return True
    node_svc(cl, rep, "start")
    return wait_node(cl, rep, 120)


def wait_settled(ctx, timeout=120):
    """The record once it has no move witness (the start reconciled it)."""
    end = time.time() + timeout
    rec = None
    while time.time() < end:
        rec = ctx.rec()
        if rec and not rec.get("move"):
            return rec
        time.sleep(5)
    return rec


# --- checks ------------------------------------------------------------------
def verify(ctx, res, case, conflict=False):
    """The invariants after a start that reconciled a move. Returns the path the
    file is at (None when it is not at exactly one place)."""
    cl, rep = ctx.cl, ctx.rep
    rec = wait_settled(ctx, 60 if conflict else 120) or {}
    fi = ctx.files()
    at0, atn = fi[ctx.p0].get("exists"), fi[ctx.pn].get("exists")
    tmps = [p for p in (ctx.p0 + TMP, ctx.pn + TMP) if fi[p].get("exists")]
    where = ctx.p0 if at0 and not atn else ctx.pn if atn and not at0 else None
    if conflict:
        ok = at0 and atn and bool(rec.get("move"))
        al = ctx.alerts("db_file_missing")
        res.record(f"{case}: both files kept, witness kept", "PASS" if ok else "FAIL",
                   note=f"old {at0}, new {atn}, move {rec.get('move')}")
        res.record(f"{case}: db_file_missing raised", "PASS" if al else "FAIL",
                   note=(al[0].get("message") or "")[:200] if al else "no alert")
        return None
    res.record(f"{case}: file exactly once", "PASS" if where and not tmps else "FAIL",
               note=f"at {'old' if where == ctx.p0 else 'new' if where else '?'} (old {at0}, new {atn}), "
                    f"temp left {tmps}")
    ok = bool(rec) and where is not None and norm(rec.get("path")) == norm(where) \
        and rec.get("state") != "ORPHANED" and not rec.get("move")
    res.record(f"{case}: record follows the file", "PASS" if ok else "FAIL",
               note=f"path {rec.get('path')}, state {rec.get('state')}, move {rec.get('move')}")
    mb_old, mb_new = fi[ctx.mailbox0].get("exists"), fi[ctx.pn + ".Incoming"].get("exists")
    if where == ctx.pn and mb_old:
        res.record(f"{case}: mailbox moved", "FAIL", note=f"{ctx.mailbox0} still there (new one {mb_new})")
    up, active = ctx.fb_active(wait=30)
    res.record(f"{case}: Firebird running", "PASS" if up else "FAIL", note=str(active))
    if where:
        att = cl.hostctl(rep, "attach", {"db": where}, check=False) or {}
        res.record(f"{case}: attachable", "PASS" if att.get("ok") else "FAIL", note=(att.get("out") or "")[:200])
    al = ctx.alerts("db_file_missing")
    res.record(f"{case}: no db_file_missing alert", "FAIL" if al else "PASS",
               note=(al[0].get("message") or "")[:200] if al else "")
    return where


def move_again(ctx, res, case, where):
    """A second move ends well; the database ends at its old path."""
    if where == ctx.pn:
        st, r = ctx.move(ctx.rel0)
        ok = st == 200 and norm(r.get("new_path")) == norm(ctx.p0)
        res.record(f"{case}: move back", "PASS" if ok else "FAIL", note=f"{st} {str(r)[:250]}")
        return ok
    if where == ctx.p0:
        st, r = ctx.move(ctx.target)
        ok = st == 200 and norm(r.get("new_path")) == norm(ctx.pn)
        st2, r2 = ctx.move(ctx.rel0) if ok else (0, {})
        ok2 = st2 == 200 and norm(r2.get("new_path")) == norm(ctx.p0)
        res.record(f"{case}: move there and back", "PASS" if ok and ok2 else "FAIL",
                   note=f"{st} {str(r)[:150]} / {st2} {str(r2)[:150]}")
        return ok and ok2
    res.record(f"{case}: second move", "FAIL", note="the file is not at one place: not tried")
    return False


def wait_healthy(ctx, res, case, timeout=180):
    end = time.time() + timeout
    rec = {}
    while time.time() < end:
        rec = ctx.rec() or {}
        if rec.get("state") in ("IN_SYNC", "LAGGING") and norm(rec.get("path")) == norm(ctx.p0):
            break
        time.sleep(10)
    ok = rec.get("state") in ("IN_SYNC", "LAGGING") and norm(rec.get("path")) == norm(ctx.p0)
    res.record(f"{case}: back at the old path, replicating", "PASS" if ok else "FAIL",
               note=f"{rec.get('path')} {rec.get('state')}/{rec.get('state_reason', '')}")


# --- cases -------------------------------------------------------------------
def race(ctx, res, kill_at):
    cl, rep, a = ctx.cl, ctx.rep, ctx.a
    case = f"race kill at {kill_at}"
    log(f"--- {case}")
    out = {}

    def post():
        out["move"] = ctx.move(ctx.target, timeout=120)

    def watch():
        out["kill"] = jcall(cl, rep, "kill-when", {"on": kill_at, "db_id": ctx.rid, "path": ctx.pn,
                                                  "timeout": 60, "action": "kill"}, timeout=180)

    tp = threading.Thread(target=post, daemon=True)
    if kill_at == "now":
        tp.start()
        time.sleep(a.kill_delay)
        watch()
    else:
        tw = threading.Thread(target=watch, daemon=True)
        tw.start()
        time.sleep(3)               # the watcher reads the journal before the request
        tp.start()
        tw.join(200)
    tp.join(200)
    st, body = out.get("move") or (0, {})
    fired = bool(out.get("kill"))
    res.record(f"{case}: node killed during the move", "PASS" if fired else "FAIL",
               note=f"kill {out.get('kill')}, move answer {st} {str(body)[:200]}")
    if not ensure_up(cl, rep):
        res.record(f"{case}: node back", "FAIL", note="the node did not come back")
        raise TbError(f"{rep}: the node did not come back")
    where = verify(ctx, res, case)
    if move_again(ctx, res, case, where):
        wait_healthy(ctx, res, case)


def crafted(ctx, res, kind):
    cl, rep = ctx.cl, ctx.rep
    case = f"crafted {kind}"
    log(f"--- {case}")
    node_svc(cl, rep, "stop")
    fb_svc(cl, rep, "stop")         # the mover stops Firebird before it renames
    try:
        if kind == "backward":
            made = jcall(cl, rep, "file-partial", {"from": ctx.p0, "to": ctx.pn + TMP, "fraction": 0.5})
        elif kind == "forward":
            made = jcall(cl, rep, "file-move", {"from": ctx.p0, "to": ctx.pn})
        else:
            made = jcall(cl, rep, "file-partial", {"from": ctx.p0, "to": ctx.pn, "fraction": 0.5})
        wit = jcall(cl, rep, "journal-append", {"line_b64": ctx.witness()}) if made else None
        res.record(f"{case}: crash state made", "PASS" if made and wit else "FAIL",
                   note=f"{made} / witness {'written' if wit else 'not written'}")
    finally:
        node_svc(cl, rep, "start")
    if not ensure_up(cl, rep):
        res.record(f"{case}: node back", "FAIL", note="the node did not come back")
        raise TbError(f"{rep}: the node did not come back")
    if not (made and wit):
        raise TbError(f"{case}: the crash state could not be made")
    if kind == "conflict":
        verify(ctx, res, case, conflict=True)
        up, active = ctx.fb_active()
        log(f"{case}: Firebird {active} after the unreconciled start")
        # An operator's fix: drop the smaller copy; the next start rolls back.
        node_svc(cl, rep, "stop")
        jcall(cl, rep, "remove-path", {"path": ctx.pn})
        node_svc(cl, rep, "start")
        if not ensure_up(cl, rep):
            raise TbError(f"{rep}: the node did not come back")
        if not up:
            fb_svc(cl, rep, "start")
        case += " then fixed"
    where = verify(ctx, res, case)
    if kind == "backward" and where != ctx.p0:
        res.record(f"{case}: rolled back", "FAIL", note=f"file at {where}")
    if kind == "forward" and where != ctx.pn:
        res.record(f"{case}: finished forward", "FAIL", note=f"file at {where}")
    if move_again(ctx, res, case, where):
        wait_healthy(ctx, res, case)


def restore(ctx, res):
    """The database back at its old path, Firebird and the node running, no
    temp copies or test folders left."""
    cl, rep = ctx.cl, ctx.rep
    fi = ctx.files()
    if fi[ctx.p0].get("exists") and fi[ctx.pn].get("exists") \
            and fi[ctx.pn].get("size", 0) < fi[ctx.p0].get("size", 0):
        # A conflict is left (the smaller copy is ours): drop it, so the next
        # start rolls the move back.
        log("restore: removing the smaller copy at the new path")
        node_svc(cl, rep, "stop")
        jcall(cl, rep, "remove-path", {"path": ctx.pn})
        node_svc(cl, rep, "start")
    if not ensure_up(cl, rep):
        res.record("restore: node running", "FAIL", note="the node does not start")
        return
    if not ctx.fb_active()[0]:
        fb_svc(cl, rep, "start")
    rec = wait_settled(ctx, 60) or {}
    fi = ctx.files()
    only_new = fi[ctx.pn].get("exists") and not fi[ctx.p0].get("exists")
    if only_new and norm(rec.get("path")) == norm(ctx.pn):
        st, r = ctx.move(ctx.rel0)
        if st != 200:
            time.sleep(30)
            st, r = ctx.move(ctx.rel0)
        log(f"restore: move back {st} {str(r)[:200]}")
    elif only_new and norm(rec.get("path")) == norm(ctx.p0) and not rec.get("move"):
        # The record never followed the file (a crafted state without its
        # witness): put the file and its mailbox back with everything stopped.
        log("restore: moving the file back by hand")
        node_svc(cl, rep, "stop")
        fb_svc(cl, rep, "stop")
        try:
            jcall(cl, rep, "file-move", {"from": ctx.pn, "to": ctx.p0})
            if fi[ctx.pn + ".Incoming"].get("exists") and not fi[ctx.mailbox0].get("exists"):
                jcall(cl, rep, "file-move", {"from": ctx.pn + ".Incoming", "to": ctx.mailbox0})
        finally:
            fb_svc(cl, rep, "start")
            node_svc(cl, rep, "start")
            ensure_up(cl, rep)
    for t in (ctx.p0 + TMP, ctx.pn + TMP):
        if ctx.files()[t].get("exists"):
            jcall(cl, rep, "remove-path", {"path": t})
    fi = ctx.files()
    rec = ctx.rec() or {}
    ok = fi[ctx.p0].get("exists") and not fi[ctx.pn].get("exists") and norm(rec.get("path")) == norm(ctx.p0)
    if ok:
        jcall(cl, rep, "remove-path", {"path": ctx.folder})     # refuses while a .fdb is inside
    res.record("restore: database at its old path", "PASS" if ok else "FAIL",
               note=f"old {fi[ctx.p0].get('exists')}, new {fi[ctx.pn].get('exists')}, record {rec.get('path')}")


def run(cl, a):
    dbs = cl.test_dbs(which=a.db)
    if not dbs:
        raise TbError("no test databases (run 'tb.py dbs prepare')")
    db = dbs[0]
    reps = pick_replicas(cl, a.replica) if a.replica else list(cl.cfg.replicas[:1])
    if len(reps) != 1:
        raise TbError(f"--replica names one replica, got {reps}")
    rep = reps[0]
    ctx = Ctx(cl, a, db, rep)
    res = Results("move_crash", vars(a).copy())
    log(f"move_crash: {ctx.rid} on {rep}: {ctx.p0} <-> {ctx.pn}")
    rec = ctx.rec() or {}
    if rec.get("state") not in ("IN_SYNC", "LAGGING") or rec.get("move"):
        raise TbError(f"the replica copy must replicate before the test: {rec.get('state')} move={rec.get('move')}")
    kills = [k.strip() for k in (a.kill_at or "").split(",") if k.strip()]
    kinds = [k.strip() for k in (a.crafted or "").split(",") if k.strip()]
    for k in kills:
        if k not in ("witness", "file", "now"):
            raise TbError(f"--kill-at: witness, file, now (got {k})")
    for k in kinds:
        if k not in ("backward", "forward", "conflict"):
            raise TbError(f"--crafted: backward, forward, conflict (got {k})")
    try:
        if not a.no_load:
            ensure_load(cl, [db], a, TAG)
        for k in kills:
            race(ctx, res, k)
        for k in kinds:
            crafted(ctx, res, k)
    except TbError as e:
        res.record("run", "FAIL", note=str(e)[:300])
    finally:
        if not a.no_load:
            cl.load_stop(TAG)
        restore(ctx, res)
    converge_and_record(cl, res, "converge", [db["path"]], a.catchup_timeout, [rep])
    return res.finish()
