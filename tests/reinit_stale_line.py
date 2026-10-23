"""Old replication.log lines do not count against a freshly reinitialised
replica (hqcluster-node review 2026-10-22: D-02).

Firebird retries a failed segment on the old image and logs the same error
until the placement stops it. Before the fix the node read the last of those
lines after the reinit: ConsecutiveUnrecoverable had survived the reset, one
stale constraint violation made the new copy NEEDS_REINIT again, and RCM
reinitialised it in a loop. Now the reset (ResetDB, when the copy is placed)
first reads the log to its end and drops the error lines of that database,
then zeroes the counters and clears the apply alerts.

Steps, on one replica and one test database (load off until step 5):
  1. before the reinit: probe lines (stats.received), 1 constraint violation
     naming the current segment and 4 generic errors: apply_error and
     NEEDS_ATTENTION
  2. the reinit (POST /v1/databases/{db_id}/reinit on the master, as
     reinit_cycles does). While the copy is placed (<file>.hqcluster-part,
     <file>.new or <file>.hqc-seeding.json exists) constraint violations
     naming old segment numbers go into the log every few seconds: the
     lines a failing old image writes
  3. after it: the operation succeeded, the generation went up, apply_error
     and replication_unrecoverable_error are gone, stats.received was reset,
     no NEEDS_REINIT anywhere in the state chain, and still none two polls
     later
  4. 2 more constraint violations naming old segment numbers (below the
     limit of 3 when the counters were reset): no alert, no NEEDS_REINIT or
     NEEDS_ATTENTION, received unchanged
  5. a short load, and the replica matches the master

Lines written after the reset count as usual (by design): step 4 shows that
the counters started from 0, not that such lines are ignored.
"""
import base64
import threading
import time

from tblib import ops
from tblib.cluster import TbError, log
from tblib.results import Results

from ._common import add_load_args, converge_and_record, ensure_load, pick_replicas, wait_generation_above
from ._sm import Trace
from .disasters import node_svc, wait_node
from .logrotate_tail import ERR, PROBE, Tail, quiet

HELP = "error lines of the old image read after a reinit: counters and alerts reset, no NEEDS_REINIT loop (D-02)"

TAG = "reinit_stale_line"
LIMIT = 5           # apply_error after 5 errors in a row
UNRECOVERABLE = 3   # NEEDS_REINIT after 3 constraint violations in a row


def add_args(p):
    p.add_argument("--db", default="db1", help="one test database")
    p.add_argument("--replica", default="", help="one replica; default: the first")
    p.add_argument("--mode", default="standard", help="reinit mode")
    p.add_argument("--probes", type=int, default=50, help="probe lines before the reinit (stats.received)")
    p.add_argument("--inject-every", type=float, default=3, help="seconds between stale lines during the placement")
    p.add_argument("--reinit-timeout", type=int, default=1800)
    p.add_argument("--poll-timeout", type=int, default=90, help="seconds to wait for one apply poll (30 s tick)")
    p.add_argument("--catchup-timeout", type=int, default=600)
    add_load_args(p)


def constraint(seq):
    return (f'violation of PRIMARY or UNIQUE KEY constraint "TB_PK" on table "TB_TEST" '
            f'(test bed, reinit_stale_line) At segment {seq}, offset 100')


def record(cl, rep, rid):
    st, r = cl.api(rep, "GET", f"/v1/databases/{rid}", check_status=False)
    return r if st == 200 and isinstance(r, dict) else {}


def wait_state(t, want, timeout):
    end = time.time() + timeout
    s = t.state()
    while s not in want and time.time() < end:
        time.sleep(10)
        s = t.state()
    return s


def run(cl, a):
    dbs = cl.test_dbs(which=a.db)
    if not dbs:
        raise TbError("no test databases (run 'tb.py dbs prepare')")
    db = dbs[0]
    reps = pick_replicas(cl, a.replica) if a.replica else list(cl.cfg.replicas[:1])
    if len(reps) != 1:
        raise TbError(f"--replica names one replica, got {reps}")
    rep = reps[0]
    t = Tail(cl, a, db, rep)        # uses a.poll_timeout and a.catchup_timeout
    res = Results("reinit_stale_line", vars(a).copy())
    log(f"reinit_stale_line: {t.rid} on {rep}, log {t.log}")
    out = {}
    try:
        quiet(t, a)
        t.reset()
        rec0 = record(cl, rep, t.rid)
        gen0 = int(rec0.get("generation") or 0)
        old = max(int(rec0.get("last_segment") or 0), int(t.stats().get("last_segment_number") or 0), 1)

        # 1. errors of the old image, read before the reinit
        t.inject(PROBE, count=a.probes, level="VERBOSE")
        t.inject(constraint(old))
        t.inject(ERR, count=LIMIT - 1)
        t.settle()
        recv0, al0, s0 = t.received(), t.alerts("apply_error"), t.state()
        ok = bool(al0) and s0 == "NEEDS_ATTENTION" and recv0 >= a.probes
        res.record("before: apply_error, NEEDS_ATTENTION", "PASS" if ok else "FAIL",
                   note=f"state {s0}, apply_error {'raised' if al0 else 'missing'}, received {recv0}")

        # 2. the reinit, with stale lines while the copy is placed
        tr = Trace(cl, rep, t.rid)
        watch = ";".join(t.rpath + s for s in (".hqcluster-part", ".new", ".hqc-seeding.json"))

        def inject_while():
            out["inj"] = t.jcall("inject-while", {
                "path": t.log, "db": t.rpath, "role": "replica", "level": "ERROR",
                "message_b64": base64.b64encode(constraint(max(old - 1, 1)).encode()).decode(),
                "watch": watch, "burst": UNRECOVERABLE, "every": a.inject_every, "max": 200, "gap": 10,
                "timeout": a.reinit_timeout, "first_timeout": 300}, timeout=a.reinit_timeout + 120)

        th = threading.Thread(target=inject_while, daemon=True)
        th.start()
        time.sleep(2)
        t0 = time.time()
        op = ops.reinit(cl, db["db_id"], rep, mode=a.mode, timeout=a.reinit_timeout)
        log(f"reinit: {op.get('state')} in {int(time.time() - t0)}s")
        th.join(360)
        inj = out.get("inj") or {}
        ok = op.get("state") == "succeeded"
        res.record("reinit", "PASS" if ok else "FAIL", note=op.get("error", "") or op.get("state"))
        if not ok:
            raise TbError(f"reinit: {op.get('state')} {op.get('error', '')}")
        if inj.get("seen") and inj.get("injected"):
            res.record("stale lines during the placement", "PASS",
                       note=f"{inj.get('injected')} blocks in {inj.get('watched_sec')}s, "
                            f"last {inj.get('last_write_before_gone')}s before the files were gone")
        else:
            res.record("stale lines during the placement", "SKIP",
                       note=f"no placement file seen ({inj or 'injector failed'}): only step 1 lines were stale")
        g = wait_generation_above(cl, rep, db["path"], gen0)
        res.record("generation", "PASS" if g > gen0 else "FAIL", note=f"{gen0} -> {g}")

        # 3. the reset: alerts and counters
        t.settle(2)
        al = t.alerts("apply_error")
        alu = t.alerts("replication_unrecoverable_error")
        res.record("alerts cleared by the reinit", "FAIL" if al or alu else "PASS",
                   note=f"apply_error {len(al)}, replication_unrecoverable_error {len(alu)}"
                        + (f": {(alu or al)[0].get('message', '')[:150]}" if al or alu else ""))
        recv1 = t.received()
        res.record("counters reset", "PASS" if recv1 < recv0 else "FAIL",
                   note=f"received {recv0} before, {recv1} after (the copy's own segments count from 0)")
        chain = tr.chain()
        bad = [s for s in chain if s.startswith("NEEDS_REINIT")]
        res.record("no NEEDS_REINIT after the stale lines", "FAIL" if bad else "PASS", note=" -> ".join(chain)[:300])
        s1 = wait_state(t, ("IN_SYNC",), 300)
        res.record("replica back in sync", "PASS" if s1 == "IN_SYNC" else "FAIL", note=str(s1))

        # 4. lines naming old segments, after the reset: below the limits
        recv2 = t.received()
        t.inject(constraint(old), count=UNRECOVERABLE - 1)
        t.settle()
        al = t.alerts("apply_error") + t.alerts("replication_unrecoverable_error")
        s2, recv3 = t.state(), t.received()
        ok = not al and s2 not in ("NEEDS_REINIT", "NEEDS_ATTENTION") and recv3 == recv2
        res.record("old segment lines after the reset change nothing", "PASS" if ok else "FAIL",
                   note=f"state {s1} -> {s2}, alerts {[x.get('code') for x in al]}, received {recv2} -> {recv3}")
        t.reset()           # end the streak of 2
    except TbError as e:
        res.record("run", "FAIL", note=str(e)[:300])
    finally:
        if not wait_node(cl, rep, 10):
            node_svc(cl, rep, "start")
            wait_node(cl, rep, 180)

    # 5. a short load, then the replica must match the master
    if not a.no_load:
        ensure_load(cl, [db], a, TAG)
        time.sleep(60)
        cl.load_stop(TAG)
    s = wait_state(t, ("IN_SYNC",), 300)
    al = t.alerts("apply_error") + t.alerts("replication_unrecoverable_error")
    res.record("end: IN_SYNC, no apply alerts", "PASS" if s == "IN_SYNC" and not al else "FAIL",
               note=f"state {s}, alerts {[x.get('code') for x in al]}")
    converge_and_record(cl, res, "converge", [db["path"]], a.catchup_timeout, [rep])
    return res.finish()
