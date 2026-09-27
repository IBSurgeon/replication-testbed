"""Every state of one database, and the transitions between them
(hqbirdrcm docs/REPLICATION_STATE_MACHINE.md, section 2.3, T1-T36).

The test makes its own database (a copy of EMPLOYEE in --subdir), drives it
through the scenarios below and reads what the master and the replica
journaled (Trace in tests/_sm.py). Each transition is one case:
"T10 IN_SYNC->LAGGING" passes when the two states follow each other in the
node's journal. The note shows the whole chain the node went through.

Most transitions come from real events: a stopped Firebird or node, a paused
peer, a segment removed from the mailbox or the archive, a new GUID on the
master, a full disk (a high free-space floor), a segment sent to the replica
from the master host with the master's certificate. Errors that Firebird
cannot be made to write on demand (apply errors, "replication stopped",
"disabled", key violations) are appended to replication.log in Firebird's
block format: the node reads that file for what Firebird did. The case note
says "simulated" for these.

Not driven here: T2 and T5 (takeover of a replication set up by hand),
T18/T19 (a segment damaged in transit: needs a man in the middle), T22 (a
foreign file put into a mailbox as a sign of a replaced master), and T28 is
not told apart from T27 (both are the same retention ladder, by API or by
the 30 s tick).

Scenarios, in the order they run:
  create        T1 T3 T6 T7 T8 T29 T30 (master), T32 (replica)
  pause         T10 T9       shipping to the replica paused under load
  replica-lag   T13 T14      Firebird on the replica stopped under load
  blocked       T12 T14      a segment removed from the replica mailbox
  backpressure  T11          replica mailbox ceiling 2, its Firebird stopped
  apply-errors  T15 T16      5 apply errors (simulated) on the replica
  master-stop   T15 T17 T9   "Replication is stopped" (simulated) on the master
  disabled      T33 T32      "replication disabled" (simulated), then reinit
  constraint    T21 T32      3 key violations (simulated), then reinit
  conflict      T20          the same sequence with other bytes, from the master host
  foreign       T23          a segment of another database under this db_id
  replaced      T24 T29 T30  the master file gets a new GUID (nbackup copy, -F)
  segment-lost  T12 T25      a segment gone from the mailbox and the archive
  disk          T27          free-space floor above the free space, peer paused
  reinit-fail   T29 T31      reinit to a replica whose node is stopped
  stale         T31 T7 T26   the master stops right after the image is sent
  orphan        T34 T35 T4 T6  the file excluded by config, then back
  remove        T34 T36      the database removed and forgotten
"""
import time

from tblib import ops
from tblib.cluster import TbError, log
from tblib.results import SKIP, Results

from ._common import add_load_args, converge_and_record, pick_replicas
from ._sm import SM, Trace, has_step, fb_start_all
from .disasters import fb_svc, node_svc, wait_node

HELP = "every state and transition of one database (T1-T36 of the state machine)"

SCENARIOS = ["create", "pause", "replica-lag", "blocked", "backpressure", "apply-errors", "master-stop",
             "disabled", "constraint", "conflict", "foreign", "replaced", "segment-lost", "disk",
             "reinit-fail", "stale", "orphan", "remove"]


def add_args(p):
    p.add_argument("--only", default="all", help="all | comma list of: " + ",".join(SCENARIOS))
    p.add_argument("--subdir", default="tbsm", help="folder of the test database under db_root")
    p.add_argument("--replicas", default="all")
    p.add_argument("--keep", action="store_true", help="keep the database at the end (skips 'remove')")
    add_load_args(p)


def check(sm, name, host, trace, steps, extra=""):
    chain = trace.chain()
    for t, frm, to in steps:
        ok = has_step(chain, frm, to)
        arrow = f"{'(new)' if frm == '^' else frm}->{to}"
        sm.res.record(f"{t} {arrow} [{name}, {host}]", "PASS" if ok else "FAIL",
                      note=(extra + "; " if extra else "") + "chain: " + " > ".join(chain[-14:]), chain=chain)


def mailbox_segments(sm, rep):
    files = sm.cl.hostctl(rep, "files", {"glob": sm.rpath(rep) + ".Incoming/*journal-*"}) or []
    return sorted(files, key=lambda f: f["path"])


def fill_mailbox(sm, rep, n=3, timeout=240):
    """Firebird on the replica is stopped: load until n segments wait in its
    mailbox. Returns them."""
    sm.load_on()
    end = time.time() + timeout
    try:
        while time.time() < end:
            segs = mailbox_segments(sm, rep)
            if len(segs) >= n:
                return segs
            time.sleep(10)
    finally:
        sm.load_off()
    return mailbox_segments(sm, rep)


# --- scenarios -----------------------------------------------------------------
def sc_create(sm):
    cl, a = sm.cl, sm.a
    if cl.test_dbs(a.subdir):
        log(f"an earlier test database in {a.subdir}: removing it first")
        ops.dbs_remove(cl, a.subdir)
    tm = Trace(cl, sm.m)
    trs = {r: Trace(cl, r) for r in sm.reps}
    dbs = ops.dbs_prepare(cl, count=1, subdir=a.subdir, seed=True)
    if not dbs:
        raise TbError("the test database was not made")
    sm.use(dbs[0])
    tm.db_id = sm.db_id
    check(sm, "create", "master", tm, [("T1", "^", "UNCONFIGURED"), ("T3", "UNCONFIGURED", "PENDING_RESTART"),
                                       ("T6", "PENDING_RESTART", "CONFIGURED"), ("T7", "CONFIGURED", "PUBLISHING"),
                                       ("T8", "PUBLISHING", "CONFIGURED"), ("T29", "CONFIGURED", "SEEDING"),
                                       ("T30", "SEEDING", "IN_SYNC")])
    for r, tr in trs.items():
        tr.db_id = sm.rid(r)
        # "*": a replica scan may register the placed file a moment before
        # the reinit sets its state.
        check(sm, "create", r, tr, [("T32", "*", "IN_SYNC|PENDING_RESTART")])


def sc_pause(sm):
    cl, rep = sm.cl, sm.rep
    tm = Trace(cl, sm.m, sm.db_id)
    body = {"database": sm.db_id, "peer": cl.h(rep)["node_id"], "reason": "test bed: states pause"}
    cl.api(sm.m, "POST", "/v1/shipping/pause", body)
    try:
        sm.load_on()
        sm.wait(sm.m, "LAGGING", 240)
    finally:
        sm.load_off()
        cl.api(sm.m, "POST", "/v1/shipping/resume", body, check_status=False)
    sm.wait(sm.m, "IN_SYNC", 300)
    check(sm, "pause", "master", tm, [("T10", "IN_SYNC", "LAGGING"), ("T9", "LAGGING", "IN_SYNC")])


def sc_replica_lag(sm):
    cl, rep = sm.cl, sm.rep
    tr = Trace(cl, rep, sm.rid(rep))
    try:
        fb_svc(cl, rep, "stop")
        sm.load_on()
        sm.wait(rep, "LAGGING", 300)
    finally:
        sm.load_off()
        fb_svc(cl, rep, "start")
    sm.wait(rep, "IN_SYNC", 400)
    check(sm, "replica-lag", rep, tr, [("T13", "IN_SYNC", "LAGGING"), ("T14", "LAGGING", "IN_SYNC")])


def sc_blocked(sm):
    cl, rep = sm.cl, sm.rep
    tr = Trace(cl, rep, sm.rid(rep))
    note = ""
    try:
        fb_svc(cl, rep, "stop")
        segs = fill_mailbox(sm, rep)
        if not segs:
            raise TbError("no segment reached the replica mailbox")
        cl.hostctl(rep, "remove-file", {"path": segs[0]["path"]})
        note = f"removed {segs[0]['path'].replace(chr(92), '/').split('/')[-1]} of {len(segs)}"
    finally:
        fb_svc(cl, rep, "start")
    sm.wait(rep, "LAGGING/blocked_on_segment", 180)
    sm.wait(rep, "IN_SYNC", 400)
    check(sm, "blocked", rep, tr, [("T12", "*", "LAGGING/blocked_on_segment"),
                                   ("T14", "LAGGING/blocked_on_segment|LAGGING", "IN_SYNC")], note)


def sc_backpressure(sm):
    cl, rep = sm.cl, sm.rep
    tm = Trace(cl, sm.m, sm.db_id)
    old = sm.set_limits(rep, {"mailbox_pending_ceiling": 2})
    row = {}
    try:
        fb_svc(cl, rep, "stop")
        sm.load_on()
        sm.wait(sm.m, "LAGGING", 240)
        row = sm.ledger(rep)
    finally:
        sm.load_off()
        fb_svc(cl, rep, "start")
        sm.set_limits(rep, {"mailbox_pending_ceiling": old.get("mailbox_pending_ceiling") or 0})
    sm.wait(sm.m, "IN_SYNC", 400)
    check(sm, "backpressure", "master", tm, [("T11", "IN_SYNC", "LAGGING"), ("T9", "LAGGING", "IN_SYNC")],
          "ledger " + ", ".join(f"{k}={v}" for k, v in row.items() if "back" in k or "last_" in k))


def sc_apply_errors(sm):
    cl, rep = sm.cl, sm.rep
    tr = Trace(cl, rep, sm.rid(rep))
    sm.inject_log(rep, sm.rpath(rep), "Test bed: simulated apply error", count=5)
    sm.wait(rep, "NEEDS_ATTENTION", 120)
    sm.load(60)
    sm.wait(rep, "IN_SYNC", 300)
    check(sm, "apply-errors", rep, tr, [("T15", "*", "NEEDS_ATTENTION/replication_log_error"),
                                        ("T16", "NEEDS_ATTENTION", "IN_SYNC")], "simulated: 5 ERROR blocks")


def sc_master_stop(sm):
    cl = sm.cl
    tm = Trace(cl, sm.m, sm.db_id)
    sm.inject_log(sm.m, sm.path, "Replication is stopped due to critical error(s)", role="master")
    sm.wait(sm.m, "NEEDS_ATTENTION", 120)
    sm.load(90)
    sm.wait(sm.m, "IN_SYNC", 300)
    check(sm, "master-stop", "master", tm, [("T15", "*", "NEEDS_ATTENTION/replication_log_error"),
                                            ("T17", "NEEDS_ATTENTION", "LAGGING"), ("T9", "LAGGING", "IN_SYNC")],
          "simulated: 'Replication is stopped'")


def sc_disabled(sm):
    cl, rep = sm.cl, sm.rep
    tr = Trace(cl, rep, sm.rid(rep))
    sm.inject_log(rep, sm.rpath(rep), "Replication is disabled for this database (test bed)")
    sm.wait(rep, "DISABLED", 120)
    sm.recover("disabled")
    check(sm, "disabled", rep, tr, [("T33", "*", "DISABLED"), ("T32", "DISABLED", "IN_SYNC|PENDING_RESTART")],
          "simulated: ERROR ... disabled")


def sc_constraint(sm):
    cl, rep = sm.cl, sm.rep
    tr = Trace(cl, rep, sm.rid(rep))
    sm.inject_log(rep, sm.rpath(rep), 'violation of PRIMARY or UNIQUE KEY constraint "TB_PK" on table "TB_TEST"',
                  count=3)
    sm.wait(rep, "NEEDS_REINIT", 120)
    sm.recover("constraint")
    check(sm, "constraint", rep, tr, [("T21", "*", "NEEDS_REINIT"),
                                      ("T32", "NEEDS_REINIT", "IN_SYNC|PENDING_RESTART")],
          "simulated: 3 key violations")


def conflict_push(sm, rep):
    """The last sequence the replica received, again, with other bytes."""
    rec = sm.rec(rep, sm.rid(rep)) or {}
    seq = int(rec.get("last_received") or 0) or int(sm.ledger(rep).get("last_acked") or 0)
    if seq <= 0:
        raise TbError("the replica has received no segment yet")
    meta = {"db_id": sm.rid(rep), "sequence": seq, "generation": int(rec.get("generation") or 0),
            "sha256": "auto"}
    return sm.push_segment(rep, meta, random_bytes=4096)


def sc_conflict(sm):
    cl, rep = sm.cl, sm.rep
    tm = Trace(cl, sm.m, sm.db_id)
    r = conflict_push(sm, rep)
    code = ((r.get("body") or {}).get("error") or {}).get("code") if isinstance(r.get("body"), dict) else None
    try:
        sm.load_on()
        sm.wait(sm.m, "NEEDS_ATTENTION/sequence_conflict", 240)
    finally:
        sm.load_off()
        cl.api(rep, "POST", f"/v1/databases/{sm.rid(rep)}/hold/release", {}, check_status=False)
    check(sm, "conflict", "master", tm, [("T20", "*", "NEEDS_ATTENTION/sequence_conflict")],
          f"replica answered {r.get('status')} {code}")
    sm.recover("conflict")


def other_segment(sm):
    """An archived segment of another database on the master (another GUID)."""
    root = sm.cl.h(sm.m)["paths"]["db_root"]
    hst = sm.cl.host(sm.m)
    for pat in (hst.join(root, "*", "*", "*.LogArch", "*journal-*"), hst.join(root, "*", "*", "*", "*.LogArch", "*journal-*")):
        for f in sm.cl.hostctl(sm.m, "files", {"glob": pat}) or []:
            if not f["path"].replace("\\", "/").lower().startswith(sm.path.replace("\\", "/").lower() + "."):
                return f["path"]
    return None


def sc_foreign(sm):
    cl, rep = sm.cl, sm.rep
    seg = other_segment(sm)
    if not seg:
        sm.res.record("T23 *->NEEDS_REINIT [foreign]", SKIP, note="no archived segment of another database on the master")
        return
    tr = Trace(cl, rep, sm.rid(rep))
    rec = sm.rec(rep, sm.rid(rep)) or {}
    seq = int(rec.get("last_received") or 0) + 500
    r = sm.push_segment(rep, {"db_id": sm.rid(rep), "sequence": seq, "generation": int(rec.get("generation") or 0),
                              "sha256": "auto"}, file=seg)
    sm.wait(rep, "NEEDS_REINIT", 60)
    check(sm, "foreign", rep, tr, [("T23", "*", "NEEDS_REINIT")], f"replica answered {r.get('status')}")
    sm.recover("foreign")


def sc_replaced(sm):
    cl = sm.cl
    tm = Trace(cl, sm.m, sm.db_id)
    cl.hostctl(sm.m, "db-new-guid", {"db": sm.path, "fb_service": cl.fb_service(sm.m)}, timeout=900)
    sm.load(60)
    sm.wait(sm.m, "NEEDS_REINIT", 240)
    sm.recover("replaced")
    check(sm, "replaced", "master", tm, [("T24", "*", "NEEDS_REINIT"), ("T29", "NEEDS_REINIT", "SEEDING"),
                                         ("T30", "SEEDING", "IN_SYNC")])


def sc_segment_lost(sm):
    cl, rep = sm.cl, sm.rep
    tm = Trace(cl, sm.m, sm.db_id)
    tr = Trace(cl, rep, sm.rid(rep))
    note = ""
    try:
        fb_svc(cl, rep, "stop")
        segs = fill_mailbox(sm, rep)
        if not segs:
            raise TbError("no segment reached the replica mailbox")
        name = segs[0]["path"].replace("\\", "/").split("/")[-1]
        seq = name.rsplit("-", 1)[-1]
        cl.hostctl(rep, "remove-file", {"path": segs[0]["path"]})
        arch = cl.hostctl(sm.m, "files", {"glob": sm.path + ".LogArch/*journal-" + seq}) or []
        for f in arch:
            cl.hostctl(sm.m, "remove-file", {"path": f["path"]})
        note = f"segment {int(seq)}: removed from the mailbox and {len(arch)} archive file(s)"
    finally:
        fb_svc(cl, rep, "start")
    sm.wait(sm.m, "NEEDS_REINIT", 300)
    check(sm, "segment-lost", rep, tr, [("T12", "*", "LAGGING/blocked_on_segment")], note)
    check(sm, "segment-lost", "master", tm, [("T25", "*", "NEEDS_REINIT")], note)
    sm.recover("segment-lost")


def sc_disk(sm):
    cl, rep = sm.cl, sm.rep
    tm = Trace(cl, sm.m, sm.db_id)
    body = {"database": sm.db_id, "peer": cl.h(rep)["node_id"], "reason": "test bed: states disk"}
    old = None
    try:
        cl.api(sm.m, "POST", "/v1/shipping/pause", body)
        sm.load(60)
        old = sm.set_limits(sm.m, {"free_space_floor_gb": 1000000})
        st, out = cl.api(sm.m, "POST", "/v1/cleanuparchive", {"database": sm.db_id}, check_status=False)
        sm.wait(sm.m, "NEEDS_REINIT", 120)
    finally:
        if old is not None:
            sm.set_limits(sm.m, {"free_space_floor_gb": old.get("free_space_floor_gb") or 0})
        cl.api(sm.m, "POST", "/v1/shipping/resume", body, check_status=False)
    check(sm, "disk", "master", tm, [("T27", "*", "NEEDS_REINIT")], "free_space_floor_gb 1000000, peer paused")
    sm.recover("disk")


def sc_reinit_fail(sm):
    cl, rep = sm.cl, sm.rep
    tm = Trace(cl, sm.m, sm.db_id)
    try:
        node_svc(cl, rep, "stop")
        st, r = sm.start_reinit(rep)
        if st in (200, 202):
            op = cl.wait_op(sm.m, r["operation_id"], timeout=600)
            note = f"reinit {op.get('state')}: {str(op.get('error'))[:150]}"
        else:
            note = f"reinit refused: HTTP {st}"
        sm.wait(sm.m, "FAILED", 60)
    finally:
        node_svc(cl, rep, "start")
        wait_node(cl, rep, 180)
    sm.recover("reinit-fail")
    check(sm, "reinit-fail", "master", tm, [("T29", "*", "SEEDING"), ("T31", "SEEDING", "FAILED"),
                                            ("T29", "FAILED", "SEEDING"), ("T30", "SEEDING", "IN_SYNC")], note)


def stale_setup(sm, rep):
    """The master node stops the moment the reinit released the nbackup lock:
    the replica got the image and places it with the new generation, the
    master fails the job and rolls its generation back. Returns a note."""
    cl = sm.cl
    gen0 = int((sm.rec(rep, sm.rid(rep)) or {}).get("generation") or 0)
    t = sm.node_on_file("unlocked", "stop", timeout=900)
    st, _ = sm.start_reinit(rep)
    t.join(960)
    node_svc(cl, sm.m, "start")
    wait_node(cl, sm.m, 180)
    end = time.time() + 180
    g = gen0
    while time.time() < end:
        g = int((sm.rec(rep, sm.rid(rep)) or {}).get("generation") or 0)
        if g > gen0:
            break
        time.sleep(5)
    mrec = sm.rec(sm.m) or {}
    return (f"reinit HTTP {st}; watcher {t.result}; replica generation {gen0}->{g}; "
            f"master {mrec.get('state')} generation {mrec.get('generation')}")


def sc_stale(sm):
    cl, rep = sm.cl, sm.rep
    tm = Trace(cl, sm.m, sm.db_id)
    note = stale_setup(sm, rep)
    if sm.state(sm.m).startswith("FAILED"):
        cl.api(sm.m, "POST", "/v1/publication/sync", {}, check_status=False)
    try:
        sm.load_on()
        sm.wait(sm.m, "NEEDS_REINIT", 300)
    finally:
        sm.load_off()
    check(sm, "stale", "master", tm, [("T31", "SEEDING", "FAILED"), ("T7", "FAILED", "PUBLISHING"),
                                      ("T26", "*", "NEEDS_REINIT")], note)
    sm.recover("stale")


def sc_orphan(sm):
    cl = sm.cl
    tm = Trace(cl, sm.m, sm.db_id)
    old = list((sm.node_config(sm.m).get("databases") or {}).get("exclude_paths") or [])
    try:
        cl.api(sm.m, "PUT", "/v1/config", {"databases": {"exclude_paths": old + [sm.path]}})
        sm.wait(sm.m, "ORPHANED", 90)
    finally:
        cl.api(sm.m, "PUT", "/v1/config", {"databases": {"exclude_paths": old}}, check_status=False)
    sm.wait(sm.m, "UNCONFIGURED|CONFIGURED|PENDING_RESTART", 90)
    if sm.state(sm.m).startswith("PENDING_RESTART"):
        ops.restart_firebird(cl, sm.m, "test bed: states orphan")
    cl.api(sm.m, "POST", "/v1/publication/sync", {}, check_status=False)
    sm.load(60)
    s = sm.wait(sm.m, "IN_SYNC", 300)
    check(sm, "orphan", "master", tm, [("T34", "*", "ORPHANED"), ("T35", "ORPHANED", "UNCONFIGURED|CONFIGURED"),
                                       ("T4", "UNCONFIGURED|CONFIGURED", "PENDING_RESTART"),
                                       ("T6", "PENDING_RESTART", "CONFIGURED"), ("T9", "CONFIGURED", "IN_SYNC")],
          f"master {s} at the end")
    if s != "IN_SYNC":
        sm.recover("orphan")


def sc_remove(sm):
    cl = sm.cl
    tm = Trace(cl, sm.m, sm.db_id)
    ops.dbs_remove(cl, sm.a.subdir)
    gone = sm.rec(sm.m) is None
    check(sm, "remove", "master", tm, [("T34", "*", "ORPHANED")])
    sm.res.record(f"T36 ORPHANED->(forgotten) [remove, master]", "PASS" if gone else "FAIL",
                  note="record removed" if gone else f"record still there: {sm.state(sm.m)}")
    sm.db_id = None


def settled(sm, timeout=120):
    """Master and replicas IN_SYNC (a replica after a reinit gets there
    through PENDING_RESTART and CONFIGURED)."""
    return sm.wait(sm.m, "IN_SYNC", timeout) == "IN_SYNC" and all(
        sm.wait(r, "IN_SYNC", timeout, db_id=sm.rid(r)) == "IN_SYNC" for r in sm.reps)


RUN = {"create": sc_create, "pause": sc_pause, "replica-lag": sc_replica_lag, "blocked": sc_blocked,
       "backpressure": sc_backpressure, "apply-errors": sc_apply_errors, "master-stop": sc_master_stop,
       "disabled": sc_disabled, "constraint": sc_constraint, "conflict": sc_conflict, "foreign": sc_foreign,
       "replaced": sc_replaced, "segment-lost": sc_segment_lost, "disk": sc_disk, "reinit-fail": sc_reinit_fail,
       "stale": sc_stale, "orphan": sc_orphan, "remove": sc_remove}


def run(cl, a):
    reps = pick_replicas(cl, a.replicas)
    names = SCENARIOS if a.only == "all" else [s.strip() for s in a.only.split(",")]
    for n in names:
        if n not in RUN:
            raise TbError(f"unknown scenario '{n}' (known: {', '.join(SCENARIOS)})")
    if a.keep and "remove" in names:
        names.remove("remove")
    res = Results("states", vars(a).copy())
    sm = SM(cl, res, a, reps)
    if names[0] != "create":
        dbs = cl.test_dbs(a.subdir)
        if not dbs:
            raise TbError(f"no test database in {a.subdir}: run with 'create' first")
        sm.use(dbs[0])
    for n in names:
        log(f"=== states: {n}")
        try:
            RUN[n](sm)
        except Exception as e:  # noqa: BLE001 - one scenario must not stop the others
            res.record(f"{n}: run", "FAIL", note=str(e)[:300])
            if n == "create":
                break
        finally:
            sm.load_off()
            fb_start_all(cl, reps)
        if sm.db_id and n != "remove" and not settled(sm):
            sm.recover(n)
    if sm.db_id:
        converge_and_record(cl, res, "converge", [sm.path], 900, reps)
    return res.finish()
