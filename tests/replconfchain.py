"""T1-T5 of the replconf chain through goafts (hqcluster-node plan
release-replconf-2027.4.3, Ш6/Ш7; next-steps Н8 п. 1-2): what a customer's
hosts get from goafts -> fbagent -> node.json, on a stand that goafts
manages ('tb.py goafts up' on hosts with Firebird and nothing else).

  t1  install    node.json of each node came from the cluster (its node id,
                 the cluster's replconf_valid_till); the node activated
                 replconf itself through fbagent (plugins/ and bin/ stay
                 root's, the plugin root:root 644); the RCM runs; a database
                 replicates (dbs prepare when there is none, a short load,
                 rows match)
  t3  date       the cluster's date moved: node.json and the node's file
                 follow, Firebird is not restarted; the date dropped:
                 node.json keeps its date; a past date: not written, the
                 agent says why; the date put back
  t5  root       the Firebird root closed (root:root 0755) and the node
                 started without its file: fbagent opens the root again by
                 itself and the node writes its file (fbagent 2.59.0+)
  t4  old agent  (--old-fbagent FILE) an fbagent without replconf_install
                 on a host whose plugin and replconf.properties are gone:
                 the node asks to update fbagent (activate_error
                 fbagent_outdated) and nothing is written in plugins/ or
                 bin/; with the agent back the node activates again
  t2  update     (--to-channel CH) the products' channel moved to CH: the
                 agents update node and RCM, replconf is active again and
                 the database replicates

t3-t5 need HQbird 2.5/3.0; t1 and t2 run on every engine (4/5 have no
replconf: the node only has to answer). Linux hosts only."""
import datetime
import json
import os
import time

from tblib import goafts as GA
from tblib import ops
from tblib.cluster import LEGACY_ENGINES, TbError, log
from tblib.remote import RemoteError
from tblib.results import Results

from ._common import converge_and_record

HELP = "goafts -> fbagent -> node.json on HQbird 2.5/3.0: install, date, closed root, old fbagent, update (T1-T5)"

NODE_JSON_PY = r"""
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
fb = d.get("firebird") or {}
print(json.dumps({"node_id": d.get("node_id"), "role": d.get("role"),
                  "valid_till": fb.get("replconf_valid_till", "")}))
"""


def add_args(p):
    p.add_argument("--steps", default="",
                   help="t1,t3,t5,t4,t2; default t1,t3,t5, plus t4 with --old-fbagent and t2 with --to-channel")
    p.add_argument("--old-fbagent", default="", help="t4: a local fbagent build without replconf_install (2.56.x)")
    p.add_argument("--to-channel", default="", help="t2: the goafts channel with newer node/RCM builds (e.g. ops-test)")
    p.add_argument("--host", default="", help="t4/t5: the host; default the first replica")
    p.add_argument("--root-minutes", type=int, default=14, help="t5: how long fbagent may take to open the root")


# ----------------------------------------------------------------- helpers --
def sh(cl, n, script, timeout=120):
    rc, out, _ = cl.host(n).run_raw(["sh", "-c", script], check=False, timeout=timeout)
    return rc, (out or "").strip()


def node_conf(cl, n):
    """node_id, role and firebird.replconf_valid_till of node.json (never the rest: it holds passwords)."""
    path = cl.host(n).join(cl.h(n)["paths"]["node"], "node.json")
    rc, out, _ = cl.host(n).run_raw(["python3", "-c", NODE_JSON_PY, path], check=False)
    try:
        return json.loads((out or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}


def replconf(cl, n):
    try:
        st, doc = cl.api(n, "GET", "/v1/replconf", check_status=False, timeout=60)
    except (TbError, RemoteError):
        return {}
    return doc if st == 200 and isinstance(doc, dict) else {}


def fb_pid(cl, n):
    return (cl.hostctl(n, "trace", {"op": "pid"}, check=False) or {}).get("pid")


def owner_mode(cl, n, path, follow=False):
    """'user:group mode' of a path (of what a link points to with follow),
    '' when it is missing."""
    if not path:
        return ""
    rc, out = sh(cl, n, f"stat {'-L ' if follow else ''}-c '%U:%G %a' '{path}' 2>/dev/null")
    return out if rc == 0 else ""


def root_only(om):
    """Owned by root and writable by no one else."""
    try:
        who, mode = om.split()
        return who.startswith("root:") and not (int(mode, 8) & 0o022)
    except ValueError:
        return False


def own_replconf(doc):
    """HQbird's own replconf set up: replconf.properties points at a file
    other than the node's, and the node did not try (no error)."""
    tgt = doc.get("properties_target") or ""
    return bool(tgt) and not doc.get("active") and tgt != doc.get("conf_path")


def plugin_path(cl, n, doc):
    p = doc.get("plugin") or ""
    if p and not p.startswith("/"):
        p = cl.host(n).join(cl.h(n)["firebird"]["root"], "plugins", p)
    return p


def wait_for(fn, timeout, every=10):
    end = time.time() + timeout
    while True:
        v = fn()
        if v or time.time() >= end:
            return v
        time.sleep(every)


def node_alert(cl, n, code):
    try:
        st, doc = cl.api(n, "GET", "/v1/alerts", check_status=False, timeout=30)
    except (TbError, RemoteError):
        return None
    items = doc if isinstance(doc, list) else (doc or {}).get("alerts", []) if isinstance(doc, dict) else []
    return next((a for a in items if isinstance(a, dict) and a.get("code") == code), None)


def node_version(cl, n):
    try:
        st, body = cl.api(n, "GET", "/v1/version", check_status=False, timeout=30)
    except (TbError, RemoteError):
        return ""
    return str((body or {}).get("version") or "") if st == 200 and isinstance(body, dict) else ""


def rcm_sum(cl):
    n = cl.cfg.rcm_host
    _, out = sh(cl, n, f"sha256sum '{cl.h(n)['paths']['rcm']}/hqbirdrcm' 2>/dev/null | cut -c1-16")
    return out


def agent_log_lines(cl, n, since, text):
    """Lines of the agent's journal and log files since `since` with `text`."""
    d = cl.base_args(n)["fbagent_dir"] or "/opt/hqbird-fbagent"
    unit = cl.base_args(n)["fbagent_service"] or "hqbirdfbagent"
    _, out = sh(cl, n, f"{{ journalctl -u {unit} --since '{since}' --no-pager 2>/dev/null; "
                       f"find '{d}/logs' -type f -newermt '{since}' -exec cat {{}} + 2>/dev/null; }} "
                       f"| grep -F '{text}' | tail -3")
    return [x for x in out.splitlines() if x.strip()]


def host_now(cl, n):
    return sh(cl, n, "date '+%Y-%m-%d %H:%M:%S'")[1]


def short_load(cl, res, case, paths, seconds=30):
    cl.load_start(paths, mode="write", tx="off", conns="1:2", tag="chain")
    time.sleep(seconds)
    cl.load_stop("chain")
    converge_and_record(cl, res, case, paths, 900)


# ---------------------------------------------------------------------- t1 --
def t1(cl, res, legacy, date):
    cfg = cl.cfg
    for n in cfg.node_hosts():
        h = cl.h(n)
        nc = node_conf(cl, n)
        ok = nc.get("node_id") == h["node_id"] and (not legacy or not date or nc.get("valid_till") == date)
        res.record(f"t1 [{n}] node.json came from the cluster (node id, the cluster's date)", "PASS" if ok else "FAIL",
                   note=f"{nc} (cluster date {date or '-'})")
        if not legacy:
            res.record(f"t1 [{n}] the node answers (no replconf on this engine)",
                       "PASS" if GA.node_up(cl, n) else "FAIL")
            continue
        doc = replconf(cl, n)
        if own_replconf(doc):
            # HQbird's full installer set its own replconf up (plugin and
            # replconf.properties pointing at HQbird's file): the node leaves
            # it until the operator activates (decision R2).
            time.sleep(60)
            doc = replconf(cl, n)
            # An activate_error is noted, not failed: it is the last failed
            # activation the node keeps until one succeeds (an earlier run).
            res.record(f"t1 [{n}] HQbird's own replconf: the node leaves it until the operator activates",
                       "PASS" if not doc.get("active") and own_replconf(doc) else "FAIL",
                       note={k: doc.get(k) for k in ("active", "properties_target", "activate_error")})
            st, act = cl.api(n, "POST", "/v1/replconf/activate", {}, check_status=False, timeout=300)
            doc = wait_for(lambda: (lambda d: d if d.get("active") else None)(replconf(cl, n)), 600) or replconf(cl, n)
            what = f"t1 [{n}] the operator's activation went through fbagent"
            note = {"activate": st, "via": (act or {}).get("via") if isinstance(act, dict) else None}
        else:
            doc = wait_for(lambda: (lambda d: d if d.get("active") else None)(replconf(cl, n)), 600) or replconf(cl, n)
            what = f"t1 [{n}] the node activated replconf itself (HQbird without a plugin)"
            note = {}
        ok = (doc.get("active") and doc.get("plugin_version") and not doc.get("activate_error")
              and (not date or (doc.get("valid_till") == date and doc.get("valid_till_source") == "config")))
        note.update({k: doc.get(k) for k in ("active", "plugin_version", "valid_till", "valid_till_source",
                                              "properties_target", "activate_error", "problems")})
        res.record(what, "PASS" if ok else "FAIL", note=note)
        root = h["firebird"]["root"]
        own = {"plugins": owner_mode(cl, n, root + "/plugins"), "bin": owner_mode(cl, n, root + "/bin"),
               "plugin": owner_mode(cl, n, plugin_path(cl, n, doc), follow=True),
               "properties": owner_mode(cl, n, doc.get("properties") or "", follow=True),
               "root": owner_mode(cl, n, root)}
        ok = all(root_only(own[k]) for k in ("plugins", "bin", "plugin", "properties"))
        res.record(f"t1 [{n}] plugins/, bin/, the plugin and replconf.properties are root's, not writable by others",
                   "PASS" if ok else "FAIL", note=own)
    if cfg.rcm_enabled:
        res.record("t1 the RCM the agent installed runs", "PASS" if GA.rcm_up(cl) else "FAIL")
    if not cl.test_dbs():
        ops.dbs_prepare(cl, count=1)
    paths = [d["path"] for d in cl.test_dbs()][:1]
    short_load(cl, res, "t1 replication: a database replicates (rows match)", paths)


# ---------------------------------------------------------------------- t3 --
def t3(cl, res, date):
    nodes = cl.cfg.node_hosts()
    d0 = date or node_conf(cl, cl.cfg.master).get("valid_till")
    if not d0:
        res.record("t3", "SKIP", note="the cluster has no replconf_valid_till (goafts.cluster.replconf_valid_till)")
        return
    pid0 = {n: fb_pid(cl, n) for n in nodes}
    new = (datetime.date.fromisoformat(d0) + datetime.timedelta(days=91)).isoformat()
    t0 = time.time()
    GA.set_date(cl, new)
    got = wait_for(lambda: all(node_conf(cl, n).get("valid_till") == new for n in nodes), 180, 3)
    res.record("t3 a new cluster date reaches node.json of every node", "PASS" if got else "FAIL",
               note=f"{d0} -> {new} in {int(time.time() - t0)} s; "
                    f"{ {n: node_conf(cl, n).get('valid_till') for n in nodes} }")
    files = wait_for(lambda: all(replconf(cl, n).get("valid_till") == new for n in nodes), 180)
    pid1 = {n: fb_pid(cl, n) for n in nodes}
    res.record("t3 the node's file has the new date; Firebird was not restarted",
               "PASS" if files and pid1 == pid0 else "FAIL",
               note=f"file dates { {n: replconf(cl, n).get('valid_till') for n in nodes} }; pid {pid0} -> {pid1}")

    GA.set_date(cl, None)
    time.sleep(60)
    kept = {n: node_conf(cl, n).get("valid_till") for n in nodes}
    res.record("t3 the date dropped from the cluster: node.json keeps its date",
               "PASS" if all(v == new for v in kept.values()) else "FAIL", note=kept)

    since = host_now(cl, cl.cfg.master)
    past = (datetime.date.fromisoformat(since[:10]) - datetime.timedelta(days=1)).isoformat()
    GA.set_date(cl, past)
    time.sleep(60)
    kept = {n: node_conf(cl, n).get("valid_till") for n in nodes}
    said = {n: agent_log_lines(cl, n, since, "has already passed") for n in nodes}
    ok = all(v == new for v in kept.values()) and all(said.values())
    res.record("t3 a past date is not written; the agent says why", "PASS" if ok else "FAIL",
               note=f"node.json {kept}; agent: { {n: (v[-1][-160:] if v else 'no line') for n, v in said.items()} }")

    GA.set_date(cl, d0)
    back = wait_for(lambda: all(node_conf(cl, n).get("valid_till") == d0 for n in nodes), 180, 3)
    res.record("t3 the date put back", "PASS" if back else "FAIL",
               note={n: node_conf(cl, n).get("valid_till") for n in nodes})


# ---------------------------------------------------------------------- t5 --
def t5(cl, res, n, minutes):
    root = cl.h(n)["firebird"]["root"]
    doc = replconf(cl, n)
    conf = doc.get("conf_path") or cl.host(n).join(root, "replconf.hqcluster.hqbird")
    before = owner_mode(cl, n, root)
    cl.hostctl(n, "node-svc", {"action": "stop"})
    sh(cl, n, f"rm -f '{conf}' && chown root:root '{root}' && chmod 0755 '{root}'")
    closed = owner_mode(cl, n, root)
    cl.hostctl(n, "node-svc", {"action": "start"})
    t0 = time.time()
    res.record("t5 setup: node stopped, its file removed, the root closed, node started",
               "PASS" if closed == "root:root 755" else "FAIL", note=f"root {before} -> {closed}")

    def opened():
        om = owner_mode(cl, n, root)
        if not om or om.split()[0].split(":")[1] != "firebird" or not (int(om.split()[1], 8) & 0o020):
            return None
        rc, _ = sh(cl, n, f"test -f '{conf}'")
        d = replconf(cl, n)
        return om if rc == 0 and d.get("active") else None

    om = wait_for(opened, minutes * 60, 15)
    res.record("t5 fbagent opened the root again by itself and the node wrote its file", "PASS" if om else "FAIL",
               note=f"after {int(time.time() - t0)} s: root {owner_mode(cl, n, root)}; "
                    f"file {'there' if sh(cl, n, f'test -f {conf}')[0] == 0 else 'missing'}; "
                    f"active {replconf(cl, n).get('active')}")
    if not om:
        # Leave the stand usable: open the root the way the agent does.
        sh(cl, n, f"chgrp firebird '{root}' && chmod g+w,+t '{root}'")


# ---------------------------------------------------------------------- t4 --
def t4(cl, res, n, binary):
    if not os.path.isfile(binary):
        raise TbError(f"--old-fbagent {binary}: no such file")
    root = cl.h(n)["firebird"]["root"]
    hst = cl.host(n)
    doc = replconf(cl, n)
    plugin, props = plugin_path(cl, n, doc), doc.get("properties") or hst.join(root, "replconf.properties")
    work = hst.join(hst.work, "t4")
    hst.mkdir(work)
    remote = hst.join(work, "fbagent-old")
    hst.put(binary, remote)
    GA.set_channel(cl, [n], self_update="off")
    a = cl.base_args(n)
    a["binary"] = remote
    _, sw, _, _ = cl.module(n, "20-goafts", "agent-swap", a)
    res.record("t4 setup: the old fbagent runs, self-update off", "PASS" if (sw or {}).get("version") else "FAIL",
               note=f"version {(sw or {}).get('version') or '?'}")

    # Name, owner, mode, size and link target of every entry: what changed is named.
    listing = f"cd '{root}' && find plugins bin -maxdepth 1 -printf '%p %u:%g %m %s %l\\n' 2>/dev/null | sort"
    cl.hostctl(n, "node-svc", {"action": "stop"})
    sh(cl, n, f"mv -f '{plugin}' '{work}/plugin.bak' 2>/dev/null; mv -f '{props}' '{work}/props.bak' 2>/dev/null; true")
    snap = sh(cl, n, listing)[1]
    own0 = (owner_mode(cl, n, root + "/plugins"), owner_mode(cl, n, root + "/bin"))
    cl.hostctl(n, "node-svc", {"action": "start"})

    # Two forms of the refusal. A node whose node.json the old agent leaves
    # alone tries the agent's route and reports activate_error
    # fbagent_outdated. In a goafts cluster an agent before 2.57.0 writes
    # firebird.replication_conf = <root>/replication.conf into node.json
    # itself: the node then raises config_backend_mismatch and leaves
    # replconf alone.
    def refused():
        d = replconf(cl, n)
        if "fbagent_outdated" in (d.get("activate_error") or ""):
            return "activate_error: " + d["activate_error"][:200]
        a = node_alert(cl, n, "config_backend_mismatch")
        if a:
            return "config_backend_mismatch (the old agent rewrote node.json): " + (a.get("message") or "")[:200]
        return None

    why = wait_for(refused, 240, 10)
    if not why:
        cl.api(n, "POST", "/v1/replconf/activate", {"dry_run": False, "ignore_window": True}, check_status=False,
               timeout=180)
        why = refused()
    res.record("t4 the node refuses: activate_error fbagent_outdated, or config_backend_mismatch",
               "PASS" if why else "FAIL",
               note=why or f"neither; replconf {replconf(cl, n).get('activate_error') or '-'}")
    time.sleep(30)
    now = sh(cl, n, listing)[1]
    same = now == snap
    changed = sorted(set(now.splitlines()) ^ set(snap.splitlines()))
    gone = sh(cl, n, f"test ! -e '{plugin}' && test ! -e '{props}'")[0] == 0
    own1 = (owner_mode(cl, n, root + "/plugins"), owner_mode(cl, n, root + "/bin"))
    res.record("t4 nothing is written in plugins/ or bin/", "PASS" if same and gone and own1 == own0 else "FAIL",
               note=f"listing same {same}{'' if same else ' ' + str(changed[:8])}; "
                    f"plugin and replconf.properties still absent {gone}; owners {own0} -> {own1}")

    a = cl.base_args(n)
    a["restore"] = True
    _, sw, _, _ = cl.module(n, "20-goafts", "agent-swap", a)
    GA.set_channel(cl, [n], self_update="on")
    back = wait_for(lambda: replconf(cl, n).get("active"), 600, 15)
    res.record("t4 with fbagent back the node activates again", "PASS" if back else "FAIL",
               note=f"agent {(sw or {}).get('version') or '?'}; "
                    f"{ {k: replconf(cl, n).get(k) for k in ('active', 'plugin_version', 'activate_error')} }")
    if not back:
        sh(cl, n, f"test -e '{plugin}' || mv -f '{work}/plugin.bak' '{plugin}'; "
                  f"test -e '{props}' || mv -f '{work}/props.bak' '{props}'; true")


# ---------------------------------------------------------------------- t2 --
def t2(cl, res, legacy, channel):
    cfg = cl.cfg
    nodes = cfg.node_hosts()
    v0 = {n: node_version(cl, n) for n in nodes}
    r0 = rcm_sum(cl) if cfg.rcm_enabled else ""
    GA.set_channel(cl, cfg.select("all"), product_channel=channel)
    t0 = time.time()

    def updated():
        v = {n: node_version(cl, n) for n in nodes}
        r = rcm_sum(cl) if cfg.rcm_enabled else ""
        return v if all(v[n] and v[n] != v0[n] for n in nodes) and (not cfg.rcm_enabled or (r and r != r0)) else None

    v1 = wait_for(updated, 1200, 20)
    res.record(f"t2 the agents updated node and RCM from {channel}", "PASS" if v1 else "FAIL",
               note=f"nodes {v0} -> { {n: node_version(cl, n) for n in nodes} } in {int(time.time() - t0)} s; "
                    f"rcm {r0} -> {rcm_sum(cl) if cfg.rcm_enabled else '-'}")
    if legacy:
        act = {n: bool(wait_for(lambda: replconf(cl, n).get("active"), 300, 10)) for n in nodes}
        res.record("t2 replconf active on every node after the update", "PASS" if all(act.values()) else "FAIL",
                   note=act)
    paths = [d["path"] for d in cl.test_dbs()][:1]
    if paths:
        short_load(cl, res, "t2 replication after the update (rows match)", paths)


# --------------------------------------------------------------------- run --
def run(cl, a):
    res = Results("replconfchain", vars(a).copy())
    cfg = cl.cfg
    if any(cl.h(n)["os"] != "linux" for n in cfg.node_hosts()):
        res.record("replconfchain", "SKIP", note="Linux hosts only")
        return res.finish()
    if GA.get(cl) is None:
        res.record("replconfchain", "FAIL", note=f"goafts has no cluster {GA.cluster_id(cl)}: run 'tb.py goafts up'")
        return res.finish()
    legacy = cl.fb_engine(cfg.master) in LEGACY_ENGINES
    date = (GA.conf(cl).get("replconf_valid_till") or "") if legacy else ""
    steps = [s.strip() for s in a.steps.split(",") if s.strip()] or \
        ["t1", "t3", "t5"] + (["t4"] if a.old_fbagent else []) + (["t2"] if a.to_channel else [])
    host = a.host or (cfg.replicas[0] if cfg.replicas else cfg.master)
    for s in steps:
        if s in ("t3", "t4", "t5") and not legacy:
            res.record(s, "SKIP", note=f"HQbird {cl.fb_engine(cfg.master) or '?'}: no replconf")
            continue
        log(f"replconfchain: {s}")
        try:
            if s == "t1":
                t1(cl, res, legacy, date)
            elif s == "t3":
                t3(cl, res, date)
            elif s == "t5":
                t5(cl, res, host, a.root_minutes)
            elif s == "t4":
                if not a.old_fbagent:
                    res.record("t4", "SKIP", note="no --old-fbagent")
                    continue
                t4(cl, res, host, a.old_fbagent)
            elif s == "t2":
                if not a.to_channel:
                    res.record("t2", "SKIP", note="no --to-channel")
                    continue
                t2(cl, res, legacy, a.to_channel)
            else:
                res.record(s, "FAIL", note="unknown step (t1, t2, t3, t4, t5)")
        except (TbError, RemoteError) as e:
            res.record(f"{s}: error", "FAIL", note=str(e)[:400])
    return res.finish()
