"""RCM reinit fixes (hqcluster RCM review 2026-10-22: F-01, F-03, F-05).

Part b (F-01): POST /v1/nodes/{id}/reinit without a usable db_id (no body,
null, a number, an empty string, no field) used to start a reinit of EVERY
database of the node. Now it is 400 bad_request ("db_id is required"), and
malformed JSON is 400 bad_json. The body is checked before the node: the
test sends the cases to a node id RCM does not know first, and to the real
master only when all of them were refused (so an old RCM never gets them for
a real node). POST /v1/reinit (all databases) is never called.

Part c (F-03): RCM restarts while a reinit it started runs. At start it marks
the command failed ("interrupted by RCM restart") and goes on polling the
job. The old code failed the job when the first poll after the restart did
not answer. The test starts a reinit through RCM, restarts RCM as soon as
the command waits for the node, and for --blocked-seconds RCM cannot reach
the master node's port (module 92-ident rcm-block: a firewall rule for the
RCM service's user only). The job must stay running, then end ok (phase
done), and the replica must match the master.

Part a (F-05): autoreinit picked a Ghost row (the donor of a promote, now a
master itself) as the reinit target. The test makes a Ghost the way a promote
job does: a ghost_replicas tombstone in RCM's state.json for the replica that
RCM sorts first (module 92-ident rcm-ghost; RCM is restarted). It turns
autoreinit on for this database only (other databases get a closed window),
gives the master database a new GUID (node POST /v1/databases/{id}/newguid,
force) so that it needs a reinit, and watches --watch-ticks check intervals:
no reinit job may go to the Ghost's node. A reinit to the other replica is
the expected action (noted). The node itself still treats the Ghost's node
as a replica, so the fixed RCM reinits the other replica again on every tick
while the master stays NEEDS_REINIT: the test does not count those as errors.

Every part puts things back: firewall rule off, RCM running, auto_reinit as
before, the tombstone cleared, and after a new GUID a reinit of the database
to every replica, then the replicas must match the master.
"""
import base64
import json
import time
import urllib.parse

from tblib import ops
from tblib.cluster import TbError, log
from tblib.results import Results

from . import _rcm
from ._common import converge_and_record

HELP = "RCM: per-node reinit needs db_id (F-01), a reinit survives an RCM restart (F-03), autoreinit skips a Ghost (F-05)"

TAG = "ghostar"
FAKE_NODE = "tb-no-such-node"
REAPED = "interrupted by RCM restart"
RUNNING = ("prepare", "snapshot", "transfer", "catchup")


def add_args(p):
    p.add_argument("--parts", default="b,c,a", help="parts to run, in this order: b (F-01), c (F-03), a (F-05)")
    p.add_argument("--db", default="", help="test database (db_id or folder name); default: the first")
    p.add_argument("--replica", default="", help="replica of part c; default: the first")
    p.add_argument("--blocked-seconds", type=int, default=20,
                   help="part c: seconds RCM cannot reach the master node after its restart")
    p.add_argument("--check-interval", type=int, default=15, help="part a: auto_reinit.check_interval_sec")
    p.add_argument("--watch-ticks", type=int, default=3, help="part a: check intervals to watch")
    p.add_argument("--timeout", type=int, default=900, help="seconds for a reinit and for the catch-up")
    p.add_argument("--no-trigger", action="store_true",
                   help="part a: no new GUID on the master (then nothing needs a reinit and the watch proves little)")


# ------------------------------------------------------------------ helpers --
def ident(cl, cmd, args, timeout=None):
    rc, res, _, out = cl.module(cl.cfg.rcm_host, "92-ident", cmd, args, check=False, timeout=timeout)
    if rc != 0 or not isinstance(res, dict):
        raise TbError(f"92-ident {cmd}: {(out or '')[-300:]}")
    return res


def rcm_raw(cl, method, path, text):
    """An RCM call (Digest) with exactly these body bytes ("" = no body)."""
    args = {"auth": "digest", "method": method, "path": path}
    if text:
        args["body_b64"] = base64.b64encode(text.encode()).decode()
    r = ident(cl, "rcm-http", args)
    return r.get("status"), r.get("body")


def err(body):
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return body["error"].get("code"), str(body["error"].get("message", ""))
    return None, str(body)[:200]


def rcm_dir(cl):
    return cl.h(cl.cfg.rcm_host)["paths"]["rcm"]


def reinit_jobs(cl):
    return ident(cl, "rcm-reinits", {"rcm_dir": rcm_dir(cl)}).get("reinits") or []


def commands(cl):
    st, body = _rcm.api(cl, "GET", "/v1/commands")
    rows = body.get("commands", body) if isinstance(body, dict) else body
    return [c for c in rows or [] if isinstance(c, dict)]


def node_reinit(cl, db_id):
    st, body = cl.api(cl.cfg.master, "GET", f"/v1/databases/{urllib.parse.quote(db_id, safe='')}/reinit",
                      check_status=False)
    return body if st == 200 and isinstance(body, dict) else {}


def wait_node_idle(cl, db_id, timeout=900):
    end = time.time() + timeout
    while True:
        ph = node_reinit(cl, db_id).get("phase")
        if ph not in RUNNING or time.time() >= end:
            return ph
        time.sleep(5)


def reinit_retry(cl, db_id, rep, timeout):
    """The node's reinit of db_id to rep, after any running one; 409 retried."""
    end = time.time() + timeout
    while True:
        wait_node_idle(cl, db_id, max(60, int(end - time.time())))
        try:
            return ops.reinit(cl, db_id, rep, timeout=timeout)
        except TbError as e:
            if "409" in str(e) and time.time() < end:
                log(f"reinit {db_id} -> {rep}: {str(e)[:200]}; retry in 20 s")
                time.sleep(20)
                continue
            raise


def rcm_up(cl):
    """RCM answers (started when it does not)."""
    try:
        if ident(cl, "rcm-svc", {"action": "status"}).get("answering"):
            return True
        return bool(ident(cl, "rcm-svc", {"action": "start"}, timeout=180).get("answering"))
    except TbError as e:
        log(f"RCM service: {e}")
        return False


def rcm_group(cl, mid, db_id):
    st, body = _rcm.api(cl, "GET", "/v1/databases")
    for g in (body or {}).get("databases") or [] if isinstance(body, dict) else []:
        if any(s.get("node_id") == mid and s.get("db_id") == db_id for s in g.get("masters") or []):
            return g
    return None


def wait_group(cl, mid, db_id, cond, timeout):
    end = time.time() + timeout
    g = None
    while True:
        try:
            _rcm.poll_now(cl)
        except TbError:
            pass
        g = rcm_group(cl, mid, db_id)
        if (g and cond(g)) or time.time() >= end:
            return bool(g and cond(g)), g
        time.sleep(10)


def master_state(cl, db_id):
    for r in cl.databases(cl.cfg.master):
        if r.get("db_id") == db_id:
            return r.get("state")
    return None


def closed_window(hhmm):
    """A one-minute window 12 hours away from hhmm (the RCM host's clock)."""
    try:
        h, m = (int(x) for x in str(hhmm).split(":"))
    except ValueError:
        h, m = 0, 0
    s = (h * 60 + m + 720) % 1440
    e = (s + 1) % 1440
    return f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}"


def short_load(cl, d, seconds=30):
    cl.load_stop(TAG)
    cl.load_start([d["path"]], mode="write", tx="off", conns="1:2", tag=TAG)
    time.sleep(seconds)
    cl.load_stop(TAG)


# ------------------------------------------------------------------- part b --
def part_b(cl, res, mid):
    cases = [("no body", ""), ("db_id null", '{"db_id": null}'), ("db_id a number", '{"db_id": 5}'),
             ("db_id empty", '{"db_id": ""}'), ("no db_id field", '{"to": "tb-no-such-node"}')]
    bad = ("truncated JSON", '{"db_id": ')

    def check(node, label):
        all_ok = True
        for name, text in cases:
            st, body = rcm_raw(cl, "POST", f"/v1/nodes/{node}/reinit", text)
            code, msg = err(body)
            ok = st == 400 and code == "bad_request" and "db_id" in msg
            res.record(f"F-01 {label}, {name}: 400 bad_request", "PASS" if ok else "FAIL",
                       note=f"HTTP {st} {code} {msg[:120]}")
            all_ok = all_ok and ok
        st, body = rcm_raw(cl, "POST", f"/v1/nodes/{node}/reinit", bad[1])
        code, msg = err(body)
        ok = st == 400 and code == "bad_json"
        res.record(f"F-01 {label}, {bad[0]}: 400 bad_json", "PASS" if ok else "FAIL",
                   note=f"HTTP {st} {code} {msg[:120]}")
        return all_ok and ok

    if not check(FAKE_NODE, "unknown node"):
        res.record("F-01 the master node", "SKIP",
                   note="not sent to the master: this RCM did not refuse them for an unknown node "
                        "(an old RCM starts a reinit of every database)")
        return
    before = {c.get("id") for c in commands(cl)}
    check(mid, "the master node")
    new = [c for c in commands(cl) if c.get("id") not in before
           and c.get("type") in ("reinit", "reinit_all") and c.get("target_node") == mid]
    res.record("F-01 no reinit command was made", "FAIL" if new else "PASS",
               note=f"new commands: {[(c.get('type'), c.get('state')) for c in new]}" if new else "none")


# ------------------------------------------------------------------- part c --
def part_c(cl, res, a, d, rep):
    m = cl.cfg.master
    mid, to = cl.h(m)["node_id"], cl.h(rep)["node_id"]
    port = cl.h(m)["node_port"]
    cl.load_stop(TAG)
    wait_node_idle(cl, d["db_id"], 600)
    st, out = _rcm.api(cl, "POST", f"/v1/nodes/{mid}/reinit",
                       {"db_id": d["db_id"], "to": to, "ignore_window": True, "mode": "standard",
                        "hold_on_long_transactions": True})
    rid = out.get("reinit_id") if isinstance(out, dict) else None
    cid = out.get("command_id") if isinstance(out, dict) else None
    if st != 202 or not rid:
        raise TbError(f"RCM reinit {d['db_id']} -> {to}: HTTP {st} {json.dumps(out)[:300]}")
    log(f"RCM reinit {rid} (command {cid}) {d['db_id']} -> {to}")
    end = time.time() + 120
    cmd = {}
    while time.time() < end:
        _, cmd = _rcm.api(cl, "GET", f"/v1/commands/{cid}")
        cmd = cmd if isinstance(cmd, dict) else {}
        if cmd.get("state") in ("waiting_stats", "succeeded", "failed", "timeout"):
            break
        time.sleep(1)
    try:
        if cmd.get("state") != "waiting_stats":
            res.record("F-03 the job after an RCM restart", "SKIP",
                       note=f"the command was {cmd.get('state')} {cmd.get('error', '')} before the restart")
        else:
            ns = node_reinit(cl, d["db_id"])
            ident(cl, "rcm-block", {"action": "on", "port": port})
            r = ident(cl, "rcm-svc", {"action": "restart"}, timeout=180)
            log(f"RCM restarted (answering {r.get('answering')}); it cannot reach the master node for "
                f"{a.blocked_seconds}s")
            stop = time.time() + a.blocked_seconds
            job = {}
            while True:
                _, job = _rcm.api(cl, "GET", f"/v1/reinit/{rid}")
                job = job if isinstance(job, dict) else {}
                if job.get("status") != "running" or time.time() >= stop:
                    break
                time.sleep(3)
            _, cmd2 = _rcm.api(cl, "GET", f"/v1/commands/{cid}")
            cmd2 = cmd2 if isinstance(cmd2, dict) else {}
            ident(cl, "rcm-block", {"action": "off", "port": port})
            if cmd2.get("error") != REAPED:
                res.record("F-03 the job after an RCM restart", "SKIP",
                           note=f"the restart did not reap the command: {cmd2.get('state')} {cmd2.get('error', '')}")
            else:
                ok = job.get("status") == "running"
                res.record("F-03 the job after an RCM restart", "PASS" if ok else "FAIL",
                           note=f"job {job.get('status')} {job.get('error', '')}; command {cmd2.get('state')} "
                                f"'{REAPED}'; node phase at the restart {ns.get('phase')}")
        job = _rcm.wait_job(cl, f"/v1/reinit/{rid}", timeout=a.timeout)
        ok = job.get("status") == "ok" and job.get("phase") == "done"
        res.record("F-03 the job ends ok", "PASS" if ok else "FAIL",
                   note=f"status {job.get('status')} phase {job.get('phase')} {job.get('error', '')}")
    finally:
        try:
            ident(cl, "rcm-block", {"action": "off", "port": port})
        except TbError as e:
            log(f"rcm-block off: {e}")
        rcm_up(cl)
    wait_node_idle(cl, d["db_id"], a.timeout)
    short_load(cl, d)
    converge_and_record(cl, res, "F-03 the replica matches the master", [d["path"]], a.timeout, [rep])


# ------------------------------------------------------------------- part a --
def part_a(cl, res, a, d):
    m = cl.cfg.master
    mid = cl.h(m)["node_id"]
    ok, g = wait_group(cl, mid, d["db_id"], lambda g: len(g.get("replicas") or []) >= 2, 60)
    reps = [r for r in (g or {}).get("replicas") or [] if r.get("status_ui") not in ("Ghost", "Normal")]
    if len(reps) < 2:
        res.record("F-05 autoreinit and a Ghost row", "SKIP",
                   note=f"RCM shows {len(reps)} replica row(s) of {d['db_id']}; two are needed")
        return
    ghost = reps[0]               # RCM sorts replicas by rel_path, then node_id
    others = sorted({r.get("node_id") for r in reps[1:]})
    rel = (g.get("masters") or [{}])[0].get("rel_path") or ""
    log(f"Ghost: {ghost.get('node_id')} {ghost.get('db_id')}; real replicas {others}; master rel_path '{rel}'")

    _, conf = _rcm.api(cl, "GET", "/v1/rcm/config")
    old_ar = ((conf or {}).get("config") or {}).get("auto_reinit") if isinstance(conf, dict) else None
    if not isinstance(old_ar, dict):
        old_ar = {"enabled": False}
    closed = closed_window(ident(cl, "rcm-svc", {"action": "status"}).get("local_time"))
    ar = {"enabled": True, "default_window": closed, "check_interval_sec": a.check_interval, "max_concurrent": 1,
          "nodes": {mid: {"window": closed, "databases": {rel: "always"}}}}
    if not rel:
        ar = {"enabled": True, "default_window": "always", "check_interval_sec": a.check_interval,
              "max_concurrent": 1}
        log("no rel_path of the master database: autoreinit is on for every database during the watch")
    known = {j.get("id") for j in reinit_jobs(cl)}
    ghost_added = False
    new_guid = False
    try:
        st, out = _rcm.api(cl, "PUT", "/v1/rcm/config", {"auto_reinit": ar})
        if st != 200:
            raise TbError(f"PUT /v1/rcm/config auto_reinit: HTTP {st} {json.dumps(out)[:300]}")
        # RCM stops, state.json gets the tombstone, RCM starts with the new config.
        r = ident(cl, "rcm-ghost", {"action": "add", "rcm_dir": rcm_dir(cl), "donor_node": ghost.get("node_id"),
                                    "donor_db": ghost.get("db_id"), "old_master_node": mid,
                                    "old_master_db": d["db_id"], "rel_path": ghost.get("rel_path"),
                                    "guid": g.get("guid"), "promoted_to_node": ghost.get("node_id")}, timeout=300)
        ghost_added = True
        if not r.get("answering"):
            raise TbError("RCM does not answer after the tombstone was added")
        _, conf = _rcm.api(cl, "GET", "/v1/rcm/config")
        run_ar = ((conf or {}).get("running") or {}).get("auto_reinit") or {} if isinstance(conf, dict) else {}
        res.record("F-05 autoreinit is on (running config)", "PASS" if run_ar.get("enabled") else "FAIL",
                   note=json.dumps(run_ar)[:300])

        def is_ghost(g):
            return any(x.get("node_id") == ghost.get("node_id") and x.get("status_ui") == "Ghost"
                       for x in g.get("replicas") or [])
        ok, g2 = wait_group(cl, mid, d["db_id"], is_ghost, 90)
        rows = [(x.get("node_id"), x.get("status_ui")) for x in (g2 or {}).get("replicas") or []]
        res.record("F-05 RCM shows the Ghost row", "PASS" if ok else "FAIL", note=f"replica rows {rows}")
        if not ok:
            return

        if a.no_trigger:
            res.record("F-05 the master database needs a reinit", "SKIP", note="--no-trigger")
        elif ops.protocol_minor(cl, m) < 11:
            res.record("F-05 the master database needs a reinit", "SKIP",
                       note="the master node has no POST /v1/databases/{id}/newguid (protocol minor < 11)")
        else:
            cl.load_stop(TAG)
            wait_node_idle(cl, d["db_id"], 600)
            st, out = cl.api(m, "POST", f"/v1/databases/{urllib.parse.quote(d['db_id'], safe='')}/newguid",
                             {"force": True}, timeout=600, check_status=False)
            if st != 200:
                raise TbError(f"newguid {d['db_id']}: HTTP {st} {json.dumps(out)[:300]}")
            new_guid = True
            log(f"new GUID {out.get('old_guid')} -> {out.get('guid')}")
            cl.load_start([d["path"]], mode="write", tx="off", conns="1:2", tag=TAG)
            end = time.time() + 180
            s = master_state(cl, d["db_id"])
            while s != "NEEDS_REINIT" and time.time() < end:
                time.sleep(10)
                s = master_state(cl, d["db_id"])
            cl.load_stop(TAG)
            ok, g3 = wait_group(cl, mid, d["db_id"],
                                lambda g: (g.get("masters") or [{}])[0].get("state") == "NEEDS_REINIT", 120)
            res.record("F-05 the master database needs a reinit", "PASS" if s == "NEEDS_REINIT" and ok else "FAIL",
                       note=f"node state {s}; RCM master state {((g3 or {}).get('masters') or [{}])[0].get('state')}")

        # The watch: no reinit job toward the Ghost's node.
        stop = time.time() + a.watch_ticks * a.check_interval + 20
        to_ghost, to_real = {}, {}
        while True:
            for j in reinit_jobs(cl):
                if j.get("id") in known or j.get("master_db") != d["db_id"]:
                    continue
                (to_ghost if j.get("replica_node") == ghost.get("node_id") else to_real)[j.get("id")] = j
            if time.time() >= stop:
                break
            time.sleep(5)
        res.record("F-05 no automatic reinit toward the Ghost", "FAIL" if to_ghost else "PASS",
                   note=(f"{len(to_ghost)} job(s) to {ghost.get('node_id')}: "
                         f"{[(j.get('status'), j.get('error')) for j in to_ghost.values()][:3]}")
                   if to_ghost else f"none in {a.watch_ticks} check interval(s) of {a.check_interval}s")
        if to_real:
            res.record("F-05 autoreinit picked a real replica", "PASS",
                       note=f"{len(to_real)} job(s) to {sorted({j.get('replica_node') for j in to_real.values()})} "
                            "(repeats are expected while the Ghost's pair needs a reinit)")
        else:
            res.record("F-05 autoreinit picked a real replica", "SKIP",
                       note="no automatic reinit of this database during the watch: the check above proves less")
    finally:
        cl.load_stop(TAG)
        try:
            st, out = _rcm.api(cl, "PUT", "/v1/rcm/config", {"auto_reinit": old_ar})
            ident(cl, "rcm-svc", {"action": "restart"}, timeout=180)
            log(f"auto_reinit back (HTTP {st}), RCM restarted")
            if st != 200:
                res.record("restore: auto_reinit as before", "FAIL",
                           note=f"HTTP {st} {json.dumps(out)[:200]}; put it in rcm.json by hand: {json.dumps(old_ar)[:200]}")
        except TbError as e:
            log(f"restore auto_reinit: {e}")
        if ghost_added:
            path = (f"/v1/nodes/{urllib.parse.quote(ghost.get('node_id', ''), safe='')}/databases/"
                    f"{urllib.parse.quote(ghost.get('db_id', ''), safe='')}/ghostreplica/clear")
            try:
                st, _ = _rcm.api(cl, "POST", path, {})
                if st not in (200, 404):
                    ident(cl, "rcm-ghost", {"action": "remove", "rcm_dir": rcm_dir(cl),
                                            "donor_node": ghost.get("node_id"), "donor_db": ghost.get("db_id")},
                          timeout=300)
                log(f"tombstone cleared (HTTP {st})")
            except TbError as e:
                log(f"clear the tombstone: {e}")
        rcm_up(cl)
        if new_guid:
            for r in cl.cfg.replicas:
                try:
                    op = reinit_retry(cl, d["db_id"], r, a.timeout)
                    res.record(f"restore: reinit to {r}", "PASS" if op.get("state") == "succeeded" else "FAIL",
                               note=f"{op.get('state')} {op.get('error', '')}")
                except TbError as e:
                    res.record(f"restore: reinit to {r}", "FAIL", note=str(e)[:300])
            short_load(cl, d)
            converge_and_record(cl, res, "restore: the replicas match the master", [d["path"]], a.timeout)


def run(cl, a):
    res = Results("ghost_autoreinit", vars(a).copy())
    if not _rcm.ready(cl):
        res.record("ghost_autoreinit", "SKIP", note="RCM is off or secrets.rcm_user / rcm_password are not set")
        return res.finish()
    parts = [x.strip() for x in a.parts.split(",") if x.strip()]
    m = cl.cfg.master
    mid = cl.h(m)["node_id"]
    rep = a.replica or cl.cfg.replicas[0]
    if rep not in cl.cfg.replicas:
        res.record("ghost_autoreinit", "FAIL", note=f"'{rep}' is not a replica")
        return res.finish()
    dbs = cl.test_dbs(which=a.db or "all")
    if not dbs:
        res.record("ghost_autoreinit", "FAIL", note="no test database on the master (run 'dbs' first)")
        return res.finish()
    d = dbs[0]
    for part in parts:
        try:
            if part == "b":
                part_b(cl, res, mid)
            elif part == "c":
                part_c(cl, res, a, d, rep)
            elif part == "a":
                part_a(cl, res, a, d)
            else:
                raise TbError(f"unknown part '{part}' (b, c, a)")
        except TbError as e:
            res.record(f"part {part}", "FAIL", note=str(e)[:400])
    cl.load_stop(TAG)
    return res.finish()
