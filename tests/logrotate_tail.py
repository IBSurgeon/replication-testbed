"""replication.log rotated under the node's tail: every line is read once,
before and after the rotation, and after a restart (hqcluster-node review
2026-10-22: D-01).

The node reads the replica's replication.log every 30 s from a saved byte
offset (<node dir>/replication.log.offset: "<offset> <drained>"). Its own
rotator drains the head of the log under <log>.rotate.lock and adds the bytes
it removed to <log>.drained; the tail moves its offset down by that much.
Before the fix a drain made the file shorter than the offset, and the tail
read the whole rest of the file again.

The test writes Firebird-style blocks for one replica database into the log
(hostctl replog-inject) and counts what the node read:
  probe   VERBOSE "Found segment ..."   stats.received +1 per block read
  error   ERROR (generic)               the 5th in a row raises apply_error
                                        (NEEDS_ATTENTION), once
  reset   VERBOSE "... is replicated, deleting the file" (segment 0): ends
          the error streak and clears the alert, as an applied segment does
Phases, each on a quiet replica (load off, mailbox empty):
  drain         the node's head drain (lock + .drained) of the lines before
                the test blocks, which stay in the file
  truncate      the node's copy-then-truncate (lock + .drained)
  rename        logrotate "create": rename, new empty file
  copytruncate  logrotate "copytruncate": copy, truncate, no sidecar
Each phase: 4 errors + 2 probes are read; the log is rotated; after a poll
  - received did not move and no alert came (nothing was read again)
then 1 error + 1 probe more:
  - received +1 and apply_error raised: the tail goes on reading, and its
    error streak (4 + 1) did not restart or double
  - exactly one journal "alert" event of apply_error, with n = 5
restart: 5 errors + 2 probes are read, the node stops, the head of the log
  before them is drained, the node starts. After two polls received is still
  0 (the counters start at 0), no apply_error came back, and one new probe
  is read once.
At the end: a reset line, a short load, IN_SYNC, no apply_error, and the
replica matches the master. Archives the test made are removed unless
--keep-archives.

Needs a quiet replica: real Firebird lines for the same database during a
phase (applied segments) end the error streak and make the counts wrong.
"""
import base64
import time

from tblib.cluster import TbError, log
from tblib.results import Results

from ._common import add_load_args, converge_and_record, ensure_load, pick_replicas, replica_record
from .disasters import node_svc, wait_node

HELP = "replication.log rotated under the tail (node drain/truncate, logrotate rename/copytruncate, restart): read once (D-01)"

TAG = "logrotate_tail"
MOD = "91-journal"
MODES = ("drain", "truncate", "rename", "copytruncate")
ERR = "Test bed: logrotate_tail simulated apply error"
PROBE = "Found segment (test bed probe, logrotate_tail)"
RESET = "Segment 0 is replicated, deleting the file (test bed reset, logrotate_tail)"
FILLER = "No new segments found, suspending (test bed filler, logrotate_tail)"
LIMIT = 5           # applywatch ApplyErrorLimit: the 5th error in a row raises apply_error


def add_args(p):
    p.add_argument("--db", default="db1", help="one test database")
    p.add_argument("--replica", default="", help="one replica; default: the first")
    p.add_argument("--modes", default=",".join(MODES), help="rotation phases: " + ", ".join(MODES))
    p.add_argument("--no-restart", action="store_true", help="skip the restart phase")
    p.add_argument("--keep-archives", action="store_true", help="keep the <log>.tb-* copies the test made")
    p.add_argument("--poll-timeout", type=int, default=90, help="seconds to wait for one apply poll (30 s tick)")
    p.add_argument("--catchup-timeout", type=int, default=600)
    add_load_args(p)


def b64(s):
    return base64.b64encode(s.encode()).decode()


class Tail:
    def __init__(self, cl, a, db, rep):
        self.cl, self.a, self.db, self.rep = cl, a, db, rep
        rec = replica_record(cl, rep, db["path"])
        if not rec:
            raise TbError(f"[{rep}] no record of {db['path']}")
        self.rid, self.rpath = rec["db_id"], rec["path"]
        _, doc = cl.api(rep, "GET", "/v1/config")
        cfg = (doc or {}).get("config", doc) or {}
        self.log = (cfg.get("firebird") or {}).get("replication_log")
        if not self.log:
            raise TbError(f"[{rep}] the node has no firebird.replication_log")
        self.lock_created = False

    # --- host and API calls ----------------------------------------------------
    def jcall(self, cmd, args=None, timeout=None):
        a = {"node_dir": self.cl.h(self.rep)["paths"]["node"]}
        a.update(args or {})
        rc, res, _, out = self.cl.module(self.rep, MOD, cmd, a, check=False, timeout=timeout)
        if rc != 0:
            log(f"[{self.rep}] {MOD} {cmd}: exit {rc}: {(out or '')[-300:]}")
            return None
        return res

    def inject(self, message, count=1, level="ERROR"):
        self.cl.hostctl(self.rep, "replog-inject", {
            "path": self.log, "db": self.rpath, "role": "replica", "level": level, "count": count,
            "message_b64": b64(message)})

    def logstate(self):
        return self.jcall("replog-state", {"path": self.log}) or {}

    def rotate(self, mode, nbytes=0):
        r = self.jcall("replog-rotate", {"path": self.log, "mode": mode, "bytes": nbytes or None}, timeout=120)
        if not r:
            raise TbError(f"replog-rotate {mode} failed")
        self.lock_created = self.lock_created or bool(r.get("lock_created"))
        return r

    def wait_poll(self, after, n=1):
        """Until the apply watcher saved its offset n times after host time
        `after` (it saves it on every poll). Returns the last replog-state."""
        end = time.time() + self.a.poll_timeout * n
        last, seen, st = after, 0, {}
        while time.time() < end:
            st = self.logstate()
            m = (st.get("offset") or {}).get("mtime") or 0
            if m > last + 0.2:
                seen, last = seen + 1, m
                if seen >= n:
                    return st
            time.sleep(3)
        raise TbError(f"no apply poll within {self.a.poll_timeout * n}s (offset {st.get('offset')})")

    def settle(self, n=1):
        """One (or n) poll(s) after now: what was written is read."""
        return self.wait_poll(self.logstate().get("now") or 0, n)

    def stats(self):
        st, doc = self.cl.api(self.rep, "GET", "/v1/stats", check_status=False)
        for s in ((doc or {}).get("databases") or []) if st == 200 else []:
            if s.get("db_id") == self.rid:
                return s
        return {}

    def received(self):
        return int(self.stats().get("received") or 0)

    def alerts(self, code):
        """The alerts of this code for the database on the replica's bus."""
        st, al = self.cl.api(self.rep, "GET", f"/v1/alerts?code={code}&database={self.rid}", check_status=False)
        return [x for x in (al or []) if isinstance(x, dict)] if st == 200 else []

    def alert(self):
        items = self.alerts("apply_error")
        return items[0] if items else None

    def state(self):
        st, r = self.cl.api(self.rep, "GET", f"/v1/databases/{self.rid}", check_status=False)
        return (r or {}).get("state") if st == 200 and isinstance(r, dict) else None

    def jmark(self):
        return int((self.jcall("journal-events", {"from": -1}) or {}).get("lines") or 0)

    def jalerts(self, start):
        """apply_error "alert" events of the database in journal.jsonl from line start."""
        r = self.jcall("journal-events", {"from": start, "db_id": self.rid, "types": "alert",
                                          "code": "apply_error"}) or {}
        return r.get("events") or []

    def reset(self):
        """End the error streak and clear apply_error, as an applied segment does."""
        self.inject(RESET, level="VERBOSE")
        self.settle()
        return self.alert() is None


# --- phases ------------------------------------------------------------------
def phase(t, res, mode):
    case = f"{mode}"
    log(f"--- rotation {mode}")
    if not t.reset():
        res.record(f"{case}: start without apply_error", "FAIL", note="the reset line did not clear it")
        return
    t.inject(FILLER, level="VERBOSE")            # the head a drain removes is never empty
    st = t.settle()
    size_pre = int(st.get("size") or 0)
    r0 = t.received()
    t.inject(ERR, count=LIMIT - 1)
    t.inject(PROBE, count=2, level="VERBOSE")
    t.settle()
    r1, al = t.received(), t.alert()
    ok = r1 == r0 + 2 and al is None
    res.record(f"{case}: blocks read before the rotation", "PASS" if ok else "FAIL",
               note=f"received {r0} -> {r1} (want +2), alert {'yes' if al else 'no'} (want no)")
    if not ok:
        return
    jm = t.jmark()
    rot = t.rotate(mode, size_pre if mode == "drain" else 0)
    log(f"{mode}: {rot}")
    t.settle()
    r2, al = t.received(), t.alert()
    ok = r2 == r1 and al is None
    res.record(f"{case}: nothing read again", "PASS" if ok else "FAIL",
               note=f"received {r1} -> {r2} (want +0), alert {'yes: ' + str(al.get('message')) if al else 'no'}; "
                    f"size {rot.get('size_before')} -> {rot.get('size_after')}, "
                    f"drained {rot.get('drained_before')} -> {rot.get('drained_after')}")
    t.inject(ERR)
    t.inject(PROBE, level="VERBOSE")
    t.settle()
    r3, al = t.received(), t.alert()
    ev = t.jalerts(jm)
    ns = [e.get("fields", {}).get("n") for e in ev]
    ok = r3 == r2 + 1 and al is not None
    res.record(f"{case}: new lines read once", "PASS" if ok else "FAIL",
               note=f"received {r2} -> {r3} (want +1), apply_error {'raised' if al else 'missing'}")
    ok = len(ev) == 1 and ns == [LIMIT]
    res.record(f"{case}: apply_error journaled once, n={LIMIT}", "PASS" if ok else "FAIL",
               note=f"journal alert events: {len(ev)}, n = {ns}")


def restart_phase(t, res):
    case = "restart"
    log("--- restart after a drain")
    t.reset()
    t.inject(FILLER, level="VERBOSE")
    st = t.settle()
    size_pre = int(st.get("size") or 0)
    t.inject(ERR, count=LIMIT)
    t.inject(PROBE, count=2, level="VERBOSE")
    t.settle()
    res.record(f"{case}: blocks read before the stop", "PASS" if t.alert() else "FAIL",
               note=f"received {t.received()}, apply_error {'raised' if t.alert() else 'missing'}")
    node_svc(t.cl, t.rep, "stop")
    try:
        rot = t.rotate("drain", size_pre)
        log(f"drain while stopped: {rot}")
    finally:
        mark = t.logstate().get("now") or 0
        node_svc(t.cl, t.rep, "start")
    if not wait_node(t.cl, t.rep, 180):
        raise TbError(f"{t.rep}: the node did not start")
    t.wait_poll(mark, 2)
    r, al = t.received(), t.alert()
    ev = t.jalerts(0)       # the journal starts empty at the start
    ok = r == 0 and al is None and not ev
    res.record(f"{case}: rotated part not read again", "PASS" if ok else "FAIL",
               note=f"received {r} (want 0), apply_error {'raised' if al else 'no'}, journal alert events {len(ev)}")
    t.inject(PROBE, level="VERBOSE")
    t.settle()
    r2 = t.received()
    res.record(f"{case}: tail goes on after the start", "PASS" if r2 == r + 1 else "FAIL",
               note=f"received {r} -> {r2} (want +1)")
    s = t.state()
    res.record(f"{case}: no NEEDS_ATTENTION left", "PASS" if s != "NEEDS_ATTENTION" else "FAIL", note=str(s))


def quiet(t, a):
    """Load off, the replica caught up, the tail at the end of the log."""
    t.cl.load_stop(TAG)
    t.cl.wait_converged([t.db["path"]], timeout=a.catchup_timeout, replicas=[t.rep])
    end = time.time() + 120
    while time.time() < end and int(t.stats().get("pending") or 0) > 0:
        time.sleep(5)
    t.settle()


def run(cl, a):
    dbs = cl.test_dbs(which=a.db)
    if not dbs:
        raise TbError("no test databases (run 'tb.py dbs prepare')")
    db = dbs[0]
    reps = pick_replicas(cl, a.replica) if a.replica else list(cl.cfg.replicas[:1])
    if len(reps) != 1:
        raise TbError(f"--replica names one replica, got {reps}")
    rep = reps[0]
    modes = [m.strip() for m in (a.modes or "").split(",") if m.strip()]
    for m in modes:
        if m not in MODES:
            raise TbError(f"--modes: {', '.join(MODES)} (got {m})")
    t = Tail(cl, a, db, rep)
    res = Results("logrotate_tail", vars(a).copy())
    log(f"logrotate_tail: {t.rid} on {rep}, log {t.log}")
    try:
        quiet(t, a)
        for m in modes:
            phase(t, res, m)
        if not a.no_restart:
            restart_phase(t, res)
    except TbError as e:
        res.record("run", "FAIL", note=str(e)[:300])
    finally:
        if not wait_node(cl, rep, 10):
            node_svc(cl, rep, "start")
            wait_node(cl, rep, 180)
        try:
            t.reset()
        except TbError as e:
            log(f"reset at the end: {e}")
        t.jcall("replog-cleanup", {"path": t.log, "lock_created": t.lock_created, "keep": a.keep_archives})
    if not a.no_load:
        ensure_load(cl, [db], a, TAG)
        time.sleep(60)
        cl.load_stop(TAG)
    end = time.time() + 300
    s = t.state()
    while s not in ("IN_SYNC",) and time.time() < end:
        time.sleep(10)
        s = t.state()
    al = t.alert()
    res.record("end: IN_SYNC, no apply_error", "PASS" if s == "IN_SYNC" and not al else "FAIL",
               note=f"state {s}, apply_error {'raised' if al else 'no'}")
    converge_and_record(cl, res, "converge", [db["path"]], a.catchup_timeout, [rep])
    return res.finish()
