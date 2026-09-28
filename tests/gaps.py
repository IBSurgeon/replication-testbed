"""Are the possible gaps of the state machine real problems?
(hqbirdrcm docs/REPLICATION_STATE_MACHINE.md, section 12, items 1-8)

Each item is made to happen on the cluster, and the case checks the
behaviour an operator needs. PASS: the node or RCM handles it, the gap is not
a real problem. FAIL: the problem is real; the note says what was seen. A
"setup" FAIL means the situation could not be made, so the item is not
answered.

  1 conflict   NEEDS_ATTENTION/sequence_conflict on the master after the
               replica's hold is released and deliveries go on
  2 disabled   DISABLED after the replica applies segments again
  3 failed     a FAILED reinit to one replica stops shipping to the others
  4 frozen     stale_generation from one replica freezes the others
  5 crash      the master node killed under the nbackup lock of a reinit:
               the lock, the SEEDING state, the next reinit; and item 6,
               the replica's abandoned receive session and that next reinit
  7 rcm-jobs   RCM verify jobs and commands cut off by an RCM restart
  8 rcm-disabled  a DISABLED database is visible to the operator in RCM

The test uses its own database (--subdir, made when missing, removed at the
end unless --keep). Items 3 and 4 need two replicas. Items 7 and 8 need the
RCM login in the local config (secrets.rcm_user, secrets.rcm_password).
"""
import base64
import json
import time

from tblib import ops
from tblib.cluster import TbError, log
from tblib.results import SKIP, Results

from ._common import add_load_args, delta_left, pick_replicas
from ._sm import SM, fb_start_all
from .disasters import node_svc, wait_node
from .states import conflict_push, settled, stale_setup

HELP = "state machine section 12: is each possible gap a real problem"

ITEMS = ["conflict", "disabled", "failed", "frozen", "crash", "rcm-jobs", "rcm-disabled"]


def add_args(p):
    p.add_argument("--only", default="all", help="all | comma list of: " + ",".join(ITEMS))
    p.add_argument("--subdir", default="tbsm", help="folder of the test database under db_root")
    p.add_argument("--replicas", default="all")
    p.add_argument("--settle", type=int, default=300, help="seconds the node gets to fix a state by itself")
    p.add_argument("--keep", action="store_true", help="keep the test database at the end")
    add_load_args(p)


def newest_archived(sm):
    files = sm.cl.hostctl(sm.m, "files", {"glob": sm.path + ".LogArch/*journal-*"}) or []
    return max((int(f["path"].rsplit("-", 1)[-1]) for f in files), default=0)


def setup_failed(sm, item, note):
    sm.res.record(f"12.{item} setup", "FAIL", note=note)


# --- items -----------------------------------------------------------------------
def g_conflict(sm):
    cl, rep, a = sm.cl, sm.rep, sm.a
    r = conflict_push(sm, rep)
    sm.load_on()
    try:
        s = sm.wait(sm.m, "NEEDS_ATTENTION/sequence_conflict", 240)
        if s != "NEEDS_ATTENTION/sequence_conflict":
            setup_failed(sm, 1, f"master is {s}; replica answered the push {r.get('status')}")
            return
        cl.api(rep, "POST", f"/v1/databases/{sm.rid(rep)}/hold/release", {})
        acked0 = int(sm.ledger(rep).get("last_acked") or 0)
        left, secs, last = sm.wait_not(sm.m, "NEEDS_ATTENTION/sequence_conflict", a.settle)
        acked1 = int(sm.ledger(rep).get("last_acked") or 0)
    finally:
        sm.load_off()
    sm.res.record("12.1 sequence_conflict clears after hold/release", "PASS" if left else "FAIL",
                  note=(f"master left it in {secs}s: {last}" if left else
                        f"confirmed: master still {last} {secs}s after the release, "
                        f"while the replica acked {acked0}->{acked1}"))


def g_disabled(sm):
    cl, rep, a = sm.cl, sm.rep, sm.a
    sm.inject_log(rep, sm.rpath(rep), "Replication is disabled for this database (test bed)")
    s = sm.wait(rep, "DISABLED", 120)
    if s != "DISABLED":
        setup_failed(sm, 2, f"replica is {s} after the simulated 'disabled' line")
        return
    seg0 = int((sm.rec(rep, sm.rid(rep)) or {}).get("last_segment") or 0)
    sm.load_on()
    try:
        left, secs, last = sm.wait_not(rep, "DISABLED", a.settle)
    finally:
        sm.load_off()
    seg1 = int((sm.rec(rep, sm.rid(rep)) or {}).get("last_segment") or 0)
    sm.res.record("12.2 DISABLED clears once segments apply again", "PASS" if left else "FAIL",
                  note=(f"left DISABLED in {secs}s: {last}" if left else
                        f"confirmed: still DISABLED {secs}s later while the replica applied segments {seg0}->{seg1}"),
                  simulated="replication.log ERROR ... disabled")


def other_rep(sm):
    return sm.reps[1] if len(sm.reps) > 1 else None


def g_failed(sm):
    cl, rep, rep2 = sm.cl, sm.rep, other_rep(sm)
    if not rep2:
        sm.res.record("12.3 FAILED stops shipping to the other replicas", SKIP, note="needs two replicas")
        return
    try:
        node_svc(cl, rep, "stop")
        st, r = sm.start_reinit(rep)
        if st in (200, 202):
            op = cl.wait_op(sm.m, r["operation_id"], timeout=600)
        else:
            op = {"state": f"HTTP {st}"}
        # State machine v2 (D5): FAILED is no longer a sticky state. The
        # failed attempt is recorded in last_op, and the view recovers as
        # soon as the pairs' evidence justifies it — the question here is
        # whether the OTHER replica keeps receiving, which the load below
        # answers directly.
        rec = sm.rec(sm.m) or {}
        failed = (rec.get("last_op") or {}).get("result") == "failed" or op.get("state") == "failed"
    finally:
        node_svc(cl, rep, "start")
        wait_node(cl, rep, 180)
    if not failed:
        setup_failed(sm, 3, f"the reinit to the stopped {rep} did not fail (op {op.get('state')})")
        return
    a0, arch0 = int(sm.ledger(rep2).get("last_acked") or 0), newest_archived(sm)
    sm.load(120)
    time.sleep(20)
    a1, arch1 = int(sm.ledger(rep2).get("last_acked") or 0), newest_archived(sm)
    ok = a1 > a0
    sm.res.record("12.3 FAILED stops shipping to the other replicas", "PASS" if ok else "FAIL",
                  note=(f"{rep2} kept receiving: acked {a0}->{a1}" if ok else
                        f"confirmed: reinit to {rep} failed (v2 records it in last_op); "
                        f"{rep2} acked {a0}->{a1} while the master archived {arch0}->{arch1}"))


def g_frozen(sm):
    cl, rep, rep2 = sm.cl, sm.rep, other_rep(sm)
    if not rep2:
        sm.res.record("12.4 stale_generation from one replica freezes the others", SKIP, note="needs two replicas")
        return
    note = stale_setup(sm, rep)
    if sm.state(sm.m).startswith("FAILED"):
        cl.api(sm.m, "POST", "/v1/publication/sync", {}, check_status=False)
    # The crash has to land between the image reaching the replica and the
    # master's own generation update; when it lands early the replica never
    # holds a newer image and no 410 ever comes. Retry the setup once — the
    # second attempt usually lands the window.
    if not sm.wait(sm.m, "NEEDS_REINIT", 60):
        note += "; retry (the first crash missed the window)"
        note2 = stale_setup(sm, rep)
        note = f"{note}; {note2}"
    sm.load_on()
    try:
        s = sm.wait(sm.m, "NEEDS_REINIT", 300)
        if s != "NEEDS_REINIT":
            setup_failed(sm, 4, f"master is {s}, no stale_generation; {note}")
            return
        a0, arch0 = int(sm.ledger(rep2).get("last_acked") or 0), newest_archived(sm)
        time.sleep(120)
    finally:
        sm.load_off()
    time.sleep(20)
    a1, arch1 = int(sm.ledger(rep2).get("last_acked") or 0), newest_archived(sm)
    ok = a1 > a0
    sm.res.record("12.4 stale_generation from one replica freezes the others", "PASS" if ok else "FAIL",
                  note=(f"{rep2} kept receiving: acked {a0}->{a1}" if ok else
                        f"confirmed: NEEDS_REINIT after 410 from {rep}; {rep2} acked {a0}->{a1} "
                        f"while the master archived {arch0}->{arch1}"), setup=note)


def g_crash(sm):
    """Items 5 and 6: the master node killed while the reinit holds the
    nbackup lock (the transfer runs under it)."""
    cl, rep, a = sm.cl, sm.rep, sm.a
    t = sm.node_on_file("locked", "kill", timeout=900)
    st, _ = sm.start_reinit(rep)
    t.join(960)
    if not isinstance(t.result, dict):
        setup_failed(sm, 5, f"the node was not killed under the lock (reinit HTTP {st})")
        return
    if not wait_node(cl, sm.m, 180):
        node_svc(cl, sm.m, "start")
        wait_node(cl, sm.m, 180)
    time.sleep(5)
    s0 = sm.state(sm.m)
    rst, _ = cl.api(sm.m, "GET", f"/v1/databases/{sm.db_id}/reinit", check_status=False)
    pst, pbody = cl.api(sm.m, "GET", f"/v1/peer/reinit/{sm.rid(rep)}/state", addr=sm.peer_addr(rep),
                        check_status=False)
    phase = (pbody or {}).get("phase") if isinstance(pbody, dict) else None
    left, secs, s1 = sm.wait_not(sm.m, "SEEDING", a.settle) if s0.startswith("SEEDING") else (True, 0, s0)
    lock = delta_left(cl, sm.path)
    ctx = f"killed with the lock on ({t.result}); after restart: {s0}, GET reinit HTTP {rst}"
    sm.res.record("12.5 nbackup lock released after a node crash", "PASS" if not lock else "FAIL",
                  note=(f"no .delta {a.settle}s later" if not lock else
                        f"confirmed: {lock[0]} still there after the node came back; {ctx}"))
    sm.res.record("12.5 SEEDING leaves after a node crash", "PASS" if left else "FAIL",
                  note=(f"{s0} -> {s1} in {secs}s" if left else f"confirmed: still {s1} after {secs}s; {ctx}"))
    op = sm.reinit(rep)
    good = op.get("state") == "succeeded"
    sm.res.record("12.5 next reinit after a node crash", "PASS" if good else "FAIL",
                  note=("succeeded" if good else f"confirmed: {op.get('state')} {str(op.get('error'))[:200]}"))
    if lock:
        cl.hostctl(sm.m, "nbackup-unlock", {"db": sm.path}, check=False)
        if not good:
            op = sm.reinit(rep)
            good = op.get("state") == "succeeded"
    sm.res.record("12.6 abandoned receive session does not block a reinit", "PASS" if good else "FAIL",
                  note=(f"replica session was '{phase}' (HTTP {pst}); the next reinit "
                        + ("succeeded" if good else f"failed: {str(op.get('error'))[:200]}")))


def rcm(sm, method, path, body=None, then_restart=False):
    args = {"method": method, "path": path, "then_restart": then_restart}
    if body is not None:
        args["body_b64"] = base64.b64encode(json.dumps(body).encode()).decode()
    r = sm.cl.hostctl(sm.cl.cfg.rcm_host, "rcm-api", args, check=False)
    if not isinstance(r, dict):
        raise TbError("RCM API call failed (is secrets.rcm_user / rcm_password set?)")
    return r.get("status"), r.get("body")


def rcm_ready(sm):
    s = sm.cl.cfg.secrets
    return sm.cl.cfg.rcm_enabled and all(s.get(k) and not s[k].startswith("<") for k in ("rcm_user", "rcm_password"))


def rcm_wait_up(sm, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try:
            st, _ = rcm(sm, "GET", "/v1/nodes")
            if st == 200:
                return True
        except TbError:
            pass
        time.sleep(5)
    return False


def g_rcm_jobs(sm):
    cl, rep, a = sm.cl, sm.rep, sm.a
    if not rcm_ready(sm):
        sm.res.record("12.7 RCM jobs after an RCM restart", SKIP, note="no RCM login in the local config")
        return
    mid, rid_node = cl.h(sm.m)["node_id"], cl.h(rep)["node_id"]
    body = {"master_node": mid, "master_db": sm.db_id, "replica_node": rid_node, "replica_db": sm.rid(rep)}
    st, job = rcm(sm, "POST", "/v1/verify", body, then_restart=True)
    vid = (job or {}).get("id") if isinstance(job, dict) else None
    rcm_wait_up(sm)
    st2, cmd = rcm(sm, "POST", f"/v1/nodes/{mid}/publication/sync", {}, then_restart=True)
    # The command endpoint answers 202 with {command_id, command}.
    cid = None
    if isinstance(cmd, dict):
        cid = cmd.get("command_id") or (cmd.get("command") or {}).get("id")
    rcm_wait_up(sm)
    time.sleep(min(a.settle, 180))
    for what, ident, path, done in (("verify job", vid, "/v1/verify/", ("ok", "desynced", "failed")),
                                    ("command publication_sync", cid, "/v1/commands/",
                                     ("succeeded", "failed", "timeout"))):
        if not ident:
            setup_failed(sm, 7, f"RCM did not start the {what}: HTTP {st if 'verify' in what else st2}")
            continue
        s, out = rcm(sm, "GET", path + ident)
        state = (out or {}).get("status") or (out or {}).get("state") if isinstance(out, dict) else None
        ok = state in done
        sm.res.record(f"12.7 RCM {what} after an RCM restart", "PASS" if ok else "FAIL",
                      note=(f"'{state}' (it may have ended before the restart)" if ok else
                            f"confirmed: '{state}' (HTTP {s}) {min(a.settle, 180)}s after the restart that cut it off"))


def g_rcm_disabled(sm):
    cl, rep = sm.cl, sm.rep
    if not rcm_ready(sm):
        sm.res.record("12.8 DISABLED is visible in RCM", SKIP, note="no RCM login in the local config")
        return
    t0 = time.time()
    sm.inject_log(rep, sm.rpath(rep), "Replication is disabled for this database (test bed)")
    s = sm.wait(rep, "DISABLED", 120)
    if s != "DISABLED":
        setup_failed(sm, 8, f"replica is {s} after the simulated 'disabled' line")
        return
    rnode, rid = cl.h(rep)["node_id"], sm.rid(rep)
    found, alerts, view = [], [], None
    end = time.time() + 180
    while time.time() < end:
        rcm(sm, "POST", "/v1/poll-now", {})
        _, alerts = rcm(sm, "GET", "/v1/alerts")
        alerts = alerts if isinstance(alerts, list) else (alerts or {}).get("alerts", []) if isinstance(alerts, dict) else []
        found = [x for x in alerts if isinstance(x, dict) and x.get("database") == rid
                 and x.get("node_id") in (rnode, None, "") and x.get("severity") in ("warn", "serious", "critical")]
        if found:
            break
        time.sleep(15)
    _, dbs = rcm(sm, "GET", "/v1/databases")
    for p in dbs if isinstance(dbs, list) else []:
        if isinstance(p, dict) and rid in str(p):
            view = {k: v for k, v in p.items() if "status" in k.lower() or "state" in k.lower()}
            break
    sm.res.record("12.8 DISABLED is visible in RCM", "PASS" if found else "FAIL",
                  note=(f"alert {found[0].get('code')} ({found[0].get('severity')}) in {int(time.time() - t0)}s"
                        if found else f"confirmed: no warn/serious/critical alert for {rid} on {rnode} in RCM; "
                                      f"RCM shows {view}"),
                  simulated="replication.log ERROR ... disabled")


RUN = {"conflict": g_conflict, "disabled": g_disabled, "failed": g_failed, "frozen": g_frozen,
       "crash": g_crash, "rcm-jobs": g_rcm_jobs, "rcm-disabled": g_rcm_disabled}


def run(cl, a):
    reps = pick_replicas(cl, a.replicas)
    names = ITEMS if a.only == "all" else [s.strip() for s in a.only.split(",")]
    for n in names:
        if n not in RUN:
            raise TbError(f"unknown item '{n}' (known: {', '.join(ITEMS)})")
    res = Results("gaps", vars(a).copy())
    sm = SM(cl, res, a, reps)
    dbs = cl.test_dbs(a.subdir)
    made = False
    if not dbs:
        log(f"making the test database in {a.subdir}")
        dbs = ops.dbs_prepare(cl, count=1, subdir=a.subdir, seed=True)
        made = True
    sm.use(dbs[0])
    for n in names:
        log(f"=== gaps: {n}")
        try:
            RUN[n](sm)
        except Exception as e:  # noqa: BLE001 - one item must not stop the others
            res.record(f"{n}: run", "FAIL", note=str(e)[:300])
        finally:
            sm.load_off()
            fb_start_all(cl, reps)
        if not settled(sm):
            sm.recover(n)
    if made and not a.keep:
        ops.dbs_remove(cl, a.subdir)
    return res.finish()
