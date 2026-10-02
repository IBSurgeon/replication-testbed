"""HQbird 2.5/3.0 through the replconf plugin (hqcluster-node
docs/hqbird-25-30-finish-plan.md, T-4). Needs firebird.engine 2.5 or 3.0 on
every node host and `tb.py dbs prepare` done (it activates the plugin).

Steps:
  activation      every node: the node's file is its default
                  (<root>/replconf.hqcluster.hqbird), replconf.properties
                  points to it, plugin 2.1.0; where the engine looks for them
                  (V-12); the engine the node sees and its Firebird unit
                  (V-25)
  publications    POST /v1/publication/sync -> 409 no_publications
  flow            load, stop, every replica matches
  restart         Firebird of the master restarted during the load
  busy_writers    a reinit while a transaction that has written stays open:
                  the node retries the lock and gives up (reinit_busy_writers);
                  once it is gone the reinit goes through
  properties      a foreign edit of replconf.properties raises
                  replconf_properties_changed and the node does not fix it
  valid_date      a valid date 20 days ahead raises replconf_expiring; the
                  file gets its date back (never a past one)
  turn_to_normal  a replica database turned to normal, then seeded again

Linux hosts only for now (the host helpers of the last steps are Linux)."""
import base64
import datetime
import json
import time

from tblib import ops
from tblib.cluster import TbError, log
from tblib.results import Results

from ._common import add_load_args, converge_and_record, pick_replicas

HELP = "HQbird 2.5/3.0: activation, flow, restart, reinit without replay, properties, valid date, turn to normal"

STEPS = ["activation", "publications", "flow", "restart", "busy_writers", "properties", "valid_date", "turn_to_normal"]


def add_args(p):
    p.add_argument("--steps", default=",".join(STEPS), help="comma list of: " + ", ".join(STEPS))
    p.add_argument("--replicas", default="all")
    p.add_argument("--minutes", type=int, default=2)
    p.add_argument("--catchup-timeout", type=int, default=900)
    add_load_args(p, tx="off")


def b64(s):
    return base64.b64encode(s.encode()).decode()


def replconf(cl, name):
    st, doc = cl.api(name, "GET", "/v1/replconf", check_status=False)
    if st != 200:
        raise TbError(f"[{name}] GET /v1/replconf: {st} {doc}")
    return doc


def alert_codes(cl, name):
    st, doc = cl.api(name, "GET", "/v1/alerts", check_status=False)
    items = doc if isinstance(doc, list) else (doc or {}).get("alerts", [])
    return {a.get("code") for a in items if isinstance(a, dict)}


def alert_severity(cl, name, code):
    """The severity of the node's alert code, None when it is not raised."""
    st, doc = cl.api(name, "GET", "/v1/alerts", check_status=False)
    items = doc if isinstance(doc, list) else (doc or {}).get("alerts", [])
    return next((a.get("severity") for a in items if isinstance(a, dict) and a.get("code") == code), None)


def wait_alert_info_at_most(cl, name, code, timeout=90):
    """Wait until code is not raised above info."""
    end = time.time() + timeout
    while True:
        replconf(cl, name)
        if alert_severity(cl, name, code) in (None, "info"):
            return True
        if time.time() >= end:
            return False
        time.sleep(5)


def wait_alert(cl, name, code, present=True, timeout=90):
    end = time.time() + timeout
    while True:
        replconf(cl, name)        # GET runs the node's check at once
        have = code in alert_codes(cl, name)
        if have == present or time.time() >= end:
            return have == present
        time.sleep(5)


def restart_node(cl, name):
    cl.hostctl(name, "node-svc", {"action": "restart"})
    end = time.time() + 120
    while time.time() < end:
        try:
            st, _ = cl.api(name, "GET", "/v1/status", check_status=False, timeout=15)
            if st == 200:
                return True
        except TbError:
            pass
        time.sleep(5)
    return False


def run(cl, a):
    m = cl.cfg.master
    nodes = [m] + pick_replicas(cl, a.replicas)
    for n in nodes:
        if not ops.legacy(cl, n):
            raise TbError(f"[{n}] firebird.engine is '{cl.fb_engine(n)}': legacy wants 2.5 or 3.0 on every node host")
        if cl.h(n)["os"] != "linux":
            raise TbError(f"[{n}] legacy runs on Linux hosts for now")
    steps = [s.strip() for s in a.steps.split(",") if s.strip()]
    for s in steps:
        if s not in STEPS:
            raise TbError(f"unknown step {s} (known: {', '.join(STEPS)})")
    dbs = cl.test_dbs()
    if not dbs:
        raise TbError("no test databases (run 'tb.py dbs prepare')")
    reps = pick_replicas(cl, a.replicas)
    paths = [d["path"] for d in dbs]
    res = Results("legacy", vars(a).copy())

    if "activation" in steps:
        for n in nodes:
            doc = replconf(cl, n)
            root = cl.h(n)["firebird"]["root"]
            want = root.rstrip("/") + "/replconf.hqcluster.hqbird"
            ok = (doc.get("active") and doc.get("conf_default") and doc.get("conf_path") == want
                  and doc.get("plugin_version") == "2.1.0" and doc.get("properties_target") == want)
            res.record(f"[{n}] activation", "PASS" if ok else "FAIL",
                       note=f"engine {doc.get('engine')}, properties {doc.get('properties')} -> {doc.get('properties_target')}, "
                            f"plugin {doc.get('plugin')} {doc.get('plugin_version')}, valid till {doc.get('valid_till')}",
                       replconf=doc)
            files = cl.hostctl(n, "files", {"glob": root.rstrip("/") + "/*/*replconf*"}, check=False) or {}
            unit = cl.hostctl(n, "fb-svc", {"action": "status", "fb_service": cl.fb_service(n)}, check=False) or {}
            _, status = cl.api(n, "GET", "/v1/status", check_status=False)
            eng = (status or {}).get("firebird") if isinstance(status, dict) else None
            ok = files and unit.get("active") == "active" and (eng or {}).get("engine") == cl.fb_engine(n)
            res.record(f"[{n}] engine files (V-12, V-25)", "PASS" if ok else "FAIL",
                       note=f"unit {unit.get('unit')} {unit.get('active')}; engine {json.dumps(eng)[:200]}",
                       files=files, unit=unit, engine=eng)

    if "publications" in steps:
        st, body = cl.api(m, "POST", "/v1/publication/sync", {}, check_status=False)
        code = (body or {}).get("error", {}).get("code") if isinstance(body, dict) else None
        res.record("publications: 409 no_publications", "PASS" if st == 409 and code == "no_publications" else "FAIL",
                   note=f"{st} {code}")

    if "flow" in steps:
        tag = "legacy"
        if not a.no_load:
            cl.load_stop(tag)
            cl.load_start(paths, mode=a.load_mode, tx=a.tx, conns=a.conns, tag=tag)
            log(f"load for {a.minutes} min on {len(dbs)} database(s)")
            time.sleep(a.minutes * 60)
            cl.load_stop(tag)
        for p in paths:
            converge_and_record(cl, res, f"flow {p}", [p], a.catchup_timeout, reps)

    if "restart" in steps:
        tag = "legacy-restart"
        cl.load_stop(tag)
        if not a.no_load:
            cl.load_start(paths, mode=a.load_mode, tx=a.tx, conns=a.conns, tag=tag)
            time.sleep(30)
        try:
            ops.restart_firebird(cl, m, "test bed legacy: restart during the flow")
            res.record("master Firebird restart during the load", "PASS")
        except TbError as e:
            res.record("master Firebird restart during the load", "FAIL", note=str(e)[:300])
        if not a.no_load:
            time.sleep(30)
            cl.load_stop(tag)
        for p in paths:
            converge_and_record(cl, res, f"after the restart {p}", [p], a.catchup_timeout, reps)

    if "busy_writers" in steps and reps:
        d, r = dbs[0], reps[0]
        cl.hostctl(m, "hold-tx", {"db": d["path"], "seconds": 150})
        time.sleep(3)
        t0 = time.time()
        op = ops.reinit(cl, d["db_id"], r, mode="standard", timeout=900)
        err = str(op.get("error") or "")
        ok = op.get("state") != "succeeded" and "reinit_busy_writers" in err
        res.record("reinit with a writer open: reinit_busy_writers", "PASS" if ok else "FAIL",
                   note=f"{op.get('state')} after {time.time() - t0:.0f}s: {err[:200]}")
        time.sleep(max(0, 160 - (time.time() - t0)))
        op = ops.reinit(cl, d["db_id"], r, mode="standard", timeout=1800)
        res.record("reinit once the writer is gone", "PASS" if op.get("state") == "succeeded" else "FAIL",
                   note=f"{op.get('state')} {str(op.get('error') or '')[:200]}")
        converge_and_record(cl, res, f"after the reinit {d['path']}", [d["path"]], a.catchup_timeout, [r])

    if "properties" in steps:
        doc = replconf(cl, m)
        props, conf = doc.get("properties"), doc.get("conf_path")
        # Point it at a copy of the node's own file: the engine reads the
        # file replconf.properties names on every call, and a missing or
        # broken one fails every attach to the instance.
        copy = conf + ".tbcopy"
        cl.hostctl(m, "file-copy", {"from": conf, "to": copy})
        cl.hostctl(m, "file-put", {"path": props, "content_b64": b64(copy + "\n")})
        try:
            raised = wait_alert(cl, m, "replconf_properties_changed", True, timeout=60)
            after = replconf(cl, m)
            kept = after.get("properties_target") == copy and not after.get("active")
            res.record("foreign edit of replconf.properties: alert, no repair", "PASS" if raised and kept else "FAIL",
                       note=f"alert {raised}, points to {after.get('properties_target')}")
        finally:
            cl.hostctl(m, "file-restore", {"path": props})
            cl.hostctl(m, "remove-file", {"path": copy}, check=False)
        cleared = wait_alert(cl, m, "replconf_properties_changed", False, timeout=60)
        res.record("replconf.properties back: alert cleared", "PASS" if cleared else "FAIL")

    if "valid_date" in steps:
        doc = replconf(cl, m)
        was = doc.get("valid_till")
        soon = (datetime.date.today() + datetime.timedelta(days=20)).isoformat()
        cl.hostctl(m, "node-conf-set", {"key": "firebird.replconf_valid_till", "value_b64": b64(soon)})
        try:
            up = restart_node(cl, m)
            raised = up and wait_alert(cl, m, "replconf_expiring", True, timeout=90)
            got = replconf(cl, m).get("valid_till")
            res.record("valid date 20 days ahead: replconf_expiring", "PASS" if raised and got == soon else "FAIL",
                       note=f"file valid till {got}")
        finally:
            # Never leave a near or past date: HQbird then refuses every attach.
            cl.hostctl(m, "node-conf-set", {"key": "firebird.replconf_valid_till", "value_b64": b64(was or "2099-12-31")})
            restart_node(cl, m)
        back_doc = replconf(cl, m)
        back = back_doc.get("valid_till")
        if back_doc.get("valid_till_source") == "default":
            # The node's own default date (decision Р-С4, node 2027.4.3)
            # keeps replconf_expiring as info until its last 7 days.
            cleared = wait_alert_info_at_most(cl, m, "replconf_expiring", timeout=90)
        else:
            cleared = wait_alert(cl, m, "replconf_expiring", False, timeout=90)
        res.record("valid date back", "PASS" if back == (was or "2099-12-31") and cleared else "FAIL",
                   note=f"file valid till {back}")

    if "turn_to_normal" in steps and reps:
        d, r = dbs[-1], reps[0]
        rid = next((x["db_id"] for x in cl.databases(r) if x.get("path") == cl.replica_path(r, d["path"])), None)
        if not rid:
            res.record("turn to normal", "FAIL", note=f"no replica database for {d['path']} on {r}")
        else:
            st, body = cl.api(r, "POST", f"/v1/databases/{rid}/turntonormal", {}, check_status=False, timeout=600)
            res.record("turn to normal", "PASS" if st in (200, 202) else "FAIL", note=f"{st} {json.dumps(body)[:300]}")
            # The replica's file is an ordinary database now: overwriting it
            # is the operator's explicit choice (without it: target_not_replica).
            op = ops.reinit(cl, d["db_id"], r, mode="standard", timeout=1800, overwrite_non_replica=True)
            res.record("seeded again after turn to normal", "PASS" if op.get("state") == "succeeded" else "FAIL",
                       note=f"{op.get('state')} {str(op.get('error') or '')[:200]}")

    return res.finish()
