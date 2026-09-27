"""Failure scenarios under load. After each one the load stops and every
replica must catch up and match the master.

  replica-node-stop   the replica node service is stopped, then started
  replica-node-kill   the replica node process is killed (the service restarts it)
  master-node-stop    the master node service is stopped, then started
  both-nodes-stop     master and replica node services stopped together
  replica-fb-stop     Firebird on the replica is stopped: segments are acked
                      but not applied while it is down
  master-fb-restart   Firebird on the master is restarted through the node API
  partition           the master cannot reach the replica node port (host firewall), then can

Every scenario restores the host in a finally block (starts what it stopped,
removes the firewall rules), even when a check fails.
"""
import time

from tblib.cluster import TbError, log
from tblib.results import Results

from ._common import add_load_args, converge_and_record, ensure_load, pick_replicas

HELP = "failure scenarios under load (node stop/kill, Firebird stop, network partition)"

SCENARIOS = ["replica-node-stop", "replica-node-kill", "master-node-stop", "both-nodes-stop",
             "replica-fb-stop", "master-fb-restart", "partition"]


def add_args(p):
    p.add_argument("--only", default="all", help="all | comma list of: " + ",".join(SCENARIOS))
    p.add_argument("--db", default="all")
    p.add_argument("--replica", default=None, help="replica to hit (default: the first one)")
    p.add_argument("--down", type=int, default=60, help="seconds a component stays down")
    p.add_argument("--warm", type=int, default=30, help="seconds of load before each scenario")
    p.add_argument("--catchup-timeout", type=int, default=900)
    add_load_args(p)


def node_svc(cl, host, action):
    return cl.hostctl(host, "node-svc", {"action": action})


def fb_svc(cl, host, action):
    return cl.hostctl(host, "fb-svc", {"action": action, "fb_service": cl.fb_service(host)})


def wait_node(cl, host, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        try:
            cl.api(host, "GET", "/v1/health", timeout=10)
            return True
        except Exception:  # noqa: BLE001 - down is expected here
            time.sleep(3)
    return False


def acked_not_applied(cl, peer_node_id):
    return [r for r in cl.transfer()
            if r["peer_id"] == peer_node_id and r["last_acked"] > r["last_applied"]]


def scenario(cl, res, name, rep, a):
    m = cl.cfg.master
    rid = cl.h(rep)["node_id"]
    if name == "replica-node-stop":
        try:
            node_svc(cl, rep, "stop")
            time.sleep(a.down)
        finally:
            node_svc(cl, rep, "start")
        res.record(f"{name}: node back", "PASS" if wait_node(cl, rep) else "FAIL")
    elif name == "replica-node-kill":
        node_svc(cl, rep, "kill")
        time.sleep(5)
        back = wait_node(cl, rep, 180)
        if not back:
            node_svc(cl, rep, "start")
        res.record(f"{name}: service restarted the node", "PASS" if back else "FAIL")
    elif name == "master-node-stop":
        try:
            node_svc(cl, m, "stop")
            time.sleep(a.down)
        finally:
            node_svc(cl, m, "start")
        res.record(f"{name}: node back", "PASS" if wait_node(cl, m) else "FAIL")
    elif name == "both-nodes-stop":
        try:
            node_svc(cl, m, "stop")
            node_svc(cl, rep, "stop")
            time.sleep(a.down)
        finally:
            node_svc(cl, rep, "start")
            node_svc(cl, m, "start")
        ok = wait_node(cl, m) and wait_node(cl, rep)
        res.record(f"{name}: nodes back", "PASS" if ok else "FAIL")
    elif name == "replica-fb-stop":
        try:
            fb_svc(cl, rep, "stop")
            time.sleep(a.down)
            lag = acked_not_applied(cl, rid)
            res.record(f"{name}: acked but not applied while down",
                       "PASS" if lag else "FAIL",
                       note=f"{len(lag)} database(s) with last_acked > last_applied")
        finally:
            fb_svc(cl, rep, "start")
    elif name == "master-fb-restart":
        st, body = cl.api(m, "POST", "/v1/firebird/restart",
                          {"reason": "test bed: disaster master-fb-restart", "ignore_window": True},
                          check_status=False)
        res.record(f"{name}: restart accepted", "PASS" if st in (200, 202) else "FAIL", note=f"HTTP {st}")
        time.sleep(10)
        res.record(f"{name}: node answers after restart", "PASS" if wait_node(cl, m) else "FAIL")
    elif name == "partition":
        # Only the replica's node port is blocked for the master address, so
        # ssh to the host keeps working even when it goes through that address.
        fw = {"addr": cl.h(m)["addr"], "port": cl.h(rep)["node_port"]}
        try:
            cl.hostctl(rep, "block-peer", fw)
            time.sleep(a.down)
        finally:
            cl.hostctl(rep, "unblock-peer", fw, check=False)
        res.record(f"{name}: firewall rules removed", "PASS")
    else:
        raise TbError(f"unknown scenario {name}")


def run(cl, a):
    dbs = cl.test_dbs(which=a.db)
    if not dbs:
        raise TbError("no test databases (run 'tb.py dbs prepare')")
    rep = a.replica or cl.cfg.replicas[0]
    pick_replicas(cl, rep)
    names = SCENARIOS if a.only == "all" else [s.strip() for s in a.only.split(",")]
    for n in names:
        if n not in SCENARIOS:
            raise TbError(f"unknown scenario '{n}' (known: {', '.join(SCENARIOS)})")
    res = Results("disasters", vars(a).copy())
    tag = "disaster"
    paths = [d["path"] for d in dbs]
    for n in names:
        log(f"=== scenario {n} (replica {rep})")
        try:
            if not a.no_load:
                ensure_load(cl, dbs, a, tag)
                time.sleep(a.warm)
            scenario(cl, res, n, rep, a)
            if not a.no_load:
                time.sleep(a.warm)
        except Exception as e:  # noqa: BLE001 - one scenario must not stop the others
            res.record(f"{n}: run", "FAIL", note=str(e)[:300])
        finally:
            if not a.no_load:
                cl.load_stop(tag)
        converge_and_record(cl, res, f"{n}: converge", paths, a.catchup_timeout)
    return res.finish()
