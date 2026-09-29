"""Upgrade of a 2027.1.x node to this build, with a state that needs the
schema 1 migration (hqcluster-node docs/v2-gaps-fix-plan.md, step 4).

  tb.py test upgrade --old-dist DIR

DIR holds the old node binary, named as in artifacts.<os> of the config
(hqclusternode-linux-amd64 ...). Run it right after `install`, before
`dbs prepare`: it removes the node and its state on every host, installs
the old node, and makes a test database in --subdir with it. Then, on the
old version:

  - a replica: NEEDS_REINIT after 3 key violations (simulated);
  - the master: a reinit killed under its nbackup lock (the old node
    records the lock in its state: ReinitLock).

This build's node is installed over it (the state stays), and the cases:

  upgrade: node opens the old state   the node answers, state schema 2001
  upgrade: lock released              the interrupted reinit's .delta goes
  upgrade: master leaves SEEDING      the interrupted reinit ends
  upgrade: replica keeps NEEDS_REINIT reason upgrade_needs_reinit, also after
                                      a minute of load (the first verdicts)
  recover after upgrade               a reinit brings every node to IN_SYNC

The test database is removed at the end unless --keep.
"""
import os
import time

from tblib import ops
from tblib.cluster import TbError, log
from tblib.results import Results

from ._common import add_load_args, delta_left, pick_replicas
from ._sm import SM
from .disasters import wait_node

HELP = "upgrade 2027.1.x -> this build: the schema 1 state migration and an interrupted reinit's lock"

SCHEMA = 2001


def add_args(p):
    p.add_argument("--old-dist", required=True, help="folder with the old hqclusternode binary")
    p.add_argument("--subdir", default="tbup", help="folder of the test database under db_root")
    p.add_argument("--replicas", default="all")
    p.add_argument("--keep", action="store_true", help="keep the test database at the end")
    add_load_args(p)


def install_node(cl, dist):
    new = cl.cfg.artifacts.get("dir")
    cl.cfg.artifacts["dir"] = dist
    try:
        ops.install(cl, "local", "all", only="node")
    finally:
        cl.cfg.artifacts["dir"] = new


def version(cl, host):
    st, v = cl.api(host, "GET", "/v1/version", check_status=False)
    return v if st == 200 and isinstance(v, dict) else {}


def run(cl, a):
    if not os.path.isdir(a.old_dist):
        raise TbError(f"--old-dist: no folder {a.old_dist}")
    reps = pick_replicas(cl, a.replicas)
    res = Results("upgrade", vars(a).copy())
    sm = SM(cl, res, a, reps)
    m = cl.cfg.master

    log("=== upgrade: the old node, with an empty state")
    ops.uninstall(cl, "local", "all", only="node", keep_work=True)
    install_node(cl, os.path.abspath(a.old_dist))
    old = version(cl, m)
    res.record("upgrade: old node installed", "PASS" if old else "FAIL",
               note=f"master version {old.get('version')} schema {old.get('state_schema_version')}")
    if not old:
        return res.finish()

    dbs = ops.dbs_prepare(cl, count=1, subdir=a.subdir, seed=True)
    sm.use(dbs[0])
    try:
        rep = reps[0]
        target = reps[1] if len(reps) > 1 else reps[0]
        sm.inject_log(rep, sm.rpath(rep), 'violation of PRIMARY or UNIQUE KEY constraint "TB_PK" on table "TB_TEST"',
                      count=3)
        before_rep = sm.wait(rep, "NEEDS_REINIT", 120, db_id=sm.rid(rep))
        t = sm.node_on_file("locked", "kill", timeout=900)
        st, _ = sm.start_reinit(target)
        t.join(1000)
        locked = bool(delta_left(cl, sm.path))
        setup = (f"old version: {rep} {before_rep}; reinit to {target} HTTP {st}, master killed "
                 f"under the lock ({t.result}), .delta {'there' if locked else 'gone'}")
        log(setup)
        if not before_rep.startswith("NEEDS_REINIT") or not locked:
            res.record("upgrade: setup", "FAIL", note=setup)

        log("=== upgrade: this build over the old state")
        install_node(cl, cl.cfg.artifacts.get("dir"))
        up = wait_node(cl, m, 180)
        new = version(cl, m)
        schema = int(new.get("state_schema_version") or 0)
        res.record("upgrade: node opens the old state", "PASS" if up and schema == SCHEMA else "FAIL",
                   note=f"version {new.get('version')} schema {schema} (want {SCHEMA}); {setup}")

        end = time.time() + 180
        lock = delta_left(cl, sm.path)
        while lock and time.time() < end:
            time.sleep(5)
            lock = delta_left(cl, sm.path)
        res.record("upgrade: lock released", "PASS" if not lock else "FAIL",
                   note=("no .delta" if not lock else f"confirmed: {lock[0]} still there 180 s after the upgrade"))

        left, secs, s = sm.wait_not(m, "SEEDING", 300)
        rec = sm.rec(m) or {}
        res.record("upgrade: master leaves SEEDING", "PASS" if left else "FAIL",
                   note=f"master {s} after {secs}s; last_op {rec.get('last_op')}")

        rid = sm.rid(rep)
        s0 = sm.state(rep, rid)
        sm.load(60)
        time.sleep(30)
        s1 = sm.state(rep, rid)
        ok = s0 == s1 == "NEEDS_REINIT/upgrade_needs_reinit"
        res.record("upgrade: replica keeps NEEDS_REINIT", "PASS" if ok else "FAIL",
                   note=f"{rep}: right after the upgrade {s0}, after a minute of load {s1}")

        sm.recover("upgrade")
    finally:
        sm.load_off()
        if not a.keep:
            ops.dbs_remove(cl, a.subdir)
    return res.finish()
