"""GUIDs of the master/replica pairs, how RCM pairs them, and the in-place
GUID change of a promote, on any engine (made for HQbird 2.5/3.0, where the
replica names its master in its own header).

  pairs     per test database and replica: the replica's Database GUID
            against the master's; on HQbird 2.5/3.0 its Replication master
            GUID against the master's Database GUID
  rcm       RCM pairs each replica with its master (/v1/databases)
  scratch   a copy of the master made the way reinit makes a replica
            (nbackup -L, copy, -N, -F -SEQUENCE, replica mode): it has the
            master's GUID, so a promote without a change would publish
            under the old master's GUID
  new guid  the in-place change on that copy through the running service:
            replica mode off ({} on 2.5/3.0, none on 4/5), single shutdown,
            nbackup -L, -F without -SEQUENCE, delta removed, validated,
            online — a new GUID, sequence 0, no replica mode, no master
            GUID left, Firebird not restarted; the copy takes a write

The copy lives beside the master database as <db>.tbguidprobe (not *.fdb:
the node does not take it) and is removed at the end. Linux hosts only."""
from tblib import ops
from tblib.cluster import TbError
from tblib.results import Results

from . import _rcm as R
from ._common import replica_record

HELP = "GUIDs of master/replica pairs, RCM pairing, and the in-place GUID change of a promote on a scratch copy"


def add_args(p):
    p.add_argument("--db", default="", help="test databases (folder names); default all")
    p.add_argument("--mode", default="shutdown", choices=["shutdown", "stop"])
    p.add_argument("--keep", action="store_true", help="keep the scratch copy")


def dname(d):
    return d["path"].replace("\\", "/").split("/")[-2]


def groups(cl):
    _, body = R.api(cl, "GET", "/v1/databases")
    return body if isinstance(body, list) else (body or {}).get("databases", []) if isinstance(body, dict) else []


def run(cl, a):
    res = Results("guidprobe", vars(a).copy())
    m = cl.cfg.master
    leg = ops.legacy(cl, m)
    dbs = cl.test_dbs(which=a.db or "all")
    if not dbs:
        raise TbError("no test databases (run 'tb.py dbs prepare')")
    mid = cl.h(m)["node_id"]

    # --- pairs ---------------------------------------------------------------
    mheads = {}
    for d in dbs:
        mh = mheads[d["path"]] = cl.hostctl(m, "db-header", {"db": d["path"]}) or {}
        for r in cl.cfg.replicas:
            rec = replica_record(cl, r, d["path"])
            if not rec:
                res.record(f"pairs: {dname(d)} on {r}", "FAIL", note="no replica record")
                continue
            rh = cl.hostctl(r, "db-header", {"db": rec["path"]}) or {}
            own = "the master's" if rh.get("guid") == mh.get("guid") else "its own"
            note = (f"master {mh.get('guid')}; replica Database GUID {rh.get('guid')} ({own}); "
                    f"sequence {rh.get('repl_seq')}; attributes '{rh.get('attributes')}'")
            if leg:
                ok = bool(rh.get("master_guid")) and rh.get("master_guid") == mh.get("guid")
                res.record(f"pairs: {dname(d)} on {r}: Replication master GUID = master's Database GUID",
                           "PASS" if ok else "FAIL", note=note + f"; Replication master GUID {rh.get('master_guid')}")
            else:
                res.record(f"pairs: {dname(d)} on {r}: the replica's GUID", "PASS" if rh.get("ok") else "FAIL",
                           note=note)

    # --- rcm -------------------------------------------------------------------
    if R.ready(cl):
        R.poll_now(cl)
        gs = groups(cl)
        want = {cl.h(r)["node_id"] for r in cl.cfg.replicas}
        for d in dbs:
            g = next((g for g in gs if any(x.get("node_id") == mid and x.get("db_id") == d["db_id"]
                                           for x in g.get("masters") or [])), None)
            reps = {x.get("node_id"): x.get("status_ui") for x in (g or {}).get("replicas") or []}
            lone = [(x.get("node_id"), x.get("status_ui")) for o in gs if not o.get("masters")
                    for x in o.get("replicas") or [] if x.get("db_id") == d["db_id"]]
            ok = g is not None and want <= set(reps)
            res.record(f"rcm: {dname(d)}: the master's group holds every replica", "PASS" if ok else "FAIL",
                       note=f"group GUID {(g or {}).get('guid')}; replicas {reps}; "
                            f"replicas in groups without a master {lone}")
    else:
        res.record("rcm", "SKIP", note="no RCM login in the local config")

    # --- scratch copy, then the in-place change ----------------------------------
    d = dbs[0]
    mh = mheads[d["path"]]
    scratch = d["path"] + ".tbguidprobe"
    try:
        c = cl.hostctl(m, "db-copy-locked", {"db": d["path"], "to": scratch, "fixup": "seq",
                                             "replica": "{" + mh.get("guid", "") + "}" if leg else "read_only"},
                       check=False) or {}
        ch = c.get("header") or {}
        ok = (ch.get("guid") == mh.get("guid") and "replica" in (ch.get("attributes") or "").lower()
              and (not leg or ch.get("master_guid") == mh.get("guid")))
        res.record("scratch: a replica copy made as reinit makes it", "PASS" if ok else "FAIL",
                   note=f"Database GUID {ch.get('guid')} (master {mh.get('guid')}); sequence {ch.get('repl_seq')} "
                        f"(master {mh.get('repl_seq')}); Replication master GUID {ch.get('master_guid') or '-'}; "
                        f"attributes '{ch.get('attributes')}'")
        if not ok:
            return res.finish()
        res.record("scratch: promoted as it is, it would publish under the master's GUID (a change is needed)",
                   "PASS" if ch.get("guid") == mh.get("guid") else "FAIL",
                   note=f"{ch.get('guid')} = {mh.get('guid')}")

        g = cl.hostctl(m, "guid-promote", {"db": scratch, "mode": a.mode, "legacy": leg,
                                           "fb_service": cl.fb_service(m)}, check=False, timeout=600) or {}
        steps = g.get("steps") or []
        res.record(f"new guid: every step through the running service ({a.mode})", "PASS" if g.get("ok") else "FAIL",
                   note=", ".join(f"{s['step']} rc {s['rc']} {s['sec']}s" for s in steps) + "; " +
                        "; ".join(s["out"][-160:] for s in steps if s["rc"]), steps=steps)
        h = g.get("after") or {}
        hm = g.get("middle") or {}
        attrs = (h.get("attributes") or "").lower()
        ok = (h.get("guid") and h.get("guid") != ch.get("guid") and hm.get("repl_seq") == 0 and not h.get("master_guid")
              and not any(w in attrs for w in ("replica", "backup", "shutdown", "maintenance")))
        res.record("new guid: new GUID, sequence 0, no replica mode, no master GUID, no lock, online",
                   "PASS" if ok else "FAIL",
                   note=f"{ch.get('guid')} -> {h.get('guid')}; sequence {ch.get('repl_seq')} -> {hm.get('repl_seq')} after -F; "
                        f"Replication master GUID {ch.get('master_guid') or '-'} -> {h.get('master_guid') or '-'}; "
                        f"attributes '{h.get('attributes')}'", middle=g.get("middle"))
        v = next((s for s in steps if s["step"] == "validate"), None)
        res.record("new guid: gfix -v -full clean", "PASS" if v and v["rc"] == 0 and "error" not in v["out"].lower() else "FAIL",
                   note=(v or {}).get("out", "not run")[-300:])
        res.record("new guid: the delta was made and removed",
                   "PASS" if g.get("delta_existed") and not g.get("delta_left") else "FAIL",
                   note=f"existed {g.get('delta_existed')}, left {g.get('delta_left')}")
        if a.mode == "shutdown":
            res.record("new guid: Firebird was not restarted",
                       "PASS" if g.get("pid_before") and g.get("pid_before") == g.get("pid_after") else "FAIL",
                       note=f"pid {g.get('pid_before')} -> {g.get('pid_after')}")
        w = cl.hostctl(m, "write-probe", {"db": scratch}, check=False) or {}
        res.record("new guid: the copy takes a write", "PASS" if w.get("written") else "FAIL",
                   note=(w.get("error") or "")[-200:])
    finally:
        if not a.keep:
            for f in (scratch, scratch + ".delta"):
                cl.hostctl(m, "remove-file", {"path": f}, check=False)
    return res.finish()
