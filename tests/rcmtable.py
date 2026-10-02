"""The database tables of the RCM Databases page, read as the page reads them
(web login, /partials/db-table): master-oriented (m1), replica-oriented (m2)
and combined (m3, docs/RCM_UI_DB_TABLE_MERGE_PLAN.md in hqcluster3).

Cases (--cases, default all, in this order):

  table  m3 has a block per test database and a row per replica; m2 the
         same rows; m1 a column per replica node
  alien  "alien clean off" on one replica marks every row of its block
         (m2 and m3), so a filter hides the whole block
  dup    a second replica of one database on one node (a copy of the file in
         another folder): two rows of one block in m3 (m1: a second column)
  peer   a replica node with no database of the master (all removed from
         its management): a "not paired" row with Initialize for it in every
         block (it is a peer of the master); Initialize from RCM brings each
         database back

Load is stopped; the end is a converge of every test database. Needs the
RCM login (secrets.rcm_user / rcm_password); dup and peer need Linux replicas.
"""
import time

from tblib import ops
from tblib.cluster import TbError, log
from tblib.results import Results

from . import _rcm as R
from ._common import converge_and_record, norm, replica_record

HELP = "RCM Databases page tables (m1/m2/m3): rows, not paired + Initialize, two replicas on one node"

CASES = ("table", "alien", "dup", "peer")


def add_args(p):
    p.add_argument("--cases", default=",".join(CASES))
    p.add_argument("--catchup-timeout", type=int, default=900)


def exclude_paths(cl, host):
    _, doc = cl.api(host, "GET", "/v1/config")
    cfg = doc.get("config", doc) if isinstance(doc, dict) else {}
    return list((cfg.get("databases") or {}).get("exclude_paths") or [])


def unmanage(cl, host, ids):
    st, body = R.api(cl, "POST", f"/v1/nodes/{cl.h(host)['node_id']}/databases/unmanage", {"db_ids": ids})
    res = (body or {}).get("results") if isinstance(body, dict) else None
    ok = st == 200 and res and all(r.get("status") == "removed" for r in res)
    return ok, res or body


def remove_files(cl, host, paths):
    """Delete database files on a host, Firebird stopped meanwhile (a replica
    file may be attached by the replication applier)."""
    cl.hostctl(host, "fb-svc", {"action": "stop", "fb_service": cl.fb_service(host)}, check=False)
    try:
        for p in paths:
            cl.hostctl(host, "remove-file", {"path": p}, check=False)
    finally:
        cl.hostctl(host, "fb-svc", {"action": "start", "fb_service": cl.fb_service(host)}, check=False)


def restart_if_pending(cl, host, why):
    if any(str(d.get("state", "")).startswith("PENDING_RESTART") for d in cl.databases(host)):
        ops.restart_firebird(cl, host, why)


def key(cl, d):
    return f"m:{cl.h(cl.cfg.master)['node_id']}|{d['db_id']}"


# ------------------------------------------------------------------- cases --
def c_table(cl, res, dbs):
    reps = {cl.h(r)["node_id"] for r in cl.cfg.replicas}
    for tab in ("m3", "m2"):
        page = R.table(cl, tab)
        bl = R.blocks(page)
        bad = []
        for d in dbs:
            b = bl.get(key(cl, d))
            if not b:
                bad.append(f"{d['db_id']}: no block")
                continue
            nodes = sorted(r["node"] for r in R.replica_rows(b))
            extra = [r for r in b["rows"] if r["kind"] != "replica"]
            if nodes != sorted(reps) or extra:
                bad.append(f"{d['db_id']}: replicas {nodes}, other rows {[r['kind'] for r in extra]}")
        res.record(f"table {tab}: a block per database, a row per replica", "FAIL" if bad else "PASS",
                   note="; ".join(bad) or f"{len(dbs)} block(s), replicas {sorted(reps)}")
    cols = R.m1_columns(R.table(cl, "m1"))
    ok = sorted(cols) == sorted(reps)
    res.record("table m1: a column per replica node", "PASS" if ok else "FAIL", note=f"columns {cols}")


def c_alien(cl, res, dbs):
    rep = cl.cfg.replicas[-1]
    d = dbs[0]
    rec = replica_record(cl, rep, d["path"])
    if not rec:
        res.record("alien: setup", "FAIL", note=f"{rep} has no record of {d['path']}")
        return
    rn = cl.h(rep)["node_id"]
    path = f"/v1/nodes/{rn}/databases/drop-foreign"
    try:
        st, body = R.api(cl, "POST", path, {"db_ids": [rec["db_id"]], "enabled": False})
        results = (body or {}).get("results") if isinstance(body, dict) else body
        if st != 200:
            res.record("alien: switch off on one replica", "FAIL", note=f"HTTP {st}: {str(body)[:300]}")
            return
        res.record("alien: switch off on one replica", "PASS", note=str(results)[:300])
        for tab in ("m2", "m3"):
            def marked(page):
                b = R.blocks(page).get(key(cl, d))
                rows = b["rows"] if b else []
                return len(rows) >= 2 and all(r["attrs"].get("data-ralienoff") == "1" for r in rows)
            ok, page = R.wait_table(cl, tab, marked, timeout=240, what="the alien-off mark")
            b = R.blocks(page).get(key(cl, d)) or {"rows": []}
            res.record(f"alien {tab}: every row of the block is marked", "PASS" if ok else "FAIL",
                       note=f"{len(b['rows'])} row(s), marks "
                            f"{[r['attrs'].get('data-ralienoff') for r in b['rows']]}")
    finally:
        R.api(cl, "POST", path, {"db_ids": [rec["db_id"]], "enabled": None})


def c_dup(cl, res, dbs):
    rep = cl.cfg.replicas[0]
    h = cl.h(rep)
    if h["os"] != "linux":
        res.record("dup: two replicas on one node", "SKIP", note=f"{rep} is not Linux")
        return
    d = dbs[0]
    rec = replica_record(cl, rep, d["path"])
    if not rec:
        res.record("dup: setup", "FAIL", note=f"{rep} has no record of {d['path']}")
        return
    hst = cl.host(rep)
    src = rec["path"]
    root = h["paths"]["db_root"].rstrip("/")
    rel = src[len(root):].lstrip("/").split("/")
    dst = hst.join(root, "tb-copy", *rel[1:])       # rel[0] is the master's node id
    rn = cl.h(rep)["node_id"]
    old = exclude_paths(cl, rep)
    copy_id = None
    try:
        log(f"dup: copy {src} -> {dst} on {rep}, Firebird stopped")
        cl.hostctl(rep, "fb-svc", {"action": "stop", "fb_service": cl.fb_service(rep)})
        try:
            cl.hostctl(rep, "file-copy", {"from": src, "to": dst})
        finally:
            cl.hostctl(rep, "fb-svc", {"action": "start", "fb_service": cl.fb_service(rep)})
        cl.api(rep, "POST", "/v1/scansync", {}, check_status=False)
        end = time.time() + 180
        while time.time() < end and not copy_id:
            copy_id = next((x["db_id"] for x in cl.databases(rep) if norm(x.get("path")) == norm(dst)), None)
            if not copy_id:
                time.sleep(10)
        if not copy_id:
            res.record("dup: the node takes the copy", "FAIL", note=f"no record of {dst} on {rep}")
            return
        res.record("dup: the node takes the copy", "PASS", note=f"{rep} record {copy_id}")

        def two(page):
            b = R.blocks(page).get(key(cl, d))
            return b is not None and len(R.replica_rows(b, rn)) == 2
        ok, page = R.wait_table(cl, "m3", two, what=f"two {rn} rows")
        b = R.blocks(page).get(key(cl, d)) or {"rows": []}
        cols = R.m1_columns(R.table(cl, "m1"))
        res.record("dup m3: two rows of one block, not a second column", "PASS" if ok else "FAIL",
                   note=f"{rn} rows {[r.get('name') for r in R.replica_rows(b, rn)]}; "
                        f"other tabs: m1 columns {cols} (m1 gives the node a column per replica)")
    finally:
        # The file goes first: a replica record whose file and mailbox are
        # both on disk is how segments reach that file, and the node refuses
        # to forget it (mailbox_active), so "Remove from management" would
        # leave it excluded and ORPHANED.
        remove_files(cl, rep, [dst])
        if copy_id:
            ok, out = unmanage(cl, rep, [copy_id])
            log(f"dup cleanup: unmanage {copy_id}: {out}")
        cl.api(rep, "PUT", "/v1/config", {"databases": {"exclude_paths": old}}, check_status=False)
        restart_if_pending(cl, rep, "test bed: rcmtable dup cleanup")

    def one(page):
        b = R.blocks(page).get(key(cl, d))
        return b is not None and len(R.replica_rows(b, rn)) == 1
    ok, _ = R.wait_table(cl, "m3", one, what="the copy gone")
    left = [x["db_id"] for x in cl.databases(rep) if norm(x.get("path")) == norm(dst)]
    res.record("dup: cleanup", "PASS" if ok and not left else "FAIL",
               note="one row again, no record of the copy" if ok and not left else f"records left {left}")


def c_peer(cl, res, dbs):
    rep = cl.cfg.replicas[-1]
    h = cl.h(rep)
    if h["os"] != "linux":
        res.record("peer: replica node with no database", "SKIP", note=f"{rep} is not Linux")
        return
    rn = h["node_id"]
    mid = cl.h(cl.cfg.master)["node_id"]
    recs = {d["db_id"]: replica_record(cl, rep, d["path"]) for d in dbs}
    missing = [k for k, v in recs.items() if not v]
    if missing:
        res.record("peer: setup", "FAIL", note=f"{rep} has no record of {missing}")
        return
    old = exclude_paths(cl, rep)
    done = set()
    try:
        # The replica loses its files (as after a disk loss), then RCM takes
        # the records out of its management. With the files still there the
        # node refuses to forget them (mailbox_active).
        remove_files(cl, rep, [r["path"] for r in recs.values()])
        ok, out = unmanage(cl, rep, [r["db_id"] for r in recs.values()])
        res.record("peer: remove every database from the replica's management", "PASS" if ok else "FAIL",
                   note=str(out)[:300])
        if not ok:
            return
        cl.api(rep, "PUT", "/v1/config", {"databases": {"exclude_paths": old}})
        restart_if_pending(cl, rep, "test bed: rcmtable peer")

        def all_not_paired(page):
            bl = R.blocks(page)
            return all((b := bl.get(key(cl, d))) and not R.replica_rows(b, rn)
                       and any(x.get("init_to") == rn for x in R.replica_rows(b, rn, "not_paired"))
                       for d in dbs)
        ok, page = R.wait_table(cl, "m3", all_not_paired, what=f"'not paired' rows of {rn}")
        cols = R.m1_columns(R.table(cl, "m1"))
        res.record("peer m3: a 'not paired' row with Initialize in every block", "PASS" if ok else "FAIL",
                   note=f"{rn} holds no database of {mid}; it is a peer. m1 columns {cols}")

        for i, d in enumerate(dbs):
            st, job = R.reinit(cl, mid, d["db_id"], rn)
            good = isinstance(job, dict) and job.get("status") == "ok"
            if good:
                done.add(d["db_id"])
            res.record(f"peer: Initialize {d['db_id']} -> {rn} from RCM", "PASS" if good else "FAIL",
                       note=(f"HTTP {st}, status {job.get('status') if isinstance(job, dict) else job}"
                             + (f", error {job.get('error')}" if isinstance(job, dict) and job.get("error") else "")))
            if good and i == 0 and len(dbs) > 1:
                def only_first(page):
                    bl = R.blocks(page)
                    b0 = bl.get(key(cl, d))
                    rest = [bl.get(key(cl, x)) for x in dbs[1:]]
                    return b0 and R.replica_rows(b0, rn) and not R.replica_rows(b0, rn, "not_paired") \
                        and all(b and R.replica_rows(b, rn, "not_paired") for b in rest)
                ok, _ = R.wait_table(cl, "m3", only_first, what=f"{d['db_id']} paired again")
                res.record("peer m3: the block of that database has its replica row again, the others do not",
                           "PASS" if ok else "FAIL")

        def none_left(page):
            bl = R.blocks(page)
            return all((b := bl.get(key(cl, d))) and R.replica_rows(b, rn)
                       and not R.replica_rows(b, None, "not_paired") for d in dbs)
        ok, _ = R.wait_table(cl, "m3", none_left, what="no 'not paired' rows")
        res.record("peer m3: no 'not paired' row left", "PASS" if ok else "FAIL")
    finally:
        cl.api(rep, "PUT", "/v1/config", {"databases": {"exclude_paths": old}}, check_status=False)
        restart_if_pending(cl, rep, "test bed: rcmtable peer cleanup")
        for d in dbs:
            if d["db_id"] not in done and not replica_record(cl, rep, d["path"]):
                log(f"peer cleanup: reinit {d['db_id']} -> {rep} through the node API")
                try:
                    ops.reinit(cl, d["db_id"], rep, overwrite_non_replica=True)
                except TbError as e:
                    log(f"peer cleanup: {e}")


def run(cl, a):
    dbs = cl.test_dbs()
    if not dbs:
        raise TbError("no test databases (run 'tb.py dbs prepare')")
    res = Results("rcmtable", vars(a).copy())
    if not R.ready(cl):
        res.record("rcmtable", "SKIP", note="no RCM login in the local config (secrets.rcm_user / rcm_password)")
        return res.finish()
    cl.load_stop()
    want = [c.strip() for c in a.cases.split(",") if c.strip()]
    for c in CASES:
        if c not in want:
            continue
        log(f"=== rcmtable: {c}")
        try:
            globals()[f"c_{c}"](cl, res, dbs)
        except TbError as e:
            res.record(f"{c}: error", "FAIL", note=str(e)[:500])
    for d in dbs:
        converge_and_record(cl, res, f"converge {d['path']}", [d["path"]], a.catchup_timeout)
    return res.finish()
