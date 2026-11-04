"""A cluster RCM serves, without goafts (fbagent contract §7; user guide
1.3), on a stand made by 'tb.py rcm up':

  init        the RCM host after fbagent --cluster-init: RCM runs as user
              hqbirdrcm, rcm.json is the agent's (0640), the seed file and
              the CAs are there, the agent's cluster source is rcm
  reinit      --cluster-init again: same CA hash, RCM still up
  badhash     --cluster-join with a wrong CA hash (scratch agent): refused,
              nothing written, the token stays unused
  reuse       a scratch agent joins with a token, is revoked, and another
              scratch agent with the same token is refused (token_used)
  readonly    PUT /v1/rcm/config: 409 rcm_config_read_only (R-12)
  unsigned    release import of a file without a vendor signature: refused
  members     every node of the config runs the mirror's version, managed
              by its agent, with its peers; RCM lists every node
  status      hqbirdrcm cluster status reports every member
  page        Settings -> Cluster answers 200
  downgrade   (--downgrade-wait S) an older signed hqbirdrcm in the mirror
              does not replace the running RCM

Linux hosts only."""
import json
import re
import time

from tblib import rcmstand as RS
from tblib.cluster import TbError, log
from tblib.results import Results

from . import _rcm

HELP = "a cluster RCM serves without goafts: init, join refusals, revoke, read-only rcm.json, members, page"


def add_args(p):
    p.add_argument("--only", default="", help="comma list of cases (default all)")
    p.add_argument("--downgrade-wait", type=int, default=0,
                   help="downgrade: seconds to wait for the agent's product check (200 or more; 0 = skip)")


def run(cl, a):
    res = Results("nogoafts", vars(a).copy())
    if not RS.sstate(cl).get("ca_hash"):
        raise TbError("no RCM cluster in the run state: run 'tb.py rcm up' first")
    only = {x.strip() for x in a.only.split(",") if x.strip()}
    cases = [("init", case_init), ("reinit", case_reinit), ("badhash", case_badhash), ("reuse", case_reuse),
             ("readonly", case_readonly), ("unsigned", case_unsigned), ("members", case_members),
             ("status", case_status), ("page", case_page), ("downgrade", case_downgrade)]
    for name, fn in cases:
        if only and name not in only:
            continue
        log(f"nogoafts: {name}")
        try:
            fn(cl, a, res)
        except TbError as e:
            res.record(name, "FAIL", note=str(e)[:400])
    return res.finish()


def check(res, case, conds, **details):
    """conds: {what: bool}. PASS when all hold, else FAIL naming the ones that do not."""
    bad = [k for k, ok in conds.items() if not ok]
    return res.record(case, "FAIL" if bad else "PASS", note=("not: " + "; ".join(bad)) if bad else "", **details)


# ------------------------------------------------------------------ cases --
def case_init(cl, a, res):
    r = cl.cfg.rcm_host
    f = RS.facts(cl, r)
    rcm = f.get("rcm") or {}
    agent_user = (f.get("agent_dir_owner") or {}).get("user")
    cid = RS.cluster_id(cl)
    check(res, "init", {
        "hqbirdrcm active": (rcm.get("unit") or {}).get("active") == "active",
        "RCM runs as hqbirdrcm": (rcm.get("unit") or {}).get("user") == "hqbirdrcm",
        "rcm.json is the agent's, 0640": (rcm.get("rcm_json") or {}).get("user") == agent_user
                                          and (rcm.get("rcm_json") or {}).get("mode") == "0o640",
        "rcm-data is hqbirdrcm's": (rcm.get("data_dir") or {}).get("user") == "hqbirdrcm",
        "cluster-init.json": bool(rcm.get("seed")),
        "cluster source rcm": (f.get("cluster_source") or {}).get("source") == "rcm",
        "CAs in clusterca/<id>": any(p.startswith(f"clusterca/{cid}/") for p in f.get("clusterca") or []),
        "7443 listens": "7443" in (f.get("listen") or []),
    }, facts=f)


def case_reinit(cl, a, res):
    r = cl.cfg.rcm_host
    st = RS.sstate(cl)
    _, out, _, text = cl.module(r, RS.MODULE, "init", {"cluster_id": RS.cluster_id(cl), "address": cl.h(r)["addr"],
                                                         "fbagent_dir": cl.base_args(r)["fbagent_dir"]}, check=False)
    out = out or {}
    try:
        RS.wait(lambda: RS.agents(cl) is not None, 120, "RCM after the second --cluster-init")
        up = True
    except TbError:
        up = False
    check(res, "reinit", {"rc 0": out.get("rc") == 0, "same CA hash": out.get("ca_hash") == st["ca_hash"],
                          "same RCM URL": out.get("rcm_url") == st["rcm_url"], "RCM answers": up},
          out=(out.get("out") or text or "")[-1500:])


def scratch(cl, n, name, agent_id=""):
    _, out, _, _ = cl.module(n, RS.MODULE, "scratch-agent", {"name": name, "agent_id": agent_id,
                                                             "fbagent_dir": cl.base_args(n)["fbagent_dir"]})
    return out["dir"]


def scratch_files(cl, n, d):
    rc, out, _ = cl.host(n).run_raw(["sh", "-c", f"cd {d} && find . -type f | sort"], check=False)
    return [x for x in (out or "").split() if x not in ("./fbagent", "./agent_config.json")]


def token_row(cl, tid):
    doc = RS.rcm_json(cl, "cluster", "tokens")
    rows = doc.get("tokens", doc) if isinstance(doc, dict) else doc
    return next((t for t in rows or [] if t.get("id") == tid), {})


def other_host(cl):
    return next((n for n in cl.cfg.select("all") if n != cl.cfg.rcm_host), cl.cfg.rcm_host)


def case_badhash(cl, a, res):
    n = other_host(cl)
    d = scratch(cl, n, "badhash", agent_id="tbbadhash")
    tid, tok = RS.join_token(cl, "nogoafts badhash")
    try:
        rc, text = RS.join(cl, n, tok, ca_hash="sha256:" + "0" * 64, agent_dir=d)
        left = scratch_files(cl, n, d)
        row = token_row(cl, tid)
        check(res, "badhash", {"refused": rc != 0, "says CA/hash": bool(re.search(r"(?i)hash|CA", text)),
                               "nothing written": not left, "token unused": not row.get("used_at")},
              out=text[-1200:], left=left, token=row)
    finally:
        RS.rcm_cli(cl, "cluster", "revoke-token", "-id", tid, check=False)
        cl.module(n, RS.MODULE, "scratch-remove", {"name": "badhash"}, check=False)


def case_reuse(cl, a, res):
    n = other_host(cl)
    aid = "tbreuse" + time.strftime("%H%M%S")
    d1 = scratch(cl, n, "reuse1", agent_id=aid)
    d2 = scratch(cl, n, "reuse2", agent_id=aid + "b")
    tid, tok = RS.join_token(cl, "nogoafts reuse")
    try:
        rc1, t1 = RS.join(cl, n, tok, agent_dir=d1)
        joined = rc1 == 0 and "Joined cluster" in t1
        listed = aid in RS.agents(cl)
        rc_rev, t_rev = RS.rcm_cli(cl, "cluster", "revoke", "-agent", aid, check=False)
        revoked = (RS.agents(cl).get(aid) or {}).get("revoked") is True
        rc2, t2 = RS.join(cl, n, tok, agent_dir=d2)
        check(res, "reuse", {"scratch agent joined": joined, "listed in RCM": listed,
                             "revoked": rc_rev == 0 and revoked,
                             "second use refused": rc2 != 0, "token_used": "token_used" in t2},
              join1=t1[-800:], revoke=t_rev[-300:], join2=t2[-800:])
    finally:
        for s in ("reuse1", "reuse2"):
            cl.module(n, RS.MODULE, "scratch-remove", {"name": s}, check=False)


def case_readonly(cl, a, res):
    st, body = _rcm.api(cl, "GET", "/v1/rcm/config")
    conf = body.get("config", body) if isinstance(body, dict) else {}
    cur = conf.get("poll_interval_sec") or 15
    st2, body2 = _rcm.api(cl, "PUT", "/v1/rcm/config", {"poll_interval_sec": cur})
    code = body2.get("error") if isinstance(body2, dict) else ""
    if isinstance(code, dict):
        code = code.get("code")
    check(res, "readonly", {"GET 200": st == 200, "PUT 409": st2 == 409, "rcm_config_read_only": code == "rcm_config_read_only"},
          put=body2)


def case_unsigned(cl, a, res):
    rc, text = RS.import_release(cl, {"product": "hqbirdrcm", "version": "2099.1.1",
                                      "file": RS.rcm_binary(cl), "sig": ""})
    _, lst = RS.rcm_cli(cl, "release", "list", "-config", cl.h(cl.cfg.rcm_host)["paths"]["rcm"] + "/rcm.json",
                        check=False)
    check(res, "unsigned", {"refused": rc != 0, "says signature": "signature" in text.lower(),
                            "not listed": "2099.1.1" not in lst}, out=text[-800:], list=lst[-1500:])


def case_members(cl, a, res):
    cfg = cl.cfg
    want = {r["product"]: r["version"] for r in RS.conf(cl).get("releases") or []}
    conds, detail = {}, {}
    for n in cfg.node_hosts():
        f = RS.facts(cl, n)
        node = (f.get("nodes") or {}).get(cl.h(n)["paths"]["node"]) or {}
        detail[n] = node
        conds[f"[{n}] node runs"] = (node.get("unit_state") or {}).get("active") == "active"
        conds[f"[{n}] managed by the agent"] = bool(node.get("managed"))
        if want.get("hqclusternode"):
            conds[f"[{n}] version {want['hqclusternode']}"] = node.get("version") == want["hqclusternode"]
        if n == cfg.master:
            peers = set(node.get("peers") or [])
            conds[f"[{n}] peers = replicas"] = peers == {cl.h(r)["node_id"] for r in cfg.replicas}
    ids = set(_rcm.node_ids(cl))
    for n in cfg.node_hosts():
        conds[f"RCM lists {cl.h(n)['node_id']}"] = cl.h(n)["node_id"] in ids
    check(res, "members", conds, nodes=detail, rcm_nodes=sorted(i for i in ids if i))


def case_status(cl, a, res):
    doc = RS.rcm_json(cl, "cluster", "status")
    text = json.dumps(doc)
    conds = {f"reports {cl.h(n)['node_id']}": cl.h(n)["node_id"] in text for n in cl.cfg.node_hosts()}
    check(res, "status", conds, report=doc)


def case_page(cl, a, res):
    st, body = _rcm.web(cl, "/settings/cluster")
    text = body if isinstance(body, str) else json.dumps(body)
    check(res, "page", {"200": st == 200, "names the cluster": RS.cluster_id(cl) in text or "cluster" in text.lower()},
          size=len(text))


def case_downgrade(cl, a, res):
    older = [r for r in RS.conf(cl).get("releases") or [] if r["product"] == "hqbirdrcm"]
    if not a.downgrade_wait or not older:
        res.record("downgrade", "SKIP", note="--downgrade-wait 0 or no hqbirdrcm in rcmcluster.releases")
        return
    r = cl.cfg.rcm_host
    before = (RS.facts(cl, r).get("rcm") or {}).get("version")
    log(f"nogoafts: RCM {before}, mirror {older[0]['version']}; waiting {a.downgrade_wait} s")
    time.sleep(a.downgrade_wait)
    after = (RS.facts(cl, r).get("rcm") or {}).get("version")
    check(res, "downgrade", {"RCM kept its version": before == after and bool(after)}, before=before, after=after,
          mirror=older[0]["version"])
