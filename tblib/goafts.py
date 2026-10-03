"""A stand that goafts manages: one goafts cluster (managed PKI) holds the
test bed hosts, and each host's fbagent writes the member's key,
certificates and node.json / rcm.json and installs hqclusternode /
hqbirdrcm from its channel - the chain a customer gets.

Config (local file):

  goafts.url / pin             bootstrap URL and SPKI pin (enrollment)
  goafts.admin                 the admin API (curl or p5ctl, tblib.ops.admin_call)
  goafts.channel               the products' channel on the agents (default stable)
  goafts.cluster.cluster_id    a lower-case DNS label, e.g. tbga30
  goafts.cluster.replconf_valid_till   YYYY-MM-DD for HQbird 2.5/3.0 (optional)
  hosts.<n>.node_id            the member id of the host's node
  hosts.<n>.paths.node         the member's install_dir: /opt/hqclusternode/<role>
  hosts.<n>.paths.rcm          /opt/hqbirdrcm

The test bed then reaches the nodes as usual: hostctl node-api with the
node's own node.json and certificates (Cluster.api), so 'dbs prepare' and
the tests run on such a stand too."""
import os
import time

from . import ops
from .cluster import LEGACY_ENGINES, TbError, log
from .remote import RemoteError

BASES = "/opt/hqclusternode,/opt/hqbirdrcm"
RCM_INGEST_PORT = 7443
RCM_OPERATOR_ADDR = "127.0.0.1:7444"


def conf(cl):
    c = (cl.cfg.goafts or {}).get("cluster") or {}
    if not c.get("cluster_id"):
        raise TbError("goafts.cluster.cluster_id is not set in the config")
    return c


def cluster_id(cl):
    return conf(cl)["cluster_id"]


def admin(cl, method, path, body=None, headers=None, ok=(200, 201, 204)):
    r = ops.admin_call(cl, method, path, body, headers)
    if r is None:
        raise TbError("goafts.admin is not configured (url + client cert, or p5ctl)")
    if ok and r[0] not in ok:
        raise TbError(f"goafts {method} {path}: HTTP {r[0]} {str(r[1])[:400]}")
    return r


def rcm_member_id(cl):
    return f"{cluster_id(cl)}-rcm"


def legacy_stand(cl):
    return cl.fb_engine(cl.cfg.master) in LEGACY_ENGINES


def member_doc(cl, n, agent_id):
    h, cfg = cl.h(n), cl.cfg
    role = "master" if n == cfg.master else "replica"
    w = cfg.windows
    limits = {"channel_a_mbps": 50, "channel_a_burst_mbps": 200, "reinit_window_mbps": 100,
              "reinit_day_disk_fraction": 0.5, "free_space_floor_gb": 1, "backlog_warn": 32,
              "backlog_critical": 48, "retention_days": 2}
    limits.update(cfg.limits)
    init = {"databases": {"root": h["paths"]["db_root"], "recursive": True, "template": "*.fdb"},
            "windows": {"restart_window": w["master_restart_window"] if role == "master" else w["replica_restart_window"],
                        "transfer_window": w["transfer_window"]},
            "limits": limits}
    return {"member_id": h["node_id"], "agent_id": agent_id, "role": role, "install_dir": h["paths"]["node"],
            "addresses": [h["addr"]], "listen_port": int(h["node_port"]), "initial_config": init}


def cluster_doc(cl, agents):
    """The cluster for POST /v1/admin/clusters: a node per node host, links
    master -> each replica, the RCM member on the RCM host."""
    cfg, c = cl.cfg, conf(cl)
    cid = c["cluster_id"]
    members = [member_doc(cl, n, agents[n]) for n in cfg.node_hosts()]
    if cfg.rcm_enabled:
        r = cl.h(cfg.rcm_host)
        rdir = r["paths"]["rcm"]
        members.append({"member_id": rcm_member_id(cl), "agent_id": agents[cfg.rcm_host], "role": "rcm",
                        "install_dir": rdir, "addresses": [r["addr"]], "listen_port": RCM_INGEST_PORT,
                        "initial_config": {"operator_addr": RCM_OPERATOR_ADDR, "data_dir": rdir + "/rcm-data",
                                           "users_file": rdir + "/rcm-data/users.json",
                                           "poll_interval_sec": 15, "stale_after_sec": 45}})
    doc = {"contract": 1, "cluster_id": cid, "name": f"replication test bed {cid}",
           "notes": "replication-testbed tb.py goafts up; removed by tb.py goafts down.",
           "pki": "managed", "cert_validity_days": 90, "renew_before_days": 30, "verify_peer_name": False,
           "members": members,
           "links": [{"from": cl.h(cfg.master)["node_id"], "to": cl.h(r)["node_id"]} for r in cfg.replicas]}
    if c.get("replconf_valid_till") and legacy_stand(cl):
        doc["replconf_valid_till"] = c["replconf_valid_till"]
    return doc


def get(cl):
    """The cluster, or None when goafts has none of this id."""
    st, doc = admin(cl, "GET", f"/v1/admin/clusters/{cluster_id(cl)}", ok=None)
    if st == 404:
        return None
    if st != 200 or not isinstance(doc, dict):
        raise TbError(f"goafts GET cluster {cluster_id(cl)}: HTTP {st} {str(doc)[:300]}")
    return doc


def update(cl, change, what):
    """GET the cluster, change(doc), PUT it with If-Match: revision."""
    for _ in range(3):
        doc = get(cl)
        if doc is None:
            raise TbError(f"goafts has no cluster {cluster_id(cl)}")
        rev = doc.get("revision")
        change(doc)
        st, body = admin(cl, "PUT", f"/v1/admin/clusters/{cluster_id(cl)}", doc,
                         headers={"If-Match": str(rev)}, ok=None)
        if st in (200, 204):
            log(f"goafts: cluster {cluster_id(cl)}: {what}")
            return body
        if st not in (409, 412):
            raise TbError(f"goafts PUT cluster {cluster_id(cl)} ({what}): HTTP {st} {str(body)[:400]}")
        time.sleep(2)
    raise TbError(f"goafts PUT cluster {cluster_id(cl)} ({what}): the revision kept changing")


def set_date(cl, value):
    """replconf_valid_till of the cluster: a date, or None to drop it."""
    def change(doc):
        if value:
            doc["replconf_valid_till"] = value
        else:
            doc.pop("replconf_valid_till", None)
    return update(cl, change, f"replconf_valid_till {value or '(none)'}")


def agent_id(cl, n):
    _, res, _, _ = cl.module(n, "20-goafts", "agent-id", {"fbagent_dir": cl.base_args(n)["fbagent_dir"]}, check=False)
    aid = (res or {}).get("agent_id") or ""
    if aid:
        cl.hstate(n)["goafts_agent_id"] = aid
    return aid


def set_channel(cl, names, product_channel="", self_update=""):
    for n in names:
        a = cl.base_args(n)
        a.update({"product_channel": product_channel, "self_update": self_update})
        cl.module(n, "20-goafts", "channel", a)


# ---------------------------------------------------------------------- up --
def up(cl, timeout=1200):
    """Enroll an fbagent from goafts on every host that has none, put the
    hosts in the cluster, wait for the nodes and the RCM the agents install."""
    cfg = cl.cfg
    cfg.validate(("goafts",))
    names = ops.install_order(cl, cfg.select("all"))
    agents = {n: agent_id(cl, n) for n in names}
    new = [n for n in names if not agents[n]]
    if new:
        install_agents(cl, new)
        agents.update({n: agent_id(cl, n) for n in new})
    for n in names:
        ops.detect(cl, n)
    missing = [n for n in names if not agents[n]]
    if missing:
        raise TbError(f"no goafts agent id on {', '.join(missing)} after enrollment")
    ch = cfg.goafts.get("channel", "stable")
    for n in names:
        a = cl.base_args(n)
        a["bases"] = BASES
        cl.module(n, "20-goafts", "cluster-prep", a)
        st = cl.hstate(n)
        st["fbagent_port"] = cl.fbagent_port(n)
        st["fb_port"] = cl.fb_port(n)
        cl.save()
    set_channel(cl, names, product_channel=ch)

    doc = cluster_doc(cl, agents)
    if get(cl) is None:
        admin(cl, "POST", "/v1/admin/clusters", doc)
        log(f"goafts: cluster {cluster_id(cl)} created ({len(doc['members'])} members)")
    else:
        def change(cur):
            for k in ("members", "links", "replconf_valid_till", "name", "notes"):
                if k in doc:
                    cur[k] = doc[k]
        update(cl, change, "members and links set from the config")
    wait_members(cl, timeout)
    for n in names:
        comps = ["fbagent"] + (["node"] if n in cfg.node_hosts() else []) + \
                (["rcm"] if cfg.rcm_enabled and n == cfg.rcm_host else [])
        cl.hstate(n)["installed"] = {"source": "goafts-cluster", "components": comps}
    cl.save()
    log("goafts: the cluster is up")


INSTALLERS = {"2.5": "fbagent-fb25_known.sh", "3.0": "fbagent-fb30_known.sh",
              "4": "fbagent-fb40_known.sh", "5": "fbagent-fb50_known.sh"}


def install_agents(cl, names):
    """An fbagent from goafts on hosts that have none. With
    goafts.installer_dir (fbagent's ops/linux-install folder) the host gets
    the customer's install: fbagent's installer for its engine puts HQbird
    (without a replconf plugin) and the agent on a bare host. Without it:
    the agent is downloaded and enrolled on the Firebird already there
    ('hosts prepare')."""
    g = cl.cfg.goafts
    idir = g.get("installer_dir") or ""
    if not idir:
        for n in names:
            cl.module(n, "20-goafts", "download", {"url": g["url"], "pin": g["pin"],
                                                   "channel": g.get("channel", "stable"), "products": "fbagent"})
            ops.detect(cl, n)
        ops.enroll_goafts(cl, names)
        return
    remote = {}
    for n in names:
        try:
            ops.detect(cl, n)
        except (TbError, RemoteError):
            pass            # a bare host: detect still noted its name and addresses
        eng = cl.fb_engine(n).split(".")[0] if cl.fb_engine(n) not in INSTALLERS else cl.fb_engine(n)
        if eng not in INSTALLERS:
            raise TbError(f"[{n}] firebird.engine must be 2.5, 3.0, 4 or 5 for the fbagent installer")
        script = os.path.join(idir, INSTALLERS[eng])
        if not os.path.isfile(script):
            raise TbError(f"no {script} (goafts.installer_dir)")
        hst = cl.host(n)
        cl.ready(n)
        remote[n] = hst.join(hst.work, INSTALLERS[eng])
        hst.put(script, remote[n])

    def run(n):
        a = cl.base_args(n)
        a.update({"script": remote[n], "goafts": g.get("installer_goafts", "chess1"),
                  "channel": g.get("channel", "stable"), "enroll_timeout": g.get("enroll_timeout", "30m")})
        return cl.module(n, "20-goafts", "installer", a)[1]

    ops.enroll_with_approval(cl, names, run)


def node_up(cl, n):
    try:
        st, _ = cl.api(n, "GET", "/v1/status", check_status=False, timeout=15)
        return st == 200
    except (TbError, RemoteError):
        return False


def rcm_up(cl):
    n = cl.cfg.rcm_host
    path = cl.h(n)["paths"]["rcm"] + "/rcm.json"
    port = RCM_OPERATOR_ADDR.rsplit(":", 1)[1]
    rc, _, _ = cl.host(n).run_raw(["sh", "-c", f"test -f {path} && ss -ltn | grep -q ':{port} '"], check=False)
    return rc == 0


def wait_members(cl, timeout=1200):
    cfg = cl.cfg
    end, last = time.time() + timeout, 0
    waiting = list(cfg.node_hosts()) + (["rcm"] if cfg.rcm_enabled else [])
    while waiting and time.time() < end:
        waiting = [n for n in waiting if not (rcm_up(cl) if n == "rcm" else node_up(cl, n))]
        if waiting and time.time() - last > 60:
            log(f"goafts: waiting for the agents to install and start: {', '.join(waiting)}")
            last = time.time()
        if waiting:
            time.sleep(15)
    if waiting:
        raise TbError(f"goafts: not up after {timeout} s: {', '.join(waiting)} "
                      "(see the agent's journal: fbagent --cluster-status)")


# -------------------------------------------------------------------- down --
def down(cl):
    """Remove the members and the cluster from goafts, deregister the agents,
    uninstall node, RCM and fbagent from the hosts."""
    cfg = cl.cfg
    if get(cl) is not None:
        def empty(doc):
            doc["members"], doc["links"] = [], []
        update(cl, empty, "members removed")
        admin(cl, "DELETE", f"/v1/admin/clusters/{cluster_id(cl)}")
        log(f"goafts: cluster {cluster_id(cl)} deleted")
    names = list(reversed(ops.install_order(cl, cfg.select("all"))))
    left = []
    for n in names:
        aid = agent_id(cl, n) or cl.hstate(n).get("goafts_agent_id")
        args = cl.base_args(n)
        comps = ["fbagent"] + (["node"] if n in cfg.node_hosts() else []) + \
                (["rcm"] if cfg.rcm_enabled and n == cfg.rcm_host else [])
        args["components"] = ",".join(comps)
        if n == cfg.master:
            cl.load_stop()
        rc, res, _, _ = cl.module(n, "20-goafts", "uninstall", args, check=False)
        if aid:
            r = admin(cl, "DELETE", f"/v1/admin/agent-versions/{aid}", ok=None)
            log(f"goafts: deregister {aid} -> {r[0]}")
        if not ops.note_leftovers(n, rc, res, left):
            cl.hstate(n).pop("installed", None)
        cl.hstate(n).pop("goafts_agent_id", None)
        cl.save()
    ops.finish_removal("goafts down", left)
