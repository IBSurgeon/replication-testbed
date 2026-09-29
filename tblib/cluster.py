"""High-level test bed operations used by tb.py and the tests.

A Cluster wraps the config, the run state (state/state.json) and one Host
object per test bed host. Everything that changes a host goes through a
module script (modules/<os>/NN-*.sh|ps1); the node is driven through its own
API (`hqclusternode api`, run on the host by 90-hostctl node-api).
"""
import base64
import datetime
import json
import os
import secrets
import shutil
import subprocess
import threading
import time

from . import config as C
from .remote import Host, RemoteError, b64


class TbError(Exception):
    pass


FB_PORT_DEFAULT = 3050
LEGACY_ENGINES = ("2.5", "3.0")     # HQbird through the replconf plugin
FBAGENT_PORT_DEFAULT = 13055     # fbagent listens here when local_api.listen is not set


def now():
    return datetime.datetime.now().strftime("%H:%M:%S")


def log(msg):
    print(f"{now()} {msg}", flush=True)


def parse_output(out):
    """Last TBRESULT JSON and all TBSECRET key=value pairs of a module run."""
    res, sec = None, {}
    for line in (out or "").splitlines():
        line = line.strip()
        if line.startswith("TBRESULT "):
            try:
                res = json.loads(line[len("TBRESULT "):])
            except ValueError:
                pass
        elif line.startswith("TBSECRET "):
            k, _, v = line[len("TBSECRET "):].partition("=")
            sec[k.strip()] = v.strip()
    return res, sec


class Cluster:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = C.load_state()
        self._hosts = {}
        self._ready = set()

    # --- hosts ---------------------------------------------------------------
    def host(self, name):
        if name not in self._hosts:
            self._hosts[name] = Host(self.cfg, name, log=log)
        return self._hosts[name]

    def h(self, name):
        return self.cfg.host(name)

    def hstate(self, name):
        return self.state.setdefault("hosts", {}).setdefault(name, {})

    def save(self):
        C.save_state(self.state)

    def fbagent_token(self, name):
        if self.cfg.secrets.get("fbagent_token"):
            return self.cfg.secrets["fbagent_token"]
        st = self.hstate(name)
        if not st.get("fbagent_token"):
            st["fbagent_token"] = secrets.token_hex(20)
            self.save()
        return st["fbagent_token"]

    def secrets_env(self, name):
        s = self.cfg.secrets
        return {"TB_FB_USER": s.get("firebird_user", "SYSDBA"),
                "TB_FB_PASSWORD": s.get("firebird_password", ""),
                "TB_FBAGENT_TOKEN": self.fbagent_token(name),
                "TB_FB_INITIAL_PASSWORD": s.get("firebird_initial_password", ""),
                # RCM operator login for the tests that use the RCM API; a
                # placeholder left from the example file counts as not set.
                "TB_RCM_USER": "" if s.get("rcm_user", "").startswith("<") else s.get("rcm_user", ""),
                "TB_RCM_PASSWORD": "" if s.get("rcm_password", "").startswith("<") else s.get("rcm_password", "")}

    def ready(self, name, force=False):
        """Copy modules and the secrets file to the host (once per run)."""
        if name in self._ready and not force:
            return
        hst = self.host(name)
        hst.sync_modules()
        hst.push_secrets(self.secrets_env(name))
        self._ready.add(name)

    def module(self, name, script, cmd, args=None, check=True, timeout=None):
        self.ready(name)
        argv = [cmd]
        for k, v in (args or {}).items():
            if v is None or v == "":
                continue
            argv += ["--" + k.replace("_", "-"), str(v).lower() if isinstance(v, bool) else str(v)]
        rc, out, err = self.host(name).module(script, argv, check=check, timeout=timeout)
        res, sec = parse_output(out)
        return rc, res, sec, out

    # --- ports and ids ------------------------------------------------------
    # A port set in the config wins. Port 0 means: what 'install' found on the
    # host (state), else the product default.
    def fb_port(self, name):
        p = int(self.h(name)["firebird"].get("port") or 0)
        return p or int(self.hstate(name).get("fb_port") or 0) or FB_PORT_DEFAULT

    def fbagent_port(self, name):
        p = int(self.h(name)["fbagent"].get("port") or 0)
        return p or int(self.hstate(name).get("fbagent_port") or 0) or FBAGENT_PORT_DEFAULT

    def fbagent_instance(self, name):
        return (self.hstate(name).get("fbagent_instance") or self.h(name)["fbagent"]["instance_id"]
                or f"tb-{name}-{self.fb_port(name)}")

    # --- common module arguments ---------------------------------------------
    def fb_engine(self, name):
        """firebird.engine of the host: "2.5", "3.0", "4", "5" or "" (auto)."""
        return str(self.h(name)["firebird"].get("engine") or "")

    def fb_service(self, name):
        h = self.h(name)
        return h["firebird"]["service"] or self.hstate(name).get("fb_unit", "")

    def base_args(self, name):
        h = self.h(name)
        return {"fb_root": h["firebird"]["root"], "fb_port": self.fb_port(name),
                "fb_service": self.fb_service(name), "fb_engine": self.fb_engine(name),
                "fbagent_mode": h["fbagent"]["mode"], "fbagent_dir": h["fbagent"]["dir"],
                "fbagent_port": self.fbagent_port(name),
                "fbagent_instance": self.fbagent_instance(name),
                "fbagent_service": h["fbagent"]["service"],
                "node_dir": h["paths"]["node"], "rcm_dir": h["paths"]["rcm"],
                "db_root": h["paths"]["db_root"]}

    def hostctl(self, name, cmd, args=None, check=True, timeout=None):
        a = {"node_dir": self.h(name)["paths"]["node"], "fb_root": self.h(name)["firebird"]["root"],
             "port": self.fb_port(name)}
        a.update(args or {})
        rc, res, _, out = self.module(name, "90-hostctl", cmd, a, check=check, timeout=timeout)
        return res if rc == 0 else None

    # --- node API ------------------------------------------------------------
    def api(self, name, method, path, body=None, timeout=60, check_status=True, addr=None):
        """Call the node API on host `name`. Returns (status, body). With
        addr ("host:port"), the call goes from `name` to that node instead,
        with the certificate of `name`: how a peer route is reached."""
        args = {"method": method, "path": path, "timeout": timeout}
        if addr:
            args["addr"] = addr
        if body is not None:
            args["body_b64"] = b64(json.dumps(body))
        self.ready(name)
        argv = ["node-api"]
        a = {"node_dir": self.h(name)["paths"]["node"]}
        a.update(args)
        for k, v in a.items():
            argv += ["--" + k.replace("_", "-"), str(v)]
        rc, out, err = self.host(name).module("90-hostctl", argv, stream=False, timeout=timeout + 60)
        res, _ = parse_output(out)
        if not isinstance(res, dict) or "status" not in res:
            raise TbError(f"[{name}] bad api answer for {method} {path}: {out[-500:]}")
        st, bd = int(res["status"]), res.get("body")
        if check_status and st >= 300:
            raise TbError(f"[{name}] {method} {path} -> {st}: {json.dumps(bd)[:500]}")
        return st, bd

    def wait_op(self, name, op_id, timeout=1800, poll=5):
        end = time.time() + timeout
        while time.time() < end:
            _, op = self.api(name, "GET", f"/v1/operations/{op_id}")
            if op.get("state") not in ("in_progress", "queued", "pending", None):
                return op
            time.sleep(poll)
        raise TbError(f"[{name}] operation {op_id} did not finish in {timeout}s")

    def databases(self, name):
        _, dbs = self.api(name, "GET", "/v1/databases")
        return dbs or []

    # --- paths ---------------------------------------------------------------
    def master_dbs_dir(self, subdir=None):
        m = self.h(self.cfg.master)
        return self.host(self.cfg.master).join(m["paths"]["db_root"], subdir or self.cfg.dbs["subdir"])

    def replica_root_for_master(self, replica):
        """Where a replica keeps the master's databases: <db_root>/<master node id>."""
        r = self.h(replica)
        return self.host(replica).join(r["paths"]["db_root"], self.h(self.cfg.master)["node_id"])

    def replica_path(self, replica, master_path):
        """Map a master database path to its copy on a replica."""
        m = self.h(self.cfg.master)
        rel = master_path[len(m["paths"]["db_root"]):].lstrip("/\\")
        rel = rel.replace("\\", "/").split("/")
        return self.host(replica).join(self.replica_root_for_master(replica), *rel)

    def test_dbs(self, subdir=None, which="all"):
        """Master records of the test databases: [{db_id, path, ...}]."""
        d = self.master_dbs_dir(subdir)
        norm = lambda p: p.replace("\\", "/").lower()
        out = [r for r in self.databases(self.cfg.master)
               if norm(r.get("path", "")).startswith(norm(d) + "/") and r.get("state") != "ORPHANED"]
        out.sort(key=lambda r: r["path"])
        if which and which != "all":
            want = set(which.split(","))
            out = [r for r in out if r["db_id"] in want or
                   os.path.basename(os.path.dirname(r["path"].replace("\\", "/"))) in want]
        return out

    # --- certificates and configs --------------------------------------------
    def stage(self, name):
        return self.host(name).join(self.h(name)["paths"]["work"], "stage")

    def exe(self, name, base):
        return base + (".exe" if self.h(name)["os"] == "windows" else "")

    def ensure_certs(self, force=False):
        out = C.state_path("certs", "ca.crt")
        m = self.cfg.master
        hst = self.host(m)
        nodes = ",".join(f"{self.h(n)['node_id']}:{self.h(n)['role']}" for n in self.cfg.node_hosts())
        sans = ["localhost", "127.0.0.1"] + sorted({self.h(n)["addr"] for n in self.cfg.all_hosts()})
        # Made for these nodes and addresses? New droplets get new addresses,
        # and certificates made for the old ones fail every dial that checks
        # the peer's address.
        made_for = C.state_path("certs", "made-for.json")
        want = {"nodes": nodes, "hosts": sans}
        if os.path.exists(out) and not force:
            try:
                with open(made_for, encoding="utf-8") as f:
                    if json.load(f) == want:
                        return os.path.dirname(out)
            except (OSError, ValueError):
                pass
            log("certificates were made for other nodes or addresses: making new ones")
        gen = hst.join(self.h(m)["paths"]["work"], "certs-gen")
        binp = hst.join(self.stage(m), "bin", self.exe(m, "hqclusternode"))
        log(f"gencerts on {m}: nodes={nodes}")
        if self.h(m)["os"] == "linux":
            hst.run_raw(["rm", "-rf", gen])
            hst.run_raw([binp, "gencerts", "-out", gen, "-nodes", nodes, "-hosts", ",".join(sans)])
            if self.h(m)["sudo"] and hst.ssh_user():
                hst.run_raw(["chown", "-R", hst.ssh_user(), gen])     # scp reads as the ssh user
        else:
            hst.run_raw(["powershell", "-NoProfile", "-Command",
                         f"Remove-Item -Recurse -Force -ErrorAction SilentlyContinue '{gen}'; "
                         f"& '{binp}' gencerts -out '{gen}' -nodes '{nodes}' -hosts '{','.join(sans)}'; exit $LASTEXITCODE"])
        local = os.path.dirname(out)
        tmp = local + ".download"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        hst.get(gen, tmp)
        src = os.path.join(tmp, os.path.basename(gen.replace("\\", "/")))
        if not os.path.isdir(src):
            src = tmp
        shutil.rmtree(local, ignore_errors=True)
        shutil.copytree(src, local)
        shutil.rmtree(tmp, ignore_errors=True)
        with open(made_for, "w", encoding="utf-8") as f:
            json.dump(want, f)
        # The CA key never stays on a host.
        if self.h(m)["os"] == "linux":
            hst.run_raw(["rm", "-rf", gen])
        else:
            hst.run_raw(["powershell", "-NoProfile", "-Command", f"Remove-Item -Recurse -Force '{gen}'"])
        return local

    def node_json(self, name):
        cfg, h = self.cfg, self.h(name)
        hst = self.host(name)
        fb = h["firebird"]
        root = fb["root"]
        role = h["role"]
        peers = []
        if role == "master":
            for r in cfg.replicas:
                rh = self.h(r)
                peers.append({"node_id": rh["node_id"], "url": f"https://{rh['addr']}:{rh['node_port']}",
                              "compress": "zstd"})
        else:
            mh = self.h(cfg.master)
            peers.append({"node_id": mh["node_id"], "url": f"https://{mh['addr']}:{mh['node_port']}",
                          "compress": "zstd"})
        if cfg.rcm_enabled:
            rcm = {"url": f"https://{self.h(cfg.rcm_host)['addr']}:7443",
                   "ca_cert": hst.join(h["paths"]["node"], "certs", "ca.crt")}
        else:
            rcm = {"url": "none"}
        limits = {"channel_a_mbps": 50, "channel_a_burst_mbps": 200, "reinit_window_mbps": 100,
                  "reinit_day_disk_fraction": 0.5, "free_space_floor_gb": 1, "backlog_warn": 32,
                  "backlog_critical": 48, "retention_days": 2}
        if role == "replica":
            limits["mailbox_pending_ceiling"] = 256
        limits.update(cfg.limits)
        svc = self.fb_service(name)
        engine = self.fb_engine(name)
        fbj = {"root": root, "user": cfg.secrets.get("firebird_user", "SYSDBA"),
               "password": cfg.secrets.get("firebird_password", ""), "port": self.fb_port(name),
               "replication_conf": hst.join(root, "replication.conf"),
               "replication_log": hst.join(root, "replication.log"),
               "firebird_log": hst.join(root, "firebird.log"),
               "fbagent_url": f"http://127.0.0.1:{self.fbagent_port(name)}",
               "fbagent_token": self.fbagent_token(name),
               "instance_id": self.fbagent_instance(name),
               "service_name": svc, "restart_timeout_sec": 300}
        if h["os"] == "linux":
            fbj["systemd_unit"] = svc
        if engine:
            fbj["engine"] = engine
        if engine in LEGACY_ENGINES:
            # HQbird 2.5/3.0: the node's replconf file is its default,
            # <root>/replconf.hqcluster.hqbird (hqcluster-node plan, U-8).
            del fbj["replication_conf"]
        w = cfg.windows
        return {
            "node_id": h["node_id"], "role": role, "listen_addr": f":{h['node_port']}",
            "rcm": rcm, "firebird": fbj,
            "databases": {"root": h["paths"]["db_root"], "recursive": True, "template": "*.fdb"},
            "segments": {"storage": "", "mirror_hierarchy": True},
            "windows": {"restart_window": w["master_restart_window"] if role == "master" else w["replica_restart_window"],
                        "transfer_window": w["transfer_window"]},
            "peers": peers, "limits": limits, "exclude_filter": "",
        }

    def rcm_json(self, name):
        hst = self.host(name)
        d = self.h(name)["paths"]["rcm"]
        return {
            "ingest_addr": "0.0.0.0:7443", "operator_addr": "127.0.0.1:7444",
            "users_file": hst.join(d, "rcm-data", "users.json"),
            "tls": {"ca_cert": hst.join(d, "certs", "ca.crt"), "cert": hst.join(d, "certs", "rcm.crt"),
                    "key": hst.join(d, "certs", "rcm.key")},
            "nodes": [{"node_id": self.h(n)["node_id"], "role": self.h(n)["role"],
                       "url": f"https://{self.h(n)['addr']}:{self.h(n)['node_port']}"}
                      for n in self.cfg.node_hosts()],
            "poll_interval_sec": 15, "stale_after_sec": 45,
            "data_dir": hst.join(d, "rcm-data"), "command_timeout_sec": 600,
        }

    def upload_stage_conf(self, name, with_node, with_rcm):
        """Render configs + pick certs into state/gen/<host>/ and copy to <stage>."""
        certs = C.state_path("certs", "ca.crt")
        certs_dir = os.path.dirname(certs)
        gen = os.path.dirname(C.state_path("gen", name, "x"))
        shutil.rmtree(gen, ignore_errors=True)
        os.makedirs(os.path.join(gen, "conf"))
        if with_node:
            os.makedirs(os.path.join(gen, "certs"))
            nid = self.h(name)["node_id"]
            for f in ("ca.crt", f"{nid}.crt", f"{nid}.key"):
                shutil.copy2(os.path.join(certs_dir, f), os.path.join(gen, "certs", f))
            _write_json(os.path.join(gen, "conf", "node.json"), self.node_json(name))
        if with_rcm:
            os.makedirs(os.path.join(gen, "rcm-certs"))
            for f in ("ca.crt", "rcm.crt", "rcm.key"):
                shutil.copy2(os.path.join(certs_dir, f), os.path.join(gen, "rcm-certs", f))
            _write_json(os.path.join(gen, "conf", "rcm.json"), self.rcm_json(name))
        hst = self.host(name)
        stage = self.stage(name)
        hst.mkdir(stage)
        for sub in ("conf", "certs", "rcm-certs"):
            p = os.path.join(gen, sub)
            if os.path.isdir(p):
                hst.mkdir(hst.join(stage, sub))
                for f in os.listdir(p):
                    hst.put(os.path.join(p, f), hst.join(stage, sub, f))

    # --- load ----------------------------------------------------------------
    def load_start(self, dbs, mode="write", tx="off", limbo=False, extended=True, conns="1:4",
                   minutes=0, think_ms=50, tag="load"):
        m = self.cfg.master
        args = {"dbs": ",".join(dbs), "port": self.fb_port(m), "mode": mode, "tx": tx,
                "limbo": limbo, "extended": extended, "conns": conns, "minutes": minutes,
                "think_ms": think_ms, "tag": tag}
        rc, res, _, _ = self.module(m, "50-load", "start", args)
        return res

    def load_stop(self, tag="all"):
        rc, res, _, _ = self.module(self.cfg.master, "50-load", "stop", {"tag": tag}, check=False)
        return res

    # --- verification ----------------------------------------------------------
    def counts(self, name, path):
        return self.hostctl(name, "counts", {"db": path}, check=False)

    def compare(self, master_path, replicas=None):
        """Row counts of master vs each replica. Returns (ok, report)."""
        replicas = replicas or self.cfg.replicas
        mc = self.counts(self.cfg.master, master_path)
        report = {"master": master_path, "replicas": {}}
        if mc is None:
            return False, dict(report, error="master count failed")
        ok = True
        # Keyed tables must match. If none is keyed (a detection problem),
        # compare every table rather than none.
        strict = any(v["keyed"] for v in mc.values())
        report["compared"] = "keyed tables" if strict else "all tables (no keyed table found)"
        for r in replicas:
            rp = self.replica_path(r, master_path)
            rc = self.counts(r, rp)
            diff = {}
            if rc is None:
                ok = False
                report["replicas"][r] = {"path": rp, "error": "count failed"}
                continue
            for t, v in mc.items():
                rv = rc.get(t, {}).get("rows")
                if (v["keyed"] or not strict) and rv != v["rows"]:
                    diff[t] = {"master": v["rows"], "replica": rv}
            ok = ok and not diff
            report["replicas"][r] = {"path": rp, "diff": diff}
        return ok, report

    def wait_converged(self, master_paths, timeout=900, poll=20, replicas=None):
        """Poll row counts until every replica matches the master."""
        end = time.time() + timeout
        last = None
        while time.time() < end:
            all_ok, reports = True, []
            for p in master_paths:
                ok, rep = self.compare(p, replicas)
                reports.append(rep)
                all_ok = all_ok and ok
            if all_ok:
                return True, reports
            last = reports
            log(f"not converged yet; next check in {poll}s")
            time.sleep(poll)
        return False, last

    def transfer(self):
        _, t = self.api(self.cfg.master, "GET", "/v1/transfer")
        return t or []


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, indent=2)
