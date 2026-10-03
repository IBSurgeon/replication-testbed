"""A replica's databases root: checked and created (hqcluster-node 2027.4.4, plan N1).

Nothing creates the folder a replica places its databases in (the test bed's
own install does, which is why a missing root never showed here), and a
reinit to a replica without it was refused. The node now names the fix in
GET /v1/permissions and creates the folder with POST
/v1/databases/root/prepare.

Steps, on one replica, with a folder of the test's own (the replica's real
root is left alone):
  1. GET /v1/permissions?root=<missing>: databases_root_exists fails and
     names the route and the command;
  2. POST /v1/databases/root/prepare: created, mode 2770, Firebird's group;
     the report passes for that root;
  3. a second call: created false;
  4. a relative path: 400.
The folder is removed at the end.
"""
import time

from tblib.cluster import TbError
from tblib.results import Results

from ._common import pick_replicas

HELP = "a replica's databases root: permissions hint and prepare (node 2027.4.4)"


def add_args(p):
    p.add_argument("--replicas", default="", help="one replica; default: the first")


def finding(rep, check):
    for f in (rep or {}).get("findings", []):
        if f.get("check") == check:
            return f
    return {}


def run(cl, a):
    res = Results("reinitroot", vars(a).copy())
    reps = pick_replicas(cl, a.replicas or "all")
    r = reps[0]
    if cl.h(r)["os"] != "linux":
        res.record("host", "SKIP", note="Linux only (stat, mode 2770)")
        return res.finish()
    root = f"/databases/tb-prep-{int(time.time())}"
    try:
        st, rep = cl.api(r, "GET", f"/v1/permissions?root={root}", check_status=False)
        f = finding(rep, "databases_root_exists")
        msg = f.get("message", "")
        ok = st == 200 and not f.get("ok") and "root/prepare" in msg and "install -d" in msg
        res.record("a missing root names the fix", "PASS" if ok else "FAIL", note=msg[:300])

        st, out = cl.api(r, "POST", "/v1/databases/root/prepare", {"root": root}, check_status=False)
        ok = st == 200 and out.get("created") and finding(out.get("report"), "databases_root_exists").get("ok")
        res.record("prepare creates it", "PASS" if ok else "FAIL", note=f"HTTP {st} via={(out or {}).get('via')}")
        s = cl.hostctl(r, "stat", {"path": root}) or {}
        ok = s.get("exists") and s.get("mode") == "2770" and s.get("group") == "firebird"
        res.record("mode 2770, group firebird", "PASS" if ok else "FAIL",
                   note=f"{s.get('owner')}:{s.get('group')} {s.get('mode')}")

        st, out = cl.api(r, "POST", "/v1/databases/root/prepare", {"root": root}, check_status=False)
        res.record("a second call leaves it", "PASS" if st == 200 and out.get("created") is False else "FAIL",
                   note=f"HTTP {st} {out}")

        st, out = cl.api(r, "POST", "/v1/databases/root/prepare", {"root": "databases/x"}, check_status=False)
        res.record("a relative path is refused", "PASS" if st == 400 else "FAIL", note=f"HTTP {st}")
    except TbError as e:
        res.record("reinitroot", "FAIL", note=str(e)[:300])
    finally:
        cl.hostctl(r, "remove-dir", {"path": root}, check=False)
    return res.finish()
