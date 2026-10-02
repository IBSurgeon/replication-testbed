"""Promote to master with a GUID of its own, under load, through the running
Firebird service. RCM promotes a replica's database to the companion master
on the same host (as test 'promote' does); then the database gets a new
GUID in place, the way the planned promote step 'new_guid' will do it:
replica mode off, single shutdown through the running server, nbackup -L,
nbackup -F without -SEQUENCE, delta removed, validated, online. Needs a
companion (hosts.<replica>.companion) and the RCM login.

  before      the promoted database still has the old master's GUID (reinit
              copies it with -SEQUENCE) and RCM raises duplicate_guid
  new guid    every step through the running service; a new GUID,
              replication sequence 0, not a replica, no backup lock, no
              delta, validation clean; Firebird not restarted (--mode
              shutdown) while it keeps applying the other replicas
  journal     the new master's first segments carry the new GUID
  rcm         duplicate_guid goes away; the new master's block holds no
              replica of the old master
  initialize  RCM Initialize from the new master to another replica is not
              refused and the replica follows a load on the new master
  guard       Initialize from the old master onto the promoted file is refused
              (now by the node: the file is no replica) and changes nothing
  converge    the old master and its other replica, and every other database
              on the promoting host, end equal

The companion node manages the file before the GUID changes, so it reports
the change (master_db_replaced, journal quarantine): these are noted, not
failed — the planned step runs before the companion enrolls the file. The
promoted database stays a master: run this test last, or prepare the
databases again. Linux hosts only (the host helpers are Linux)."""
import os
import tempfile
import time

from tblib import ops
from tblib.cluster import TbError, log
from tblib.results import Results

from . import _rcm as R
from ._common import converge_and_record, norm, replica_record

HELP = "promote, then a new GUID in place through the running service (shutdown, nbackup -L/-F) under load"


def add_args(p):
    p.add_argument("--db", default="", help="test database (folder name); default the last one not promoted yet")
    p.add_argument("--replica", default="", help="replica host with a companion; default the first one")
    p.add_argument("--mode", default="shutdown", choices=["shutdown", "stop"],
                   help="shutdown: the database in single shutdown, Firebird running; stop: Firebird stopped")
    p.add_argument("--minutes", type=float, default=2, help="load on the new master after Initialize")
    p.add_argument("--no-load", action="store_true")
    p.add_argument("--timeout", type=int, default=1200)


def pick_db(cl, a, donor, cn):
    dbs = cl.test_dbs(which=a.db or "all")
    if not dbs:
        raise TbError("no test databases (run 'tb.py dbs prepare')")
    if a.db:
        return dbs[0]
    _, held = cl.companion_api(donor, "GET", "/v1/databases", check_status=False)
    held = {norm(x.get("path")) for x in held or [] if isinstance(x, dict)}
    for d in reversed(dbs):
        rec = replica_record(cl, donor, d["path"])
        if rec and rec.get("state") != "ORPHANED" and norm(rec.get("path")) not in held:
            return d
    raise TbError(f"every test database is promoted on {cn} already: prepare the databases again")


def ensure_loadgen(cl, host):
    """fb-loadgen on another host than the master: copied from the master."""
    m = cl.cfg.master
    src = cl.host(m).join(cl.host(m).work, "loadgen", "fb-loadgen")
    dst = cl.host(host).join(cl.host(host).work, "loadgen", "fb-loadgen")
    if cl.hostctl(host, "files", {"glob": dst}, check=False):
        return
    tmp = os.path.join(tempfile.mkdtemp(prefix="tb-lg-"), "fb-loadgen")
    cl.host(m).get(src, tmp)
    cl.host(host).mkdir(os.path.dirname(dst).replace("\\", "/"))
    cl.host(host).put(tmp, dst)
    cl.host(host).run_raw(["chmod", "755", dst])


def group_of(cl, node, db_id):
    _, body = R.api(cl, "GET", "/v1/databases")
    groups = body if isinstance(body, list) else (body or {}).get("databases", []) if isinstance(body, dict) else []
    for g in groups:
        if any(m.get("node_id") == node and m.get("db_id") == db_id for m in g.get("masters") or []):
            return g
    return None


def counts_equal(cl, a_host, a_path, b_host, b_path, timeout, poll=20):
    # TB_PROBE (write-probe) is made after the publication was set up: the
    # node publishes the tables it found then, a later table only on the
    # next sync publication, so its rows do not reach the replica.
    end = time.time() + timeout
    diff = {}
    while True:
        ca, cb = cl.counts(a_host, a_path), cl.counts(b_host, b_path)
        if ca is not None and cb is not None:
            diff = {t: {"master": v["rows"], "replica": (cb.get(t) or {}).get("rows")}
                    for t, v in ca.items()
                    if v["keyed"] and t != "TB_PROBE" and (cb.get(t) or {}).get("rows") != v["rows"]}
            if not diff:
                return True, {}
        if time.time() >= end:
            return False, diff or {"error": "count failed"}
        time.sleep(poll)


def run(cl, a):
    res = Results("guidpromote", vars(a).copy())
    comps = cl.cfg.companion_hosts()
    if not comps:
        res.record("guidpromote", "SKIP", note="no companion in the config (hosts.<replica>.companion)")
        return res.finish()
    if not R.ready(cl):
        res.record("guidpromote", "SKIP", note="no RCM login in the local config")
        return res.finish()
    donor = a.replica or comps[0]
    if donor not in comps:
        raise TbError(f"{donor} has no companion (companions: {', '.join(comps)})")
    m = cl.cfg.master
    mid, dn = cl.h(m)["node_id"], cl.h(donor)["node_id"]
    cn = cl.h(donor)["companion"]["node_id"]
    others = [r for r in cl.cfg.replicas if r != donor]
    d = pick_db(cl, a, donor, cn)
    key = f"m:{mid}|{d['db_id']}"
    rec = replica_record(cl, donor, d["path"])
    if not rec:
        res.record("setup: donor record", "FAIL", note=f"{donor} has no record of {d['path']}")
        return res.finish()
    rid, dpath = rec["db_id"], rec["path"]
    port = cl.fb_port(donor)
    legacy = ops.legacy(cl, donor)
    log(f"guidpromote: {d['path']} on {dn} -> {cn}, mode {a.mode}")

    if not a.no_load:
        cl.load_start([d["path"]], mode="write", tx="off", conns="1:2", tag="guidpromote")
    cdb = None
    try:
        old = cl.hostctl(m, "db-header", {"db": d["path"], "port": cl.fb_port(m)}) or {}

        # --- promote (RCM) ---------------------------------------------------
        st, body = R.api(cl, "POST", f"/v1/nodes/{dn}/databases/{rid}/promotetomaster", {})
        pid = body.get("promote_id") if isinstance(body, dict) else None
        if st != 202 or not pid:
            res.record("promote: job", "FAIL", note=f"HTTP {st}: {str(body)[:400]}")
            return res.finish()
        job = R.wait_job(cl, f"/v1/promotes/{pid}", timeout=a.timeout, poll=5)
        res.record("promote: job ends ok", "PASS" if job.get("status") == "ok" else "FAIL",
                   note=f"status {job.get('status')} {job.get('error') or ''}")
        if job.get("status") != "ok":
            return res.finish()
        end = time.time() + 180
        while time.time() < end and not cdb:
            _, cdbs = cl.companion_api(donor, "GET", "/v1/databases", check_status=False)
            cdb = next((x for x in cdbs or [] if isinstance(x, dict) and norm(x.get("path")) == norm(dpath)), None)
            if not cdb:
                time.sleep(10)
        if not cdb:
            res.record("promote: the companion holds the file", "FAIL", note=f"{cn} does not report {dpath}")
            return res.finish()
        cid = cdb["db_id"]

        # --- before ----------------------------------------------------------
        h0 = cl.hostctl(donor, "db-header", {"db": dpath, "port": port}) or {}
        same = h0.get("guid") and h0.get("guid") == old.get("guid")
        res.record("before: the promoted database has the old master's GUID", "PASS" if same else "FAIL",
                   note=f"master {old.get('guid')} sequence {old.get('repl_seq')}; promoted {h0.get('guid')} "
                        f"sequence {h0.get('repl_seq')} ({h0.get('attributes')})")

        def dup_raised():
            # Per database: the group of the new master is a duplicate-GUID
            # group (the alert itself is one per set of master nodes).
            g = group_of(cl, cn, cid)
            return bool(g and g.get("duplicate_guid"))
        end = time.time() + 180
        dup = dup_raised()
        while not dup and time.time() < end:
            R.poll_now(cl)
            time.sleep(15)
            dup = dup_raised()
        res.record("before: RCM sees a duplicate GUID for the two masters", "PASS" if dup else "FAIL",
                   note="; ".join(str(x.get("message"))[:160] for x in R.alerts(cl, "duplicate_guid"))[:400] or "no alert")

        # --- new GUID in place ---------------------------------------------------
        r = cl.hostctl(donor, "guid-promote", {"db": dpath, "mode": a.mode, "legacy": legacy, "port": port,
                                               "fb_service": cl.fb_service(donor)}, check=False, timeout=900) or {}
        steps = r.get("steps") or []
        brief = ", ".join(f"{s['step']} rc {s['rc']} {s['sec']}s" for s in steps)
        res.record(f"new guid: every step through the running service ({a.mode})", "PASS" if r.get("ok") else "FAIL",
                   note=brief + "; " + "; ".join(s["out"][-160:] for s in steps if s["rc"]), steps=steps)
        h1 = r.get("after") or {}
        # The sequence right after -F: once online, a publishing master
        # opens segment 1 at once.
        hm = r.get("middle") or {}
        ok = (h1.get("guid") and h1.get("guid") != h0.get("guid") and hm.get("repl_seq") == 0
              and "replica" not in (h1.get("attributes") or "").lower()
              and "backup" not in (h1.get("attributes") or "").lower()
              and "shutdown" not in (h1.get("attributes") or "").lower())
        res.record("new guid: new GUID, sequence 0, not a replica, no lock, online", "PASS" if ok else "FAIL",
                   note=f"{h0.get('guid')} -> {h1.get('guid')}; sequence {h0.get('repl_seq')} -> {hm.get('repl_seq')} after -F, "
                        f"{h1.get('repl_seq')} online; "
                        f"attributes '{h1.get('attributes')}'; backup GUID {h0.get('backup_guid')} -> {h1.get('backup_guid')}",
                   middle=r.get("middle"))
        v = next((s for s in steps if s["step"] == "validate"), None)
        ok = v is not None and v["rc"] == 0 and "error" not in v["out"].lower()
        res.record("new guid: gfix -v -full clean", "PASS" if ok else "FAIL", note=(v or {}).get("out", "not run")[-300:])
        res.record("new guid: the delta was made and removed", "PASS" if r.get("delta_existed") and not r.get("delta_left") else "FAIL",
                   note=f"existed {r.get('delta_existed')}, left {r.get('delta_left')}")
        if a.mode == "shutdown":
            same_pid = r.get("pid_before") and r.get("pid_before") == r.get("pid_after")
            res.record("new guid: Firebird was not restarted", "PASS" if same_pid else "FAIL",
                       note=f"pid {r.get('pid_before')} -> {r.get('pid_after')}")
        if not r.get("ok"):
            return res.finish()
        new_guid = h1.get("guid")

        # --- journal of the new master -------------------------------------------
        w = cl.hostctl(donor, "write-probe", {"db": dpath, "port": port}, check=False) or {}
        segs = []
        end = time.time() + 120
        while time.time() < end:
            segs = cl.hostctl(donor, "segment-guids", {"glob": f"{dpath}.ReplLog/*;{dpath}.LogArch/*"}, check=False) or []
            if any(s.get("guid") == new_guid for s in segs):
                break
            time.sleep(10)
        mine = sorted(s["sequence"] for s in segs if s.get("guid") == new_guid)
        foreign = sorted({s.get("guid") for s in segs if s.get("guid") != new_guid})
        res.record("journal: the new master writes segments with the new GUID", "PASS" if w.get("written") and mine else "FAIL",
                   note=f"write {w.get('written')}; segments of the new GUID {mine[:5]}{'…' if len(mine) > 5 else ''}; "
                        f"other GUIDs left in the journal folders {foreign}")

        # --- rcm ------------------------------------------------------------------
        end = time.time() + 300
        dup = dup_raised()
        while dup and time.time() < end:
            R.poll_now(cl)
            time.sleep(15)
            dup = dup_raised()
        res.record("rcm: the duplicate GUID goes away", "FAIL" if dup else "PASS",
                   note="the group is still a duplicate-GUID group" if dup else "the new master's group is not a duplicate")

        nkey = f"m:{cn}|{cid}"
        ok, page = R.wait_table(cl, "m3", lambda pg: nkey in R.blocks(pg), what="the new master's block")
        newb = {k: v for k, v in R.blocks(page).items() if k == nkey}
        claimed = [(x.get("node"), x.get("status")) for v in newb.values() for x in R.replica_rows(v)]
        res.record("rcm: the new master's block holds no replica of the old master", "PASS" if newb and not claimed else "FAIL",
                   note=f"blocks {list(newb)}; replica rows {claimed}")

        # --- initialize from the new master ------------------------------------------
        if others:
            tgt = others[0]
            tn = cl.h(tgt)["node_id"]
            st, jr = R.reinit(cl, cn, cid, tn, timeout=a.timeout)
            ok = st == 202 and isinstance(jr, dict) and jr.get("status") == "ok"
            res.record(f"initialize: new master -> {tgt} is not refused", "PASS" if ok else "FAIL",
                       note=f"HTTP {st}: {str(jr.get('error') or jr.get('code') or jr.get('status') if isinstance(jr, dict) else jr)[:300]}")
            rrec = next((x for x in cl.databases(tgt) if x.get("db_id") == cid), None)
            if ok and rrec:
                if a.minutes > 0 and not a.no_load:
                    ensure_loadgen(cl, donor)
                    cl.module(donor, "50-load", "start", {"dbs": dpath, "port": port, "mode": "write", "tx": "off",
                                                          "conns": "1:2", "tag": "guidpromote-new"})
                    time.sleep(a.minutes * 60)
                    cl.module(donor, "50-load", "stop", {"tag": "guidpromote-new"}, check=False)
                ok, diff = counts_equal(cl, donor, dpath, tgt, rrec["path"], 900)
                res.record(f"initialize: {tgt} follows the new master under load", "PASS" if ok else "FAIL",
                           note="rows match" if ok else f"differ: {diff}")

        # --- guard --------------------------------------------------------------------
        st, jr = R.reinit(cl, mid, d["db_id"], dn, timeout=a.timeout)
        refused = st != 202 or (isinstance(jr, dict) and jr.get("status") != "ok")
        h2 = cl.hostctl(donor, "db-header", {"db": dpath, "port": port}) or {}
        w2 = cl.hostctl(donor, "write-probe", {"db": dpath, "port": port}, check=False) or {}
        why = (jr.get("error") or jr.get("code") or jr.get("status")) if isinstance(jr, dict) else jr
        ok = refused and h2.get("guid") == new_guid and w2.get("written")
        res.record("guard: Initialize from the old master onto the promoted file is refused", "PASS" if ok else "FAIL",
                   note=f"HTTP {st}: {str(why)[:300]}; GUID kept {h2.get('guid') == new_guid}; writable {w2.get('written')}")
    finally:
        cl.load_stop("guidpromote")
        cl.module(donor, "50-load", "stop", {"tag": "guidpromote-new"}, check=False)

    # --- observations: what the companion node saw ------------------------------------
    seen = sorted({x.get("code") for x in R.alerts(cl) if x.get("node_id") == cn and x.get("code") in
                   ("master_db_replaced", "old_journal_quarantined", "db_file_replaced", "shipping_paused")})
    res.record("observed: the companion's alerts after the GUID change (the planned step runs before enroll)", "PASS",
               note=", ".join(seen) or "none")

    # --- converge (every load stopped: the master must be quiet) ------------------------
    cl.load_stop()
    if others:
        converge_and_record(cl, res, f"converge: old master -> {', '.join(others)}", [d["path"]], 900, replicas=others)
    rest = [x["path"] for x in cl.test_dbs() if x["path"] != d["path"] and replica_record(cl, donor, x["path"])
            and (replica_record(cl, donor, x["path"]) or {}).get("state") != "ORPHANED"]
    if rest:
        converge_and_record(cl, res, f"converge: the other databases on {donor} (same Firebird)", rest, 900,
                            replicas=[donor])
    return res.finish()
