"""Periodic data check (hqcluster-node N-09): turn it on with a short interval,
run load, stop, and check the history on every node and the master's
comparisons. Under load a pair must be "not_comparable", never a mismatch;
once quiet, every replica must get a "match"."""
import time

from tblib.cluster import TbError, log
from tblib.results import Results

from ._common import add_load_args, converge_and_record, pick_replicas
from .disasters import node_svc, wait_node

HELP = "periodic data check: history on every node, no mismatch under load, a match once quiet"


def add_args(p):
    p.add_argument("--db", default="all")
    p.add_argument("--replicas", default="all")
    p.add_argument("--minutes", type=int, default=4, help="load while the check runs")
    p.add_argument("--interval", type=int, default=60, help="data_check_interval_sec")
    p.add_argument("--quiet", type=int, default=30, help="data_check_quiet_sec")
    p.add_argument("--catchup-timeout", type=int, default=900)
    p.add_argument("--match-timeout", type=int, default=600)
    add_load_args(p)


def configure(cl, hosts, limits, tables):
    body = {"limits": limits}
    if tables is not None:
        body["databases"] = {"sync_tables": tables}
    for h in hosts:
        cl.api(h, "PUT", "/v1/config", body)
        node_svc(cl, h, "restart")
    for h in hosts:
        if not wait_node(cl, h, 180):
            raise TbError(f"{h}: the node did not come back after the restart")


def history(cl, host, db_id):
    _, out = cl.api(host, "GET", f"/v1/databases/{db_id}/datacheck")
    return (out or {}).get("entries") or []


def run(cl, a):
    dbs = cl.test_dbs(which=a.db)
    if not dbs:
        raise TbError("no test databases (run 'tb.py dbs prepare')")
    reps = pick_replicas(cl, a.replicas)
    hosts = [cl.cfg.master] + reps
    res = Results("datacheck", vars(a).copy())

    counts = cl.counts(cl.cfg.master, dbs[0]["path"]) or {}
    keyed = sorted(t for t, v in counts.items() if v.get("keyed"))
    if not keyed:
        raise TbError("no keyed table in the test database")
    tables = ",".join(f'"{t}"' for t in keyed)
    log(f"data check on {len(hosts)} nodes, every {a.interval}s, tables {tables}")
    on = {"data_check_enabled": True, "data_check_interval_sec": a.interval,
          "data_check_quiet_sec": a.quiet, "data_check_query_timeout_sec": 120}
    started = time.time()
    try:
        configure(cl, hosts, on, tables)
        tag = "datacheck"
        if not a.no_load:
            cl.load_stop(tag)
            cl.load_start([d["path"] for d in dbs], mode=a.load_mode, tx=a.tx, conns=a.conns, tag=tag)
            log(f"load for {a.minutes} min")
            time.sleep(a.minutes * 60)
            cl.load_stop(tag)
        for d in dbs:
            converge_and_record(cl, res, f"converge {d['db_id']}", [d["path"]], a.catchup_timeout, reps)

        for d in dbs:
            db_id = d["db_id"]
            # Every node keeps a history of its own samples.
            for h in hosts:
                s = [e for e in history(cl, h, db_id) if e.get("kind") == "sample"]
                ok = [e for e in s if e.get("status") == "ok"]
                res.record(f"{db_id} history on {h}", "PASS" if ok else "FAIL",
                           note=f"{len(s)} samples, {len(ok)} ok", last=s[-1] if s else None)
            # Once quiet, every replica gets a match; no mismatch ever.
            end = time.time() + a.match_timeout
            while True:
                cmp_ = [e for e in history(cl, cl.cfg.master, db_id) if e.get("kind") == "compare"]
                matched = {e.get("peer") for e in cmp_ if e.get("status") == "match"}
                want = {cl.h(r)["node_id"] for r in reps}
                if want <= matched or time.time() >= end:
                    break
                time.sleep(15)
            mism = [e for e in cmp_ if e.get("status") == "mismatch"]
            by = {}
            for e in cmp_:
                by.setdefault(e.get("status"), 0)
                by[e.get("status")] += 1
            res.record(f"{db_id} no mismatch", "FAIL" if mism else "PASS",
                       note=f"comparisons {by}", mismatches=mism[:5])
            res.record(f"{db_id} match per replica", "PASS" if want <= matched else "FAIL",
                       note=f"matched {sorted(matched)} of {sorted(want)}")
        _, alerts = cl.api(cl.cfg.master, "GET", "/v1/alerts")
        dm = [x for x in (alerts or []) if isinstance(x, dict) and x.get("code") == "data_mismatch"]
        res.record("no data_mismatch alert", "FAIL" if dm else "PASS", note=f"{len(dm)} alerts", alerts=dm[:5])
    finally:
        log(f"data check off again ({int(time.time() - started)}s)")
        configure(cl, hosts, {"data_check_enabled": False}, None)
    return res.finish()
