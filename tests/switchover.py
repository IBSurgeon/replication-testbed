"""Switchover, failover and automatic failover of an RCM service (cluster
switchover API plan, stages 1-5), under load through a router. Needs a stand
made by 'tb.py rcm up' with a service of two hosts (--service; default: the
only one) whose routers run on the hosts, and fb-loadgen on --load-host.

  switch      --rounds switchovers back and forth: each ends ok at the next
              epoch, the old master is the replica again (rejoin), every
              router routes to the new master, the load goes on; at the end
              no new ORPHANED replica record is on the service nodes, and
              each master node keeps at most one (the old master's)
  hung        the master's Firebird is stopped (SIGSTOP): it takes TCP
              connections but answers nothing. RCM fails the service (node
              protocol minor 22); SIGCONT; resume brings it back to ok at the
              same epoch
  failover    the master host is powered off (hosts.<h>.power.off/on in the
              local config; skipped without them); a failover by hand with
              accept_data_loss; power on; the old master is fenced at its
              first answer; rejoin. --fence-mode sets policy.fence_mode for
              the case. --disable-table takes that table out of the
              publication of the master first: the failover carries it to the
              new master from the old master's last stats (minor 23)
  rcmcut      with auto failover on, the RCM host loses the network to the
              service hosts for --cut seconds: no failover starts
  rcmrestart  with auto failover on, RCM restarts: no failover starts
  mastercut   with auto failover on, the master host loses the network to
              every other host: RCM fails the service over by itself, the rule
              turns itself off; the network comes back, fence, rejoin

Every case undoes what it changed in a finally block (blocks removed, host
powered on, the policy put back). Linux hosts only (HAProxy routers)."""
import json
import posixpath
import re
import subprocess
import time

from tblib import rcmstand as RS
from tblib.cluster import TbError, log, parse_output
from tblib.remote import b64
from tblib.results import Results

HELP = "switchover back and forth, failover of a lost host, automatic failover chaos (RCM service, under load)"

CASES = ["switch", "hung", "failover", "rcmcut", "rcmrestart", "mastercut"]
TAG = "switchover"


def add_args(p):
    p.add_argument("--only", default="", help="comma list of: " + ",".join(CASES) + " (default all)")
    p.add_argument("--service", default="", help="RCM service id (default: the only service)")
    p.add_argument("--rounds", type=int, default=2, help="switch: number of switchovers")
    p.add_argument("--settle", type=int, default=180, help="seconds for routers and the rejoined replica to settle")
    p.add_argument("--cut", type=int, default=150, help="rcmcut/rcmrestart: seconds to watch (more than failover_delay_sec)")
    p.add_argument("--hung-wait", type=int, default=360, help="hung: seconds to wait for the failed service")
    p.add_argument("--fence-mode", default="", choices=["", "route", "shutdown"],
                   help="failover: policy.fence_mode for the case (default: as it is)")
    p.add_argument("--disable-table", default="",
                   help="failover: a table to take out of the publication before (one the load does not write)")
    p.add_argument("--load-host", default="", help="host that runs fb-loadgen through its router (default: the RCM host)")
    p.add_argument("--no-load", action="store_true", help="run without load")


def run(cl, a):
    res = Results("switchover", vars(a).copy())
    t = Stand(cl, a)
    only = {x.strip() for x in a.only.split(",") if x.strip()} or set(CASES)
    unknown = only - set(CASES)
    if unknown:
        raise TbError(f"unknown case(s): {', '.join(sorted(unknown))}")
    if not a.no_load:
        t.load_start()
    try:
        for name in CASES:
            if name not in only:
                continue
            log(f"switchover: {name}")
            try:
                globals()["case_" + name](t, a, res)
            except TbError as e:
                res.record(name, "FAIL", note=str(e)[:400])
    finally:
        if not a.no_load:
            res.record("load ran", "PASS" if t.load_total() > t.load_total0 else "FAIL",
                       note=f"transactions {t.load_total0} -> {t.load_total()}")
            t.load_stop()
    return res.finish()


def check(res, case, conds, **details):
    """conds: {what: bool}. PASS when all hold, else FAIL naming the ones that do not."""
    bad = [k for k, ok in conds.items() if not ok]
    return res.record(case, "FAIL" if bad else "PASS", note=("not: " + "; ".join(bad)) if bad else "", **details)


class Stand:
    """The service under test and the hosts it runs on."""

    def __init__(self, cl, a):
        self.cl = cl
        self.svc_id = a.service or self._only_service()
        s = self.service()
        hosts = [m["host"] for m in s.get("members", [])]
        if len(hosts) != 2:
            raise TbError(f"service {self.svc_id}: {len(hosts)} hosts; this test needs two")
        # RCM names hosts by their host name; the test bed by config names.
        self.by_hostname = {RS.hostname(cl, n): n for n in cl.cfg.raw["hosts"]}
        missing = [h for h in hosts if h not in self.by_hostname]
        if missing:
            raise TbError(f"service hosts not in the test bed config: {missing}")
        self.hosts = hosts
        self.load_host = a.load_host or cl.cfg.rcm_host
        self.load_total0 = 0

    # --- RCM ----------------------------------------------------------------
    def rcm(self, *args, timeout=900):
        rc, text = RS.rcm_cli(self.cl, *args, check=False, timeout=timeout)
        i = text.find("\n{")
        i = i + 1 if i >= 0 else text.find("{")
        doc = None
        if i >= 0:
            try:
                doc, _ = json.JSONDecoder().raw_decode(text[i:])
            except ValueError:
                pass
        return rc, text, doc

    def _only_service(self):
        _, text, doc = self.rcm("services", "list")
        ids = [s.get("service_id") for s in (doc or {}).get("services", [])] if doc else []
        if len(ids) != 1:
            raise TbError(f"--service is needed: the RCM has services {ids}")
        return ids[0]

    def service(self):
        _, text, doc = self.rcm("services", "show", "-id", self.svc_id)
        if not doc:
            raise TbError(f"services show {self.svc_id}: {text[-300:]}")
        return doc

    def where(self):
        _, text, doc = self.rcm("where", "-service", self.svc_id)
        if not doc:
            raise TbError(f"where {self.svc_id}: {text[-300:]}")
        return doc

    def master_host(self):
        return self.service()["master"]["host"]

    def other(self, host):
        return [h for h in self.hosts if h != host][0]

    def policy(self):
        _, text, doc = self.rcm("policy", "-service", self.svc_id)
        if not doc:
            raise TbError(f"policy {self.svc_id}: {text[-300:]}")
        return doc

    def set_policy(self, p):
        """p: a policy document (auto_failover, max_data_loss_sec, ...)."""
        args = ["policy", "-service", self.svc_id, "-auto-failover", "on" if p.get("auto_failover") else "off"]
        for flag, key in (("-max-data-loss", "max_data_loss_sec"), ("-failover-delay", "failover_delay_sec"),
                          ("-min-interval", "min_interval_sec"), ("-fence-mode", "fence_mode")):
            if p.get(key) not in (None, ""):
                args += [flag, str(p[key])]
        rc, text, _ = self.rcm(*args)
        if rc != 0:
            raise TbError(f"{' '.join(args)}: {text[-300:]}")

    def settled(self, master, epoch):
        """where says: state ok at epoch, master on master, the replica in sync,
        and every router routes to master at epoch."""
        w = self.where()
        reps = w.get("replicas") or []
        routers = w.get("routers") or []
        return (w.get("epoch") == epoch and w.get("state") == "ok" and w["master"]["host"] == master
                and bool(reps) and all(r.get("state") in ("IN_SYNC", "LAGGING") for r in reps)
                and bool(routers) and all(r.get("active") == master and r.get("epoch") == epoch for r in routers)), w

    def wait_settled(self, master, epoch, timeout):
        end = time.time() + timeout
        w = {}
        while time.time() < end:
            ok, w = self.settled(master, epoch)
            if ok:
                return True, w
            time.sleep(10)
        return False, w

    def wait_fenced(self, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if not self.service().get("pending_fences"):
                return True
            time.sleep(10)
        return False

    def wait_state(self, state, timeout):
        """The service document once its state is state, or the last one."""
        end = time.time() + timeout
        s = {}
        while time.time() < end:
            s = self.service()
            if s.get("state") == state:
                break
            time.sleep(10)
        return s

    def orphaned(self):
        """{"masters": [...], "replicas": [...]}: 'db_id on node' of each
        ORPHANED record of the service's nodes, by the list RCM files it in."""
        _, text, doc = self.rcm("databases")
        if not doc:
            raise TbError(f"databases: {text[-300:]}")
        nodes = {n for m in self.service().get("members", []) for n in (m.get("master_node"), m.get("replica_node"))}
        return {kind: sorted(f"{r.get('db_id')} on {r.get('node_id')}"
                             for g in doc.get("databases", []) for r in g.get(kind) or []
                             if r.get("state") == "ORPHANED" and r.get("node_id") in nodes)
                for kind in ("masters", "replicas")}

    # --- nodes --------------------------------------------------------------
    def node_api(self, hostname, role, method, path, body=None, timeout=60):
        """The node API of the <role> node (master or replica) of a service
        host, with that node's certificate. 'rcm up' installs the two nodes
        of a host side by side, /opt/hqclusternode/<role>, and paths.node is
        one of them. Returns (status, body)."""
        n = self.tb(hostname)
        node_dir = posixpath.join(posixpath.dirname(self.cl.h(n)["paths"]["node"].rstrip("/")), role)
        argv = ["node-api", "--node-dir", node_dir, "--method", method, "--path", path, "--timeout", str(timeout)]
        if body is not None:
            argv += ["--body-b64", b64(json.dumps(body))]
        self.cl.ready(n)
        _, out, _ = self.cl.host(n).module("90-hostctl", argv, stream=False, timeout=timeout + 60)
        res, _ = parse_output(out)
        if not isinstance(res, dict) or "status" not in res:
            raise TbError(f"[{n}] bad api answer for {method} {path}: {out[-500:]}")
        return int(res["status"]), res.get("body")

    def disabled_tables(self, hostname, db_id):
        """The tables taken out of the publication of the master database."""
        st, body = self.node_api(hostname, "master", "GET", f"/v1/databases/{db_id}/publication/tables")
        if st != 200:
            raise TbError(f"publication tables of {db_id} on {hostname}: {st} {json.dumps(body)[:300]}")
        return sorted(x["name"] for x in body.get("tables") or [] if x.get("disabled"))

    def publication_list(self, hostname, db_id):
        """disabled_tables, or None when the node gives no list (HQbird
        2.5/3.0, no db_id)."""
        if not db_id:
            return None
        try:
            return self.disabled_tables(hostname, db_id)
        except TbError:
            return None

    def set_disabled_tables(self, hostname, db_id, names):
        st, body = self.node_api(hostname, "master", "PUT", f"/v1/databases/{db_id}/publication/tables",
                                 {"disabled": list(names)})
        if st != 200:
            raise TbError(f"set publication tables of {db_id} on {hostname}: {st} {json.dumps(body)[:300]}")

    def firebird_signal(self, hostname, sig):
        """kill -<sig> the Firebird server process of a service host (the one
        under hosts.<h>.firebird.root, not fbguard)."""
        n = self.tb(hostname)
        exe = posixpath.join(self.cl.h(n)["firebird"]["root"], "bin", "firebird")
        cmd = (f"sh -c 'n=0; for p in $(pgrep -x firebird); do "
               f"[ \"$(readlink /proc/$p/exe)\" = {exe} ] && kill -{sig} $p && n=$((n+1)); done; [ $n -gt 0 ]'")
        rc, out, err = self.cl.host(n).run_raw(cmd, check=False, timeout=60)
        if rc != 0:
            raise TbError(f"kill -{sig} firebird on {hostname}: rc {rc}: {(out + err)[-200:]}")

    # --- hosts --------------------------------------------------------------
    def tb(self, hostname):
        return self.by_hostname[hostname]

    def block(self, hostname, peers, undo=False):
        n = self.tb(hostname)
        for p in peers:
            self.cl.hostctl(n, "unblock-peer" if undo else "block-peer", {"addr": self.cl.h(p)["addr"]},
                            check=not undo)

    def power(self, hostname, what):
        cmd = self.cl.h(self.tb(hostname))["power"][what]
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
        if p.returncode != 0:
            raise TbError(f"power {what} of {hostname}: rc {p.returncode}: {(p.stdout + p.stderr)[-300:]}")

    # --- load ---------------------------------------------------------------
    def load_start(self):
        s = self.service()
        port = s.get("router_port")
        if not port:
            raise TbError(f"service {self.svc_id} has no router_port: no router to load through")
        dsn = f"{self.cl.h(self.load_host)['addr']}/{port}:{s.get('alias') or self.svc_id}"
        self.cl.load_stop(TAG)
        self.cl.module(self.load_host, "50-load", "start",
                       {"dsn": dsn, "mode": "write", "conns": "2:4", "think_ms": 50, "extended": False,
                        "tag": TAG})
        time.sleep(20)
        self.load_total0 = self.load_total()

    def load_total(self):
        _, _, _, out = self.cl.module(self.load_host, "50-load", "status", {"tag": TAG}, check=False)
        m = re.findall(r"Total: (\d+)", out or "")
        return int(m[-1]) if m else 0

    def load_stop(self):
        self.cl.module(self.load_host, "50-load", "stop", {"tag": TAG}, check=False)

    def load_moves(self, timeout=90):
        """The load commits again: its total grows within timeout."""
        t0 = self.load_total()
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(10)
            if self.load_total() > t0:
                return True
        return False


def op_summary(doc):
    steps = {s["id"]: s for s in (doc or {}).get("steps", [])}
    return steps, (doc or {}).get("status"), (doc or {}).get("epoch")


# ------------------------------------------------------------------ cases --
def case_switch(t, a, res):
    before = set(t.orphaned()["replicas"])
    for i in range(1, a.rounds + 1):
        frm = t.master_host()
        to = t.other(frm)
        e0 = t.service()["epoch"]
        pub0 = t.publication_list(frm, t.where()["master"]["db_id"])
        t0 = time.time()
        rc, text, doc = t.rcm("switchover", "-service", t.svc_id, "-to", to)
        took = time.time() - t0
        steps, status, epoch = op_summary(doc)
        q = steps.get("quiesce", {})
        check(res, f"switch {i}: {frm} -> {to}", {
            "rc 0": rc == 0, "status ok": status == "ok", f"epoch {e0 + 1}": epoch == e0 + 1,
            "quiesce done": q.get("state") == "done",
        }, seconds=round(took), quiesce=q.get("detail", ""), error=(doc or {}).get("error", text[-300:] if rc else ""))
        if rc != 0 or status != "ok":
            return
        ok, w = t.wait_settled(to, e0 + 1, a.settle)
        check(res, f"switch {i}: routers and replica settled", {"settled": ok},
              where={"epoch": w.get("epoch"), "state": w.get("state"),
                     "routers": [(r.get("active"), r.get("epoch")) for r in w.get("routers") or []],
                     "replicas": [(r.get("host"), r.get("state")) for r in w.get("replicas") or []]})
        # The new master takes out of the publication what the old one did,
        # no more: its record from an earlier term keeps that term's list.
        pub1 = t.publication_list(to, (w.get("master") or {}).get("db_id") or "")
        if pub0 is not None and pub1 is not None:
            check(res, f"switch {i}: the new master has the publication list of the old one", {"same list": pub1 == pub0},
                  old=pub0, new=pub1)
        if not a.no_load:
            res.record(f"switch {i}: load commits again", "PASS" if t.load_moves() else "FAIL")
    # The rejoin forgets the donor's record (RCM rejoin_cleanup); before,
    # each switchover left one more. The old master's record stays ORPHANED:
    # it carries the generation the next switchover back continues, so each
    # master node keeps at most one. Replica records older than the case are
    # only listed: forgetdatabase removes them.
    left = t.orphaned()
    new = sorted(set(left["replicas"]) - before)
    per_node = {}
    for r in left["masters"]:
        node = r.rsplit(" on ", 1)[-1]
        per_node[node] = per_node.get(node, 0) + 1
    check(res, "switch: no new ORPHANED replica record, one old master record at most per node",
          {"no new replica record": not new, "one per master node": all(n <= 1 for n in per_node.values())},
          orphaned_new=new, orphaned_older=sorted(set(left["replicas"]) & before), old_masters=left["masters"])


def case_hung(t, a, res):
    host = t.master_host()
    e0 = t.service()["epoch"]
    stopped = False
    try:
        t.firebird_signal(host, "STOP")
        stopped = True
        t0 = time.time()
        s = t.wait_state("failed", a.hung_wait)
        check(res, f"hung: Firebird of {host} stopped -> the service fails", {"failed": s.get("state") == "failed"},
              seconds=round(time.time() - t0), state=s.get("state"), reason=s.get("state_reason", ""))
    finally:
        if stopped:
            t.firebird_signal(host, "CONT")
    # The node withdraws firebird_not_answering at its next answer (it tries
    # every minute); resume refuses until then.
    t0 = time.time()
    doc = None
    while time.time() - t0 < 240:
        _, _, doc = t.rcm("resume", "-service", t.svc_id, "-check")
        if doc and doc.get("ok"):
            break
        time.sleep(10)
    check(res, "hung: resume -check passes after SIGCONT", {"ok": bool(doc and doc.get("ok"))},
          seconds=round(time.time() - t0), problems=(doc or {}).get("problems"))
    rc, text, doc = t.rcm("resume", "-service", t.svc_id)
    check(res, "hung: resume", {"rc 0": rc == 0, "ok": bool(doc and doc.get("ok")), "to ok": (doc or {}).get("to") == "ok"},
          answer=doc or text[-300:])
    ok, w = t.wait_settled(host, e0, a.settle)
    check(res, f"hung: service ok, master {host} at epoch {e0}", {"settled": ok},
          where={"epoch": w.get("epoch"), "state": w.get("state"),
                 "replicas": [(r.get("host"), r.get("state")) for r in w.get("replicas") or []]})
    if not a.no_load:
        res.record("hung: load commits again", "PASS" if t.load_moves(120) else "FAIL")


def case_failover(t, a, res):
    lost = t.master_host()
    power = t.cl.h(t.tb(lost))["power"]
    if not power.get("off") or not power.get("on"):
        res.record("failover", "SKIP", note=f"hosts.{t.tb(lost)}.power.off/on are not set")
        return
    to = t.other(lost)
    e0 = t.service()["epoch"]
    was_policy = None
    if a.fence_mode:
        was_policy = dict(t.policy().get("policy") or {})
        t.set_policy(dict(was_policy, fence_mode=a.fence_mode))
    try:
        failover_body(t, a, res, lost, to, e0)
    finally:
        if was_policy is not None:
            t.set_policy(was_policy)


def failover_body(t, a, res, lost, to, e0):
    before = None
    if a.disable_table:
        db0 = t.where()["master"]["db_id"]
        before = t.disabled_tables(lost, db0)
        t.set_disabled_tables(lost, db0, sorted(set(before) | {a.disable_table}))
        # Two stats pushes (30 s each): RCM keeps the master's last list.
        time.sleep(70)
    try:
        failover_steps(t, a, res, lost, to, e0)
    finally:
        if before is not None:
            # Put the list back on whichever host is the master now.
            try:
                w = t.where()
                t.set_disabled_tables(w["master"]["host"], w["master"]["db_id"], before)
            except TbError as e:
                res.record("failover: publication tables put back", "FAIL", note=str(e)[:300])


def failover_steps(t, a, res, lost, to, e0):
    powered_off = False
    doc = None
    op_id = ""
    try:
        t.power(lost, "off")
        powered_off = True
        t0 = time.time()
        doc = None
        while time.time() - t0 < 300:
            _, _, doc = t.rcm("failover", "-service", t.svc_id, "-check")
            if doc and doc.get("ok"):
                break
            time.sleep(10)
        check(res, "failover: check passes after the power-off", {"ok": bool(doc and doc.get("ok"))},
              seconds=round(time.time() - t0), data_loss=(doc or {}).get("data_loss"),
              problems=(doc or {}).get("problems"))
        rc, text, doc = t.rcm("failover", "-service", t.svc_id, "-to", to, "-accept-data-loss")
        steps, status, epoch = op_summary(doc)
        check(res, f"failover: {lost} lost -> {to}", {"rc 0": rc == 0, "status ok": status == "ok",
                                                      f"epoch {e0 + 1}": epoch == e0 + 1},
              seconds_since_power_off=round(time.time() - t0),
              error=(doc or {}).get("error", text[-300:] if rc else ""))
        op_id = (doc or {}).get("operation_id", "")
        if a.disable_table and status == "ok":
            detail = steps.get("tables", {}).get("detail", "")
            w = t.where()
            now = t.disabled_tables(to, w["master"]["db_id"])
            check(res, f"failover: {a.disable_table} stays out of the publication on {to}", {
                "taken from the old master's last stats": "its last stats" in detail,
                f"{a.disable_table} disabled on the new master": a.disable_table in now,
            }, tables_step=detail, disabled_now=now)
        if not a.no_load:
            res.record("failover: load commits again", "PASS" if t.load_moves(120) else "FAIL")
    finally:
        if powered_off:
            t.power(lost, "on")
    fenced = t.wait_fenced(300)
    warns = ""
    if op_id:
        _, _, op = t.rcm("operations", "show", "-id", op_id)
        warns = "\n".join((op or {}).get("warnings") or [])
    conds = {"fenced": fenced, "fence in the operation's warnings": f"fenced at epoch {e0 + 1}" in warns}
    if a.fence_mode == "shutdown":
        conds["the database was shut down first"] = "database shut down" in warns
    check(res, "failover: the old master is fenced at its first answer", conds,
          fence=[x for x in warns.splitlines() if "fenced at epoch" in x][-1:])
    rejoin(t, a, res, "failover", to, e0 + 1)


def rejoin(t, a, res, case, master, epoch):
    rc, text, doc = t.rcm("rejoin", "-service", t.svc_id)
    _, status, _ = op_summary(doc)
    check(res, f"{case}: rejoin", {"rc 0": rc == 0, "status ok": status == "ok"},
          error=(doc or {}).get("error", text[-300:] if rc else ""))
    ok, w = t.wait_settled(master, epoch, a.settle)
    check(res, f"{case}: service ok after the rejoin", {"settled": ok},
          where={"epoch": w.get("epoch"), "state": w.get("state"),
                 "replicas": [(r.get("host"), r.get("state")) for r in w.get("replicas") or []]})


def with_auto(t, fn, **changes):
    """Runs fn with policy.auto_failover on (and the policy values in
    changes) and puts the old policy back."""
    was = dict(t.policy().get("policy") or {})
    t.set_policy(dict(was, auto_failover=True, **changes))
    try:
        fn()
    finally:
        t.set_policy(was)


def case_rcmcut(t, a, res):
    s0 = t.service()
    rcm_host = t.cl.cfg.rcm_host
    peers = [t.tb(h) for h in t.hosts if t.tb(h) != rcm_host]

    def body():
        try:
            for p in peers:
                t.cl.hostctl(rcm_host, "block-peer", {"addr": t.cl.h(p)["addr"]})
            time.sleep(a.cut)
        finally:
            for p in peers:
                t.cl.hostctl(rcm_host, "unblock-peer", {"addr": t.cl.h(p)["addr"]}, check=False)
        time.sleep(60)
    with_auto(t, body)
    s = t.service()
    check(res, f"rcmcut: no failover after {a.cut} s without the service hosts",
          {f"epoch {s0['epoch']}": s.get("epoch") == s0["epoch"], "state ok": s.get("state") == "ok"},
          epoch=s.get("epoch"), state=s.get("state"))


def case_rcmrestart(t, a, res):
    s0 = t.service()

    def body():
        rc, _, err = t.cl.host(t.cl.cfg.rcm_host).run_raw("systemctl restart hqbirdrcm", check=False, timeout=120)
        if rc != 0:
            raise TbError(f"restart hqbirdrcm: rc {rc}: {err[-200:]}")
        time.sleep(a.cut)
    with_auto(t, body)
    s = t.service()
    check(res, "rcmrestart: no failover after an RCM restart",
          {f"epoch {s0['epoch']}": s.get("epoch") == s0["epoch"], "state ok": s.get("state") == "ok"},
          epoch=s.get("epoch"), state=s.get("state"))


def case_mastercut(t, a, res):
    lost = t.master_host()
    to = t.other(lost)
    e0 = t.service()["epoch"]
    lost_n = t.tb(lost)
    peers = [n for n in t.cl.cfg.raw["hosts"] if n != lost_n]
    delay = int(t.policy().get("policy", {}).get("failover_delay_sec") or 90)
    out = {}

    def body():
        t0 = time.time()
        try:
            t.block(lost, peers)
            while time.time() - t0 < delay + 300:
                s = t.service()
                if s.get("epoch") == e0 + 1 and s.get("state") != "failing_over":
                    break
                time.sleep(10)
            out["seconds"] = round(time.time() - t0)
        finally:
            t.block(lost, peers, undo=True)
        p = t.policy()
        out["auto_after"] = p.get("policy", {}).get("auto_failover")
        out["last"] = p.get("last_auto_failover") or {}
    # A short min_interval_sec: an earlier run of this case may have failed
    # over less than the default hour ago.
    with_auto(t, body, min_interval_sec=60)
    s = t.service()
    check(res, f"mastercut: automatic failover {lost} -> {to}", {
        f"epoch {e0 + 1}": s.get("epoch") == e0 + 1, f"master {to}": s.get("master", {}).get("host") == to,
        "the rule turned itself off": out.get("auto_after") is False,
        "last_auto_failover recorded": bool(out.get("last")),
    }, seconds=out.get("seconds"), last_auto_failover=out.get("last"))
    if s.get("epoch") != e0 + 1:
        return
    if not a.no_load:
        res.record("mastercut: load commits again", "PASS" if t.load_moves(120) else "FAIL")
    check(res, "mastercut: the old master is fenced at its first answer", {"fenced": t.wait_fenced(300)})
    rejoin(t, a, res, "mastercut", to, e0 + 1)
