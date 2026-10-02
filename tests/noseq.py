"""A replica recreated by hand without -SEQUENCE over a working
replication — the way the HQbird guide (4.6.1, nbackup -f without -seq)
makes one. Firebird 4.0 then skips the new segments without an error,
5.0.4+ stops with "zero sequence number". Is there a sign the node could
watch without verbose_logging? The sign checked here: the replica's
replication sequence is 0 while the control file {master GUID} in its
journal source folder holds db_sequence > 0.

  baseline  a fresh reinit (-SEQUENCE) and a short load: the replica's
            sequence N > 0 and its control file's db_sequence = N
  recreate  the master copied under nbackup lock, -F without -SEQUENCE,
            replica mode, put in place of the replica with Firebird stopped
  sign      replica sequence 0 while the control file holds db_sequence > 0;
            it stays while the next segments go
  firebird  what the next segments do: rows on the replica and the
            replication.log lines about them (noted, not failed)
  node      the node state or RCM flags the replica — FAIL when a replica
            that lost rows still looks healthy (alerts are noted only)
  restore   reinit (-SEQUENCE) and converge

Firebird 4.0/5.0 only; Linux hosts only. The copy goes through this
machine (scp), so keep the database small."""
import os
import tempfile
import time

from tblib import ops
from tblib.cluster import TbError, log
from tblib.results import Results

from . import _rcm as R
from ._common import converge_and_record, replica_record

HELP = "a replica recreated by hand without -SEQUENCE: the sign (sequence 0, control file db_sequence > 0) and who notices"

FLAG_STATES = ("NEEDS_REINIT", "NEEDS_ATTENTION", "DISABLED", "FAILED")
LOG_KEYS = ("zero sequence", "sequence has been changed", "is scanned", "is replicated", "fast forward",
            "resetting", "replaced", "older", "error")


def add_args(p):
    p.add_argument("--db", default="", help="test database (folder name); default the last one")
    p.add_argument("--replica", default="", help="replica host; default the first one")
    p.add_argument("--seconds", type=int, default=90, help="load after the recreation")


def is_replica(cl, rep, d):
    # guidpromote leaves a promoted file where the replica was.
    path = (replica_record(cl, rep, d["path"]) or {}).get("path") or cl.replica_path(rep, d["path"])
    h = cl.hostctl(rep, "db-header", {"db": path}, check=False) or {}
    return "replica" in (h.get("attributes") or "").lower()


def control_for(cl, rep, mailbox, guid):
    for c in cl.hostctl(rep, "replctl", {"dir": mailbox}, check=False) or []:
        if guid and guid.upper() in c.get("file", "").upper():
            return c
    return None


def run(cl, a):
    res = Results("noseq", vars(a).copy())
    rep = a.replica or cl.cfg.replicas[0]
    if ops.legacy(cl, rep):
        res.record("noseq", "SKIP", note="HQbird 2.5/3.0: the replica names its master in its header")
        return res.finish()
    dbs = cl.test_dbs(which=a.db or "all")
    if not dbs:
        raise TbError("no test databases (run 'tb.py dbs prepare')")
    m = cl.cfg.master
    d = dbs[0] if a.db else next((x for x in reversed(dbs) if is_replica(cl, rep, x)), None)
    if not d:
        raise TbError(f"no test database is a replica on {rep}")
    rn = cl.h(rep)["node_id"]
    conf = cl.host(rep).join(cl.h(rep)["firebird"]["root"], "replication.conf")
    rlog = cl.host(rep).join(cl.h(rep)["firebird"]["root"], "replication.log")
    cl.load_stop("noseq")
    try:
        # --- baseline ----------------------------------------------------------
        ops.reinit(cl, d["db_id"], rep)
        cl.load_start([d["path"]], mode="write", tx="off", conns="1:2", tag="noseq")
        time.sleep(30)
        cl.load_stop("noseq")
        conv, _ = cl.wait_converged([d["path"]], timeout=600, replicas=[rep])
        rec = replica_record(cl, rep, d["path"]) or {}
        rp = rec.get("path") or cl.replica_path(rep, d["path"])
        mailbox = rec.get("incoming") or rp + ".Incoming"
        mh = cl.hostctl(m, "db-header", {"db": d["path"]}) or {}
        rh = cl.hostctl(rep, "db-header", {"db": rp}) or {}
        ctl = control_for(cl, rep, mailbox, mh.get("guid"))
        ok = conv and rh.get("repl_seq", 0) > 0 and ctl and ctl.get("db_sequence") == rh.get("repl_seq")
        res.record("baseline: replica sequence N > 0 and control file db_sequence = N", "PASS" if ok else "FAIL",
                   note=f"rows {'match' if conv else 'differ'}; master {mh.get('guid')} sequence {mh.get('repl_seq')}; "
                        f"replica {rh.get('guid')} sequence {rh.get('repl_seq')}; control {ctl}")
        if not ok:
            return res.finish()

        # --- recreate by hand, without -SEQUENCE ----------------------------------
        tmp_m, tmp_r = d["path"] + ".tbnoseq", rp + ".tbnoseq"
        c = cl.hostctl(m, "db-copy-locked", {"db": d["path"], "to": tmp_m, "fixup": "noseq", "replica": "read_only"},
                       check=False) or {}
        local = os.path.join(tempfile.mkdtemp(prefix="tb-noseq-"), "copy.fdb")
        cl.host(m).get(tmp_m, local)
        cl.hostctl(m, "remove-file", {"path": tmp_m}, check=False)
        cl.host(rep).put(local, tmp_r)
        r = cl.hostctl(rep, "replace-db", {"db": rp, "with": tmp_r, "fb_service": cl.fb_service(rep)}, check=False) or {}
        nh = r.get("header") or {}
        ok = (nh.get("guid") and nh.get("guid") != mh.get("guid") and nh.get("repl_seq") == 0
              and "replica" in (nh.get("attributes") or "").lower())
        res.record("recreate: own GUID, sequence 0, read-only replica, in place of the replica", "PASS" if ok else "FAIL",
                   note=f"copy {(c.get('header') or {}).get('guid')}; placed {nh.get('guid')} sequence {nh.get('repl_seq')} "
                        f"'{nh.get('attributes')}'")
        if not ok:
            return res.finish()

        # --- the sign ----------------------------------------------------------------
        ctl2 = control_for(cl, rep, mailbox, mh.get("guid"))
        sign = nh.get("repl_seq") == 0 and ctl2 is not None and (ctl2.get("db_sequence") or 0) > 0
        res.record("sign: replica sequence 0 while the control file holds db_sequence > 0", "PASS" if sign else "FAIL",
                   note=f"replica sequence {nh.get('repl_seq')}; control {ctl2}")

        # --- what Firebird does with the next segments ----------------------------------
        cl.load_start([d["path"]], mode="write", tx="off", conns="1:2", tag="noseq")
        time.sleep(a.seconds)
        cl.load_stop("noseq")
        time.sleep(60)
        same, report = cl.compare(d["path"], [rep])
        _, out, _ = cl.host(rep).run_raw(["tail", "-n", "600", rlog], check=False)
        # A record: a time line, then tab-indented "Database: <path>" and the
        # message lines.
        lines, ours = [], False
        for x in out.splitlines():
            if x.strip().startswith("Database:"):
                ours = x.strip()[len("Database:"):].strip() == rp
            elif x[:1] not in ("\t", " "):
                ours = False
            elif ours and any(k in x.lower() for k in LOG_KEYS):
                lines.append(x.strip())
        lines = lines[-8:]
        _, vout, _ = cl.host(rep).run_raw(["grep", "-i", "verbose_logging", conf], check=False)
        verbose = [x.strip() for x in vout.splitlines() if x.strip() and not x.strip().startswith("#")]
        ctl3 = control_for(cl, rep, mailbox, mh.get("guid"))
        res.record("firebird: the next segments (noted)", "PASS",
                   note=f"engine {cl.fb_engine(rep) or 'auto'}; rows {'match' if same else 'differ'}; "
                        f"control after {ctl3}; verbose_logging {verbose}; log {lines}",
                   report=report)
        if ctl3 and ctl2:
            res.record("sign: it stays while segments go (control sequence moves, db_sequence does not)",
                       "PASS" if ctl3.get("db_sequence") == ctl2.get("db_sequence") and nh.get("repl_seq") == 0 else "FAIL",
                       note=f"control sequence {ctl2.get('sequence')} -> {ctl3.get('sequence')}; "
                            f"db_sequence {ctl2.get('db_sequence')} -> {ctl3.get('db_sequence')}")

        # --- does anyone notice? -----------------------------------------------------------
        st = next((x for x in cl.databases(rep) if x.get("db_id") == rec.get("db_id")), {}) or {}
        alerts = []
        rcm = ""
        if R.ready(cl):
            R.poll_now(cl)
            time.sleep(5)
            alerts = sorted({x.get("code") for x in R.alerts(cl) if x.get("node_id") == rn})
            _, body = R.api(cl, "GET", "/v1/databases")
            gs = body if isinstance(body, list) else (body or {}).get("databases", []) if isinstance(body, dict) else []
            rcm = next((x.get("status_ui") for g in gs for x in g.get("replicas") or []
                        if x.get("node_id") == rn and x.get("db_id") == rec.get("db_id")), "")
        # db_file_replaced is not counted: it says the file changed (new
        # GUID), only while the node saw the old file, not that segments are
        # lost.
        flagged = st.get("state") in FLAG_STATES or rcm in ("Error", "Disabled")
        res.record("node: the replica that loses segments is not shown healthy (state or RCM)",
                   "PASS" if same or flagged else "FAIL",
                   note=f"node state {st.get('state')} {st.get('state_reason') or ''}; RCM {rcm or '-'}; "
                        f"alerts on {rn}: {alerts}")
    finally:
        cl.load_stop("noseq")
        for h, f in ((m, d["path"] + ".tbnoseq"), (rep, cl.replica_path(rep, d["path"]) + ".tbnoseq")):
            cl.hostctl(h, "remove-file", {"path": f}, check=False)
        log("noseq: restoring the replica with a reinit")
        try:
            ops.reinit(cl, d["db_id"], rep)
        except TbError as e:
            res.record("restore: reinit", "FAIL", note=str(e)[:300])
    converge_and_record(cl, res, "restore: converge", [d["path"]], 900, replicas=[rep])
    return res.finish()
