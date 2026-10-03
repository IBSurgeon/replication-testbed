"""The valid date of the node's replconf file on HQbird 2.5/3.0
(hqcluster-node docs/replconf-valid-date-plan.md, checks С-1..С-6). Needs
firebird.engine 2.5 or 3.0 on every node host, a node that has never had
firebird.replconf_valid_till in node.json (a fresh `install`) and
`tb.py dbs prepare` done (it activates the plugin).

Steps:
  default        С-1  every node: node.json got the node's first start + 30
                      days (journal event replconf_default_valid_till),
                      valid_till_source "default", the node's file holds the
                      date, replconf_expiring is info; a node restart keeps it
  first_start    С-6  firebird.log of the fresh install: "Valid date" lines,
                      and the file the engine read before the activation
                      (noted)
  activation     С-2  every node: plugin 2.1.0, replconf.properties names the
                      node's file; a Firebird restart and an attach add no
                      "Valid date" line
  auto_activate  С-2  (decision Р-С2 A) replconf.properties names a missing
                      file: a replica switches back by itself at its next
                      start. The master inside its restart window (the
                      default "always"; decision Р2, node 2027.4.3) is
                      switched at once too. Outside a timed window: the
                      master with Firebird running only raises a
                      critical alert; Firebird stopped, the node restarted
                      (its unit starts Firebird again): HQbird 3.0 stops on
                      the broken file and the node, checking each minute,
                      switches it; HQbird 2.5 keeps running (fbguard starts
                      the server again after each refused attach): the
                      master stays with a critical alert, or is switched
                      when the node finds the port closed twice
  config_date    С-3  what fbagent does: a new date in node.json with the
                      node stopped; at start the node's file has it, source
                      "config", no replconf_expiring; a past date is not
                      written (replconf_not_ready); the default back
  expiry         С-4  the replica's clock moved: 6 days before the default
                      date replconf_expiring is warn, on the date
                      replconf_expired is critical (attaches noted); clock
                      back: cleared
  old_plugin     С-5  on a replica, the engine as before the activation: the
                      plugin HQbird installs (before 2.1.0) and a DataGuard
                      file in v2.0, as DataGuard writes it on Linux.
                      2099-12-29 and 2028-01-31 work today, 2099-12-29 works
                      on the 30th (the plugin compares whole dates), and the
                      node sees no breakage. The v1.2 template HQbird
                      installs is refused (V-12: this plugin reads no v1.2),
                      and the node raises a critical alert; the replica then
                      switches by itself and attaches work

The clock steps (expiry, old_plugin) move the clock of one replica host and
put it back from this machine's clock. Linux hosts only."""
import base64
import datetime
import json
import time

from tblib import ops
from tblib import replconf as RC
from tblib.cluster import TbError, log
from tblib.results import Results

from ._common import converge_and_record, pick_replicas
from .legacy import b64, replconf, restart_node

HELP = "HQbird 2.5/3.0: the replconf valid date (default, activation, auto-activation, fbagent date, expiry, old plugin)"

STEPS = ["default", "first_start", "activation", "auto_activate", "config_date", "expiry", "old_plugin"]
KEY = "firebird.replconf_valid_till"

FB_LOG_PY = r'''
import json, sys
out = []
try:
    text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
except FileNotFoundError:
    text = ""
head = ""
for line in text.splitlines():
    if line[:1] not in ("\t", " ") and line.strip():
        head = line.strip()
    elif "valid date" in line.lower():
        out.append(head + " | " + line.strip())
print(json.dumps(out))
'''

JOURNAL_PY = r'''
import json, sys
out = []
try:
    for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
        if "replconf_default_valid_till" in line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
except FileNotFoundError:
    pass
print(json.dumps(out))
'''

NODE_KEY_PY = r'''
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("firebird", {}).get("replconf_valid_till", ""))
'''


def add_args(p):
    p.add_argument("--steps", default=",".join(STEPS), help="comma list of: " + ", ".join(STEPS))
    p.add_argument("--replica", default="", help="replica host for the clock steps; default the first one")
    p.add_argument("--catchup-timeout", type=int, default=900)


# --- host helpers ------------------------------------------------------------
def sh(cl, n, argv):
    rc, out, _ = cl.host(n).run_raw(argv, check=False)
    return rc, out or ""


def json_of(cl, n, argv, default):
    rc, out = sh(cl, n, argv)
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return default


def host_today(cl, n):
    return datetime.date.fromisoformat(sh(cl, n, ["date", "+%F"])[1].strip())


def node_key(cl, n):
    path = cl.host(n).join(cl.h(n)["paths"]["node"], "node.json")
    return sh(cl, n, ["python3", "-c", NODE_KEY_PY, path])[1].strip()


def file_info(cl, n, path):
    """Format, RegName, date and record count of a replconf file (never the
    records: they hold the replica's password)."""
    rc, out = sh(cl, n, ["base64", "-w0", path])
    if rc != 0 or not out.strip():
        return {"error": f"cannot read {path}"}
    return RC.decode(base64.b64decode(out.strip()))


def fb_valid_lines(cl, n):
    path = cl.host(n).join(cl.h(n)["firebird"]["root"], "firebird.log")
    return json_of(cl, n, ["python3", "-c", FB_LOG_PY, path], [])


def journal_defaults(cl, n):
    path = cl.host(n).join(cl.h(n)["paths"]["node"], "journal.jsonl")
    return json_of(cl, n, ["python3", "-c", JOURNAL_PY, path], [])


def alert(cl, n, code):
    st, doc = cl.api(n, "GET", "/v1/alerts", check_status=False)
    items = doc if isinstance(doc, list) else (doc or {}).get("alerts", []) if isinstance(doc, dict) else []
    return next((a for a in items if isinstance(a, dict) and a.get("code") == code), None)


def check(cl, n):
    """GET /v1/replconf runs the node's check at once; then the alerts."""
    return replconf(cl, n)


def wait_for(fn, timeout, every=5):
    end = time.time() + timeout
    while True:
        v = fn()
        if v or time.time() >= end:
            return v
        time.sleep(every)


def attach(cl, n, db):
    return cl.hostctl(n, "attach", {"db": db}, check=False) or {"ok": False, "out": "hostctl failed"}


def set_key(cl, n, value):
    """What fbagent does: node.json changed with the node stopped."""
    cl.hostctl(n, "node-svc", {"action": "stop"})
    cl.hostctl(n, "node-conf-set", {"key": KEY, "value_b64": b64(value)})
    cl.hostctl(n, "node-svc", {"action": "start"})
    return restart_wait(cl, n)


def restart_wait(cl, n):
    end = time.time() + 120
    while time.time() < end:
        try:
            st, _ = cl.api(n, "GET", "/v1/status", check_status=False, timeout=15)
            if st == 200:
                return True
        except TbError:
            pass
        time.sleep(5)
    return False


def clock_shift(cl, n, days):
    return cl.hostctl(n, "clock", {"shift_days": days}) or {}


def clock_back(cl, n):
    return cl.hostctl(n, "clock", {"epoch": int(time.time())}, check=False) or {}


def short(a):
    return f"{a.get('severity')}: {a.get('message', '')[:160]}" if a else "none"


# --- steps --------------------------------------------------------------------
def step_default(cl, res, nodes):
    for n in nodes:
        key = node_key(cl, n)
        doc = check(cl, n)
        info = file_info(cl, n, doc.get("conf_path", ""))
        ev = journal_defaults(cl, n)
        first = ev[0] if ev else {}
        fields = first.get("fields") or {}
        at = str(first.get("ts") or "")
        want = ""
        if at:
            want = (datetime.date.fromisoformat(at[:10]) + datetime.timedelta(days=30)).isoformat()
        # A compacted journal may have lost the event; the store still knows
        # the default (source "default"), and the date is at most 30 days on.
        ahead = (datetime.date.fromisoformat(key) - host_today(cl, n)).days if key else -1
        # A reinstall writes node.json anew, and the node then journals its
        # default again: more than one event is fine while every one holds
        # the same date (a changed date is the bug this catches).
        same = all(((e.get("fields") or {}).get("value")) == key for e in ev)
        ok = (key and doc.get("valid_till_source") == "default" and info.get("valid_till") == key and 0 < ahead <= 30
              and (not ev or (same and want == key)))
        res.record(f"[{n}] С-1 default: first start + 30 days in node.json and in the node's file", "PASS" if ok else "FAIL",
                   note=f"node.json {key}; journal {len(ev)} event(s), at {at[:19]} value {fields.get('value')} (want {want or '?'}); "
                        f"source {doc.get('valid_till_source')}; file {info}")
        a = alert(cl, n, "replconf_expiring")
        res.record(f"[{n}] С-1 default: replconf_expiring is info", "PASS" if a and a.get("severity") == "info" else "FAIL",
                   note=short(a))
        up = restart_node(cl, n)
        after, doc2 = node_key(cl, n), check(cl, n)
        ok = up and after == key and doc2.get("valid_till") == key and doc2.get("valid_till_source") == "default"
        res.record(f"[{n}] С-1 default: a node restart keeps the date", "PASS" if ok else "FAIL",
                   note=f"node.json {after}; file {doc2.get('valid_till')} {doc2.get('valid_till_source')}")


def step_first_start(cl, res, nodes):
    for n in nodes:
        doc = check(cl, n)
        props = doc.get("properties") or ""
        backups = cl.hostctl(n, "files", {"glob": props + ".hqcluster-*"}, check=False) or []
        engine_file, info = "", {}
        if backups:
            _, out = sh(cl, n, ["cat", backups[0]["path"]])
            engine_file = out.strip().splitlines()[0].strip() if out.strip() else ""
            info = file_info(cl, n, engine_file) if engine_file else {}
        plugins = cl.hostctl(n, "files", {"glob": (doc.get("plugin") or "") + ".hqcluster-*"}, check=False) or []
        lines = fb_valid_lines(cl, n)
        res.record(f"[{n}] С-6 first start: what the engine read before the activation (noted)", "PASS",
                   note=f"before: {engine_file or '-'} {json.dumps(info)}; old plugin kept: {len(plugins)}; "
                        f"firebird.log 'Valid date' lines: {len(lines)} {lines[:4]}")


def step_activation(cl, res, nodes, dbs):
    for n in nodes:
        doc = check(cl, n)
        ok = (doc.get("active") and doc.get("plugin_version") == "2.1.0"
              and doc.get("properties_target") == doc.get("conf_path"))
        res.record(f"[{n}] С-2 activation: plugin 2.1.0, replconf.properties names the node's file", "PASS" if ok else "FAIL",
                   note=f"{doc.get('properties')} -> {doc.get('properties_target')}; plugin {doc.get('plugin_version')}")
        before = len(fb_valid_lines(cl, n))
        try:
            ops.restart_firebird(cl, n, "test bed replconfdate: С-2")
        except TbError as e:
            res.record(f"[{n}] С-2 Firebird restart", "FAIL", note=str(e)[:300])
            continue
        db = dbs[0]["path"] if n == cl.cfg.master else cl.replica_path(n, dbs[0]["path"])
        at = attach(cl, n, db)
        lines = fb_valid_lines(cl, n)
        res.record(f"[{n}] С-2 after a Firebird restart: attach works, no new 'Valid date' line",
                   "PASS" if at.get("ok") and len(lines) == before else "FAIL",
                   note=f"attach {at}; new lines {lines[before:][:3]}")


def break_properties(cl, n, doc):
    missing = cl.host(n).join(cl.h(n)["firebird"]["root"], "tb-missing.hqbird")
    cl.hostctl(n, "file-put", {"path": doc["properties"], "content_b64": b64(missing + "\n")})
    return missing


def drop_bak(cl, n, doc):
    cl.hostctl(n, "remove-file", {"path": doc["properties"] + ".tb-bak"}, check=False)


def active_again(cl, n):
    d = check(cl, n)
    return d if d.get("active") and d.get("properties_target") == d.get("conf_path") else None


def step_auto_activate(cl, res, m, reps, dbs):
    for r in reps[:1]:
        doc = check(cl, r)
        missing = break_properties(cl, r, doc)
        try:
            # GET /v1/replconf runs the node's check; the node itself checks
            # only at start, once an hour and after its Firebird restarts.
            a = wait_for(lambda: (lambda x: x if x and x.get("severity") == "critical" else None)(
                check(cl, r) and alert(cl, r, "replconf_properties_changed")), 60)
            res.record(f"[{r}] С-2 auto: a missing file is a critical replconf_properties_changed",
                       "PASS" if a else "FAIL", note=short(a))
            restart_node(cl, r)
            d = wait_for(lambda: active_again(cl, r), 300, 10)
            at = attach(cl, r, cl.replica_path(r, dbs[0]["path"]))
            res.record(f"[{r}] С-2 auto: the replica switches back at its start, attach works",
                       "PASS" if d and at.get("ok") else "FAIL",
                       note=f"points to {(d or check(cl, r)).get('properties_target')}; attach {at}")
        finally:
            if not active_again(cl, r):
                cl.hostctl(r, "file-restore", {"path": doc["properties"]}, check=False)
                cl.hostctl(r, "fb-svc", {"action": "restart", "fb_service": cl.fb_service(r)}, check=False)
            drop_bak(cl, r, doc)
        log(f"[{r}] auto-activation checked ({missing})")

    doc = check(cl, m)
    break_properties(cl, m, doc)
    # Decision Р2 (hqcluster-node 2027.4.3): a master also activates without
    # a request inside its restart window. With the test bed's default
    # window "always" the master is switched at once; only a timed window
    # keeps it waiting outside the window, with a critical alert.
    in_window = str(cl.cfg.windows.get("master_restart_window", "always")).strip().lower() in ("", "always")
    try:
        restart_node(cl, m)
        if in_window:
            d = wait_for(lambda: active_again(cl, m), 300, 10)
            at = attach(cl, m, dbs[0]["path"])
            res.record(f"[{m}] С-2 auto (Р2): the master in its restart window is switched, attach works",
                       "PASS" if d and at.get("ok") else "FAIL",
                       note=f"points to {(d or check(cl, m)).get('properties_target')}; attach {at}")
            return
        time.sleep(20)
        d, a = check(cl, m), alert(cl, m, "replconf_properties_changed")
        # The text of the alert is the last check's: GET /v1/replconf says the
        # node "switches when it may", its start said why it may not.
        ok = not d.get("active") and a and a.get("severity") == "critical"
        res.record(f"[{m}] С-2 auto: the master with Firebird running is not switched, critical alert",
                   "PASS" if ok else "FAIL", note=f"points to {d.get('properties_target')}; alert {short(a)}")
        # The node's unit wants Firebird: the node's start starts it again.
        # HQbird 3.0 then stops at once on the broken file (V-12), and the
        # node, checking again each minute, switches it. HQbird 2.5 keeps
        # running (fbguard restarts the server), so the master is not switched.
        cl.hostctl(m, "fb-svc", {"action": "stop", "fb_service": cl.fb_service(m)})
        time.sleep(20)
        restart_node(cl, m)
        time.sleep(20)
        first = (cl.hostctl(m, "fb-svc", {"action": "status", "fb_service": cl.fb_service(m)}, check=False) or {}).get("active")
        d = wait_for(lambda: active_again(cl, m), 300, 10)
        unit = cl.hostctl(m, "fb-svc", {"action": "status", "fb_service": cl.fb_service(m)}, check=False) or {}
        at = attach(cl, m, dbs[0]["path"])
        d = d or active_again(cl, m)   # the end state: a check a moment later may have switched it
        if first != "active":
            res.record(f"[{m}] С-2 auto: Firebird stopped on the broken file after the node's start: switched, Firebird started",
                       "PASS" if d and unit.get("active") == "active" and at.get("ok") else "FAIL",
                       note=f"unit {first} -> {unit.get('active')}; points to {(d or check(cl, m)).get('properties_target')}; attach {at}")
        else:
            # HQbird 2.5: fbguard starts the server again after each refused
            # attach. The node switches only when it finds the port closed
            # twice (Firebird does not work), which depends on timing: either
            # outcome is right, a half-way one is not.
            a = alert(cl, m, "replconf_properties_changed")
            kept = not d and a and a.get("severity") == "critical"
            fixed = d and at.get("ok")
            res.record(f"[{m}] С-2 auto: Firebird kept running on the broken file: not switched with a critical alert, "
                       "or switched when the node found the port closed",
                       "PASS" if kept or fixed else "FAIL",
                       note=f"{'switched' if d else 'not switched'}; unit {first} -> {unit.get('active')}; attach {at}; alert {short(a)}")
    finally:
        if not active_again(cl, m):
            cl.hostctl(m, "file-restore", {"path": doc["properties"]}, check=False)
        cl.hostctl(m, "fb-svc", {"action": "start", "fb_service": cl.fb_service(m)}, check=False)
        drop_bak(cl, m, doc)


def step_config_date(cl, res, m):
    was = node_key(cl, m)
    src = check(cl, m).get("valid_till_source")
    new = (host_today(cl, m) + datetime.timedelta(days=200)).isoformat()
    past = (host_today(cl, m) - datetime.timedelta(days=1)).isoformat()
    try:
        up = set_key(cl, m, new)
        d = check(cl, m)
        info = file_info(cl, m, d.get("conf_path", ""))
        a = alert(cl, m, "replconf_expiring")
        ok = up and d.get("valid_till") == new and info.get("valid_till") == new and d.get("valid_till_source") == "config" and not a
        res.record("С-3 a new date in node.json (as fbagent writes it): the node's file has it, source config",
                   "PASS" if ok else "FAIL",
                   note=f"node.json {new}; file {info.get('valid_till')}; source {d.get('valid_till_source')}; replconf_expiring {short(a)}")
        up = set_key(cl, m, past)
        d = check(cl, m)
        info = file_info(cl, m, d.get("conf_path", ""))
        a = alert(cl, m, "replconf_not_ready")
        ok = up and info.get("valid_till") == new and a is not None
        res.record("С-3 a past date is not written: the file keeps its date, replconf_not_ready",
                   "PASS" if ok else "FAIL",
                   note=f"node up {up}; file on disk {info.get('valid_till')} (API valid_till {d.get('valid_till')}); alert {short(a)}")
    finally:
        set_key(cl, m, was)
    d = check(cl, m)
    a = alert(cl, m, "replconf_expiring")
    ok = d.get("valid_till") == was and d.get("valid_till_source") == src and (src != "default" or (a and a.get("severity") == "info"))
    res.record("С-3 the default back: source default, replconf_expiring info", "PASS" if ok else "FAIL",
               note=f"file {d.get('valid_till')} {d.get('valid_till_source')}; alert {short(a)}")


def step_expiry(cl, res, r, dbs):
    db = cl.replica_path(r, dbs[0]["path"])
    doc = check(cl, r)
    if doc.get("valid_till_source") != "default":
        res.record("С-4 expiry", "SKIP", note=f"the replica's date is not the default ({doc.get('valid_till_source')})")
        return
    left = (datetime.date.fromisoformat(doc["valid_till"]) - host_today(cl, r)).days
    try:
        c = clock_shift(cl, r, left - 6)
        a = alert(cl, r, "replconf_expiring") if check(cl, r) else None
        res.record("С-4 6 days before the default date: replconf_expiring warn",
                   "PASS" if a and a.get("severity") == "warn" else "FAIL", note=f"host date {c.get('date')}; {short(a)}")
        c = clock_shift(cl, r, 6)
        check(cl, r)
        a = alert(cl, r, "replconf_expired")
        at = attach(cl, r, db)
        lines = fb_valid_lines(cl, r)
        res.record("С-4 on the default date: replconf_expired critical (attach noted)",
                   "PASS" if a and a.get("severity") == "critical" else "FAIL",
                   note=f"host date {c.get('date')}; {short(a)}; attach {at}; firebird.log {lines[-2:]}")
    finally:
        c = clock_back(cl, r)
    check(cl, r)
    a, x = alert(cl, r, "replconf_expiring"), alert(cl, r, "replconf_expired")
    at = attach(cl, r, db)
    res.record("С-4 clock back: replconf_expired gone, info again, attach works",
               "PASS" if not x and a and a.get("severity") == "info" and at.get("ok") else "FAIL",
               note=f"host date {c.get('date')} ntp {c.get('ntp')}; expiring {short(a)}; expired {short(x)}; attach {at}")


def step_old_plugin(cl, res, r, dbs):
    db = cl.replica_path(r, dbs[0]["path"])
    doc = check(cl, r)
    plugin, props, root = doc.get("plugin") or "", doc.get("properties") or "", cl.h(r)["firebird"]["root"]
    olds = cl.hostctl(r, "files", {"glob": plugin + ".hqcluster-*"}, check=False) or []
    if not plugin or not olds:
        res.record("С-5 old plugin", "SKIP", note=f"no plugin kept from before the activation ({plugin})")
        return
    dg = cl.host(r).join(root, "tb-dataguard.hqbird")
    unit = cl.fb_service(r)

    def fb(action):
        # systemctl, not the node: a restart through the node checks (and
        # may switch) the engine at once.
        return cl.hostctl(r, "fb-svc", {"action": action, "fb_service": unit}, check=False) or {}

    def put_dg(date, fmt="v2.0"):
        # DataGuard on Linux writes v2.0 (the file a fresh install has).
        cl.hostctl(r, "file-put", {"path": dg, "content_b64": base64.b64encode(RC.encode(date, fmt=fmt)).decode()})

    def engine_sees(label, want_ok, want_broken, extra=""):
        fb("restart")
        at = settled_attach()
        d, a = check(cl, r), alert(cl, r, "replconf_properties_changed")
        ok = bool(at.get("ok")) == want_ok and bool(d.get("in_use_broken")) == want_broken
        if want_broken:
            ok = ok and a is not None and a.get("severity") == "critical"
        res.record(label, "PASS" if ok else "FAIL",
                   note=f"{extra}plugin {d.get('plugin_version') or '(before 2.1.0)'}; attach {at}; "
                        f"node: in use {d.get('in_use_problem') or '-'}; alert {short(a)}; firebird.log {fb_valid_lines(cl, r)[-1:]}")

    def settled_attach():
        # Firebird may still be starting: a refusal counts after 3 tries.
        at = {}
        for _ in range(3):
            time.sleep(5)
            at = attach(cl, r, db)
            if at.get("ok"):
                break
        return at

    try:
        # The engine as a fresh install has it: the old plugin, DataGuard's
        # file. The plugin is replaced with Firebird stopped: the server maps it.
        fb("stop")
        cl.hostctl(r, "file-copy", {"from": plugin, "to": plugin + ".tbsave"})
        cl.hostctl(r, "file-copy", {"from": olds[0]["path"], "to": plugin})
        put_dg("2099-12-29")
        cl.hostctl(r, "file-put", {"path": props, "content_b64": b64(dg + "\n")})
        today = host_today(cl, r)
        engine_sees(f"С-6 old plugin, DataGuard v2.0 2099-12-29, day {today.day}: attach works, the node sees no breakage",
                    True, False)
        # DataGuard with no databases writes 2028-01-31. The plugin compares
        # whole dates: it works in any month (not the field-wise rule of the
        # replconf-master sources).
        put_dg("2028-01-31")
        engine_sees(f"С-6 old plugin, DataGuard v2.0 2028-01-31, month {today.month}: attach works (whole dates)",
                    True, False)
        # The 30th: 2099-12-29 still works (no 30th/31st rule).
        put_dg("2099-12-29")
        c = clock_shift(cl, r, (30 - today.day) if today.day <= 30 else 0)
        engine_sees("С-5 the 30th, old plugin, DataGuard v2.0 2099-12-29: attach works, the node sees no breakage",
                    True, False, f"host date {c.get('date')}; ")
        # V-12: a fresh HQbird reads the v1.2 template HQbird installs, and
        # this plugin refuses every v1.2 file.
        tpl = "/opt/hqbird/conf/replconf.hqbird.empty"
        rc, raw = sh(cl, r, ["base64", "-w0", tpl])
        if rc == 0 and raw.strip():
            # Its content, with the rights of the other test files.
            cl.hostctl(r, "file-put", {"path": dg, "content_b64": raw.strip()})
        else:
            put_dg("2099-12-29", fmt="v1.2")
        engine_sees(f"С-6 V-12: old plugin, the v1.2 template {file_info(cl, r, dg)}: attach refused, the node raises a critical alert",
                    False, True)
        restart_node(cl, r)
        d = wait_for(lambda: active_again(cl, r), 300, 10)
        at = settled_attach()
        res.record("С-5 the broken engine: the replica switches to its file at start, attach works",
                   "PASS" if d and d.get("plugin_version") == "2.1.0" and at.get("ok") else "FAIL",
                   note=f"plugin {(d or check(cl, r)).get('plugin_version')}; attach {at}")
    finally:
        clock_back(cl, r)
        if not active_again(cl, r):
            fb("stop")
            cl.hostctl(r, "file-copy", {"from": plugin + ".tbsave", "to": plugin}, check=False)
            cl.hostctl(r, "file-restore", {"path": props}, check=False)
            fb("start")
            cl.hostctl(r, "node-svc", {"action": "start"}, check=False)
            restart_wait(cl, r)
        for f in (plugin + ".tbsave", props + ".tb-bak", dg, dg + ".tb-bak"):
            cl.hostctl(r, "remove-file", {"path": f}, check=False)
    d = check(cl, r)
    res.record("С-5 after: the node's file active, plugin 2.1.0",
               "PASS" if d.get("active") and d.get("plugin_version") == "2.1.0" else "FAIL",
               note=f"{d.get('properties_target')} plugin {d.get('plugin_version')}")


def run(cl, a):
    m = cl.cfg.master
    reps = pick_replicas(cl, "all")
    nodes = [m] + reps
    for n in nodes:
        if not ops.legacy(cl, n):
            raise TbError(f"[{n}] firebird.engine is '{cl.fb_engine(n)}': replconfdate wants 2.5 or 3.0 on every node host")
        if cl.h(n)["os"] != "linux":
            raise TbError(f"[{n}] replconfdate runs on Linux hosts only")
    steps = [s.strip() for s in a.steps.split(",") if s.strip()]
    for s in steps:
        if s not in STEPS:
            raise TbError(f"unknown step {s} (known: {', '.join(STEPS)})")
    dbs = cl.test_dbs()
    if not dbs:
        raise TbError("no test databases (run 'tb.py dbs prepare')")
    r = a.replica or (reps[0] if reps else "")
    res = Results("replconfdate", vars(a).copy())
    if "default" in steps:
        step_default(cl, res, nodes)
    if "first_start" in steps:
        step_first_start(cl, res, nodes)
    if "activation" in steps:
        step_activation(cl, res, nodes, dbs)
    if "auto_activate" in steps:
        step_auto_activate(cl, res, m, reps, dbs)
    if "config_date" in steps:
        step_config_date(cl, res, m)
    if r and "expiry" in steps:
        step_expiry(cl, res, r, dbs)
    if r and "old_plugin" in steps:
        step_old_plugin(cl, res, r, dbs)
    # Nothing above may leave replication broken.
    for d in dbs:
        converge_and_record(cl, res, f"replication after the steps {d['path']}", [d["path"]], a.catchup_timeout, reps)
    return res.finish()
