"""Upgrade of a 2027.1.x node to this build, with a state that needs the
schema 1 migration (hqcluster-node docs/v2-gaps-fix-plan.md, step 4).

  tb.py test upgrade --old-dist DIR

DIR holds the old node binary, named as in artifacts.<os> of the config
(hqclusternode-linux-amd64 ...). The test removes the node and its state on
every host, installs the old node and makes two test databases in --subdir
with it. It needs no fb-loadgen: nothing here runs load. Then, on the old
version:

  - db2: NEEDS_ATTENTION on the master ("Replication is stopped",
    simulated);
  - db1: NEEDS_REINIT on a replica (3 key violations, simulated), and on the
    master a reinit killed under its nbackup lock, the node kept down (the
    old node records the lock in its state: ReinitLock).

This build's node is installed over it (the state stays), and the cases:

  upgrade: node opens the old state   the node answers, state schema 2001
  upgrade: lock released              this build releases the old node's lock
  upgrade: master leaves SEEDING      the interrupted reinit ends
  upgrade: master keeps NEEDS_ATTENTION   db2 over a minute of reconcile ticks
                                      (without the migration the first tick
                                      made it IN_SYNC)
  upgrade: replica keeps NEEDS_REINIT reason upgrade_needs_reinit
  recover after upgrade               a reinit brings db1 to IN_SYNC everywhere

The test databases are removed at the end unless --keep. The nodes stay on
this build. Run it on a fresh install, or last: it drops every node's state.
"""
import os
import time

from tblib import ops
from tblib.cluster import TbError, log
from tblib.results import Results

from ._common import delta_left, pick_replicas
from ._sm import SM
from .disasters import wait_node

HELP = "upgrade 2027.1.x -> this build: the schema 1 state migration and an interrupted reinit's lock"

SCHEMA = 2001


def add_args(p):
    p.add_argument("--old-dist", required=True, help="folder with the old hqclusternode binary")
    p.add_argument("--subdir", default="tbup", help="folder of the test databases under db_root")
    p.add_argument("--replicas", default="all")
    p.add_argument("--keep", action="store_true", help="keep the test databases at the end")


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
    m = cl.cfg.master
    new_dist = cl.cfg.artifacts.get("dir")

    log("=== upgrade: the old node, with an empty state")
    ops.uninstall(cl, "local", "all", only="node", keep_work=True)
    install_node(cl, os.path.abspath(a.old_dist))
    old = version(cl, m)
    res.record("upgrade: old node installed", "PASS" if old else "FAIL",
               note=f"master version {old.get('version')} schema {old.get('state_schema_version')}")
    if not old:
        return res.finish()

    dbs = ops.dbs_prepare(cl, count=2, subdir=a.subdir, seed=True)
    sm, sm2 = SM(cl, res, a, reps), SM(cl, res, a, reps)
    sm.use(dbs[0])
    sm2.use(dbs[1])
    try:
        # db2: NEEDS_ATTENTION on the master.
        sm2.inject_log(m, sm2.path, "Replication is stopped due to critical error(s)", role="master")
        att = sm2.wait(m, "NEEDS_ATTENTION", 120)
        # db1: a replica NEEDS_REINIT, and the master killed under a reinit's lock.
        rep = reps[0]
        target = reps[1] if len(reps) > 1 else reps[0]
        sm.inject_log(rep, sm.rpath(rep), 'violation of PRIMARY or UNIQUE KEY constraint "TB_PK" on table "TB_TEST"',
                      count=3)
        nr = sm.wait(rep, "NEEDS_REINIT", 120, db_id=sm.rid(rep))
        t = sm.node_on_file("locked", "kill-stay", timeout=900)
        st, _ = sm.start_reinit(target)
        t.join(1000)
        time.sleep(10)          # systemd would have started it again by now
        down = not wait_node(cl, m, 5)
        locked = bool(delta_left(cl, sm.path))
        setup = (f"old version: master db2 {att}; {rep} db1 {nr}; reinit db1 to {target} HTTP {st}, master "
                 f"killed under the lock ({t.result}), node {'down' if down else 'UP'}, "
                 f".delta {'there' if locked else 'gone'}")
        log(setup)
        good = att.startswith("NEEDS_ATTENTION") and nr.startswith("NEEDS_REINIT") and down and locked
        res.record("upgrade: setup", "PASS" if good else "FAIL", note=setup)

        log("=== upgrade: this build over the old state")
        install_node(cl, new_dist)
        up = wait_node(cl, m, 180)
        new = version(cl, m)
        schema = int(new.get("state_schema_version") or 0)
        res.record("upgrade: node opens the old state", "PASS" if up and schema == SCHEMA else "FAIL",
                   note=f"version {new.get('version')} schema {schema} (want {SCHEMA})")

        end = time.time() + 180
        lock = delta_left(cl, sm.path)
        while lock and time.time() < end:
            time.sleep(5)
            lock = delta_left(cl, sm.path)
        res.record("upgrade: lock released", "PASS" if locked and not lock else "FAIL",
                   note=("the old node's lock is gone" if locked and not lock else
                         f"confirmed: {lock[0]} still there 180 s after the upgrade" if lock else
                         "no lock before the upgrade: not tested"))

        left, secs, s = sm.wait_not(m, "SEEDING", 300)
        rec = sm.rec(m) or {}
        res.record("upgrade: master leaves SEEDING", "PASS" if left else "FAIL",
                   note=f"db1 master {s} after {secs}s; last_op {rec.get('last_op')}")

        seen = set()
        for _ in range(12):     # a minute of reconcile ticks, no load
            seen.add(sm2.state(m))
            time.sleep(5)
        ok = seen == {"NEEDS_ATTENTION/replication_log_error"}
        res.record("upgrade: master keeps NEEDS_ATTENTION", "PASS" if ok else "FAIL",
                   note=f"db2 master over a minute: {sorted(seen)}")

        rid = sm.rid(rep)
        s0 = sm.state(rep, rid)
        time.sleep(60)
        s1 = sm.state(rep, rid)
        ok = s0 == s1 == "NEEDS_REINIT/upgrade_needs_reinit"
        res.record("upgrade: replica keeps NEEDS_REINIT", "PASS" if ok else "FAIL",
                   note=f"{rep} db1: right after the upgrade {s0}, a minute later {s1}")

        sm.recover("upgrade")
    finally:
        if not a.keep:
            ops.dbs_remove(cl, a.subdir)
    return res.finish()
