"""Helpers shared by the tests."""
import time

from tblib.cluster import TbError, log


def norm(p):
    return (p or "").replace("\\", "/").lower()


def replica_record(cl, replica, master_path):
    """The replica's own record of its copy of master_path, or None."""
    want = norm(cl.replica_path(replica, master_path))
    for d in cl.databases(replica):
        if norm(d.get("path")) == want:
            return d
    return None


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
    return ok
