"""Helpers shared by the tests."""
import time

from tblib.cluster import TbError, log


def norm(p):
    return (p or "").replace("\\", "/").lower()


def replica_records(cl, replica, master_path):
    """Every record the replica holds for its copy of master_path."""
    want = norm(cl.replica_path(replica, master_path))
    return [d for d in cl.databases(replica) if norm(d.get("path")) == want]


def replica_record(cl, replica, master_path):
    """The replica's own record of its copy of master_path, or None. There is
    one per file (hqcluster-node N-12; converge_and_record checks it)."""
    recs = replica_records(cl, replica, master_path)
    return recs[0] if recs else None


def duplicate_records(cl, paths, replicas=None):
    """{replica: {path: [db_id, ...]}} for every file a replica holds more
    than one record of (hqcluster-node N-12)."""
    bad = {}
    for p in paths:
        for r in replicas or cl.cfg.replicas:
            recs = replica_records(cl, r, p)
            if len(recs) > 1:
                bad.setdefault(r, {})[p] = [d.get("db_id") for d in recs]
    return bad


def replica_generation(cl, replica, master_path):
    rec = replica_record(cl, replica, master_path)
    return int(rec.get("generation") or 0) if rec else 0


def wait_generation_above(cl, replica, master_path, gen0, timeout=180):
    end = time.time() + timeout
    g = gen0
    while time.time() < end:
        g = replica_generation(cl, replica, master_path)
        if g > gen0:
            return g
        time.sleep(5)
    return g


def delta_left(cl, master_path):
    files = cl.hostctl(cl.cfg.master, "files", {"glob": master_path + ".delta"}) or []
    return [f["path"] for f in files]


def limbo_count(cl, master_path):
    r = cl.hostctl(cl.cfg.master, "limbo", {"db": master_path}, check=False) or {}
    return r.get("limbo")


def pick_replicas(cl, spec):
    if not spec or spec == "all":
        return list(cl.cfg.replicas)
    out = []
    for r in spec.split(","):
        if r not in cl.cfg.replicas:
            raise TbError(f"'{r}' is not a replica (replicas: {', '.join(cl.cfg.replicas)})")
        out.append(r)
    return out


def ensure_load(cl, dbs, args, tag):
    st = cl.module(cl.cfg.master, "50-load", "status", {"tag": tag}, check=False)[1] or {}
    if st.get("running", 0) > 0 and st.get("running") == st.get("total"):
        return
    cl.load_stop(tag)
    cl.load_start([d["path"] for d in dbs], mode=args.load_mode, tx=args.tx, conns=args.conns, tag=tag)


def add_load_args(p, tx="emul-safe"):
    p.add_argument("--load-mode", default="write", choices=["write", "read", "mixed", "spike", "oltp-emul"])
    p.add_argument("--tx", default=tx, choices=["off", "emul-safe", "full"])
    p.add_argument("--conns", default="2:5")
    p.add_argument("--no-load", action="store_true", help="run without fb-loadgen load")


def converge_and_record(cl, res, case, paths, timeout, replicas=None):
    log(f"{case}: waiting until the replicas match the master (up to {timeout}s)")
    ok, reports = cl.wait_converged(paths, timeout=timeout, replicas=replicas)
    note = "rows match" if ok else "row counts differ"
    res.record(case, "PASS" if ok else "FAIL", note=note, reports=reports)
    if ok:
        bad = stale_active(cl, paths, replicas)
        summary = {r: {p: {"count": len(a), "oldest_segment": min(t["seq"] for t in a),
                           "sample": a[:5]} for p, a in v.items()} for r, v in bad.items()}
        note = ("active transactions left after the load: " + ", ".join(
            f"{r} {len(a)}" for r, v in bad.items() for a in v.values())) if bad else "no active transactions left"
        res.record(f"{case}: replica control files", "FAIL" if bad else "PASS", note=note, stale=summary)
        dup = duplicate_records(cl, paths, replicas)
        note = ("more than one record: " + ", ".join(
            f"{r} {ids}" for r, v in dup.items() for ids in v.values())) if dup else "one record per file"
        res.record(f"{case}: replica records", "FAIL" if dup else "PASS", note=note, duplicates=dup)
        ok = not bad and not dup
    return ok


def replica_control(cl, replica, master_path):
    """Firebird's control files in the replica's journal source folder."""
    rec = replica_record(cl, replica, master_path) or {}
    rp = rec.get("path") or cl.replica_path(replica, master_path)
    mailbox = rec.get("incoming") or rp + ".Incoming"
    return cl.hostctl(replica, "replctl", {"dir": mailbox}, check=False) or []


def stale_active(cl, paths, replicas=None, timeout=90):
    """With the load stopped and the rows equal, no replica control file may
    still hold a transaction as active. One that does keeps the replica's OAT
    and every segment after it, and is replayed again at the next Firebird
    restart (hqcluster-node N-14). Returns {replica: {path: [entries]}}."""
    end = time.time() + timeout
    while True:
        bad = {}
        for p in paths:
            for r in replicas or cl.cfg.replicas:
                act = [dict(t, control_sequence=c.get("sequence"))
                       for c in replica_control(cl, r, p) for t in c.get("active") or []]
                if act:
                    bad.setdefault(r, {})[p] = act
        if not bad or time.time() >= end:
            return bad
        time.sleep(10)
