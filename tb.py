#!/usr/bin/env python3
"""Replication test bed driver.

Runs on the operator machine (Windows or Linux, Python 3.8+, OpenSSH client).
Reads config/testbed.local.json (git-ignored), copies the module scripts to
each host and runs them over ssh. See README.md.

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
        cl.module(m, "40-loadgen", "smoke", {"db": dbs[0]["path"], "port": cl.h(m)["firebird"]["port"]})
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
        handler = {"check": cmd_check, "install": cmd_install, "uninstall": cmd_uninstall,
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
