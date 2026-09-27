"""Basic replication: load for a while, stop, all replicas must match."""
import time

from tblib.cluster import TbError, log
from tblib.results import Results

from ._common import add_load_args, converge_and_record, pick_replicas

HELP = "load N minutes, then every replica must have the master's rows"


def add_args(p):
    p.add_argument("--db", default="all")
    p.add_argument("--replicas", default="all")
    p.add_argument("--minutes", type=int, default=3)
    p.add_argument("--catchup-timeout", type=int, default=900)
    add_load_args(p, tx="off")


def run(cl, a):
    dbs = cl.test_dbs(which=a.db)
    if not dbs:
        raise TbError("no test databases (run 'tb.py dbs prepare')")
    reps = pick_replicas(cl, a.replicas)
    res = Results("basic", vars(a).copy())
    tag = "basic"
    if not a.no_load:
        cl.load_stop(tag)
        cl.load_start([d["path"] for d in dbs], mode=a.load_mode, tx=a.tx, conns=a.conns, tag=tag)
        log(f"load for {a.minutes} min on {len(dbs)} database(s)")
        time.sleep(a.minutes * 60)
        cl.load_stop(tag)
    for d in dbs:
        converge_and_record(cl, res, f"converge {d['path']}", [d["path"]], a.catchup_timeout, reps)
    return res.finish()
