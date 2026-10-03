"""The wait for a replica's Firebird restart after a reinit (hqcluster-node 2027.4.4, plan N2).

A replica that places a new database only asks for a restart: debounced,
inside its restart window. The master used to show the reinit's
"restarting" step as done while the replica applied nothing to the database.
Now the step is "waiting" and the job says how (queued, window, retry,
manual); a restart that loads the section ends the wait and leaves no second
restart behind.

Steps, on one replica, with a database of the test's own (subdir tbrw):
  1. the replica's restart window is closed (a one-minute window 12 hours
     away);
  2. reinit the new database to it: the job is done, its restarting step is
     "waiting", job.restart.state queued or window; the replica's copy is
     PENDING_RESTART;
  3. the operator restarts the replica's Firebird through the node: the
     copy's conf goes ACTIVE, the alert restart_required_after_reinit goes;
  4. the window opens (always): no further restart for that database
     (no reinit_restart line in the node's log after step 3);
  5. the copy matches the master after a short load.
The window is put back and the test database removed at the end.
"""
import datetime
import time

from tblib import ops
from tblib.cluster import TbError, log
from tblib.results import Results

from ._common import converge_and_record, pick_replicas, replica_record

HELP = "the wait for the replica's restart after a reinit; no extra restart (node 2027.4.4)"

SUBDIR = "tbrw"


def add_args(p):
    p.add_argument("--replicas", default="", help="one replica; default: the first")
    p.add_argument("--catchup-timeout", type=int, default=600)


def window(cl, r):
    _, cfg = cl.api(r, "GET", "/v1/config")
    return ((cfg or {}).get("windows") or {}).get("restart_window", "")


def set_window(cl, r, w):
    cl.api(r, "PUT", "/v1/config", {"windows": {"restart_window": w}})


def node_log_lines(cl, r, word):
    out = cl.hostctl(r, "files", {"glob": cl.host(r).join(cl.h(r)["paths"]["node"], "logs", "*.log")}) or []
    n = 0
    for f in out:
        t = cl.hostctl(r, "tail", {"path": f["path"], "lines": 400}, check=False)
        n += str(t or "").count(word)
    return n


def run(cl, a):
    res = Results("restartwait", vars(a).copy())
    reps = pick_replicas(cl, a.replicas or "all")
    r = reps[0]
    m = cl.cfg.master
    old = window(cl, r)
    later = (datetime.datetime.now() + datetime.timedelta(hours=12)).strftime("%H:%M")
    end = (datetime.datetime.now() + datetime.timedelta(hours=12, minutes=1)).strftime("%H:%M")
    try:
        dbs = ops.dbs_prepare(cl, count=1, subdir=SUBDIR, seed=False)
        if not dbs:
            raise TbError("no database made in " + SUBDIR)
        d = dbs[0]
        set_window(cl, r, f"{later}-{end}")
        log(f"[{r}] restart window {later}-{end} (closed now)")

        op = ops.reinit(cl, d["db_id"], r)
        _, job = cl.api(m, "GET", f"/v1/databases/{d['db_id']}/reinit")
        steps = {s["id"]: s for s in (job or {}).get("steps", [])}
        rs = steps.get("restarting", {})
        wait = (job or {}).get("restart") or {}
        ok = op.get("state") == "succeeded" and rs.get("state") == "waiting" and wait.get("state") in ("queued", "window")
        res.record("the restarting step waits", "PASS" if ok else "FAIL",
                   note=f"op={op.get('state')} step={rs.get('state')} '{rs.get('detail', '')}' restart={wait}")
        rec = replica_record(cl, r, d["path"]) or {}
        res.record("the copy is PENDING_RESTART", "PASS" if rec.get("conf") == "PENDING_RESTART" else "FAIL",
                   note=f"conf={rec.get('conf')} state={rec.get('state')}")

        mark = time.time()
        ops.restart_firebird(cl, r, "test bed: restartwait, the operator's restart")
        rec = replica_record(cl, r, d["path"]) or {}
        _, al = cl.api(r, "GET", "/v1/alerts")
        left = [x for x in (al or []) if x.get("code") == "restart_required_after_reinit" and x.get("database") == d["db_id"]]
        res.record("the operator's restart ends the wait", "PASS" if rec.get("conf") == "ACTIVE" and not left else "FAIL",
                   note=f"conf={rec.get('conf')} alerts left={len(left)}")

        before = node_log_lines(cl, r, '"reinit_restart"')
        set_window(cl, r, "always")
        time.sleep(90)          # longer than limits.reinit_restart_delay_sec (30 s) and a retry
        after = node_log_lines(cl, r, '"reinit_restart"')
        res.record("no second restart when the window opens", "PASS" if after == before else "FAIL",
                   note=f"reinit_restart lines {before} -> {after} (since {int(time.time() - mark)}s)")

        cl.load_stop("restartwait")
        cl.load_start([d["path"]], mode="write", tx="off", conns="1:2", tag="restartwait")
        time.sleep(60)
        cl.load_stop("restartwait")
        converge_and_record(cl, res, "the copy matches", [d["path"]], a.catchup_timeout, [r])
    except TbError as e:
        res.record("restartwait", "FAIL", note=str(e)[:400])
    finally:
        cl.load_stop("restartwait")
        try:
            set_window(cl, r, old or "always")
        except TbError as e:
            log(f"[{r}] restore window: {e}")
        try:
            ops.dbs_remove(cl, SUBDIR)
        except TbError as e:
            log(f"remove {SUBDIR}: {e}")
    return res.finish()
