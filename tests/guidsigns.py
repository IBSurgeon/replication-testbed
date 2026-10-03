"""The master as a replica knows it (hqcluster-node 2027.4.5, protocol minor
11; plan replica-own-guid v3): the fields a replica node reports, and the
control file sign on Firebird 4.0/5.0.

  fields    GET /v1/databases/{id}/guid and GET /v1/stats on the replica:
            master_guid = the master's GUID, master_guid_from (conf on
            4.0/5.0, header on 2.5/3.0), control_guid (4.0/5.0) and
            source_node_id / source_db_id of the reinit
  control   the control file {master GUID} removed from the mailbox of a
            working replica (4.0/5.0) under load: replica_control_missing is
            raised and the replica is NEEDS_REINIT. Firebird 5.0 resets the
            replication ("Database sequence has been changed from 0 to N")
            and deletes the next segments without applying them; the lines
            are noted
  restore   reinit: the alert goes, the replica converges

The noseq test covers the other sign (replica_sequence_zero). Linux hosts
only."""
import time

from tblib import ops
from tblib.cluster import TbError, log
from tblib.results import Results

from ._common import converge_and_record, replica_record

HELP = "node 2027.4.5: master_guid of a replica (fields), replica_control_missing when its control file goes"


def add_args(p):
    p.add_argument("--db", default="", help="test database (folder name); default the first one")
    p.add_argument("--replica", default="", help="replica host; default the first one")
    p.add_argument("--seconds", type=int, default=30, help="load before and after the control file goes")


def run(cl, a):
    res = Results("guidsigns", vars(a).copy())
    rep = a.replica or cl.cfg.replicas[0]
    if ops.protocol_minor(cl, rep) < 11:
        res.record("guidsigns", "SKIP", note="the replica node is older than 2027.4.5 (protocol minor 11)")
        return res.finish()
    dbs = cl.test_dbs(which=a.db or "all")
    if not dbs:
        raise TbError("no test databases (run 'tb.py dbs prepare')")
    d = dbs[0]
    m = cl.cfg.master
    legacy = ops.legacy(cl, rep)
    cl.load_stop("guidsigns")
    try:
        ops.reinit(cl, d["db_id"], rep)
        cl.load_start([d["path"]], mode="write", tx="off", conns="1:2", tag="guidsigns")
        time.sleep(a.seconds)
        cl.load_stop("guidsigns")
        converge_and_record(cl, res, "setup: reinit and a short load converge", [d["path"]], 600, replicas=[rep])
        rec = replica_record(cl, rep, d["path"]) or {}
        rid = rec.get("db_id")
        # The master's GUID as the node says it (canonical): gstat of HQbird
        # 2.5/3.0 prints the words in another order (hostctl db-header).
        _, mg = cl.api(m, "GET", f"/v1/databases/{d['db_id']}/guid", check_status=False)
        master_guid = ((mg or {}).get("guid") or "").upper().strip("{}")

        # --- fields ---------------------------------------------------------------
        _, g = cl.api(rep, "GET", f"/v1/databases/{rid}/guid", check_status=False)
        g = g if isinstance(g, dict) else {}
        want_from = "header" if legacy else "conf"
        ok = ((g.get("master_guid") or "").upper() == master_guid and g.get("master_guid_from") == want_from
              and g.get("source_node_id") == cl.h(m)["node_id"] and g.get("source_db_id") == rid)
        if not legacy:
            ok = ok and (g.get("control_guid") or "").upper() == master_guid
        res.record("fields: GET /guid names the master as the replica knows it", "PASS" if ok else "FAIL",
                   note=f"master {master_guid}; answer {g}")
        _, s = cl.api(rep, "GET", "/v1/stats", check_status=False)
        row = next((x for x in (s or {}).get("databases", []) if isinstance(x, dict) and x.get("db_id") == rid), {})
        ok = (row.get("master_guid") or "").upper() == master_guid and row.get("master_guid_from") == want_from
        res.record("fields: the stats row carries master_guid", "PASS" if ok else "FAIL",
                   note={k: row.get(k) for k in ("master_guid", "master_guid_from", "control_guid",
                                                 "source_node_id", "source_db_id")})

        # --- control file ---------------------------------------------------------------
        if legacy:
            res.record("control: replica_control_missing", "SKIP", note="Firebird 4.0/5.0 only")
            return res.finish()
        rp = rec.get("path") or cl.replica_path(rep, d["path"])
        mailbox = rec.get("incoming") or rp + ".Incoming"
        ctl = cl.host(rep).join(mailbox, "{" + master_guid + "}")
        cl.hostctl(rep, "remove-file", {"path": ctl}, check=False)
        cl.load_start([d["path"]], mode="write", tx="off", conns="1:2", tag="guidsigns")
        end, raised = time.time() + 180, False
        while not raised and time.time() < end:
            time.sleep(10)
            raised = bool(ops.node_alerts(cl, rep, "replica_control_missing", rid))
        res.record("control: replica_control_missing raised after the control file went", "PASS" if raised else "FAIL",
                   note=[x.get("message", "")[:200] for x in ops.node_alerts(cl, rep, "replica_control_missing", rid)])
        time.sleep(max(0, a.seconds - 10))
        cl.load_stop("guidsigns")
        st = next((x for x in cl.databases(rep) if x.get("db_id") == rid), {}) or {}
        res.record("control: the replica is NEEDS_REINIT", "PASS" if st.get("state") == "NEEDS_REINIT" else "FAIL",
                   note=f"state {st.get('state')} {st.get('state_reason') or ''}")
        rlog = cl.host(rep).join(cl.h(rep)["firebird"]["root"], "replication.log")
        _, out, _ = cl.host(rep).run_raw(["tail", "-n", "400", rlog], check=False)
        keys = ("sequence has been changed", "is scanned", "resetting")
        seen = sorted({k for x in out.splitlines() for k in keys if k in x.lower()})
        same, _ = cl.compare(d["path"], [rep])
        res.record("control: what Firebird did (noted)", "PASS",
                   note=f"replication.log: {seen}; rows {'match' if same else 'differ'}")
    finally:
        cl.load_stop("guidsigns")
        if not legacy:
            log("guidsigns: restoring the replica with a reinit")
            try:
                ops.reinit(cl, d["db_id"], rep)
            except TbError as e:
                res.record("restore: reinit", "FAIL", note=str(e)[:300])
    rec = replica_record(cl, rep, d["path"]) or {}
    left = ops.node_alerts(cl, rep, "replica_control_missing", rec.get("db_id"))
    res.record("restore: the alert goes with the reinit", "FAIL" if left else "PASS", note=str(left)[:200])
    converge_and_record(cl, res, "restore: converge", [d["path"]], 900, replicas=[rep])
    return res.finish()
