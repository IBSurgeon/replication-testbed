"""Many reinits under load (nbackup -L on the master), for one or more databases.

Each reinit locks the master database with nbackup (-L), copies it to the
replica, unlocks it, and the replica then applies the journal from that
point. Per reinit the test checks:
  - the operation succeeds (smooth: retried while long transactions block it)
  - no <db>.delta is left on the master (the nbackup lock was released)
  - the replica's database generation went up (a new image was placed)
At the end the load stops and every replica must match the master.
"""
import time

from tblib import ops
from tblib.cluster import TbError, log
from tblib.results import Results

from ._common import (add_load_args, converge_and_record, delta_left, ensure_load, limbo_count,
                      pick_replicas, replica_generation, wait_generation_above)

HELP = "N reinit cycles (nbackup -L) under load, one or more databases, smooth/standard"


def add_args(p):
    p.add_argument("--cycles", type=int, default=5)
    p.add_argument("--db", default="all", help="all | db1,db2")
    p.add_argument("--replicas", default="all")
    p.add_argument("--modes", default="standard,smooth", help="used in turn: cycle 1 standard, 2 smooth, ...")
    p.add_argument("--parallel", action="store_true",
                   help="start the reinits of all databases at once (the node queues them)")
    p.add_argument("--gap", type=int, default=30, help="seconds between cycles")
    p.add_argument("--reinit-timeout", type=int, default=1800)
    p.add_argument("--refusal-timeout", type=int, default=600, help="smooth: retry while refused")
    p.add_argument("--catchup-timeout", type=int, default=900)
    add_load_args(p)


def check_after(cl, res, case, d, rep, gen0, op):
    ok = op.get("state") == "succeeded"
    res.record(f"{case} reinit", "PASS" if ok else "FAIL", note=op.get("error", "") or op.get("state"))
    if not ok:
        return
    left = delta_left(cl, d["path"])
    res.record(f"{case} no .delta", "FAIL" if left else "PASS", note=", ".join(left))
    g = wait_generation_above(cl, rep, d["path"], gen0)
    res.record(f"{case} generation", "PASS" if g > gen0 else "FAIL", note=f"{gen0} -> {g}")


def run(cl, a):
    dbs = cl.test_dbs(which=a.db)
    if not dbs:
        raise TbError("no test databases (run 'tb.py dbs prepare')")
    reps = pick_replicas(cl, a.replicas)
    modes = [m.strip() for m in a.modes.split(",") if m.strip()]
    res = Results("reinit_cycles", vars(a).copy())
    tag = "reinit"
    log(f"reinit cycles: {a.cycles} x {len(dbs)} db x {len(reps)} replica, modes {modes}, "
        f"{'parallel' if a.parallel else 'one by one'}")
    try:
        if not a.no_load:
            ensure_load(cl, dbs, a, tag)
        for cycle in range(1, a.cycles + 1):
            mode = modes[(cycle - 1) % len(modes)]
            for rep in reps:
                if a.parallel:
                    started = []
                    for d in dbs:
                        gen0 = replica_generation(cl, rep, d["path"])
                        body = {"to": cl.h(rep)["node_id"], "mode": mode, "ignore_window": True,
                                "allow_restart": True, "hold_on_long_transactions": True}
                        st, r = cl.api(cl.cfg.master, "POST", f"/v1/databases/{d['db_id']}/reinit", body,
                                       check_status=False)
                        case = f"c{cycle} {mode} {d['db_id']} -> {rep}"
                        if st in (200, 202):
                            started.append((case, d, gen0, r["operation_id"], r.get("queue_position")))
                        else:
                            res.record(f"{case} reinit", "FAIL", note=f"HTTP {st} {r}")
                    for case, d, gen0, op_id, qpos in started:
                        op = cl.wait_op(cl.cfg.master, op_id, timeout=a.reinit_timeout)
                        check_after(cl, res, case + (f" (queue {qpos})" if qpos else ""), d, rep, gen0, op)
                else:
                    for d in dbs:
                        case = f"c{cycle} {mode} {d['db_id']} -> {rep}"
                        gen0 = replica_generation(cl, rep, d["path"])
                        t0 = time.time()
                        try:
                            op = ops.reinit(cl, d["db_id"], rep, mode=mode, timeout=a.reinit_timeout,
                                            refusal_timeout=a.refusal_timeout)
                        except TbError as e:
                            res.record(f"{case} reinit", "FAIL", note=str(e)[:300])
                            continue
                        log(f"{case}: {op.get('state')} in {int(time.time() - t0)}s")
                        check_after(cl, res, case, d, rep, gen0, op)
            if not a.no_load:
                ensure_load(cl, dbs, a, tag)        # a reinit may drop load connections
            if cycle < a.cycles:
                time.sleep(a.gap)
    finally:
        if not a.no_load:
            cl.load_stop(tag)
    for d in dbs:
        n = limbo_count(cl, d["path"])
        if n:
            res.record(f"limbo {d['db_id']}", "FAIL", note=f"{n} limbo transaction(s) on the master")
        converge_and_record(cl, res, f"converge {d['db_id']}", [d["path"]], a.catchup_timeout, reps)
    return res.finish()
