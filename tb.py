#!/usr/bin/env python3
"""Replication test bed driver.

Runs on the operator machine (Windows or Linux, Python 3.8+, OpenSSH client).
Reads config/testbed.local.json (git-ignored), copies the module scripts to
each host and runs them over ssh. See README.md.

  tb.py do create|list|destroy [--yes]   (DigitalOcean hosts, optional)
  tb.py hosts prepare [--hosts ...]       (packages + HQbird/Firebird on Linux hosts)
  tb.py hosts wipe [--hosts ...] --yes    (remove all the test bed put on the hosts; check nothing is left)
  tb.py check
  tb.py install   --source local|goafts [--hosts all|master|replicas|h1,h2] [--components ...] [--new-certs]
  tb.py uninstall --source local|goafts [--hosts ...] [--components ...] [--deregister] [--keep-work]
  tb.py dbs prepare [--count 2] [--subdir tb] [--no-seed]
  tb.py dbs remove  [--subdir tb]
  tb.py dbs list    [--subdir tb]
  tb.py loadgen deploy --from git|local|binary [--path P] [--target master|local] [--no-smoke]
  tb.py loadgen smoke  [--db dbN]
  tb.py load start  [--db all|db1,db2] [--mode write|read|mixed|spike|oltp-emul] [--tx off|emul-safe|full]
                    [--limbo] [--no-extended] [--conns 1:4] [--minutes 0] [--tag load]
  tb.py load stop   [--tag all]
  tb.py load status [--tag all]
  tb.py verify [--db all] [--timeout 900]
  tb.py status
  tb.py test list
  tb.py test <name> [test options]      (tests/<name>.py)
"""
import argparse
import importlib
import json
import os
import pkgutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tblib import config as C          # noqa: E402
from tblib import ops                  # noqa: E402
from tblib.cluster import Cluster, TbError, log   # noqa: E402
from tblib.remote import RemoteError   # noqa: E402
import tests                           # noqa: E402


def cmd_check(cl, a):
    cfg = cl.cfg
    cfg.validate()
    log(f"config {cfg.path}: master={cfg.master} replicas={','.join(cfg.replicas)} "
        f"rcm={'on ' + cfg.rcm_host if cfg.rcm_enabled else 'off'}")
    bad = 0
    for n in cfg.all_hosts():
        h = cfg.host(n)
        try:
            cl.ready(n)
            res = ops.detect(cl, n)
            log(f"[{n}] {h['os']} ok: {json.dumps(res)}")
        except (TbError, RemoteError) as e:
            bad += 1
            log(f"[{n}] FAILED: {e}")
    return 1 if bad else 0


def cmd_do(cl, a):
    from tblib.digitalocean import DO
    do = DO(cl.cfg)
    if a.action == "create":
        do.create()
    elif a.action == "list":
        do.list()
    else:
        if not a.yes:
            do.list()
            if input(f"Delete these droplets (tag {do.tag})? Type 'yes': ").strip() != "yes":
                log("aborted")
                return 1
        do.destroy()
    return 0


def cmd_hosts(cl, a):
    if a.action == "wipe":
        if not a.yes:
            log("hosts wipe removes rcm, node, a test bed fbagent, load processes and the work folder "
                f"from: {', '.join(cl.cfg.select(a.hosts))}. Add --yes to do it.")
            return 1
        ops.wipe(cl, a.hosts)
        return 0
    import base64
    import threading
    enc = lambda v: base64.b64encode(v.encode()).decode()
    inst = cl.cfg.raw.get("firebird_installer") or {}
    names = cl.cfg.select(a.hosts)

    def installer_url(n):
        # One installer script per engine (firebird.engine), else linux_url.
        return (inst.get("linux_urls") or {}).get(cl.fb_engine(n)) or inst.get("linux_url", "")
    results = {}

    def one(n):
        h = cl.h(n)
        if h["os"] == "linux":
            args = {"installer_url_b64": enc(installer_url(n)), "root_b64": enc(h["firebird"]["root"])}
            rc, res, _, _ = cl.module(n, "05-dbms", "install", args, check=False)
        else:
            src = h["firebird"].get("copy_of", "")
            if src:
                # A second instance on the host: a copy of an installed root
                # with its own port and service (06-instance).
                rc, res, _, _ = cl.module(n, "06-instance", "create", {
                    "source": src, "target": h["firebird"]["root"], "port": cl.fb_port(n),
                    "service": h["firebird"]["service"]}, check=False)
                if rc != 0:
                    results[n] = (rc, res)
                    return
            args = {"fb_root": h["firebird"]["root"], "fb_service": h["firebird"]["service"],
                    "port": cl.fb_port(n)}
            rc, res, _, _ = cl.module(n, "05-dbms", "check", args, check=False)
        results[n] = (rc, res)

    for n in names:
        cl.ready(n)                      # sequential: ready() is not thread-safe
    threads = [threading.Thread(target=one, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    bad = 0
    for n in names:
        rc, res = results.get(n, (1, None))
        log(f"[{n}] {'ready' if rc == 0 else 'FAILED'} {res or ''}")
        bad += rc != 0
    return 1 if bad else 0


def cmd_install(cl, a):
    ops.install(cl, a.source, a.hosts, a.components, a.new_certs)
    return 0


def cmd_uninstall(cl, a):
    ops.uninstall(cl, a.source, a.hosts, a.components, a.deregister, a.keep_work)
    return 0


def cmd_dbs(cl, a):
    if a.action == "prepare":
        dbs = ops.dbs_prepare(cl, a.count, a.subdir, seed=not a.no_seed)
        for d in dbs:
            log(f"  {d['db_id']}  {d['path']}  {d.get('state')}")
    elif a.action == "remove":
        ops.dbs_remove(cl, a.subdir)
    else:
        for d in cl.test_dbs(a.subdir):
            log(f"  {d['db_id']}  {d['path']}  {d.get('state')}  gen={d.get('generation')}")
    return 0


def cmd_loadgen(cl, a):
    if a.action == "deploy":
        ops.loadgen_deploy(cl, a.source, a.target, a.path, smoke=not a.no_smoke)
    else:
        dbs = cl.test_dbs(which=a.db)
        if not dbs:
            raise TbError("no test databases (run 'dbs prepare')")
        m = cl.cfg.master
        cl.module(m, "40-loadgen", "smoke", {"db": dbs[0]["path"], "port": cl.fb_port(m)})
    return 0


def cmd_load(cl, a):
    if a.action == "start":
        dbs = [d["path"] for d in cl.test_dbs(which=a.db)]
        if not dbs:
            raise TbError("no test databases match --db")
        res = cl.load_start(dbs, a.mode, a.tx, a.limbo, not a.no_extended, a.conns, a.minutes,
                            a.think_ms, a.tag)
        log(f"load started: {res}")
    elif a.action == "stop":
        log(f"load stopped: {cl.load_stop(a.tag)}")
    else:
        cl.module(cl.cfg.master, "50-load", "status", {"tag": a.tag})
    return 0


def cmd_verify(cl, a):
    dbs = [d["path"] for d in cl.test_dbs(which=a.db)]
    ok, reports = cl.wait_converged(dbs, timeout=a.timeout)
    for r in reports or []:
        for rep, v in r["replicas"].items():
            state = "OK" if not v.get("diff") and not v.get("error") else f"DIFF {v.get('diff') or v.get('error')}"
            log(f"  {r['master']} -> {rep}: {state}")
    log("verify: " + ("all replicas match" if ok else "MISMATCH"))
    return 0 if ok else 1


def cmd_status(cl, a):
    cfg = cl.cfg
    for n in cfg.node_hosts():
        try:
            _, s = cl.api(n, "GET", "/v1/status")
            log(f"[{n}] {cfg.host(n)['node_id']}: {json.dumps(s)[:300]}")
            for d in cl.databases(n):
                log(f"    {d.get('state', '?'):16} gen={d.get('generation')} {d.get('path')}")
        except (TbError, RemoteError) as e:
            log(f"[{n}] unavailable: {e}")
    for row in cl.transfer():
        log(f"  ship {row['db_id']} -> {row['peer_id']}: shipped={row['last_shipped']} "
            f"acked={row['last_acked']} applied={row['last_applied']} retry={row['retry_count']}"
            + (f" gap_at={row['gap_at']}" if row.get("gap_at") else ""))
    cl.module(cfg.master, "50-load", "status", {"tag": "all"}, check=False)
    return 0


def test_modules():
    return sorted(m.name for m in pkgutil.iter_modules(tests.__path__) if not m.name.startswith("_"))


def main():
    p = argparse.ArgumentParser(prog="tb.py", description="Replication test bed driver")
    p.add_argument("--config", help="config file (default config/testbed.local.json or $TB_CONFIG)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("do", help="DigitalOcean droplets for the hosts")
    s.add_argument("action", choices=["create", "list", "destroy"])
    s.add_argument("--yes", action="store_true")

    s = sub.add_parser("hosts", help="prepare hosts: packages, HQbird/Firebird, SYSDBA password")
    s.add_argument("action", choices=["prepare", "wipe"])
    s.add_argument("--hosts", default="all")
    s.add_argument("--yes", action="store_true", help="wipe: really remove")

    sub.add_parser("check", help="validate config, reach every host, detect Firebird")

    for name in ("install", "uninstall"):
        s = sub.add_parser(name)
        s.add_argument("--source", choices=["local", "goafts"], required=True)
        s.add_argument("--hosts", default="all")
        s.add_argument("--components", help="subset of fbagent,node,rcm")
        if name == "install":
            s.add_argument("--new-certs", action="store_true", help="generate a new cluster CA")
        else:
            s.add_argument("--deregister", action="store_true", help="goafts: delete the agents on the server")
            s.add_argument("--keep-work", action="store_true", help="keep <work> (modules, stage, load logs)")

    s = sub.add_parser("dbs")
    s.add_argument("action", choices=["prepare", "remove", "list"])
    s.add_argument("--count", type=int)
    s.add_argument("--subdir")
    s.add_argument("--no-seed", action="store_true", help="prepare: do not copy to the replicas (reinit)")

    s = sub.add_parser("loadgen")
    s.add_argument("action", choices=["deploy", "smoke"])
    s.add_argument("--from", dest="source", choices=["git", "local", "binary"], default="git")
    s.add_argument("--path", help="git URL, local checkout or binary (default: from config)")
    s.add_argument("--target", choices=["master", "local"], default="master")
    s.add_argument("--no-smoke", action="store_true")
    s.add_argument("--db", default="all")

    s = sub.add_parser("load")
    s.add_argument("action", choices=["start", "stop", "status"])
    s.add_argument("--db", default="all", help="all | db1,db2 (folder names) | db ids")
    s.add_argument("--mode", default="write", choices=["write", "read", "mixed", "spike", "oltp-emul"])
    s.add_argument("--tx", default="off", choices=["off", "emul-safe", "full"],
                   help="off: no transaction changes; emul-safe/full: changing transactions")
    s.add_argument("--limbo", action="store_true", help="allow limbo transactions (off by default)")
    s.add_argument("--no-extended", action="store_true", help="classic fb-loadgen run (no bulk/heavy ops)")
    s.add_argument("--conns", default="1:4", help="MIN:MAX connections per process")
    s.add_argument("--minutes", type=int, default=0, help="0 = until 'load stop'")
    s.add_argument("--think-ms", type=int, default=50)
    s.add_argument("--tag", default=None)

    s = sub.add_parser("verify")
    s.add_argument("--db", default="all")
    s.add_argument("--timeout", type=int, default=900)

    sub.add_parser("status")

    s = sub.add_parser("test")
    tsub = s.add_subparsers(dest="test", required=True)
    tsub.add_parser("list")
    for name in test_modules():
        mod = importlib.import_module(f"tests.{name}")
        ts = tsub.add_parser(name, help=getattr(mod, "HELP", ""))
        mod.add_args(ts)

    # Hosts print UTF-8; a Windows console (cp1252) must not crash on it.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    a = p.parse_args()
    if a.cmd == "load" and a.tag is None:
        a.tag = "load" if a.action == "start" else "all"
    if a.cmd == "test" and a.test == "list":
        for name in test_modules():
            mod = importlib.import_module(f"tests.{name}")
            print(f"  {name:20} {getattr(mod, 'HELP', '')}")
        return 0
    try:
        cl = Cluster(C.load(a.config))
        handler = {"do": cmd_do, "hosts": cmd_hosts, "check": cmd_check, "install": cmd_install, "uninstall": cmd_uninstall,
                   "dbs": cmd_dbs, "loadgen": cmd_loadgen, "load": cmd_load, "verify": cmd_verify,
                   "status": cmd_status}.get(a.cmd)
        if handler:
            return handler(cl, a)
        mod = importlib.import_module(f"tests.{a.test}")
        return mod.run(cl, a)
    except (C.ConfigError, TbError, RemoteError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
