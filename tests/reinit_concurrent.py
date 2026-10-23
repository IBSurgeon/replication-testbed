"""Two reinits at once, each to another replica (hqcluster-node review
2026-10-22, finding C-04).

The master had one reinit channel for the whole process, and each run aimed
it at its own replica. With limits.max_concurrent_reinits above 1, run B
re-aimed it while run A sent chunks: A's chunks and its Complete went to B's
replica. Now each peer has a channel of its own.

The test sets max_concurrent_reinits (--limit, default 2) and a low
reinit_window_mbps (--window-mbps, default 2, so the copies take long enough
to overlap) through PUT /v1/config, and restarts the master node: both are
read only at start. Two test databases and two replicas; under a light load,
per round both reinits start at the same moment (round 1: db1 -> replica1
and db2 -> replica2; round 2 crossed):

  limits in effect    GET /v1/config shows the two values after the restart
  overlap             at one poll both jobs (GET .../reinit, our operation
                      ids) held a slot: phase prepare to catchup. SKIP when
                      they never overlapped (databases too small for
                      --window-mbps)
  <db> -> <replica>   the operation succeeded, the job is done, its peer_id
                      is that replica; no stale_generation, chunk or complete
                      error in the operation or the job
  generation          the target replica's record of the database got a new
                      generation; the other replica's record of it did not
                      change (it got no image of that database)
  journal             the master's reinit_step prepare event of each run
                      names its own replica; no stale_generation anywhere in
                      the master's journal or alerts for the round
  received            each replica's db-file-ops.jsonl has reinit_receive
                      only for the database sent to it
  converge            the load stops, every replica matches the master

The same database cannot be reinitialized twice at once (the second run
fails "reinit already in progress"), so one test database is not enough:
the test is skipped. finally: the two limits are put back and the master
restarted again.
"""
import time

from tblib import ops
from tblib.cluster import TbError, log
from tblib.results import Results

from ._common import (add_load_args, converge_and_record, ensure_load, norm, pick_replicas,
                      replica_generation, replica_record, wait_generation_above)
from .disasters import node_svc, wait_node

HELP = "two reinits at once to two replicas, max_concurrent_reinits 2 (review C-04)"

LIMITS = ("max_concurrent_reinits", "reinit_window_mbps")
HOLDING = ("prepare", "snapshot", "transfer", "catchup")
BAD_WORDS = ("stale_generation", "chunk", "complete")
TAG = "reinitcc"


def add_args(p):
    p.add_argument("--dbs", default="", help="two db_ids or folders, comma separated; default: the first two")
    p.add_argument("--replicas", default="all", help="the first two are used")
    p.add_argument("--rounds", type=int, default=2, help="round 2 crosses the pairs")
    p.add_argument("--limit", type=int, default=2, help="limits.max_concurrent_reinits during the test")
    p.add_argument("--window-mbps", type=int, default=2,
                   help="limits.reinit_window_mbps during the test, so the copies overlap (0: keep)")
    p.add_argument("--mode", default="standard", choices=["standard", "smooth"])
    p.add_argument("--reinit-timeout", type=int, default=600)
    p.add_argument("--catchup-timeout", type=int, default=600)
    add_load_args(p, tx="off")


def s1(cl, host, cmd, args, timeout=None):
    """One command of module 93-stage1; its result, or TbError."""
    rc, res, _, out = cl.module(host, "93-stage1", cmd, args, check=False, timeout=timeout)
    if rc != 0 or not isinstance(res, dict):
        raise TbError(f"[{host}] 93-stage1 {cmd}: exit {rc} {(out or '')[-400:]}")
    return res


def jlines(cl, host, name):
    return s1(cl, host, "journal", {"node_dir": cl.h(host)["paths"]["node"], "file": name, "from": -1})["lines"]


def jevents(cl, host, name, start, **kw):
    args = {"node_dir": cl.h(host)["paths"]["node"], "file": name, "from": start}
    args.update(kw)
    return s1(cl, host, "journal", args).get("events") or []


def restart_master(cl, m):
    node_svc(cl, m, "restart")
    if not wait_node(cl, m, 180):
        raise TbError(f"{m}: the node did not come back after the restart")


def limits_now(cl, m):
    _, c = cl.api(m, "GET", "/v1/config")
    lim = ((c or {}).get("config") or {}).get("limits") or {}
    return {k: lim.get(k) for k in LIMITS}


def job(cl, db_id):
    st, j = cl.api(cl.cfg.master, "GET", f"/v1/databases/{db_id}/reinit", check_status=False)
    return j if st == 200 and isinstance(j, dict) else {}


def op_state(cl, op_id):
    st, op = cl.api(cl.cfg.master, "GET", f"/v1/operations/{op_id}", check_status=False)
    return op if st == 200 and isinstance(op, dict) else {}


def one_round(cl, res, a, rnd, pairs, reps):
    m = cl.cfg.master
    j0 = jlines(cl, m, "journal.jsonl")
    f0 = {r: jlines(cl, r, "db-file-ops.jsonl") for r in reps}
    gen0 = {(d["db_id"], r): replica_generation(cl, r, d["path"]) for d, _ in pairs for r in reps}
    runs = []
    for d, r in pairs:
        body = {"to": cl.h(r)["node_id"], "mode": a.mode, "ignore_window": True, "allow_restart": True,
                "hold_on_long_transactions": True}
        st, out = cl.api(m, "POST", f"/v1/databases/{d['db_id']}/reinit", body, check_status=False)
        case = f"r{rnd} {d['db_id']} -> {r}"
        if st not in (200, 202) or not isinstance(out, dict):
            res.record(case, "FAIL", note=f"HTTP {st} {out}")
            continue
        runs.append({"case": case, "d": d, "r": r, "op": out["operation_id"], "qpos": out.get("queue_position")})
    if len(runs) < 2:
        raise TbError(f"round {rnd}: only {len(runs)} reinit(s) started")
    log(f"round {rnd}: " + ", ".join(f"{x['case']} (queue {x['qpos']})" for x in runs))

    # Both running: watch both jobs until both operations end.
    overlap, seen = False, []
    end = time.time() + a.reinit_timeout
    while time.time() < end:
        ops_ = [op_state(cl, x["op"]) for x in runs]
        jobs = [job(cl, x["d"]["db_id"]) for x in runs]
        phases = [j.get("phase") if j.get("operation_id") == x["op"] else None for j, x in zip(jobs, runs)]
        seen.append(phases)
        if all(p in HOLDING for p in phases):
            overlap = True
        if all(o.get("state") not in ("in_progress", "queued", "pending", None) for o in ops_):
            break
        time.sleep(3)
    for x in runs:
        x["opr"], x["job"] = op_state(cl, x["op"]), job(cl, x["d"]["db_id"])
    if overlap:
        res.record(f"r{rnd} overlap", "PASS", note="both runs held a slot at the same time")
    else:
        res.record(f"r{rnd} overlap", "SKIP",
                   note=f"the runs did not overlap ({len(seen)} polls; a lower --window-mbps?): phases {seen[-3:]}")

    # Each run: its own replica, no error of the other's traffic.
    for x in runs:
        d, r, o, j = x["d"], x["r"], x["opr"], x["job"]
        rid = cl.h(r)["node_id"]
        errs = " ".join(str(v) for v in (o.get("error"), j.get("error"), j.get("code")) if v)
        bad = [w for w in BAD_WORDS if w in errs.lower()]
        ok = (o.get("state") == "succeeded" and j.get("operation_id") == x["op"] and j.get("phase") == "done"
              and j.get("peer_id") == rid and not bad)
        res.record(x["case"], "PASS" if ok else "FAIL",
                   note=f"op {o.get('state')}, job {j.get('phase')} peer {j.get('peer_id')} gen {j.get('generation')}"
                        + (f"; errors: {errs[:300]}" if errs else ""))
        g = wait_generation_above(cl, r, d["path"], gen0[(d["db_id"], r)], timeout=120)
        others = {rr: replica_generation(cl, rr, d["path"]) for rr in reps if rr != r}
        moved = {rr: (gen0[(d["db_id"], rr)], g2) for rr, g2 in others.items() if g2 != gen0[(d["db_id"], rr)]}
        ok = g > gen0[(d["db_id"], r)] and not moved
        res.record(f"{x['case']} generation", "PASS" if ok else "FAIL",
                   note=f"{r}: {gen0[(d['db_id'], r)]} -> {g} (job {j.get('generation')}); "
                        + (f"other replica changed: {moved}" if moved else "other replica unchanged"))

    # The master's journal: each prepare names its own peer; no stale_generation.
    steps = jevents(cl, m, "journal.jsonl", j0, types="reinit_step")
    for x in runs:
        rid = cl.h(x["r"])["node_id"]
        peers = sorted({str((e.get("fields") or {}).get("peer")) for e in steps
                        if e.get("db_id") == x["d"]["db_id"] and (e.get("fields") or {}).get("phase") == "prepare"})
        res.record(f"{x['case']} journal", "PASS" if peers == [rid] else "FAIL",
                   note=f"prepare events name {peers}")
    stale = jevents(cl, m, "journal.jsonl", j0, grep="stale_generation")
    stale_al = [al for al in ops.node_alerts(cl, m) if "stale_generation" in str(al.get("message") or "")]
    res.record(f"r{rnd} no stale_generation", "FAIL" if stale or stale_al else "PASS",
               note=f"journal {len(stale)}, alerts {len(stale_al)}", events=stale[:3], alerts=stale_al[:3])

    # Each replica received only the database sent to it.
    for r in reps:
        ev = jevents(cl, r, "db-file-ops.jsonl", f0[r], grep="reinit_receive")
        ids = {e.get("db_id") for e in ev}
        names = {norm(e.get("path")) for e in ev}

        def got(d):
            rec = replica_record(cl, r, d["path"]) or {}
            return d["db_id"] in ids or (rec.get("db_id") and rec.get("db_id") in ids) or \
                any(norm(cl.replica_path(r, d["path"])) in n for n in names)

        want = [x["d"] for x in runs if x["r"] == r]
        other = [x["d"] for x in runs if x["r"] != r]
        miss = [d["db_id"] for d in want if not got(d)]
        extra = [d["db_id"] for d in other if got(d)]
        res.record(f"r{rnd} {r} received", "PASS" if not miss and not extra else "FAIL",
                   note=f"{len(ev)} reinit_receive; missing {miss}; not sent here but received {extra}")


def run(cl, a):
    res = Results("reinit_concurrent", vars(a).copy())
    m = cl.cfg.master
    dbs = cl.test_dbs(which=a.dbs or "all")[:2]
    reps = pick_replicas(cl, a.replicas)[:2]
    if len(dbs) < 2:
        res.record("databases", "SKIP", note="needs two test databases: one database cannot be "
                                             "reinitialized twice at once (reinit already in progress)")
        return res.finish()
    if len(reps) < 2:
        res.record("replicas", "SKIP", note="needs two replicas: C-04 is two runs to two peers")
        return res.finish()
    lim0 = limits_now(cl, m)
    log(f"limits before: {lim0}")
    changed = False
    try:
        new = {"max_concurrent_reinits": a.limit}
        if a.window_mbps > 0:
            new["reinit_window_mbps"] = a.window_mbps
        cl.api(m, "PUT", "/v1/config", {"limits": new})
        changed = True
        restart_master(cl, m)
        now = limits_now(cl, m)
        res.record("limits in effect", "PASS" if all(now.get(k) == v for k, v in new.items()) else "FAIL",
                   note=f"{now}")
        if not a.no_load:
            ensure_load(cl, dbs, a, TAG)
        for rnd in range(1, a.rounds + 1):
            if rnd % 2:
                pairs = [(dbs[0], reps[0]), (dbs[1], reps[1])]
            else:
                pairs = [(dbs[0], reps[1]), (dbs[1], reps[0])]
            one_round(cl, res, a, rnd, pairs, reps)
        cl.load_stop(TAG)
        converge_and_record(cl, res, "converge", [d["path"] for d in dbs], a.catchup_timeout, reps)
    except TbError as e:
        res.record("run", "FAIL", note=str(e)[:500])
    finally:
        cl.load_stop(TAG)
        if changed:
            try:
                # A missing value goes back as 0: the node's default.
                cl.api(m, "PUT", "/v1/config", {"limits": {k: (v or 0) for k, v in lim0.items()}})
                restart_master(cl, m)
                log(f"limits put back: {limits_now(cl, m)}")
            except Exception as e:  # noqa: BLE001 - cleanup goes on
                log(f"put back the limits: {e}")
    return res.finish()
