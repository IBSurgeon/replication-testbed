"""Promote to master: a replica's database is handed to the companion master
node on the same host (same Firebird), through RCM, as the Databases page's
Promote button does. Needs a companion (hosts.<replica>.companion in the
config, installed by 'install --source local') and the RCM login.

  setup      RCM knows the companion (role master, same host label as the
             replica); no topology alert; m3 offers Promote on the replica's row
  promote    POST .../promotetomaster, the job ends ok, every step done
  companion  the companion reports the file as its database
  donor      the replica node no longer holds it; the file is in its
             databases.exclude_paths
  writable   the promoted database takes a write (it is not a replica now)
  old master keeps its other replica: not Degraded, not paused, and a
             minute of load reaches that replica
  rcm view   /v1/databases and m3: a Ghost row of the donor under the old
             master; a block of the new master; duplicate_guid is raised
             (both masters hold the same GUID)
  clear      "Clear" removes the Ghost row; m3 then shows the donor's node as
             "not paired" for the old master's database
  guard      Initialize from that row (old master -> donor node) must not
             overwrite the promoted database: RCM or the node refuses it,
             and the new master still takes writes

The promoted database stays a master on the companion: run this test last
(or remove and prepare the databases again).
"""
import time

from tblib.cluster import TbError, log
from tblib.results import Results

from . import _rcm as R
from ._common import converge_and_record, norm, replica_record

HELP = "promote a replica's database to the companion master (RCM), then check both sides"


def add_args(p):
    p.add_argument("--db", default="", help="test database (db_id or folder name); default the last one")
    p.add_argument("--replica", default="", help="replica host with a companion; default the first one")
    p.add_argument("--timeout", type=int, default=1200)


def rel_to_root(path, root):
    p, r = path.replace("\\", "/"), root.replace("\\", "/").rstrip("/")
    return p[len(r) + 1:] if p.lower().startswith(r.lower() + "/") else p


def run(cl, a):
    res = Results("promote", vars(a).copy())
    comps = cl.cfg.companion_hosts()
    if not comps:
        res.record("promote", "SKIP", note="no companion in the config (hosts.<replica>.companion)")
        return res.finish()
    if not R.ready(cl):
        res.record("promote", "SKIP", note="no RCM login in the local config")
        return res.finish()
    donor = a.replica or comps[0]
    if donor not in comps:
        raise TbError(f"{donor} has no companion (companions: {', '.join(comps)})")
    dbs = cl.test_dbs(which=a.db or "all")
    if not dbs:
        raise TbError("no test databases (run 'tb.py dbs prepare')")
    d = dbs[-1]
    others = [r for r in cl.cfg.replicas if r != donor]
    mid = cl.h(cl.cfg.master)["node_id"]
    dn = cl.h(donor)["node_id"]
    cn = cl.h(donor)["companion"]["node_id"]
    key = f"m:{mid}|{d['db_id']}"
    cl.load_stop()

    # --- setup -----------------------------------------------------------
    _, rc = R.api(cl, "GET", "/v1/rcm/config")
    rnodes = {n.get("node_id"): n for n in ((rc or {}).get("config") or {}).get("nodes") or []} \
        if isinstance(rc, dict) else {}
    c, r = rnodes.get(cn) or {}, rnodes.get(dn) or {}
    ok = c.get("role") == "master" and c.get("host") and c.get("host") == r.get("host") and cn in R.node_ids(cl)
    res.record("setup: RCM pairs the companion with the replica", "PASS" if ok else "FAIL",
               note=f"{cn}: role {c.get('role')}, host {c.get('host')}; {dn}: host {r.get('host')}")
    topo = R.alerts(cl, "topology_mismatch")
    res.record("setup: no topology alert", "FAIL" if topo else "PASS",
               note="; ".join(f"{x.get('node_id')}: {x.get('message')}" for x in topo)[:400] or "peers as RCM expects")
    rec = replica_record(cl, donor, d["path"])
    if not rec:
        res.record("setup: donor record", "FAIL", note=f"{donor} has no record of {d['path']}")
        return res.finish()
    rid, dpath = rec["db_id"], rec["path"]

    def offers(page):
        b = R.blocks(page).get(key)
        return b is not None and any(r["promote"] for r in R.replica_rows(b, dn))
    ok, _ = R.wait_table(cl, "m3", offers, timeout=120, what="the Promote button")
    res.record("setup: m3 offers Promote on the donor's row", "PASS" if ok else "FAIL")

    # --- promote -----------------------------------------------------------
    st, body = R.api(cl, "POST", f"/v1/nodes/{dn}/databases/{rid}/promotetomaster", {})
    pid = body.get("promote_id") if isinstance(body, dict) else None
    if st != 202 or not pid:
        res.record("promote: job", "FAIL", note=f"HTTP {st}: {str(body)[:400]}")
        return res.finish()
    job = R.wait_job(cl, f"/v1/promotes/{pid}", timeout=a.timeout, poll=5)
    steps = [(s.get("id"), s.get("state")) for s in job.get("steps") or []]
    ok = job.get("status") == "ok" and all(s == "done" for _, s in steps)
    res.record("promote: job ends ok, every step done", "PASS" if ok else "FAIL",
               note=f"status {job.get('status')} {job.get('error') or ''} steps {steps}")
    if job.get("status") != "ok":
        return res.finish()

    # --- companion -------------------------------------------------------
    new = None
    end = time.time() + 180
    while time.time() < end and not new:
        _, cdbs = cl.companion_api(donor, "GET", "/v1/databases", check_status=False)
        new = next((x for x in cdbs or [] if isinstance(x, dict) and norm(x.get("path")) == norm(dpath)), None)
        if not new:
            time.sleep(10)
    res.record("companion: holds the file as its database", "PASS" if new else "FAIL",
               note=f"{cn}: {new.get('db_id')} {new.get('state')}" if new else f"{cn} does not report {dpath}")

    # --- donor -------------------------------------------------------------
    # The donor keeps its record ORPHANED (RCM hides it behind the Ghost
    # row); it must not manage the file any more.
    left = [(x.get("db_id"), x.get("state")) for x in cl.databases(donor) if norm(x.get("path")) == norm(dpath)]
    _, doc = cl.api(donor, "GET", "/v1/config")
    cfgd = doc.get("config", doc) if isinstance(doc, dict) else {}
    excl = (cfgd.get("databases") or {}).get("exclude_paths") or []
    rel = rel_to_root(dpath, (cfgd.get("databases") or {}).get("root", ""))
    ok = all(s == "ORPHANED" for _, s in left) and rel in excl
    res.record("donor: the replica node lets the file go (excluded, record ORPHANED at most)",
               "PASS" if ok else "FAIL", note=f"records {left}; exclude_paths has it: {rel in excl}")

    # --- writable ------------------------------------------------------------
    w = cl.hostctl(donor, "write-probe", {"db": dpath}, check=False) or {}
    res.record("writable: the promoted database takes a write", "PASS" if w.get("written") else "FAIL",
               note=(w.get("error") or "")[-200:])

    # --- old master ------------------------------------------------------------
    _, groups = R.api(cl, "GET", "/v1/databases")
    groups = groups if isinstance(groups, list) else (groups or {}).get("databases", []) if isinstance(groups, dict) else []
    old_m, ghost = None, None
    for g in groups:
        for m in g.get("masters") or []:
            if m.get("node_id") == mid and m.get("db_id") == d["db_id"]:
                old_m = m
                ghost = next((r for r in g.get("replicas") or [] if r.get("node_id") == dn
                              and r.get("status_ui") == "Ghost"), None)
    ok = old_m is not None and old_m.get("status_ui") != "Degraded" and not old_m.get("paused_to")
    res.record("old master: not Degraded, not paused (it keeps another replica)", "PASS" if ok else "FAIL",
               note=f"{mid} {d['db_id']}: {(old_m or {}).get('status_ui')} paused_to {(old_m or {}).get('paused_to')}")
    if others:
        cl.load_start([d["path"]], mode="write", tx="off", conns="1:3", tag="promote")
        time.sleep(60)
        cl.load_stop("promote")
        converge_and_record(cl, res, f"old master -> {', '.join(others)}", [d["path"]], 900, replicas=others)

    # --- rcm view --------------------------------------------------------------
    res.record("rcm view: Ghost row of the donor under the old master", "PASS" if ghost else "FAIL",
               note=f"{dn} {rid}" if ghost else "no Ghost row in /v1/databases")

    def view(page):
        bl = R.blocks(page)
        b = bl.get(key)
        g = b and any(r.get("status") == "Ghost" for r in R.replica_rows(b, dn))
        nb = any(v["master_node"] == cn for v in bl.values())
        return bool(g and nb)
    ok, page = R.wait_table(cl, "m3", view, what="Ghost row and the new master's block")
    newb = {k: v for k, v in R.blocks(page).items() if v["master_node"] == cn}
    res.record("rcm view m3: Ghost row, and a block of the new master", "PASS" if ok else "FAIL",
               note=f"new master blocks: {list(newb)}")
    # The new master has no replica yet. Both masters hold one GUID, so RCM
    # puts them in one group; the new master's block must still not claim
    # the old master's replicas (their db_id is the old master's).
    claimed = [(r.get("node"), r.get("status")) for v in newb.values() for r in R.replica_rows(v)]
    res.record("rcm view m3: the new master's block shows no replica of the old master",
               "FAIL" if claimed or not newb else "PASS",
               note=f"replica rows in the new master's block: {claimed}")
    dup = R.alerts(cl, "duplicate_guid")
    res.record("rcm view: duplicate_guid is raised (two masters, one GUID)", "PASS" if dup else "FAIL",
               note="; ".join(str(x.get("message"))[:160] for x in dup)[:400] or "no alert")

    # --- clear ghost -------------------------------------------------------------
    st, body = R.api(cl, "POST", f"/v1/nodes/{dn}/databases/{rid}/ghostreplica/clear")

    def cleared(page):
        b = R.blocks(page).get(key)
        return b is not None and not any(r.get("status") == "Ghost" for r in R.replica_rows(b, dn)) \
            and any(r.get("init_to") == dn for r in R.replica_rows(b, dn, "not_paired"))
    ok, page = R.wait_table(cl, "m3", cleared, what="Ghost gone, 'not paired' row")
    b = R.blocks(page).get(key) or {"rows": []}
    res.record("clear: Ghost row gone; the donor's node is 'not paired' for the old master",
               "PASS" if ok and st == 200 else "FAIL",
               note=f"clear HTTP {st}; old master's block: "
                    f"{[(r['kind'], r.get('node'), r.get('status')) for r in b['rows']]} "
                    f"(the donor's ORPHANED record must not come back as a replica row)")

    # --- guard ---------------------------------------------------------------------
    st, jobr = R.reinit(cl, mid, d["db_id"], dn, timeout=a.timeout)
    refused = st != 202 or (isinstance(jobr, dict) and jobr.get("status") != "ok")
    w2 = cl.hostctl(donor, "write-probe", {"db": dpath}, check=False) or {}
    _, cdbs = cl.companion_api(donor, "GET", "/v1/databases", check_status=False)
    still = any(isinstance(x, dict) and norm(x.get("path")) == norm(dpath) for x in cdbs or [])
    ok = refused and w2.get("written") and still
    why = (jobr.get("error") or jobr.get("code") or jobr.get("status")) if isinstance(jobr, dict) else jobr
    res.record("guard: Initialize onto the promoted file is refused; the new master is intact",
               "PASS" if ok else "FAIL",
               note=f"reinit HTTP {st}: {str(why)[:300]}; new master writable {w2.get('written')}, "
                    f"companion still holds it {still}")
    return res.finish()
