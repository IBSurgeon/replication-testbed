"""A stand whose cluster RCM serves, without goafts (fbagent contract §7,
"source rcm"; hqcluster-node user guide 1.3): the customer's path when a
site has no HQCenter.

  1. each host has an fbagent with an agent id and no goafts
     ('tb.py install --source local --components fbagent')
  2. the RCM build goes into the agent's staging folder on the RCM host,
     as the HQbird setup payload leaves it
  3. fbagent --cluster-init on the RCM host: the CAs, RCM as user hqbirdrcm
  4. a join token per other host, fbagent --cluster-join there
  5. signed releases imported into RCM's mirror
  6. the members and links stored in RCM (hqbirdrcm cluster apply); the
     agents install the nodes from the mirror

Config (local file):

  rcmcluster.cluster_id     a lower-case DNS label, e.g. tbnogo
  rcmcluster.rcm_binary     the RCM build to stage (default: the artifacts'
                            linux hqbirdrcm)
  rcmcluster.releases       [{"product", "version", "file", "sig"}]: signed
                            Linux releases for the mirror; "sig" is the
                            vendor signature file of "file"
  hosts.<n>.node_id         the member id of the host's node
  hosts.<n>.paths.node      the member's install_dir: /opt/hqclusternode/<role>

The test bed then reaches the nodes as on a goafts stand (Cluster.api with
the node's own node.json and certificates)."""
import base64
import json
import os
import re
import tempfile
import time

from . import goafts as GA
from . import ops
from .cluster import TbError, log
from .remote import RemoteError

MODULE = "94-rcmcluster"


def conf(cl):
    c = cl.cfg.raw.get("rcmcluster") or {}
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", c.get("cluster_id") or ""):
        raise TbError("rcmcluster.cluster_id must be a lower-case DNS label (config)")
    return c


def cluster_id(cl):
    return conf(cl)["cluster_id"]


def sstate(cl):
    """Run state of the stand: rcm_url, ca_hash."""
    return cl.state.setdefault("rcmcluster", {})


def _b64(v):
    return base64.b64encode(v.encode()).decode()


def rcm_cli(cl, *args, check=True, timeout=None):
    """hqbirdrcm <args> on the RCM host. Returns (rc, output)."""
    _, res, _, out = cl.module(cl.cfg.rcm_host, MODULE, "rcm",
                               {"args_b64": _b64(json.dumps(list(args))),
                                "rcm_dir": cl.h(cl.cfg.rcm_host)["paths"]["rcm"]}, check=False, timeout=timeout)
    rc, text = (res or {}).get("rc", 1), (res or {}).get("out", out or "")
    if check and rc != 0:
        raise TbError(f"hqbirdrcm {' '.join(args)}: rc {rc}: {text[-600:]}")
    return rc, text


def rcm_json(cl, *args):
    """hqbirdrcm <args> that prints JSON: the parsed document."""
    _, text = rcm_cli(cl, *args)
    start = text.find("{")
    if start < 0:
        raise TbError(f"hqbirdrcm {' '.join(args)}: no JSON in: {text[-300:]}")
    return json.loads(text[start:])


def agents(cl):
    return {a["agent_id"]: a for a in rcm_json(cl, "cluster", "agents").get("agents") or []}


def join_token(cl, note):
    """A join token: (id, token)."""
    _, text = rcm_cli(cl, "cluster", "join-token", "-note", note)
    tid = re.search(r"Join token (\S+)", text)
    tok = re.search(r"--token (\S+)", text)
    if not (tid and tok):
        raise TbError(f"join-token: no token in the output: {text[-300:]}")
    return tid.group(1), tok.group(1)


def join(cl, n, token, url=None, ca_hash=None, agent_dir=None):
    """fbagent --cluster-join on host n. Returns (rc, output)."""
    st = sstate(cl)
    args = {"url": url or st["rcm_url"], "ca_hash": ca_hash or st["ca_hash"], "token_b64": _b64(token),
            "fbagent_dir": agent_dir or cl.base_args(n)["fbagent_dir"]}
    _, res, _, out = cl.module(n, MODULE, "join", args, check=False)
    return (res or {}).get("rc", 1), (res or {}).get("out", out or "")


def facts(cl, n):
    a = cl.base_args(n)
    dirs = [cl.h(n)["paths"]["node"]] if n in cl.cfg.node_hosts() else []
    _, res, _, _ = cl.module(n, MODULE, "facts", {"fbagent_dir": a["fbagent_dir"], "rcm_dir": a["rcm_dir"],
                                                   "node_dirs": ",".join(dirs)}, check=False)
    return res or {}


def put_file(cl, n, local):
    hst = cl.host(n)
    cl.ready(n)
    remote = hst.join(hst.work, "rcmstand", os.path.basename(local))
    hst.mkdir(hst.join(hst.work, "rcmstand"))
    hst.put(local, remote)
    return remote


def import_release(cl, rel):
    """One release of rcmcluster.releases into RCM's mirror. Returns (rc, output)."""
    n = cl.cfg.rcm_host
    f = put_file(cl, n, rel["file"])
    sig = put_file(cl, n, rel["sig"]) if rel.get("sig") else ""
    _, res, _, out = cl.module(n, MODULE, "release-import", {
        "file": f, "sig": sig, "product": rel["product"], "version": rel["version"],
        "platform": rel.get("platform", "linux-amd64"), "rcm_dir": cl.h(n)["paths"]["rcm"]}, check=False)
    return (res or {}).get("rc", 1), (res or {}).get("out", out or "")


def cluster_doc(cl, agent_ids):
    """The document RCM has, with a node member per node host and links
    master -> each replica (the RCM member of --cluster-init is kept)."""
    cfg = cl.cfg
    doc = rcm_json(cl, "cluster", "show")
    keep = [m for m in doc.get("members") or [] if m.get("role") == "rcm"]
    doc["members"] = keep + [GA.member_doc(cl, n, agent_ids[n]) for n in cfg.node_hosts()]
    doc["links"] = [{"from": cl.h(cfg.master)["node_id"], "to": cl.h(r)["node_id"]} for r in cfg.replicas]
    doc["name"] = f"replication test bed {cluster_id(cl)}"
    doc["notes"] = "replication-testbed tb.py rcm up; removed by tb.py rcm down."
    return doc


def apply(cl, doc):
    """hqbirdrcm cluster apply -f <doc>. Returns (rc, output)."""
    fd, tmp = tempfile.mkstemp(prefix="tb-cluster-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
        remote = put_file(cl, cl.cfg.rcm_host, tmp)
    finally:
        os.remove(tmp)
    return rcm_cli(cl, "cluster", "apply", "-f", remote, check=False)


def rcm_binary(cl):
    c = conf(cl)
    if c.get("rcm_binary"):
        return c["rcm_binary"]
    a = cl.cfg.artifacts
    return os.path.join(a.get("dir", ""), (a.get("linux") or {}).get("hqbirdrcm", "hqbirdrcm-linux-amd64"))


# ---------------------------------------------------------------------- up --
def up(cl, timeout=1200):
    cfg = cl.cfg
    c = conf(cl)
    names = ops.install_order(cl, cfg.select("all"))
    ids = {n: GA.agent_id(cl, n) for n in names}
    missing = [n for n in names if not ids[n]]
    if missing:
        raise TbError(f"no fbagent with an agent id on {', '.join(missing)}: "
                      "run 'tb.py install --source local --components fbagent' first")
    for n in names:
        ops.detect(cl, n)
        a = cl.base_args(n)
        a["bases"] = GA.BASES
        cl.module(n, "20-goafts", "cluster-prep", a)
        st = cl.hstate(n)
        st["fbagent_port"] = cl.fbagent_port(n)
        st["fb_port"] = cl.fb_port(n)
    cl.save()

    r = cfg.rcm_host
    st = sstate(cl)
    binary = rcm_binary(cl)
    if not os.path.isfile(binary):
        raise TbError(f"no RCM build {binary} (rcmcluster.rcm_binary or artifacts)")
    _, res, _, _ = cl.module(r, MODULE, "stage-rcm", {"binary": put_file(cl, r, binary),
                                                       "fbagent_dir": cl.base_args(r)["fbagent_dir"]})
    log(f"rcm: staged hqbirdrcm {(res or {}).get('version')} on {r}")
    _, res, _, out = cl.module(r, MODULE, "init", {"cluster_id": c["cluster_id"], "address": cl.h(r)["addr"],
                                                    "fbagent_dir": cl.base_args(r)["fbagent_dir"]}, check=False)
    if not res or res.get("rc") != 0 or not res.get("ca_hash"):
        raise TbError(f"fbagent --cluster-init failed: {(res or {}).get('out', out)[-800:]}")
    st.update({"rcm_url": res["rcm_url"], "ca_hash": res["ca_hash"], "cluster_id": c["cluster_id"]})
    cl.save()
    log(f"rcm: cluster {c['cluster_id']} made; RCM URL {res['rcm_url']}")
    wait(lambda: GA.rcm_up(cl), 120, "RCM operator port")

    for rel in c.get("releases") or []:
        rc, text = import_release(cl, rel)
        if rc != 0:
            raise TbError(f"release import {rel['product']} {rel['version']}: {text[-600:]}")
        log(f"rcm: imported {rel['product']} {rel['version']}")

    for n in names:
        if n == r:
            continue
        if ids[n] in agents(cl):
            log(f"rcm: [{n}] agent {ids[n]} joined already")
            continue
        _, tok = join_token(cl, n)
        rc, text = join(cl, n, tok)
        if rc != 0:
            raise TbError(f"[{n}] fbagent --cluster-join failed: {text[-800:]}")
        log(f"rcm: [{n}] agent {ids[n]} joined")
    wait(lambda: all(i in agents(cl) for i in ids.values()), 300, "every agent in RCM's agent list")

    rc, text = apply(cl, cluster_doc(cl, ids))
    if rc != 0:
        raise TbError(f"cluster apply: {text[-800:]}")
    log(f"rcm: members and links stored ({text.strip().splitlines()[-1] if text.strip() else ''})")
    GA.wait_members(cl, timeout)
    for n in names:
        comps = ["fbagent"] + (["node"] if n in cfg.node_hosts() else []) + (["rcm"] if n == r else [])
        cl.hstate(n)["installed"] = {"source": "rcm-cluster", "components": comps}
    cl.save()
    log("rcm: the cluster is up")


# ----------------------------------------------------------------- up easy --
# The easy install (hqcluster-node docs/cluster-easy-install-plan.ru.md): the
# HQbird setup makes on every host an RCM of its own (cluster id = the host
# name) and opens the agent's pairing window (port 7450, 24 h). The operator
# then links the hosts from one RCM: Settings -> Link hosts, or
# 'hqbirdrcm cluster link'. The other agents join it and stop their own RCM.
def easy_cluster_id(hostname):
    """The cluster id the setup takes from a host name (fbagent
    setupinstall.ClusterIDFromHost)."""
    h = re.sub(r"[^a-z0-9]", "-", hostname.strip().lower().split(".")[0]).strip("-")
    return h[:63].rstrip("-") or "hqcluster"


def hostname(cl, n):
    _, out, _ = cl.host(n).run_raw(["hostname"], check=False)
    return (out or "").strip()


def releases(cl):
    return {r["product"]: r for r in conf_easy(cl).get("releases") or []}


def conf_easy(cl):
    """rcmcluster of the config; the easy stand needs no cluster_id."""
    return cl.cfg.raw.get("rcmcluster") or {}


def stage(cl, n, product, binary, sig="", version=""):
    """A release in the agent's staging folder of host n, as the setup
    payload leaves it (signed with sig). Returns the module's result."""
    args = {"product": product, "binary": put_file(cl, n, binary), "version": version,
            "fbagent_dir": cl.base_args(n)["fbagent_dir"]}
    if sig:
        args["sig"] = put_file(cl, n, sig)
    _, res, _, _ = cl.module(n, MODULE, "stage", args)
    return res or {}


def pairing(cl, n, state="status", rcm_allow=""):
    """fbagent --cluster-pairing on host n. Returns (rc, output)."""
    _, res, _, out = cl.module(n, MODULE, "pairing", {"state": state, "rcm_allow": rcm_allow,
                                                      "fbagent_dir": cl.base_args(n)["fbagent_dir"]}, check=False)
    return (res or {}).get("rc", 1), (res or {}).get("out", out or "")


def pair_call(cl, frm, to_addr, body=None, port=7450):
    """GET /v1/pair/info (body None) or POST /v1/pair/join from host frm to
    to_addr. Returns {"status" (0: no connection), "body", "fingerprint"}."""
    cmd = "pair-info" if body is None else "pair-join"
    args = {"host": to_addr, "port": port}
    if body is not None:
        args["body_b64"] = _b64(json.dumps(body))
    _, res, _, _ = cl.module(frm, MODULE, cmd, args, check=False)
    return res or {"status": 0}


def setup_host(cl, n):
    """What the HQbird setup does on host n: both cluster products staged
    (hqclusternode signed, for RCM's mirror), fbagent --cluster-init with the
    host name as the cluster id, the pairing window open. Returns
    {hostname, cluster_id, rcm_url, ca_hash}."""
    rels = releases(cl)
    node = rels.get("hqclusternode")
    if not node or not node.get("sig"):
        raise TbError("rcmcluster.releases needs a signed hqclusternode (file and sig): "
                      "the RCM mirror gets it from the agents' staging folders")
    binary = rcm_binary(cl)
    if not os.path.isfile(binary):
        raise TbError(f"no RCM build {binary} (rcmcluster.rcm_binary or artifacts)")
    rv = stage(cl, n, "hqbirdrcm", binary)
    nv = stage(cl, n, "hqclusternode", node["file"], sig=node["sig"], version=node["version"])
    name = hostname(cl, n)
    cid = easy_cluster_id(name)
    _, res, _, out = cl.module(n, MODULE, "init", {"cluster_id": cid, "fbagent_dir": cl.base_args(n)["fbagent_dir"]},
                               check=False)
    if not res or res.get("rc") != 0 or not res.get("ca_hash"):
        raise TbError(f"[{n}] fbagent --cluster-init {cid} failed: {(res or {}).get('out', out)[-800:]}")
    rc, text = pairing(cl, n, "on")
    if rc != 0:
        raise TbError(f"[{n}] fbagent --cluster-pairing on failed: {text[-600:]}")
    log(f"rcm: [{n}] set up as the HQbird setup does: hqbirdrcm {rv.get('version')}, "
        f"hqclusternode {nv.get('version')} (signed), cluster {cid}, pairing open")
    return {"hostname": name, "cluster_id": cid, "rcm_url": res["rcm_url"], "ca_hash": res["ca_hash"]}


def up_easy(cl, timeout=1200, link=True):
    """link=False stops before the link: the hosts are free with the pairing
    window open, and the operator links them in RCM (Settings -> Link hosts)."""
    cfg = cl.cfg
    names = ops.install_order(cl, cfg.select("all"))
    ids = {n: GA.agent_id(cl, n) for n in names}
    missing = [n for n in names if not ids[n]]
    if missing:
        raise TbError(f"no fbagent with an agent id on {', '.join(missing)}: "
                      "run 'tb.py install --source local --components fbagent' first")
    for n in names:
        ops.detect(cl, n)
        a = cl.base_args(n)
        a["bases"] = GA.BASES
        cl.module(n, "20-goafts", "cluster-prep", a)
        st = cl.hstate(n)
        st["fbagent_port"] = cl.fbagent_port(n)
        st["fb_port"] = cl.fb_port(n)
    cl.save()

    r = cfg.rcm_host
    # Easy before the setup: a setup that fails half way leaves an RCM on
    # some hosts, and 'rcm down' removes it only on an easy stand.
    st = sstate(cl)
    st.clear()
    st["easy"] = True
    cl.save()
    hosts = {n: setup_host(cl, n) for n in names}
    st.update({"cluster_id": hosts[r]["cluster_id"], "ca_hash": hosts[r]["ca_hash"], "hosts": hosts})
    cl.save()
    wait(lambda: GA.rcm_up(cl), 120, "RCM operator port")
    # The agent service checks the pairing window every 30 s, so port 7450
    # opens up to that long after the setup. An operator takes longer to log in.
    inputs = [cl.h(n)["addr"] for n in names]
    wait(lambda: all(pair_call(cl, r, a).get("status") == 200 for a in inputs), 120, "pairing port 7450")
    if not link:
        for n in names:
            cl.hstate(n)["installed"] = {"source": "rcm-easy", "components": ["fbagent", "rcm"]}
        cl.save()
        log(f"rcm: not linked: link the hosts in the RCM of {r} (Settings -> Link hosts)")
        return

    # The operator links the hosts from the RCM of host r, by address.
    log(f"rcm: hqbirdrcm cluster link -hosts {','.join(inputs)} -master {cl.h(cfg.master)['addr']}")
    rc, text = rcm_cli(cl, "cluster", "link", "-hosts", ",".join(inputs), "-master", cl.h(cfg.master)["addr"],
                       check=False, timeout=timeout + 900)
    st["link_out"] = text[-4000:]
    cl.save()
    if rc != 0:
        raise TbError(f"cluster link: rc {rc}: {text[-1500:]}")
    log("rcm: " + (text.strip().splitlines()[-1] if text.strip() else "linked"))

    doc = rcm_json(cl, "cluster", "show")
    st["rcm_url"] = next((f"https://{m['addresses'][0]}:{m.get('listen_port', 7443)}" for m in doc.get("members") or []
                          if m.get("role") == "rcm" and m.get("addresses")), "")
    st["members"] = {n: sorted(m["member_id"] for m in doc.get("members") or []
                               if m.get("agent_id") == ids[n] and m.get("role") != "rcm") for n in names}
    for n in names:
        comps = ["fbagent", "rcm"] + (["node"] if n in cfg.node_hosts() else [])
        cl.hstate(n)["installed"] = {"source": "rcm-easy", "components": comps}
    cl.save()
    GA.wait_members(cl, timeout)
    log(f"rcm: the cluster {st['cluster_id']} is up: " +
        ", ".join(f"{n}={'+'.join(v) or '-'}" for n, v in st["members"].items()))


def wait(cond, timeout, what, poll=10):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return
        except (TbError, RemoteError):
            pass
        time.sleep(poll)
    raise TbError(f"rcm: not ready after {timeout} s: {what}")


def status(cl):
    cfg = cl.cfg
    rc, text = rcm_cli(cl, "cluster", "status", check=False)
    log(f"rcm: cluster {sstate(cl).get('cluster_id') or cluster_id(cl)} status (rc {rc}):\n{text.strip()[:3000]}")
    for n in cfg.select("all"):
        f = facts(cl, n)
        log(f"  [{n}] agent {f.get('agent_id')} source {json.dumps(f.get('cluster_source'))} "
            f"rcm {json.dumps(f.get('rcm', {}).get('unit'))} nodes "
            f"{json.dumps({k: (v.get('version'), (v.get('unit_state') or {}).get('active')) for k, v in (f.get('nodes') or {}).items()})}")
    return 0


# -------------------------------------------------------------------- down --
def down(cl):
    """Uninstall node, RCM and fbagent from the hosts. The CAs live in the
    agent's folder and go with it; user hqbirdrcm stays. On an easy stand
    every host has an RCM (stopped and disabled but on the RCM host)."""
    cfg = cl.cfg
    names = list(reversed(ops.install_order(cl, cfg.select("all"))))
    every_rcm = bool(sstate(cl).get("easy"))
    left = []
    for n in names:
        args = cl.base_args(n)
        comps = (["fbagent"] + (["node"] if n in cfg.node_hosts() else [])
                 + (["rcm"] if every_rcm or n == cfg.rcm_host else []))
        args["components"] = ",".join(comps)
        if n == cfg.master:
            cl.load_stop()
        rc, res, _, _ = cl.module(n, "20-goafts", "uninstall", args, check=False)
        cl.host(n).run_raw(["rm", "-rf", "/var/tmp/tb-releases"], check=False)
        if not ops.note_leftovers(n, rc, res, left):
            cl.hstate(n).pop("installed", None)
        cl.save()
    cl.state.pop("rcmcluster", None)
    cl.save()
    ops.finish_removal("rcm down", left)
