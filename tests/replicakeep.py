"""A replica keeps the copies reinit placed (hqcluster-node 2027.4.4, plan N7).

Reinit puts a replica copy at <root>/<master node>/<rel>. A replica whose own
scan does not cover that path (recursive=false, a template that misses the
master's file name, include_paths) used to turn its copies ORPHANED at the
next scansync -- at start, on a config change -- and their sections left
replication.conf; with no other database it refused with "scan would empty
a previously-populated managed region".

Steps, per replica:
  1. databases.recursive=false through PUT /v1/config (that runs a scansync);
  2. no copy goes ORPHANED, the scan neither refuses nor orphans anything;
  3. the node restarts with that config: still nothing ORPHANED;
  4. under load the replicas still match the master;
  5. recursive=true again.
"""
import time

from tblib.cluster import TbError, log
from tblib.results import Results

from ._common import add_load_args, converge_and_record, pick_replicas

HELP = "a replica keeps its reinit copies when its scan does not cover them (node 2027.4.4)"


def add_args(p):
    p.add_argument("--replicas", default="all")
    p.add_argument("--minutes", type=int, default=2)
    p.add_argument("--catchup-timeout", type=int, default=600)
    add_load_args(p, tx="off")


def live(cl, r):
    """The live copies reinit placed (a generation): what must be kept. A
    file the scan enrolled by itself may drop out with recursive=false."""
    return {d["db_id"]: d for d in cl.databases(r)
            if d.get("state") != "ORPHANED" and int(d.get("generation") or 0) > 0}


def set_recursive(cl, r, on):
    st, out = cl.api(r, "PUT", "/v1/config", {"databases": {"recursive": on}}, check_status=False)
    if st != 200:
        raise TbError(f"[{r}] PUT /v1/config recursive={on}: HTTP {st} {out}")
    return out or {}


def run(cl, a):
    res = Results("replicakeep", vars(a).copy())
    dbs = cl.test_dbs()
    if not dbs:
        raise TbError("no test databases (run 'tb.py dbs prepare')")
    reps = pick_replicas(cl, a.replicas)
    tag = "replicakeep"
    try:
        for r in reps:
            before = live(cl, r)
            if not before:
                res.record(f"{r}: copies", "SKIP", note="the replica holds no database")
                continue
            out = set_recursive(cl, r, False)
            scan = out.get("scansync") or {}
            err = out.get("scansync_failed") or ""
            after = live(cl, r)
            lost = sorted(set(before) - set(after))
            res.record(f"{r}: recursive=false keeps every reinit copy",
                       "PASS" if not lost and "scan would empty" not in str(err) else "FAIL",
                       note=f"{len(before)} before, lost {lost} {err}")
            st, dry = cl.api(r, "POST", "/v1/scansync", {"dry_run": True}, check_status=False)
            orph = (dry or {}).get("orphaned") or []
            res.record(f"{r}: a scan orphans nothing", "PASS" if st == 200 and not orph else "FAIL",
                       note=f"HTTP {st} orphaned={orph}")
            cl.hostctl(r, "node-svc", {"action": "restart"})
            time.sleep(20)
            after = live(cl, r)
            lost = sorted(set(before) - set(after))
            res.record(f"{r}: after a node restart nothing is ORPHANED", "PASS" if not lost else "FAIL",
                       note=f"lost {lost}")
        if not a.no_load:
            cl.load_stop(tag)
            cl.load_start([d["path"] for d in dbs], mode=a.load_mode, tx=a.tx, conns=a.conns, tag=tag)
            log(f"load for {a.minutes} min with recursive=false on the replicas")
            time.sleep(a.minutes * 60)
            cl.load_stop(tag)
        converge_and_record(cl, res, "replicas match with recursive=false", [d["path"] for d in dbs],
                            a.catchup_timeout, reps)
    finally:
        if not a.no_load:
            cl.load_stop(tag)
        for r in reps:
            try:
                set_recursive(cl, r, True)
            except TbError as e:
                log(f"[{r}] restore recursive: {e}")
    return res.finish()
