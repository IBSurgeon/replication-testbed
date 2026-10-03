"""Test bed operations behind the tb.py commands."""
import base64
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time

from . import config as C
from .cluster import LEGACY_ENGINES, FB_PORT_DEFAULT as C_FB_PORT_DEFAULT
from .cluster import FBAGENT_PORT_DEFAULT, Cluster, TbError, log


# ------------------------------------------------------------------ helpers --
def components_of(cl, name, only=None):
    comps = []
    if name in cl.cfg.node_hosts():
        comps += ["fbagent", "node"]
    if name in cl.cfg.companion_hosts():
        comps.append("companion")     # after "node": it copies the replica's binary
    if cl.cfg.rcm_enabled and name == cl.cfg.rcm_host:
        comps.append("rcm")
    if only:
        comps = [c for c in comps if c in only.split(",")]
    return comps


def install_order(cl, names):
    """Replicas first, then the master, then an RCM-only host."""
    reps = [n for n in names if n in cl.cfg.replicas]
    rest = [n for n in names if n == cl.cfg.master]
    other = [n for n in names if n not in reps and n not in rest]
    return reps + rest + other


def detect(cl, name):
    a = cl.base_args(name)
    rc, res, _, _ = cl.module(name, "10-local", "detect", {"fb_root": a["fb_root"], "fb_service": a["fb_service"]})
    res = res or {}
    st = cl.hstate(name)
    if cl.h(name)["os"] == "linux" and not cl.h(name)["firebird"]["service"] and res.get("fb_unit"):
        st["fb_unit"] = res["fb_unit"]
    st["hostname"] = res.get("hostname", "")
    st["hostname_full"] = res.get("hostname_full", "")
    st["ips"] = [ip for ip in (res.get("ips") or []) if ip]
    # Firebird port: firebird.conf (RemoteServicePort) says where Firebird
    # listens. A port set in the config must agree with it.
    conf_port = int(res.get("fb_conf_port") or 0) or C_FB_PORT_DEFAULT
    set_port = int(cl.h(name)["firebird"].get("port") or 0)
    if set_port and set_port != conf_port:
        raise TbError(f"[{name}] hosts.{name}.firebird.port is {set_port}, but firebird.conf "
                      f"says RemoteServicePort = {conf_port}; fix one of them")
    st["fb_port"] = conf_port
    cl.save()
    if not res.get("fb_root_ok"):
        raise TbError(f"[{name}] no Firebird in {a['fb_root']}; install HQbird/Firebird first")
    return res


def adopt_existing_fbagent(cl, name):
    """fbagent mode 'existing': take token and instance id from its config."""
    rc, res, sec, _ = cl.module(name, "10-local", "fbagent-info", {"fbagent_dir": cl.h(name)["fbagent"]["dir"]})
    tok = sec.get("fbagent_token")
    if not tok:
        raise TbError(f"[{name}] existing fbagent has no local_api token; enable local_api in its agent_config.json")
    st = cl.hstate(name)
    st["fbagent_token"] = tok
    if res and res.get("instance_id"):
        st["fbagent_instance"] = res["instance_id"]
    # local_api.listen of the agent, else what the agent binds without it:
    # 10000 + the Firebird port (fbagent localapi.EffectiveListen; 13055 is
    # only the library default for a port that would not fit). A port set in
    # the config must agree with it.
    listen = str((res or {}).get("listen") or "")
    if ":" in listen and listen.rsplit(":", 1)[1].isdigit():
        port = int(listen.rsplit(":", 1)[1])
    else:
        fbp = cl.fb_port(name) or 3050
        port = 10000 + fbp if fbp <= 55535 else FBAGENT_PORT_DEFAULT
    set_port = int(cl.h(name)["fbagent"].get("port") or 0)
    if set_port and set_port != port:
        raise TbError(f"[{name}] hosts.{name}.fbagent.port is {set_port}, but the existing agent "
                      f"listens on {port} (local_api.listen = '{listen or 'not set'}')")
    st["fbagent_port"] = port
    log(f"[{name}] existing fbagent: local_api on 127.0.0.1:{port}")
    cl.save()
    cl.ready(name, force=True)


def stage_local_binaries(cl, name, comps):
    h = cl.h(name)
    art = cl.cfg.artifacts
    src_dir = art.get("dir", "")
    names = art.get(h["os"], {})
    hst = cl.host(name)
    dest = hst.join(cl.stage(name), "bin")
    hst.mkdir(dest)
    need = []
    if "fbagent" in comps and h["fbagent"]["mode"] == "install":
        need.append("fbagent")
    if "node" in comps:
        need.append("hqclusternode")
    if "rcm" in comps:
        need.append("hqbirdrcm")
    if cl.cfg.master == name and "hqclusternode" not in need:
        need.append("hqclusternode")          # gencerts runs on the master
    for prod in need:
        p = os.path.join(src_dir, names.get(prod, ""))
        if not names.get(prod) or not os.path.isfile(p):
            raise TbError(f"artifact for {prod} ({h['os']}) not found: {p}")
        log(f"[{name}] upload {os.path.basename(p)}")
        target = hst.join(dest, cl.exe(name, prod))
        hst.put(p, target)
        if h["os"] == "linux":
            hst.run_raw(["chmod", "0755", target])     # scp from Windows drops the exec bit


def pin_b64(pin):
    return base64.b64encode(bytes.fromhex(pin)).decode()


def admin_call(cl, method, path, body=None):
    """goafts admin API through curl (mTLS client cert + X-Admin-Token + SPKI pin)."""
    g = cl.cfg.goafts
    a = g.get("admin", {})
    if not a.get("url"):
        return None
    cmd = ["curl", "-sS", "-k", "--pinnedpubkey", "sha256//" + pin_b64(g["pin"]),
           "--cert", a["client_cert"], "--key", a["client_key"], "-X", method,
           "-H", "Content-Type: application/json", "-w", "\n%{http_code}"]
    hdr = tempfile.NamedTemporaryFile("w", delete=False, suffix=".hdr")
    try:
        hdr.write(f"X-Admin-Token: {a['token']}\n")
        hdr.close()
        cmd += ["-H", "@" + hdr.name]
        if body is not None:
            cmd += ["--data-binary", json.dumps(body)]
        cmd.append(a["url"].rstrip("/") + path)
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    finally:
        os.remove(hdr.name)
    text, _, code = p.stdout.rpartition("\n")
    try:
        return int(code), (json.loads(text) if text.strip() else None)
    except ValueError:
        return int(code or 0), text


def csr_expectations(cl, names, since):
    """What a CSR of each enrolling host must look like to be approved.

    A pending CSR has no agent id yet (goafts assigns it on approval); it has
    the host name the agent sent (CN), the address it came from and when it
    was made. All three must match, and only one request per host.
    """
    import ipaddress
    import socket
    out = {}
    for n in names:
        st, h = cl.hstate(n), cl.h(n)
        hostnames = {x.lower() for x in (st.get("hostname"), st.get("hostname_full")) if x}
        ips = set(st.get("ips") or [])
        for v in (h.get("addr", ""), h.get("ssh", "").rpartition("@")[2]):
            if not v or v == "local":
                continue
            try:
                ips.add(str(ipaddress.ip_address(v)))
            except ValueError:
                try:
                    ips.update(i[4][0] for i in socket.getaddrinfo(v, None))
                except OSError:
                    pass
        out[n] = {"hostnames": hostnames, "ips": ips, "since": since}
    return out


def _csr_time(v):
    import datetime
    try:
        t = datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return t.timestamp() if t.tzinfo else None
    except ValueError:
        return None


CSR_CLOCK_SLACK = 300      # seconds of clock difference between us and goafts


def approve_pending(cl, expect, approved, warned):
    """Approve the one pending CSR of each host in `expect` that matches it
    exactly (host name, source address, made after enrollment began).
    Anything unclear is left for a person, and said once."""
    r = admin_call(cl, "GET", "/v1/admin/csr-requests?status=pending")
    if not r or r[0] != 200:
        return 0
    items = r[1]
    if isinstance(items, dict):
        items = items.get("items") or items.get("requests") or items.get("csr_requests") or []
    by_host = {n: [] for n in expect}
    for it in items or []:
        hn = str(it.get("hostname") or "").lower()
        for n, e in expect.items():
            if n in approved or hn not in e["hostnames"]:
                continue
            rid = it.get("request_id") or it.get("id")
            why = []
            if str(it.get("source_ip") or "") not in e["ips"]:
                why.append(f"source address {it.get('source_ip') or '?'} is not one of {sorted(e['ips'])}")
            t = _csr_time(it.get("created_at"))
            if t is None or t < e["since"] - CSR_CLOCK_SLACK:
                why.append(f"made at {it.get('created_at') or '?'}, before this enrollment")
            if why:
                if rid not in warned:
                    warned.add(rid)
                    log(f"goafts: NOT approving CSR {rid} ({hn}) for {n}: " + "; ".join(why))
                continue
            by_host[n].append(rid)
    n_ok = 0
    for n, rids in by_host.items():
        if len(rids) > 1:
            key = "multi:" + n
            if key not in warned:
                warned.add(key)
                log(f"goafts: {len(rids)} matching CSRs for {n} ({', '.join(map(str, rids))}); "
                    f"approve the right one in the goafts admin panel")
            continue
        if len(rids) == 1:
            code, _ = admin_call(cl, "POST", f"/v1/admin/csr-requests/{rids[0]}/approve",
                                 {"comment": "replication test bed auto-approve"})
            log(f"goafts: approve CSR {rids[0]} for {n} -> {code}")
            if code in (200, 201, 204):
                approved.add(n)
                n_ok += 1
    return n_ok


# ----------------------------------------------------------------- install --
def install(cl, source, hosts="all", only=None, new_certs=False):
    cfg = cl.cfg
    cfg.validate(("artifacts",) if source == "local" else ("goafts",))
    names = install_order(cl, cfg.select(hosts))
    plan = {n: components_of(cl, n, only) for n in names}
    log("install plan: " + ", ".join(f"{n}={'+'.join(c) or '-'}" for n, c in plan.items()))

    for n in names:
        if "fbagent" in plan[n] or "node" in plan[n]:
            detect(cl, n)
    if source == "local":
        for n in names:
            if "fbagent" in plan[n] and cl.h(n)["fbagent"]["mode"] == "existing":
                adopt_existing_fbagent(cl, n)
            stage_local_binaries(cl, n, plan[n])
    else:
        g = cfg.goafts
        for n in names:
            prods = []
            if "fbagent" in plan[n]:
                prods.append("fbagent")
            if "node" in plan[n] or n == cfg.master:
                prods.append("hqclusternode")
            if "rcm" in plan[n]:
                prods.append("hqbirdrcm")
            cl.module(n, "20-goafts", "download", {"url": g["url"], "pin": g["pin"],
                                                   "channel": g.get("channel", "stable"),
                                                   "products": ",".join(prods)})
        enroll_goafts(cl, [n for n in names if "fbagent" in plan[n]])

    have = os.path.exists(C.state_path("certs", "ca.crt"))
    if cfg.master not in names and not have:
        raise TbError("no certificates yet: include the master host in the first install")
    # With the master in the install, ensure_certs also checks the certificates
    # were made for these addresses: new droplets get new ones. A partial
    # install keeps what the other hosts already have.
    if cfg.master in names or new_certs or not have:
        cl.ensure_certs(force=new_certs)

    for n in names:
        comps = plan[n]
        if not comps:
            continue
        if source != "local" and "companion" in comps:
            log(f"[{n}] companion: only 'install --source local' installs it; skipped")
            comps = [c for c in comps if c != "companion"]
        cl.upload_stage_conf(n, "node" in comps, "rcm" in comps, "companion" in comps)
        args = cl.base_args(n)
        if source == "local":
            args.update({"components": ",".join(comps), "stage": cl.stage(n)})
            cl.module(n, "10-local", "install", args)
        else:
            args.update({"components": ",".join(c for c in comps if c != "fbagent"), "stage": cl.stage(n),
                         "product_install": cfg.goafts.get("product_install", "direct"),
                         "channel": cfg.goafts.get("channel", "stable")})
            cl.module(n, "20-goafts", "install", args)
        st = cl.hstate(n)
        st["installed"] = {"source": source, "components": comps}
        st["fb_port"] = cl.fb_port(n)
        st["fbagent_port"] = cl.fbagent_port(n)
        cl.save()
    log("install done")


def enroll_goafts(cl, names):
    g = cl.cfg.goafts
    errors = {}

    def run(n):
        try:
            args = cl.base_args(n)
            args.update({"url": g["url"], "pin": g["pin"], "enroll_timeout": g.get("enroll_timeout", "30m")})
            rc, res, _, _ = cl.module(n, "20-goafts", "enroll", args)
            cl.hstate(n)["goafts_agent_id"] = (res or {}).get("agent_id", "")
        except Exception as e:  # noqa: BLE001 - reported below
            errors[n] = e

    threads = [threading.Thread(target=run, args=(n,), daemon=True) for n in names]
    for n in names:
        cl.ready(n)                     # before threads: ready() is not thread-safe
    expect = csr_expectations(cl, names, time.time())
    approved, warned = set(), set()
    for t in threads:
        t.start()
    hostnames = [cl.hstate(n).get("hostname", "") for n in names]
    if g.get("admin", {}).get("url"):
        log("goafts: approving CSRs of the test bed hosts through the admin API")
    else:
        log("goafts: approve the CSR of each host in the goafts admin panel now "
            f"(hosts: {', '.join(h for h in hostnames if h)})")
    while any(t.is_alive() for t in threads):
        if g.get("admin", {}).get("url"):
            try:
                approve_pending(cl, expect, approved, warned)
            except Exception as e:  # noqa: BLE001
                log(f"goafts: approve failed: {e}")
        time.sleep(5)
    cl.save()
    if errors:
        raise TbError("enrollment failed: " + "; ".join(f"{k}: {v}" for k, v in errors.items()))


def uninstall(cl, source, hosts="all", only=None, deregister=False, keep_work=False):
    cfg = cl.cfg
    names = list(reversed(install_order(cl, cfg.select(hosts))))
    left = []
    for n in names:
        comps = components_of(cl, n, only)
        if not comps:
            continue
        args = cl.base_args(n)
        args["components"] = ",".join(comps)
        if source == "goafts" and "fbagent" in comps and deregister:
            _, res, _, _ = cl.module(n, "20-goafts", "agent-id", {"fbagent_dir": args["fbagent_dir"]}, check=False)
            aid = (res or {}).get("agent_id") or cl.hstate(n).get("goafts_agent_id")
            if aid:
                r = admin_call(cl, "DELETE", f"/v1/admin/agent-versions/{aid}")
                log(f"goafts: deregister {aid} -> {r[0] if r else 'no admin API configured'}")
        if n == cfg.master:
            cl.load_stop()
        script = "10-local" if source == "local" else "20-goafts"
        rc, res, _, _ = cl.module(n, script, "uninstall", args, check=False)
        if not note_leftovers(n, rc, res, left):
            cl.hstate(n).pop("installed", None)
        cl.save()
        if not keep_work:
            remove_work(cl, n)
    finish_removal("uninstall", left)


def note_leftovers(n, rc, res, left):
    """Record what a removal left on host n. True when something is left."""
    items = list((res or {}).get("leftovers") or [])
    if rc != 0 and not items:
        items = [f"the module failed (exit {rc}) before it could check"]
    for i in items:
        left.append(f"[{n}] {i}")
    return bool(items)


def finish_removal(what, left):
    if left:
        raise TbError(f"{what}: {len(left)} thing(s) are still on the hosts:\n  " + "\n  ".join(left)
                      + "\nRun 'tb.py hosts wipe --hosts <host> --yes' to remove what the test bed put there.")
    log(f"{what} done: nothing left")


def wipe(cl, hosts):
    """Remove everything the test bed put on the hosts, whatever was
    installed, and check that nothing is left. An 'existing' fbagent stays."""
    names = list(reversed(install_order(cl, cl.cfg.select(hosts))))
    left = []
    for n in names:
        rc, res, _, _ = cl.module(n, "10-local", "wipe", cl.base_args(n), check=False)
        if not note_leftovers(n, rc, res, left):
            cl.hstate(n).pop("installed", None)
        cl.save()
        h = cl.h(n)
        if h["os"] == "windows" and h["firebird"].get("copy_of"):
            rc, res, _, _ = cl.module(n, "06-instance", "remove", {
                "target": h["firebird"]["root"], "service": h["firebird"]["service"]}, check=False)
            if rc != 0:
                left.append(f"[{n}] firebird copy {h['firebird']['root']}: {res}")
        remove_work(cl, n)
        w = cl.h(n)["paths"]["work"]
        if cl.h(n)["os"] == "linux":
            rc2, out, _ = cl.host(n).run_raw(["test", "-e", w], check=False)
        else:
            rc2, out, _ = cl.host(n).run_raw(["powershell", "-NoProfile", "-Command",
                                              f"if (Test-Path '{w}') {{ exit 0 }} else {{ exit 1 }}"], check=False)
        if rc2 == 0:
            left.append(f"[{n}] work: folder {w}")
    finish_removal("wipe", left)


def remove_work(cl, n):
    hst = cl.host(n)
    w = cl.h(n)["paths"]["work"]
    if len(w.strip("/\\")) < 3:
        raise TbError(f"refusing to remove work folder '{w}'")
    log(f"[{n}] remove {w}")
    if cl.h(n)["os"] == "linux":
        hst.run_raw(["rm", "-rf", w], check=False)
    else:
        hst.run_raw(["powershell", "-NoProfile", "-Command",
                     f"Remove-Item -Recurse -Force -ErrorAction SilentlyContinue '{w}'"], check=False)
    cl._ready.discard(n)


# --------------------------------------------------------------- databases --
def wait_no_pending_restart(cl, name, timeout=600):
    end = time.time() + timeout
    while time.time() < end:
        pend = [d["db_id"] for d in cl.databases(name) if d.get("state") == "PENDING_RESTART"]
        if not pend:
            return
        time.sleep(5)
    raise TbError(f"[{name}] databases still PENDING_RESTART after {timeout}s")


def restart_firebird(cl, name, reason):
    st, body = cl.api(name, "POST", "/v1/firebird/restart", {"reason": reason, "ignore_window": True})
    op = (body or {}).get("operation_id") if isinstance(body, dict) else None
    if op:
        try:
            cl.wait_op(name, op, timeout=600)
        except TbError as e:        # the id may belong to fbagent, not to the node
            log(f"[{name}] restart operation {op}: {e}")
    wait_no_pending_restart(cl, name)


def reinit(cl, db_id, replica, mode="standard", timeout=3600, refusal_timeout=600, overwrite_non_replica=False):
    """Reinit one database to one replica; waits for the end. Returns the op.
    overwrite_non_replica: the replica's file may be an ordinary database (a
    replica turned to normal); without it the replica refuses."""
    to = cl.h(replica)["node_id"]
    body = {"to": to, "mode": mode, "ignore_window": True, "allow_restart": True,
            "hold_on_long_transactions": True}
    if overwrite_non_replica:
        body["overwrite_non_replica"] = True
    end_refusal = time.time() + refusal_timeout
    while True:
        st, resp = cl.api(cl.cfg.master, "POST", f"/v1/databases/{db_id}/reinit", body, check_status=False)
        if st in (200, 202):
            break
        code = (resp or {}).get("error", {}).get("code") if isinstance(resp, dict) else None
        if st == 409 and code == "long_transactions" and time.time() < end_refusal:
            log(f"reinit {db_id} -> {to}: refused (long transactions), retry in 30 s")
            time.sleep(30)
            continue
        if st == 409 and code == "reinit_lock_recovery_pending" and time.time() < end_refusal:
            # The node is still releasing the nbackup lock an interrupted
            # reinit left (it retries every minute after a start).
            log(f"reinit {db_id} -> {to}: the lock of an interrupted reinit is not released yet, retry in 20 s")
            time.sleep(20)
            continue
        raise TbError(f"reinit {db_id} -> {to}: HTTP {st} {json.dumps(resp)[:400]}")
    op = cl.wait_op(cl.cfg.master, resp["operation_id"], timeout=timeout)
    return op


def legacy(cl, name):
    """HQbird 2.5/3.0 on this host (firebird.engine)."""
    return cl.fb_engine(name) in LEGACY_ENGINES


def guid_forms(guid):
    """Both text forms of a GUID, upper case: as given, and with the word
    order gstat of HQbird 2.5/3.0 prints (the first group's two halves
    swapped, the bytes of each 16-bit word of the last two groups swapped).
    The swap is its own inverse."""
    g = (guid or "").upper().strip("{}")
    p = g.split("-")
    if len(p) != 5 or len(p[0]) != 8:
        return {g} if g else set()

    def sw(x):
        return "".join(x[i + 2:i + 4] + x[i:i + 2] for i in range(0, len(x), 4))
    return {g, "-".join([p[0][4:] + p[0][:4], p[1], p[2], sw(p[3]), sw(p[4])])}


def protocol_minor(cl, name):
    """The node's protocol minor (GET /v1/version), 0 when unknown."""
    st, body = cl.api(name, "GET", "/v1/version", check_status=False)
    try:
        return int((body or {}).get("protocol_minor") or 0) if st == 200 else 0
    except (TypeError, ValueError, AttributeError):
        return 0


def node_alerts(cl, name, code=None, db_id=None):
    """The node's alerts (GET /v1/alerts), filtered by code and database."""
    _, body = cl.api(name, "GET", "/v1/alerts", check_status=False)
    items = body if isinstance(body, list) else (body or {}).get("alerts", []) if isinstance(body, dict) else []
    return [x for x in items if isinstance(x, dict)
            and (code is None or x.get("code") == code)
            and (db_id is None or x.get("database") in (db_id, "", None))]


def activate_replconf(cl, name):
    """HQbird 2.5/3.0: switch the engine to the node's replconf file (the
    node's plugin, replconf.properties, one Firebird restart through
    fbagent). Nothing to do when it is active already. Returns the plan."""
    st, plan = cl.api(name, "POST", "/v1/replconf/activate", {"dry_run": True, "ignore_window": True}, check_status=False)
    if st != 200:
        raise TbError(f"[{name}] replconf activate (dry run): HTTP {st} {json.dumps(plan)[:400]}")
    if not plan.get("restart"):
        log(f"[{name}] replconf: active, plugin {plan.get('plugin_version')}")
        return plan
    log(f"[{name}] replconf: activate (install plugin: {plan.get('install_plugin')}, "
        f"import {len((plan.get('import') or {}).get('imported') or [])} record(s)); Firebird restarts")
    st, plan = cl.api(name, "POST", "/v1/replconf/activate", {"ignore_window": True}, timeout=900, check_status=False)
    if st != 200 or not plan.get("done") or not (plan.get("active") or (plan.get("state") or {}).get("active")):
        raise TbError(f"[{name}] replconf activate: HTTP {st} {json.dumps(plan)[:500]}")
    log(f"[{name}] replconf: active, {plan.get('conf_path')} valid till {plan.get('valid_till')}, plugin {plan.get('plugin_version')}")
    return plan


def dbs_prepare(cl, count=None, subdir=None, seed=True):
    cfg = cl.cfg
    m = cfg.master
    # HQbird 2.5/3.0: every node's engine reads the node's replconf file
    # before any record the node writes can apply.
    for n in cfg.node_hosts():
        if legacy(cl, n):
            activate_replconf(cl, n)
    count = count or cfg.dbs["count"]
    subdir = subdir or cfg.dbs["subdir"]
    a = cl.base_args(m)
    rc, res, _, _ = cl.module(m, "30-dbs", "prepare", {
        "db_root": a["db_root"], "subdir": subdir, "count": count, "file_name": cfg.dbs["file_name"],
        "source": cfg.dbs.get("source", ""), "fb_root": a["fb_root"], "port": a["fb_port"]})
    files = (res or {}).get("files", [])
    log(f"master: {len(files)} database file(s) in {subdir}")
    st, out = cl.api(m, "POST", "/v1/scansync", {})
    log(f"scansync: {out.get('status') if isinstance(out, dict) else out}")
    restart_firebird(cl, m, "test bed: databases prepared")
    dbs = cl.test_dbs(subdir)
    if legacy(cl, m):
        # No publications on 2.5/3.0: the master record makes the engine
        # write segments (the restart above opened the databases anew).
        log(f"{len(dbs)} database(s): HQbird {cl.fb_engine(m)} has no publications")
    else:
        st, out = cl.api(m, "POST", "/v1/publication/sync", {})
        for d in dbs:
            _, pub = cl.api(m, "GET", f"/v1/databases/{d['db_id']}/publication")
            if not (pub or {}).get("publication_enabled"):
                raise TbError(f"publication is not enabled on {d['path']}: {json.dumps(pub)[:300]}")
        log(f"publication enabled on {len(dbs)} database(s)")
    if seed:
        for r in cfg.replicas:
            for d in dbs:
                log(f"seed {d['path']} -> {r}")
                op = reinit(cl, d["db_id"], r, mode="standard")
                if op.get("state") != "succeeded":
                    raise TbError(f"seed reinit {d['db_id']} -> {r} failed: {op.get('error')}")
    return dbs


def dbs_remove(cl, subdir=None):
    cfg = cl.cfg
    subdir = subdir or cfg.dbs["subdir"]
    m = cfg.master
    cl.load_stop()
    before = {d["db_id"] for d in cl.test_dbs(subdir)}
    targets = [(m, cl.h(m)["paths"]["db_root"])] + [(r, cl.replica_root_for_master(r)) for r in cfg.replicas]
    for name, root in targets:
        cl.module(name, "30-dbs", "remove", {"db_root": root, "subdir": subdir})
        cl.api(name, "POST", "/v1/scansync", {"allow_shrink": True}, check_status=False)
        try:
            restart_firebird(cl, name, "test bed: databases removed")
        except TbError as e:
            log(f"[{name}] restart: {e}")
        for d in cl.databases(name):
            if d.get("state") == "ORPHANED" and (name != m or d["db_id"] in before):
                st, _ = cl.api(name, "DELETE", f"/v1/databases/{d['db_id']}", check_status=False)
                log(f"[{name}] forget {d['db_id']} -> {st}")
    log("databases removed")


# ------------------------------------------------------------------ loadgen --
def build_loadgen(cl, source, target_os, path=None):
    """Build fb-loadgen on this machine. Returns the local binary path."""
    lg = cl.cfg.loadgen
    out_dir = os.path.dirname(C.state_path("loadgen", "x"))
    exe = os.path.join(out_dir, f"fb-loadgen-{target_os}" + (".exe" if target_os == "windows" else ""))
    if source == "binary":
        p = path or lg.get(f"binary_{target_os}", "")
        if not p or not os.path.isfile(p):
            raise TbError(f"no fb-loadgen binary for {target_os}: {p}")
        return p
    if source == "git":
        url = path or lg.get("git_url", "")
        if not url or "<" in url:
            raise TbError("set loadgen.git_url in the local config (or pass --path)")
        src = os.path.join(out_dir, "src")
        if os.path.isdir(os.path.join(src, ".git")):
            subprocess.run(["git", "-C", src, "fetch", "--quiet", "origin"], check=True)
        else:
            shutil.rmtree(src, ignore_errors=True)
            subprocess.run(["git", "clone", "--quiet", url, src], check=True)
        subprocess.run(["git", "-C", src, "checkout", "--quiet", lg.get("git_ref", "main")], check=True)
        subprocess.run(["git", "-C", src, "pull", "--quiet", "--ff-only"], check=False)
    else:
        src = path or lg.get("local_src", "")
        if not src or not os.path.isdir(src):
            raise TbError(f"no local fb-loadgen checkout: {src}")
    env = dict(os.environ, GOOS=target_os, GOARCH="amd64", CGO_ENABLED="0")
    log(f"go build fb-loadgen ({target_os}) from {src}")
    subprocess.run(["go", "build", "-trimpath", "-ldflags", "-s -w", "-o", exe, "."], cwd=src, env=env, check=True)
    return exe


def loadgen_deploy(cl, source, target="master", path=None, smoke=True):
    m = cl.cfg.master
    mh = cl.h(m)
    dbs = cl.test_dbs()
    if target == "local":
        tos ="windows" if platform.system() == "Windows" else "linux"
        exe = build_loadgen(cl, source, tos, path)
        dest = os.path.dirname(C.state_path("loadgen", "bin", "x"))
        final = os.path.join(dest, os.path.basename(exe).replace(f"-{tos}", ""))
        shutil.copy2(exe, final)
        log(f"fb-loadgen ready on this machine: {final}")
        if smoke and dbs:
            dsn = f"{mh['addr']}/{mh['firebird']['port']}:{dbs[0]['path']}"
            r = subprocess.run([final, "--profile", "write-heavy", "--dsn", dsn,
                                "--user", cl.cfg.secrets.get("firebird_user", "SYSDBA"),
                                "--pass", cl.cfg.secrets.get("firebird_password", ""),
                                "--warmup", "0", "--main", "5", "--cooldown", "0", "--conn-min", "1",
                                "--conn-max", "2", "--think-ms", "0", "--extended-load=false",
                                "--csv", os.path.join(dest, "smoke.txt")],
                               capture_output=True, text=True, timeout=180)
            totals = re.findall(r"Total: (\d+)", r.stdout)
            ok = r.returncode == 0 and "FINAL LOAD TEST REPORT" in r.stdout and totals and int(totals[-1]) > 0
            log(f"smoke from this machine: {'OK' if ok else 'FAILED'}")
            if not ok:
                raise TbError("fb-loadgen smoke failed:\n" + "\n".join((r.stdout + r.stderr).splitlines()[-20:]))
        return final
    exe = build_loadgen(cl, source, mh["os"], path)
    hst = cl.host(m)
    binp = hst.join(cl.stage(m), "bin", cl.exe(m, "fb-loadgen"))
    hst.mkdir(hst.join(cl.stage(m), "bin"))
    hst.put(exe, binp)
    cl.module(m, "40-loadgen", "install", {"binary": binp})
    if smoke:
        if not dbs:
            log("no test databases yet: smoke skipped (run 'dbs prepare' and 'loadgen smoke')")
        else:
            cl.module(m, "40-loadgen", "smoke", {"db": dbs[0]["path"], "port": cl.fb_port(m)})
    return binp
