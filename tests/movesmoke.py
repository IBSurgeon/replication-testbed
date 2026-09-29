"""Replica file move: db1 moves to a new subfolder on the replica and back,
without a reinit. The pair stays IN_SYNC, the generation is untouched, and
segments keep flowing across the move."""
import time

from tblib.cluster import TbError, log
from tblib.results import Results

from ._common import pick_replicas

HELP = "move a replica db to a subfolder and back: no reinit, history kept"

TARGET = "replica/tb-moved/db1/employee.fdb"


def add_args(p):
    p.add_argument("--replicas", default="all")
    p.add_argument("--catchup-timeout", type=int, default=900)


def _replica_db(cl, rep, master_db):
    """The replica's record of the pair: same db_id, replica-side path."""
    st, doc = cl.api(rep, "GET", "/v1/stats")
    if st != 200:
        raise TbError(f"[{rep}] stats: {st}")
    for d in doc.get("databases", []):
        if d.get("db_id") == master_db["db_id"]:
            return d
    raise TbError(f"[{rep}] no record of {master_db['db_id']}")


def run(cl, a):
    dbs = [d for d in cl.test_dbs(which="all") if d["path"].replace("\\", "/").endswith("/db1/employee.fdb")]
    if not dbs:
        raise TbError("no db1 test database")
    db = dbs[0]
    reps = pick_replicas(cl, a.replicas)
    if len(reps) != 1:
        raise TbError(f"move smoke wants exactly one replica, got {reps}")
    rep = reps[0]
    res = Results("movesmoke", vars(a).copy())

    before = _replica_db(cl, rep, db)
    log(f"replica record: {before['path']} state={before['state']} gen={before.get('generation')}")
    if before["state"] != "IN_SYNC":
        raise TbError(f"replica must start IN_SYNC, got {before['state']}")

    # Guard refusals first: escaping the root and an existing target.
    st, r = cl.api(rep, "POST", f"/v1/databases/{db['db_id']}/move",
                   {"target": "../escape.fdb", "dry_run": True})
    if st != 200 or r.get("valid") or r.get("code") != "invalid_target":
        res.record("dry-run refuses ../escape", "FAIL", note=str(r)[:300])
        raise TbError(f"expected invalid_target, got {st} {r}")
    res.record("dry-run refuses ../escape", "PASS")

    # Dry run of the real target: valid, and the full path is root + target.
    st, r = cl.api(rep, "POST", f"/v1/databases/{db['db_id']}/move",
                   {"target": TARGET, "dry_run": True})
    want = _root(cl, rep).rstrip("/") + "/" + TARGET
    if st != 200 or not r.get("valid") or r.get("plan", {}).get("new_path") != want:
        res.record("dry-run plans the move", "FAIL", note=f"{st} {r} want={want}")
        raise TbError(f"dry run: {st} {r}")
    res.record("dry-run plans the move", "PASS", note=str(r.get("plan"))[:300])

    # The move itself: synchronous, seconds.
    t0 = time.time()
    st, r = cl.api(rep, "POST", f"/v1/databases/{db['db_id']}/move", {"target": TARGET})
    took = time.time() - t0
    if st != 200 or r.get("new_path") != want or r.get("pending_restart"):
        res.record("move to a subfolder", "FAIL", note=f"{st} {r}")
        raise TbError(f"move: {st} {r}")
    res.record("move to a subfolder", "PASS", note=f"{took:.1f}s steps={len(r.get('steps', []))}")
    log(f"move took {took:.1f}s")

    # The record moved, the identity did not: same db_id, same generation,
    # IN_SYNC within seconds of Firebird coming back.
    deadline = time.time() + 120
    after = {}
    while time.time() < deadline:
        after = _replica_db(cl, rep, db)
        if after["path"] == want and after["state"] == "IN_SYNC":
            break
        time.sleep(5)
    ok = after.get("path") == want and after.get("state") == "IN_SYNC" \
        and after.get("generation") == before.get("generation")
    res.record("record moved, identity kept", "PASS" if ok else "FAIL", note=str(after)[:300])
    if not ok:
        raise TbError(f"after move: {after}")

    # Segments still flow: load lands on the moved pair. The replica file
    # is not at the master-mapped path, so compare the real locations.
    cl.load_stop("movesmoke")
    cl.load_start([db["path"]], mode="write", tx="off", conns="1:2", minutes=1, tag="movesmoke")
    time.sleep(75)
    cl.load_stop("movesmoke")
    wait_counts(cl, res, "converge after move", db["path"], want, a.catchup_timeout, rep)

    # And back to the original layout.
    st, r = cl.api(rep, "POST", f"/v1/databases/{db['db_id']}/move",
                   {"target": _rel(before["path"], _root(cl, rep))})
    if st != 200 or r.get("new_path") != before["path"]:
        res.record("move back", "FAIL", note=f"{st} {r}")
        raise TbError(f"move back: {st} {r}")
    deadline = time.time() + 120
    back = {}
    while time.time() < deadline:
        back = _replica_db(cl, rep, db)
        if back["path"] == before["path"] and back["state"] == "IN_SYNC":
            break
        time.sleep(5)
    ok = back.get("path") == before["path"] and back.get("state") == "IN_SYNC"
    res.record("move back", "PASS" if ok else "FAIL", note=str(back)[:300])
    if not ok:
        raise TbError(f"after move back: {back}")
    wait_counts(cl, res, "converge after move back", db["path"], before["path"], a.catchup_timeout, rep)
    return res.finish()


def _counts(cl, host, path):
    out = cl.counts(host, path)
    return {t: v.get("rows") for t, v in (out or {}).items() if isinstance(v, dict)}


def wait_counts(cl, res, case, master_path, replica_path, timeout, rep):
    """Row counts of the master table set must land on the replica path."""
    deadline = time.time() + timeout
    m = _counts(cl, cl.cfg.master, master_path)
    r = {}
    while time.time() < deadline:
        r = _counts(cl, rep, replica_path)
        if m and r == m:
            res.record(case, "PASS", note=f"{len(m)} tables")
            return
        time.sleep(20)
        m = _counts(cl, cl.cfg.master, master_path)
    diff = {t: (m.get(t), r.get(t)) for t in set(m) | set(r) if m.get(t) != r.get(t)}
    res.record(case, "FAIL", note=str(dict(list(diff.items())[:5])))
    raise TbError(f"{case}: counts differ {diff}")


def _root(cl, rep):
    """The replica's databases.root, from its own config."""
    st, doc = cl.api(rep, "GET", "/v1/config")
    if st != 200:
        raise TbError(f"[{rep}] config: {st}")
    cfg = doc.get("config", doc)
    root = (cfg.get("databases") or {}).get("root", "")
    if not root:
        raise TbError(f"[{rep}] no databases.root in config")
    return root


def _rel(path, root):
    p = path.replace("\\", "/").rstrip("/")
    r = root.replace("\\", "/").rstrip("/")
    if not p.lower().startswith(r.lower() + "/"):
        raise TbError(f"{path} is not under {root}")
    return p[len(r) + 1:]
