"""The easy install (hqcluster-node docs/cluster-easy-install-plan.ru.md;
user guide 1.3), on a stand made by 'tb.py rcm up --easy': the setup made an
RCM on every host and opened the pairing window, then one RCM linked the
hosts with 'hqbirdrcm cluster link'.

  linked    the link job ended done; every node runs, managed by its agent,
            with the member id <host>-<role>; RCM lists every member; every
            agent's cluster source is rcm with the URL of the linking RCM
  rcmoff    the RCM of every other host is stopped and disabled, and its own
            empty cluster is kept in cluster/left/; the linking RCM runs
  port      no host listens on 7450 after the link
  mirror    RCM's mirror has the hqclusternode the agents staged (signed):
            the RCM host put it in import/, the nodes run that version
  again     a host in the cluster opens its window again: no RCM can invite
            it (the port stays closed, or 409 agent_in_cluster)
  seats     (--spare HOST) a probe of the cluster hosts and one more free
            host: seats_exceeded, nothing linked
  allow     (--spare HOST) the spare allows only another RCM: an invitation
            from this RCM is 403 rcm_not_allowed
  window    (--spare HOST) the spare closes its window: the port closes

--spare names a config host outside master/replicas with a test bed fbagent
('tb.py install --source local --components fbagent --hosts HOST'); the test
sets it up as the setup does and leaves it free with its window open.

Linux hosts only."""
import time

from tblib import rcmstand as RS
from tblib.cluster import TbError, log
from tblib.results import Results

from . import _rcm

HELP = "the easy install: an RCM per host, pairing, cluster link; extra RCMs off, port 7450, seats, rcm_allow"


def add_args(p):
    p.add_argument("--only", default="", help="comma list of cases (default all)")
    p.add_argument("--spare", default="", help="a config host outside the cluster, for seats, allow and window")


def run(cl, a):
    res = Results("easyinstall", vars(a).copy())
    if not RS.sstate(cl).get("easy"):
        raise TbError("no easy stand in the run state: run 'tb.py rcm up --easy' first")
    only = {x.strip() for x in a.only.split(",") if x.strip()}
    cases = [("linked", case_linked), ("rcmoff", case_rcmoff), ("port", case_port), ("mirror", case_mirror),
             ("again", case_again), ("seats", case_seats), ("allow", case_allow), ("window", case_window)]
    for name, fn in cases:
        if only and name not in only:
            continue
        log(f"easyinstall: {name}")
        try:
            fn(cl, a, res)
        except TbError as e:
            res.record(name, "FAIL", note=str(e)[:400])
    return res.finish()


def check(res, case, conds, **details):
    """conds: {what: bool}. PASS when all hold, else FAIL naming the ones that do not."""
    bad = [k for k, ok in conds.items() if not ok]
    return res.record(case, "FAIL" if bad else "PASS", note=("not: " + "; ".join(bad)) if bad else "", **details)


def names(cl):
    return cl.cfg.select("all")


def unit(f):
    return (f.get("rcm") or {}).get("unit") or {}


# ------------------------------------------------------------------ cases --
def case_linked(cl, a, res):
    cfg, st = cl.cfg, RS.sstate(cl)
    conds, detail = {"link done": "Done:" in (st.get("link_out") or "")}, {}
    rcm_nodes = set(_rcm.node_ids(cl))
    for n in names(cl):
        f = RS.facts(cl, n)
        src = f.get("cluster_source") or {}
        conds[f"[{n}] source rcm"] = src.get("source") == "rcm"
        conds[f"[{n}] joins the linking RCM"] = n == cfg.rcm_host or src.get("rcm_url") == st.get("rcm_url")
        if n not in cfg.node_hosts():
            continue
        role = "master" if n == cfg.master else "replica"
        want = f"{st['hosts'][n]['cluster_id']}-{role}"
        node = (f.get("nodes") or {}).get(cl.h(n)["paths"]["node"]) or {}
        detail[n] = node
        conds[f"[{n}] member {want}"] = want in (st.get("members") or {}).get(n, [])
        conds[f"[{n}] node runs"] = (node.get("unit_state") or {}).get("active") == "active"
        conds[f"[{n}] managed by the agent"] = bool(node.get("managed"))
        conds[f"RCM lists {want}"] = want in rcm_nodes
    check(res, "linked", conds, nodes=detail, members=st.get("members"), rcm_nodes=sorted(i for i in rcm_nodes if i),
          link=(st.get("link_out") or "")[-1500:])


def case_rcmoff(cl, a, res):
    conds, detail = {}, {}
    for n in names(cl):
        f = RS.facts(cl, n)
        u = unit(f)
        detail[n] = {"unit": u, "left": f.get("left")}
        if n == cl.cfg.rcm_host:
            conds[f"[{n}] RCM runs"] = u.get("active") == "active"
            conds[f"[{n}] RCM enabled"] = u.get("enabled") == "enabled"
            continue
        cid = RS.sstate(cl)["hosts"][n]["cluster_id"]
        conds[f"[{n}] own RCM stopped"] = u.get("active") != "active"
        conds[f"[{n}] own RCM disabled"] = u.get("enabled") == "disabled"
        conds[f"[{n}] own cluster in left/"] = any(x.startswith(cid + "-") for x in f.get("left") or [])
    check(res, "rcmoff", conds, hosts=detail)


def case_port(cl, a, res):
    listen = {}

    def closed():
        for n in names(cl):
            listen[n] = RS.facts(cl, n).get("listen") or []
        return all("7450" not in v for v in listen.values())
    try:
        RS.wait(closed, 90, "port 7450 closed on every host", poll=15)
    except TbError:
        pass
    check(res, "port", {f"[{n}] 7450 closed": "7450" not in v for n, v in listen.items()}, listen=listen)


def case_mirror(cl, a, res):
    node = RS.releases(cl).get("hqclusternode") or {}
    r = cl.cfg.rcm_host
    f = RS.facts(cl, r)
    _, lst = RS.rcm_cli(cl, "release", "list", "-config", cl.h(r)["paths"]["rcm"] + "/rcm.json", check=False)
    conds = {"import/ has hqclusternode": any(x.endswith(".release.json") for x in (f.get("rcm") or {}).get("import") or []),
             f"mirror lists hqclusternode {node.get('version')}": bool(node.get("version")) and node["version"] in lst}
    for n in cl.cfg.node_hosts():
        v = ((RS.facts(cl, n).get("nodes") or {}).get(cl.h(n)["paths"]["node"]) or {}).get("version")
        conds[f"[{n}] node {node.get('version')}"] = v == node.get("version")
    check(res, "mirror", conds, imported=(f.get("rcm") or {}).get("import"), list=lst[-1500:])


def invitation(cl):
    """An invitation that cannot work (no join token): the agent must refuse
    it before it reads the token."""
    st = RS.sstate(cl)
    body = {"contract": 1, "rcm_url": st["rcm_url"], "ca_hash": st["ca_hash"]}
    body["token"] = "x" * 43
    return body


def case_again(cl, a, res):
    cfg = cl.cfg
    n = next((x for x in cfg.replicas if x != cfg.rcm_host), "")
    if not n:
        res.record("again", "SKIP", note="no replica host apart from the RCM host")
        return
    rc_on, text_on = RS.pairing(cl, n, "on")
    try:
        time.sleep(40)  # the agent checks its window every 30 s
        info = RS.pair_call(cl, cfg.rcm_host, cl.h(n)["addr"])
        join = RS.pair_call(cl, cfg.rcm_host, cl.h(n)["addr"], invitation(cl))
        err = (join.get("body") or {}).get("error") if isinstance(join.get("body"), dict) else None
        code = err.get("code") if isinstance(err, dict) else ""
        check(res, "again", {
            # A linked agent is "claimed by <RCM>"; one joined by token "in_cluster".
            "pairing says not free": "State:       claimed by" in text_on or "State:       in_cluster" in text_on,
            "no invitation taken": join.get("status") == 0 or (join.get("status") == 409 and code == "agent_in_cluster"),
            "info closed or in_cluster": info.get("status") == 0 or (info.get("body") or {}).get("state") == "in_cluster",
        }, pairing=text_on[-800:], rc=rc_on, info=info, join=join)
    finally:
        RS.pairing(cl, n, "off")


def spare(cl, a, res, case):
    if not a.spare:
        res.record(case, "SKIP", note="no --spare host")
        return None
    if a.spare in names(cl):
        raise TbError(f"--spare {a.spare} is in the cluster (master, replica or RCM host)")
    st = RS.sstate(cl).setdefault("spare", {})
    if st.get("host") != a.spare:
        st.update({"host": a.spare, **RS.setup_host(cl, a.spare)})
        cl.save()
    return a.spare


def open_port(cl, s):
    RS.wait(lambda: RS.pair_call(cl, cl.cfg.rcm_host, cl.h(s)["addr"]).get("status") == 200, 90,
            f"the pairing port of {s}", poll=10)


def case_seats(cl, a, res):
    s = spare(cl, a, res, "seats")
    if not s:
        return
    RS.pairing(cl, s, "on", rcm_allow="any")
    open_port(cl, s)
    hosts = [cl.h(n)["addr"] for n in names(cl)] + [cl.h(s)["addr"]]
    stc, body = _rcm.api(cl, "POST", "/v1/admin/pair/probe", {"hosts": hosts})
    rows = (body or {}).get("hosts") or [] if isinstance(body, dict) else []
    codes = {h.get("input"): h.get("code") or h.get("state") for h in rows}
    check(res, "seats", {"probe 200": stc == 200, "not ok": isinstance(body, dict) and body.get("ok") is False,
                         "seats_exceeded": "seats_exceeded" in codes.values(),
                         "spare is free with its own RCM": any(h.get("input") == cl.h(s)["addr"] and
                                                               (h.get("rcm_local") or h.get("code") == "seats_exceeded")
                                                               for h in rows)},
          codes=codes)


def case_allow(cl, a, res):
    s = spare(cl, a, res, "allow")
    if not s:
        return
    try:
        rc, text = RS.pairing(cl, s, "on", rcm_allow="192.0.2.1")
        open_port(cl, s)
        join = RS.pair_call(cl, cl.cfg.rcm_host, cl.h(s)["addr"], invitation(cl))
        err = (join.get("body") or {}).get("error") if isinstance(join.get("body"), dict) else None
        check(res, "allow", {"pairing on": rc == 0, "403": join.get("status") == 403,
                             "rcm_not_allowed": isinstance(err, dict) and err.get("code") == "rcm_not_allowed"},
              pairing=text[-600:], join=join)
    finally:
        RS.pairing(cl, s, "on", rcm_allow="any")


def case_window(cl, a, res):
    s = spare(cl, a, res, "window")
    if not s:
        return
    try:
        rc, text = RS.pairing(cl, s, "off")
        closed = True
        try:
            RS.wait(lambda: RS.pair_call(cl, cl.cfg.rcm_host, cl.h(s)["addr"]).get("status") == 0, 90,
                    f"the pairing port of {s} closed", poll=10)
        except TbError:
            closed = False
        check(res, "window", {"pairing off": rc == 0, "says closed": "closed" in text, "port closed": closed},
              pairing=text[-600:])
    finally:
        RS.pairing(cl, s, "on", rcm_allow="any")
