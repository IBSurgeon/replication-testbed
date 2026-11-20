"""Switchover keep-replicas (hqcluster-node
docs/switchover-keep-replicas-plan.ru.md, stage 3): at a planned switchover
A -> B the third replica C of an RCM service is re-bound in place to B (node
POST /v1/databases/{id}/reseat, protocol minor 28; RCM step reseat_replicas)
instead of a reinit. Needs a stand made by 'tb.py rcm up --easy' (a master
and a replica node on every host, named <host>-master and <host>-replica)
with three hosts, node minor 28 and an RCM with the step reseat_replicas;
the case 'setup' makes the service. Linux hosts only.

  setup     the service --service of the three hosts. The database is
            db1 of 'tb.py dbs prepare --count 1 --no-seed' on the config's
            master; a reinit puts it at the same path on the replica node of
            the two other hosts (db_id comes from the path, and reseat needs
            C's db_id = B's); the links master -> replica between every two
            hosts; the service in RCM. Skipped when the service exists
  cycle     --rounds switchovers A->B->C->A under load (fb-loadgen on
            --load-host, straight to the master's Firebird; it follows the
            master). After each: the operation ok at the next epoch, step
            reseat_replicas done, the third replica of class ready and
            re-bound, no reinit of it in its node's file audit, its control
            file names the new master's GUID and it applied segments of it;
            with the load stopped all three files are equal (row count and a
            hash of every column of every keyed table)
  behind    the shipping old master -> C is paused before the switchover, so
            C is behind S at 'apply': class behind, the switchover ok, RCM
            reinits C after the operation; files equal
  seg1      segment 1 of the new master's GUID is deleted on B before B
            ships it: B does not ship 2 in its place (the pair halts with a
            gap), C applies nothing of the new GUID (control file G2:0) and is
            never IN_SYNC with other data; a reinit B -> C; files equal
  kill      C's node is killed at each point of --kill-points while it
            re-binds (systemd starts it again): the switchover ok; RCM's
            repeat resumes by the witness and C ends re-bound with no
            reinit; after nbackup -F only forward (no new -L or -N); never
            IN_SYNC with other data; files equal
  noagent   fbagent on C is stopped: reseat refused with fbagent_required,
            class failed; fbagent started again; RCM reinits C; files equal
  nolink    the link B -> C is taken out of the cluster: class unknown; the
            link back; RCM reinits C; files equal

Each negative case is one switchover to the next host of the cycle; C is the
host that is neither the master nor the target. When RCM does not bring C
back within --recover-wait, the case fails and the test reinits C itself
(B -> C) so that the next case starts from a working service."""
import base64
import json
import os
import posixpath
import re
import tempfile
import threading
import time

from tblib import rcmstand as RS
from tblib.cluster import TbError, log
from tblib.results import Results

from . import switchover as SW

HELP = "switchover keeps the third replica: reseat in place, negative cases (RCM service of three hosts, under load)"

CASES = ["setup", "cycle", "behind", "seg1", "kill", "noagent", "nolink"]
TAG = "reseat"
# Where to kill C's node, with the line that marks the point in its state
# journal (journal.jsonl) or its file audit (db-file-ops.jsonl).
KILL_POINTS = {
    "witness": ('"type":"reseat"', '"fixed":false'),   # the witness is written, nothing changed yet
    "lock": ('"op":"nbackup_lock"', ""),                # after nbackup -L, before -F: rolls back
    "fixup": ('"op":"nbackup_fixup"', ""),              # after nbackup -F: only forward
    "fixed": ('"type":"reseat"', '"fixed":true'),       # the header checked, before -replica read_only
    "control": ('"reason":"reseat_control"', ""),        # the mailbox purged, control file G2:0 written
    "conf": ('"reason":"reseat_conf_update"', ""),       # source_guid written, before the state reset
}
RECOVER_CLASSES = ("behind", "ahead", "unknown", "failed")

DIGEST_SQL = """set heading off;
set transaction read only;
set term ^;
execute block returns (r varchar(160)) as
  declare t varchar(63);
  declare f varchar(63);
  declare k smallint;
  declare e varchar(16000);
  declare n bigint;
  declare h bigint;
begin
  for select trim(rl.rdb$relation_name),
             iif(exists(select 1 from rdb$indices i where i.rdb$relation_name = rl.rdb$relation_name
                        and i.rdb$unique_flag = 1 and coalesce(i.rdb$index_inactive, 0) = 0), 1, 0)
      from rdb$relations rl
      where coalesce(rl.rdb$system_flag, 0) = 0 and rl.rdb$view_blr is null
        and coalesce(rl.rdb$relation_type, 0) = 0
      order by 1 into :t, :k do
  begin
    e = '0';
    for select trim(rf.rdb$field_name) from rdb$relation_fields rf
          join rdb$fields fd on fd.rdb$field_name = rf.rdb$field_source
        where rf.rdb$relation_name = :t and fd.rdb$computed_blr is null
          and fd.rdb$field_type <> 261 and fd.rdb$dimensions is null
        order by rf.rdb$field_position into :f do
      e = e || ' + coalesce(mod(hash("' || f || '"), 1000003), 7)';
    execute statement 'select count(*), coalesce(sum(' || e || '), 0) from "' || t || '"' into :n, :h;
    r = 'DG|' || t || '|' || k || '|' || n || '|' || h;
    suspend;
  end
end^
set term ;^
"""

WATCH_KILL = r"""#!/bin/sh
# tb reseat: kill the node at a point of its re-bind.
# $1 journal  $2 file audit  $3 first mark  $4 second mark (or empty)  $5 pkill pattern
tail -n0 -F "$1" "$2" 2>/dev/null | while IFS= read -r l; do
  printf '%s\n' "$l" | grep -qF -- "$3" || continue
  [ -z "$4" ] || printf '%s\n' "$l" | grep -qF -- "$4" || continue
  pkill -9 -f "$5"
  echo "$(date +%s) $l" > /tmp/tb-reseat-watch.hit
  break
done
"""

WATCH_SEG1 = r"""#!/bin/sh
# tb reseat: delete segment 1 of the new master's GUID from its archive.
# $1 replication.conf  $2 database path  $3 seconds to give up
T0=$(date +%s); END=$((T0 + $3))
while [ "$(date +%s)" -lt "$END" ]; do
  D=$(awk -v db="$2" '{gsub(/"/, "")} $1 == "database" {cur = $3} $1 == "journal_archive_directory" && cur == db {print $3; exit}' "$1")
  if [ -n "$D" ] && [ -d "$D" ]; then
    for f in "$D"/*.journal-000000001; do
      [ -e "$f" ] || continue
      [ "$(stat -c %Y "$f")" -ge "$T0" ] || continue
      rm -f "$f" && echo "$(date +%s) $f" > /tmp/tb-reseat-seg1.hit && exit 0
    done
  fi
  sleep 0.2
done
echo "$(date +%s) gave up" > /tmp/tb-reseat-seg1.hit
"""


def add_args(p):
    p.add_argument("--only", default="", help="comma list of: " + ",".join(CASES) + " (default all but setup)")
    p.add_argument("--service", default="st3", help="RCM service id (setup makes it)")
    p.add_argument("--rounds", type=int, default=40, help="cycle: number of switchovers")
    p.add_argument("--third-wait", type=int, default=0,
                   help="switchover -third-replica-wait in seconds (0: RCM's default, 60)")
    p.add_argument("--kill-points", default=",".join(KILL_POINTS),
                   help="kill: comma list of " + ",".join(KILL_POINTS))
    p.add_argument("--settle", type=int, default=600, help="seconds for the three files to become equal")
    p.add_argument("--recover-wait", type=int, default=900,
                   help="seconds RCM gets to bring C back (reinit or reseat) before the test reinits it")
    p.add_argument("--load-sec", type=int, default=30,
                   help="seconds of load on the new master after each switchover (it makes segments of the new GUID)")
    p.add_argument("--behind-sec", type=int, default=30, help="behind: seconds of load while C's pair is paused")
    p.add_argument("--watch", type=int, default=120, help="seg1: seconds to watch C before the reinit")
    p.add_argument("--load-host", default="", help="host that runs fb-loadgen (default: the RCM host)")
    p.add_argument("--no-load", action="store_true", help="run without load")


def run(cl, a):
    res = Results("reseat", vars(a).copy())
    only = [x.strip() for x in a.only.split(",") if x.strip()] or [c for c in CASES if c != "setup"]
    unknown = set(only) - set(CASES)
    if unknown:
        raise TbError(f"unknown case(s): {', '.join(sorted(unknown))}")
    bad = [p for p in a.kill_points.split(",") if p.strip() and p.strip() not in KILL_POINTS]
    if bad:
        raise TbError(f"unknown kill point(s): {', '.join(bad)}")
    t = Svc(cl, a)
    if "setup" in only:
        try:
            case_setup(t, a, res)
        except TbError as e:
            res.record("setup", "FAIL", note=str(e)[:400])
            return res.finish()
    t.attach()
    try:
        for name in CASES:
            if name == "setup" or name not in only:
                continue
            log(f"reseat: {name}")
            try:
                globals()["case_" + name](t, a, res)
            except TbError as e:
                res.record(name, "FAIL", note=str(e)[:400])
    finally:
        t.load_stop()
    return res.finish()


def check(res, case, conds, **details):
    return SW.check(res, case, conds, **details)


def norm_guid(g):
    return (g or "").strip().strip("{}").upper()


class Svc(SW.Stand):
    """The service of three hosts. Hosts are named by their host names, as
    RCM names them; the test bed name of each is self.tb(hostname)."""

    def __init__(self, cl, a):  # noqa: D401 - no call to Stand.__init__: it wants two hosts
        self.cl = cl
        self.a = a
        self.svc_id = a.service
        self.by_hostname = {RS.hostname(cl, n): n for n in cl.cfg.raw["hosts"]}
        by_name = {v: k for k, v in self.by_hostname.items()}
        order = [cl.cfg.master] + list(cl.cfg.replicas)
        self.hosts = [by_name[n] for n in order]
        if len(self.hosts) != 3:
            raise TbError(f"this test needs three hosts (config master + two replicas), not {len(self.hosts)}")
        self.load_host = a.load_host or cl.cfg.rcm_host
        self.load_total0 = 0
        self.load_on = ""
        self.path = ""
        self.db = ""
        self.last_op = ""
        self._files = {}

    def attach(self):
        """The service's path and db_id, from RCM."""
        s = self.service()
        paths = {m.get("path") for m in s.get("members", [])}
        hosts = [m["host"] for m in s.get("members", [])]
        if len(paths) != 1 or sorted(hosts) != sorted(self.hosts):
            raise TbError(f"service {self.svc_id}: needs the three hosts {self.hosts} on one path; has {hosts} {sorted(paths)}")
        self.path = paths.pop()
        self.db = self.where()["master"]["db_id"]

    # --- names ------------------------------------------------------------
    @staticmethod
    def node_id(host, role):
        return f"{host}-{role}"

    def next_host(self, host):
        return self.hosts[(self.hosts.index(host) + 1) % 3]

    def third(self, frm, to):
        return [h for h in self.hosts if h not in (frm, to)][0]

    def addr(self, host):
        return self.cl.h(self.tb(host))["addr"]

    def raw(self, host, cmd, timeout=120, check=True):
        rc, out, err = self.cl.host(self.tb(host)).run_raw(cmd, check=False, timeout=timeout)
        if check and rc != 0:
            raise TbError(f"[{host}] rc {rc}: {(out + err)[-300:]}")
        return rc, out or ""

    def put_script(self, host, name, text):
        b = base64.b64encode(text.encode()).decode()
        self.raw(host, f"echo {b} | base64 -d > /tmp/{name} && chmod 755 /tmp/{name}")
        return f"/tmp/{name}"

    # --- node ---------------------------------------------------------------
    def node(self, host, role, method, path, body=None, timeout=60):
        return self.node_api(host, role, method, path, body, timeout)

    def record(self, host, role="replica"):
        st, body = self.node(host, role, "GET", f"/v1/databases/{self.db}")
        return body if st == 200 and isinstance(body, dict) else {}

    def applied(self, host):
        st, body = self.node(host, "replica", "GET", f"/v1/databases/{self.db}/applied?fresh=1")
        return body if st == 200 and isinstance(body, dict) else {}

    def alerts(self, host, role):
        st, body = self.node(host, role, "GET", "/v1/alerts")
        if st != 200:
            return []
        items = body.get("alerts", body) if isinstance(body, dict) else body
        return items if isinstance(items, list) else []

    def node_files(self, host, role):
        """journal.jsonl and db-file-ops.jsonl of a node."""
        key = (host, role)
        if key not in self._files:
            nd = self.node_dir(host, role)
            _, out = self.raw(host, f"find {nd} -name journal.jsonl -o -name db-file-ops.jsonl | sort")
            got = {posixpath.basename(x): x for x in out.split()}
            if "journal.jsonl" not in got or "db-file-ops.jsonl" not in got:
                raise TbError(f"[{host}] {role} node: no journal.jsonl or db-file-ops.jsonl under {nd}: {out[-200:]}")
            self._files[key] = (got["journal.jsonl"], got["db-file-ops.jsonl"])
        return self._files[key]

    def node_dir(self, host, role):
        n = self.tb(host)
        return posixpath.join(posixpath.dirname(self.cl.h(n)["paths"]["node"].rstrip("/")), role)

    def audit_since(self, host, role, t0):
        """File audit lines of a node written at or after t0 (epoch seconds)."""
        _, ops = self.node_files(host, role)
        _, out = self.raw(host, f"tail -n 2000 {ops}")
        lines = []
        for x in out.splitlines():
            try:
                d = json.loads(x)
            except ValueError:
                continue
            at = d.get("at", "")
            try:
                ts = time.mktime(time.strptime(at[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
            except ValueError:
                continue
            if ts >= t0 - 1 and d.get("db_id") in ("", None, self.db):
                lines.append(d)
        return lines

    @staticmethod
    def reinit_ops(lines):
        return [f"{d.get('op')}:{d.get('reason', '')}" for d in lines
                if "reinit" in (d.get("reason") or "") or d.get("op") in ("replace", "takeover")]

    def replog_len(self, host):
        root = self.cl.h(self.tb(host))["firebird"]["root"]
        _, out = self.raw(host, f"wc -l < {root}/replication.log 2>/dev/null || echo 0", check=False)
        try:
            return int(out.strip() or 0)
        except ValueError:
            return 0

    def replog_since(self, host, n0, pattern):
        root = self.cl.h(self.tb(host))["firebird"]["root"]
        _, out = self.raw(host, f"tail -n +{n0 + 1} {root}/replication.log 2>/dev/null | grep -E '{pattern}' | tail -5",
                          check=False)
        return [x for x in out.splitlines() if x.strip()]

    # --- data ---------------------------------------------------------------
    def digest(self, host):
        """{table: (keyed, rows, hash)} of the service file on host."""
        n = self.tb(host)
        r = self.cl.hostctl(n, "sql", {"db": self.path, "sql_b64": base64.b64encode(DIGEST_SQL.encode()).decode()},
                            check=False) or {}
        if not r.get("ok"):
            return None
        out = {}
        for line in (r.get("out") or "").splitlines():
            line = line.strip()
            if not line.startswith("DG|"):
                continue
            p = line.split("|")
            if len(p) == 5:
                out[p[1]] = (p[2] == "1", int(p[3]), int(p[4]))
        return out or None

    def files_equal(self, timeout, master):
        """Waits until the keyed tables of the three files are equal and the
        two replicas are in sync. Returns (ok, seconds, detail)."""
        t0 = time.time()
        detail = {}
        while True:
            dg = {h: self.digest(h) for h in self.hosts}
            m = dg.get(master)
            diff = {}
            if m is None:
                diff[master] = "no digest"
            for h in self.hosts:
                if h == master or m is None:
                    continue
                d = dg.get(h)
                if d is None:
                    diff[h] = "no digest"
                    continue
                bad = sorted(k for k, v in m.items() if v[0] and d.get(k) != v)
                if bad:
                    diff[h] = {k: {"master": m[k][1:], "replica": (d.get(k) or (0, 0, 0))[1:]} for k in bad[:4]}
            states = {h: self.record(h).get("state") for h in self.hosts if h != master}
            detail = {"differ": diff, "states": states,
                      "tables": len(m or {}), "rows": sum(v[1] for v in (m or {}).values())}
            if not diff and all(s in ("IN_SYNC", "LAGGING") for s in states.values()):
                return True, round(time.time() - t0), detail
            if time.time() - t0 > timeout:
                return False, round(time.time() - t0), detail
            time.sleep(15)

    # --- load ---------------------------------------------------------------
    def load_start(self, master=None):
        if self.a.no_load:
            return
        master = master or self.master_host()
        n = self.tb(master)
        dsn = f"{self.addr(master)}/{self.cl.fb_port(n)}:{self.path}"
        self.load_stop()
        self.cl.module(self.load_host, "50-load", "start",
                       {"dsn": dsn, "mode": "write", "conns": "2:4", "think_ms": 50, "extended": False, "tag": TAG})
        self.load_on = master
        time.sleep(10)
        self.load_total0 = self.load_total()

    def load_total(self):
        _, _, _, out = self.cl.module(self.load_host, "50-load", "status", {"tag": TAG}, check=False)
        m = re.findall(r"Total: (\d+)", out or "")
        return int(m[-1]) if m else 0

    def load_stop(self):
        if self.a.no_load:
            return
        self.cl.module(self.load_host, "50-load", "stop", {"tag": TAG}, check=False)
        self.load_on = ""

    # --- RCM ----------------------------------------------------------------
    def wait_ready(self, to, timeout=300):
        """Waits until 'switchover -check' to `to` passes: the replicas are
        IN_SYNC in RCM's poll snapshot. Returns the check answer."""
        end = time.time() + timeout
        while True:
            rc, text, doc = self.rcm("switchover", "-service", self.svc_id, "-to", to, "-check")
            if rc == 0 and (doc or {}).get("ok", True):
                return doc or {}
            if time.time() > end:
                raise TbError(f"switchover -check to {to} does not pass in {timeout} s: {text[-400:]}")
            time.sleep(15)

    def switch(self, to):
        """One switchover; returns (rc, operation document, seconds). The
        load writes to the old master until its quiesce starts, then stops:
        it goes to the master's address, not through a router, and would
        attach again to the old master's file while the rejoin turns it into
        a replica (gfix -replica: object in use)."""
        args = ["switchover", "-service", self.svc_id, "-to", to]
        if self.a.third_wait:
            args += ["-third-replica-wait", str(self.a.third_wait)]
        t0 = time.time()
        box = {}

        def call():
            box["r"] = self.rcm(*args)

        th = threading.Thread(target=call, daemon=True)
        th.start()
        while th.is_alive() and self.load_on:
            if self.quiesce_started(t0):
                self.load_stop()
                break
            th.join(2)
        th.join()
        rc, text, doc = box["r"]
        took = round(time.time() - t0)
        op_id = (doc or {}).get("operation_id", "") or (doc or {}).get("id", "")
        self.last_op = op_id
        if op_id:
            _, _, full = self.rcm("operations", "show", "-id", op_id)
            if full:
                doc = full
        if rc != 0 and not doc:
            doc = {"error": text[-400:]}
        return rc, doc or {}, took

    def quiesce_started(self, t0):
        """The running switchover of the service is at its quiesce step or later."""
        _, _, doc = self.rcm("operations", "list", "-service", self.svc_id)
        for op in (doc or {}).get("operations") or []:
            if op.get("status") not in ("running", "", None):
                continue
            for st in op.get("steps") or []:
                if st.get("id") == "quiesce" and st.get("state") in ("active", "done"):
                    return True
        return time.time() - t0 > 60

    def third_of(self, doc, host):
        """The operation's entry of the third replica on host."""
        items = (doc.get("thirds") or (doc.get("data") or {}).get("thirds") or [])
        names = {host, self.node_id(host, "replica")}
        for x in items:
            if names & {str(x.get(k, "")) for k in ("host", "node", "node_id", "replica_node")}:
                return x
        return {}

    def cluster_links(self, drop=None, add=None):
        doc = RS.rcm_json(self.cl, "cluster", "show")
        links = [dict(x) for x in doc.get("links") or []]
        if drop:
            links = [x for x in links if (x.get("from"), x.get("to")) != drop]
        if add and add not in [(x.get("from"), x.get("to")) for x in links]:
            links.append({"from": add[0], "to": add[1]})
        doc["links"] = links
        rc, text = RS.apply(self.cl, doc)
        if rc != 0:
            raise TbError(f"cluster apply: {text[-400:]}")

    def peers_of(self, host, role):
        st, body = self.node(host, role, "GET", "/v1/peers")
        items = body.get("peers", body) if isinstance(body, dict) else body
        return {str(p.get("node_id") or p.get("id") or "") for p in items or [] if isinstance(p, dict)}

    def wait_peer(self, host, role, peer, present, timeout=300):
        end = time.time() + timeout
        while time.time() < end:
            if (peer in self.peers_of(host, role)) == present:
                return True
            time.sleep(10)
        return False

    # --- C ------------------------------------------------------------------
    def reinit(self, frm, to, timeout=3600):
        """A reinit of the service file from frm's master node to to's
        replica node, through RCM: RCM clears its own marks of the replica
        (a "turned to normal" mark left by a failed switchover makes the next
        switchover skip turn_to_normal). Waits for the master node's job.
        Returns {state, phase, error, reinit_id, operation_id}."""
        master, replica = self.node_id(frm, "master"), self.node_id(to, "replica")
        st, job0 = self.node(frm, "master", "GET", f"/v1/databases/{self.db}/reinit")
        op0 = (job0 or {}).get("operation_id", "") if st == 200 else ""
        end = time.time() + 600
        while True:
            rc, text, doc = self.rcm("reinit", "-node", master, "-db", self.db, "-to", replica,
                                     "-mode", "standard", "-allow-restart")
            if rc == 0:
                break
            if time.time() > end:
                raise TbError(f"reinit {master} -> {replica}: {text[-400:]}")
            time.sleep(20)
        rid = (doc or {}).get("reinit_id", "")
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(10)
            st, job = self.node(frm, "master", "GET", f"/v1/databases/{self.db}/reinit")
            if st != 200 or not isinstance(job, dict) or job.get("operation_id", "") in ("", op0):
                continue
            if job.get("phase") in ("ready", "failed"):
                return {"state": "succeeded" if job["phase"] == "ready" else "failed", "phase": job["phase"],
                        "error": job.get("error", ""), "reinit_id": rid, "operation_id": job["operation_id"]}
        raise TbError(f"reinit {master} -> {replica} (RCM {rid}): no end in {timeout} s")

    def after_switch(self, to):
        """Load on the new master for --load-sec: segments of its GUID."""
        if self.a.no_load:
            return
        self.load_start(to)
        time.sleep(self.a.load_sec)

    def recover(self, res, case, b, c, t0, guid, expect="reinit"):
        """Waits for C to come back under B's GUID with equal data (RCM's
        reinit after the operation, or a re-bind); else reinits C itself.
        Records what happened. Never IN_SYNC with other data. expect
        'reinit': the plan wants RCM to reinit C after the operation (the
        third's result 'reinit' and a reinit in C's file audit)."""
        self.load_stop()
        end = time.time() + self.a.recover_wait
        silent = []
        ok = False
        while time.time() < end:
            rec = self.record(c)
            ap = self.applied(c)
            if rec.get("state") in ("IN_SYNC", "LAGGING"):
                dc, db_ = self.digest(c), self.digest(b)
                same = bool(dc and db_ and all(dc.get(k) == v for k, v in db_.items() if v[0]))
                if same and norm_guid(ap.get("control_guid")) == guid:
                    ok = True
                    break
                if not same and norm_guid(ap.get("control_guid")) == guid:
                    silent.append({"at": round(time.time() - t0), "state": rec.get("state")})
            time.sleep(15)
        how = self.reinit_ops(self.audit_since(c, "replica", t0))
        th = {}
        if self.last_op:
            _, _, d = self.rcm("operations", "show", "-id", self.last_op)
            th = self.third_of(d or {}, c)
            save_doc(res, case + " after", d or {})
        check(res, f"{case}: no silent divergence (C never IN_SYNC under the new GUID with other data)",
              {"never": not silent}, seen=silent[:3])
        conds = {"back": ok}
        if expect == "reinit":
            conds["RCM reinit after the operation"] = th.get("result") == "reinit" and bool(how)
        elif expect == "reinit_audit":
            conds["a reinit of C in its file audit"] = bool(how)
        check(res, f"{case}: RCM brings C back within {self.a.recover_wait} s", conds,
              how="reinit " + ",".join(how) if how else "re-bind (no reinit in the audit)", third=th,
              state=self.record(c).get("state"), alerts=[x.get("code") for x in self.alerts(c, "replica")][:6])
        if not ok:
            try:
                op = self.reinit(b, c)
                okr, secs, det = self.files_equal(self.a.settle, b)
                check(res, f"{case}: the test's reinit {b} -> {c} brings C back", {
                    "reinit done": op.get("state") == "succeeded", "files equal": okr},
                    seconds=secs, op_state=op.get("state"), **det)
            except TbError as e:
                res.record(f"{case}: the test's reinit {b} -> {c}", "FAIL", note=str(e)[:300])
                raise
        else:
            okr, secs, det = self.files_equal(self.a.settle, b)
            check(res, f"{case}: three files equal", {"equal": okr}, seconds=secs, **det)


# ------------------------------------------------------------------ setup --
def case_setup(t, a, res):
    rc, text, doc = t.rcm("services", "show", "-id", t.svc_id)
    if rc == 0 and doc and doc.get("members"):
        res.record("setup", "SKIP", note=f"service {t.svc_id} exists")
        return
    a_host = t.hosts[0]
    a_tb = t.tb(a_host)
    dbs = t.cl.test_dbs(which="db1")
    if not dbs:
        # An easy-install node has no databases.root: enroll the folder of
        # 'dbs prepare' on the master node, then restart Firebird and turn
        # the publication on, as 'dbs prepare' does on other stands.
        root = posixpath.dirname(t.cl.master_dbs_dir(None).rstrip("/"))
        rc, text, _ = t.rcm("enroll-databases", "-node", t.node_id(a_host, "master"), "-root", root)
        if rc != 0:
            raise TbError(f"enroll-databases {root}: {text[-300:]}")
        end = time.time() + 180
        while not dbs and time.time() < end:
            time.sleep(10)
            dbs = t.cl.test_dbs(which="db1")
        if not dbs:
            raise TbError("no db1 on the master: run 'tb.py dbs prepare --count 1 --no-seed' first")
        from tblib import ops
        try:
            ops.restart_firebird(t.cl, a_tb, "test bed: reseat setup")
        except TbError as e:
            log(f"restart: {e}")
        st, out = t.cl.api(a_tb, "POST", "/v1/publication/sync", {})
        res.record("setup: db1 enrolled on the master node", "PASS", root=root, publication=st)
    t.path, t.db = dbs[0]["path"], dbs[0]["db_id"]
    # The replica node of every host scans the master node's folder (RCM's
    # exclude_donor needs a databases.root on the donor); the one on the
    # master host leaves out the master's file, as after a promote.
    st, mc = t.node(a_host, "master", "GET", "/v1/config")
    root = (((mc or {}).get("config") or {}).get("databases") or {}).get("root", "")
    if not root:
        raise TbError(f"{t.node_id(a_host, 'master')} has no databases.root")
    rel = posixpath.relpath(t.path, root)
    for h in t.hosts:
        body = {"root": root, "recursive": True, "template": "*.fdb"}
        if h == a_host:
            body["exclude_paths"] = [rel]
        st, out = t.node(h, "replica", "PUT", "/v1/config", {"databases": body})
        if st != 200:
            raise TbError(f"{t.node_id(h, 'replica')}: PUT /v1/config: {st} {json.dumps(out)[:300]}")
    res.record("setup: databases.root of the replica nodes", "PASS", root=root, excluded_on=a_host)
    # Every master node ships to the replica node of every other host.
    want = [(t.node_id(f, "master"), t.node_id(o, "replica")) for f in t.hosts for o in t.hosts if f != o]
    doc = RS.rcm_json(t.cl, "cluster", "show")
    have = {(x.get("from"), x.get("to")) for x in doc.get("links") or []}
    missing = [x for x in want if x not in have]
    if missing:
        doc["links"] = list(doc.get("links") or []) + [{"from": f, "to": o} for f, o in missing]
        rc, text = RS.apply(t.cl, doc)
        if rc != 0:
            raise TbError(f"cluster apply: {text[-400:]}")
    for f, o in want:
        host = f.rsplit("-", 1)[0]
        if not t.wait_peer(host, "master", o, True):
            raise TbError(f"{f} does not list {o} as a peer after the link")
    res.record("setup: links master -> replica between every two hosts", "PASS", added=len(missing))
    # The same folders on the other hosts, from the root down, owned as on
    # the master (the replica node probes that it may write in the root).
    d = posixpath.dirname(t.path)
    dirs = [root]
    for part in posixpath.relpath(d, root).split("/"):
        dirs.append(posixpath.join(dirs[-1], part))
    for x in dirs:
        _, own = t.raw(a_host, f"stat -c '%U:%G %a' {x}")
        owner, mode = own.split()
        for h in t.hosts[1:]:
            t.raw(h, f"mkdir -p {x} && chown {owner} {x} && chmod {mode} {x}")
    for h in t.hosts[1:]:
        op = t.reinit(a_host, h)
        check(res, f"setup: reinit {a_host} -> {h} at {t.path}", {"done": op.get("state") == "succeeded"},
              state=op.get("state"), error=op.get("error", ""))
    svc = {"alias": t.svc_id, "members": [
        {"host": h, "master_node": t.node_id(h, "master"), "replica_node": t.node_id(h, "replica"),
         "path": t.path, "priority": 100 - 10 * i} for i, h in enumerate(t.hosts)],
        "policy": {"auto_failover": False, "max_data_loss_sec": 30, "failover_delay_sec": 90,
                   "min_interval_sec": 3600, "fence_mode": "route"}}
    fd, tmp = tempfile.mkstemp(prefix="tb-svc-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(svc, f, indent=2)
        remote = RS.put_file(t.cl, t.cl.cfg.rcm_host, tmp)
    finally:
        os.remove(tmp)
    rc, text, _ = t.rcm("services", "put", "-id", t.svc_id, "-file", remote)
    check(res, f"setup: service {t.svc_id} in RCM", {"rc 0": rc == 0}, out=text[-300:] if rc else "")


# ------------------------------------------------------------------ cycle --
def save_doc(res, case, doc):
    """The whole operation document next to results.json."""
    slug = re.sub(r"[^a-z0-9]+", "-", case.lower()).strip("-")
    with open(os.path.join(res.dir, f"op-{slug}.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)


def one_switch(t, res, case, expect_class):
    """A switchover to the next host, then --load-sec of load on it. Returns
    (frm, to, c, doc, t0, n0, guid) or None when the operation failed
    (recorded)."""
    frm = t.master_host()
    to = t.next_host(frm)
    c = t.third(frm, to)
    e0 = t.service()["epoch"]
    n0 = t.replog_len(c)
    t0 = time.time()
    rc, doc, took = t.switch(to)
    steps, status, epoch = SW.op_summary(doc)
    step = steps.get("reseat_replicas", {})
    th = t.third_of(doc, c)
    save_doc(res, case, doc)
    conds = {"rc 0": rc == 0, "status ok": status == "ok", f"epoch {e0 + 1}": epoch == e0 + 1,
             "step reseat_replicas done": step.get("state") == "done"}
    if expect_class:
        conds[f"class {expect_class}"] = th.get("class") == expect_class
    check(res, f"{case}: {frm} -> {to}, third {c}", conds, seconds=took, third=th,
          reseat_step=str(step.get("detail", ""))[:300], op=t.last_op,
          not_done=[k for k, v in steps.items() if v.get("state") != "done"],
          error=doc.get("error", "") if rc or status != "ok" else "", warnings=(doc.get("warnings") or [])[-4:])
    if rc != 0 or status != "ok":
        return None
    guid = norm_guid(t.where()["master"].get("guid"))
    t.after_switch(to)
    return frm, to, c, doc, t0, n0, guid


def case_cycle(t, a, res):
    for i in range(1, a.rounds + 1):
        t.load_start()
        t.wait_ready(t.next_host(t.master_host()))
        got = one_switch(t, res, f"cycle {i}", "ready")
        if not got:
            return
        frm, to, c, doc, t0, n0, guid = got
        th = t.third_of(doc, c)
        moved = a.no_load or t.load_total() > 0
        rec = t.record(c)
        ap = t.applied(c)
        reinits = t.reinit_ops(t.audit_since(c, "replica", t0))
        check(res, f"cycle {i}: {c} re-bound in place, no reinit", {
            "result done": th.get("result") in ("done", "reseated") and not th.get("error"),
            "no reinit in the audit": not reinits,
            "control file of the new GUID": norm_guid(ap.get("control_guid")) == guid,
            "load commits on the new master": moved,
        }, reinits=reinits, generation=th.get("generation"), state=rec.get("state"),
            control=(ap.get("control_guid"), ap.get("control_sequence"), ap.get("control_offset")))
        t.load_stop()
        ok, secs, det = t.files_equal(a.settle, to)
        ap = t.applied(c)
        missing = t.replog_since(c, n0, "Required segment|is missing")
        check(res, f"cycle {i}: three files equal, {c} applied the new GUID from segment 1", {
            "equal": ok,
            "applied segments of the new GUID": norm_guid(ap.get("control_guid")) == guid
            and (ap.get("applied_sequence") or 0) >= 1,
            "no missing segment in replication.log": not missing,
        }, seconds=secs, applied=ap.get("applied_sequence"), replication_log=missing, **det)
        if not ok:
            return


# --------------------------------------------------------------- negatives --
def case_behind(t, a, res):
    frm = t.master_host()
    to = t.next_host(frm)
    c = t.third(frm, to)
    t.load_start(frm)
    t.wait_ready(to)
    body = {"database": t.db, "peer": t.node_id(c, "replica")}
    st, out = t.node(frm, "master", "POST", "/v1/shipping/pause", body)
    if st != 200:
        raise TbError(f"pause {frm} -> {c}: {st} {json.dumps(out)[:300]}")
    try:
        time.sleep(a.behind_sec)
        got = one_switch(t, res, "behind", "behind")
    finally:
        t.node(frm, "master", "POST", "/v1/shipping/resume", body)
    if got:
        _, to, c, doc, t0, _, guid = got
        t.recover(res, "behind", to, c, t0, guid, expect="reinit")


def case_seg1(t, a, res):
    frm = t.master_host()
    to = t.next_host(frm)
    c = t.third(frm, to)
    t.load_start(frm)
    t.wait_ready(to)
    root = t.cl.h(t.tb(to))["firebird"]["root"]
    script = t.put_script(to, "tb-reseat-seg1.sh", WATCH_SEG1)
    t.raw(to, f"rm -f /tmp/tb-reseat-seg1.hit; nohup setsid sh {script} {root}/replication.conf {t.path} 900 "
              f">/dev/null 2>&1 < /dev/null &")
    hit = ""
    try:
        got = one_switch(t, res, "seg1", "ready")
        end = time.time() + 600
        while got and time.time() < end:
            _, hit = t.raw(to, "cat /tmp/tb-reseat-seg1.hit 2>/dev/null || true", check=False)
            if hit.strip():
                break
            time.sleep(10)
    finally:
        t.raw(to, f"pkill -f '{script}' || true", check=False)
    check(res, f"seg1: segment 1 of the new GUID deleted on {to}", {"deleted": ".journal-000000001" in hit},
          hit=hit.strip())
    if not got or ".journal-000000001" not in hit:
        return
    _, to, c, doc, t0, _, guid = got
    # More load: segments 2.. of the new GUID are archived; then no load.
    time.sleep(a.load_sec + 60)
    t.load_stop()
    # B must not ship 2 in place of 1; C must apply nothing of the new GUID
    # and must never be IN_SYNC with other data (plan §4, §10).
    end = time.time() + a.watch
    silent, applied, gap = [], 0, []
    while time.time() < end:
        ap = t.applied(c)
        if norm_guid(ap.get("control_guid")) == guid:
            applied = max(applied, ap.get("applied_sequence") or 0)
        rec = t.record(c)
        if rec.get("state") == "IN_SYNC":
            dc, db_ = t.digest(c), t.digest(to)
            if not (dc and db_ and all(dc.get(k) == v for k, v in db_.items() if v[0])):
                silent.append(rec.get("state"))
        gap = gap or [x.get("code") for x in t.alerts(to, "master") if "gap" in str(x.get("code", ""))]
        time.sleep(15)
    check(res, f"seg1: {to} halts its pair with {c} with a gap (no segment 2 in place of 1)",
          {"gap alert": bool(gap)}, alerts=gap)
    check(res, f"seg1: {c} applies nothing of the new GUID and is never IN_SYNC with other data",
          {"applied 0": applied == 0, "no silent divergence": not silent},
          applied=applied, state=t.record(c).get("state"))
    # Remedy. The old master is a replica of B too and may miss segment 1.
    for h in (c, frm):
        if h == frm:
            dh, db_ = t.digest(h), t.digest(to)
            if dh and db_ and all(dh.get(k) == v for k, v in db_.items() if v[0]) \
                    and t.record(h).get("state") in ("IN_SYNC", "LAGGING"):
                res.record(f"seg1: the old master {frm} applied the new GUID (got segment 1 before the delete)", "PASS")
                continue
        op = t.reinit(to, h)
        res.record(f"seg1: reinit {to} -> {h}", "PASS" if op.get("state") == "succeeded" else "FAIL",
                   state=op.get("state"), error=op.get("error", ""))
    ok, secs, det = t.files_equal(a.settle, to)
    check(res, "seg1: three files equal after the reinit", {"equal": ok}, seconds=secs, **det)


def forward_only(lines):
    """After nbackup -F the re-bind only goes forward: no new -L or -N."""
    ops = [d.get("op") for d in lines]
    if "nbackup_fixup" not in ops:
        return True
    after = ops[len(ops) - 1 - ops[::-1].index("nbackup_fixup"):]
    return not ({"nbackup_lock", "nbackup_unlock"} & set(after[1:]))


def case_kill(t, a, res):
    points = [p.strip() for p in a.kill_points.split(",") if p.strip()]
    for p in points:
        frm = t.master_host()
        to = t.next_host(frm)
        c = t.third(frm, to)
        t.load_start(frm)
        t.wait_ready(to)
        journal, ops = t.node_files(c, "replica")
        pat = t.node_dir(c, "replica") + "/hqclusternod[e] serve"
        m1, m2 = KILL_POINTS[p]
        script = t.put_script(c, "tb-reseat-watch.sh", WATCH_KILL)
        t.raw(c, f"rm -f /tmp/tb-reseat-watch.hit; nohup setsid sh {script} {journal} {ops} "
                 f"'{m1}' '{m2}' '{pat}' >/dev/null 2>&1 < /dev/null &")
        try:
            got = one_switch(t, res, f"kill at {p}", "ready")
            _, hit = t.raw(c, "cat /tmp/tb-reseat-watch.hit 2>/dev/null || true", check=False)
        finally:
            t.raw(c, f"pkill -f '{script}' || true; pkill -f 'tail -n0 -F {journal}' || true", check=False)
        res.record(f"kill at {p}: the node of {c} killed at the point", "PASS" if hit.strip() else "FAIL",
                   hit=hit.strip()[:300])
        if not got:
            return
        _, to, c, doc, t0, _, guid = got
        t.recover(res, f"kill at {p}", to, c, t0, guid, expect="any")
        lines = t.audit_since(c, "replica", t0)
        th = t.third_of(t.rcm("operations", "show", "-id", t.last_op)[2] or {}, c)
        check(res, f"kill at {p}: the repeat resumed by the witness (re-bound, no reinit), forward only after -F", {
            "re-bound": th.get("result") in ("done", "reseated") and not t.reinit_ops(lines),
            "forward only after -F": forward_only(lines),
        }, third=th, audit=[f"{d.get('op')}:{d.get('reason', '')}" for d in lines][-12:])


def case_noagent(t, a, res):
    frm = t.master_host()
    to = t.next_host(frm)
    c = t.third(frm, to)
    unit = t.cl.h(t.tb(c))["fbagent"].get("service") or "hqbirdfbagent"
    t.load_start(frm)
    t.wait_ready(to)
    t.raw(c, f"systemctl stop {unit}")
    try:
        got = one_switch(t, res, "noagent", "failed")
        if got:
            th = t.third_of(got[3], c)
            check(res, f"noagent: reseat on {c} refused with fbagent_required",
                  {"fbagent_required": "fbagent_required" in json.dumps(th)}, third=th)
    finally:
        t.raw(c, f"systemctl start {unit}", check=False)
    if got:
        _, to, c, doc, t0, _, guid = got
        t.recover(res, "noagent", to, c, t0, guid, expect="reinit_audit")


def case_nolink(t, a, res):
    frm = t.master_host()
    to = t.next_host(frm)
    c = t.third(frm, to)
    link = (t.node_id(to, "master"), t.node_id(c, "replica"))
    t.load_start(frm)
    t.wait_ready(to)
    t.cluster_links(drop=link)
    try:
        gone = t.wait_peer(c, "replica", link[0], False)
        res.record(f"nolink: {link[1]} no longer lists {link[0]} as a peer", "PASS" if gone else "FAIL")
        got = one_switch(t, res, "nolink", "unknown")
    finally:
        t.cluster_links(add=link)
    back = t.wait_peer(c, "replica", link[0], True)
    res.record(f"nolink: the link {link[0]} -> {link[1]} back", "PASS" if back else "FAIL")
    if got:
        _, to, c, doc, t0, _, guid = got
        t.recover(res, "nolink", to, c, t0, guid, expect="reinit_audit")
