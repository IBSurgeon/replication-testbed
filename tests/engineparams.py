"""Engine replication keys (hqcluster-node plan section 6, step 8.9) on
Firebird 4/5: GET/POST /v1/engine/params on the master and the replicas.

Master: a value for some databases rewrites replication.conf and marks every
database PENDING_RESTART, but the node does not restart Firebird itself; a
restart through the API loads the values. The node default reaches the
databases without a value of their own; unset brings everything back.
Replica: a node default for all databases; the replica restarts Firebird on
its own inside the restart window (when no master shares the instance).

Every change is undone at the end."""
import time

from tblib.cluster import TbError, log
from tblib.results import Results

from ._common import pick_replicas

HELP = "engine replication keys: per database and node default, master and replicas"


def add_args(p):
    p.add_argument("--replicas", default="all")
    p.add_argument("--restart-timeout", type=int, default=600)


def _params(cl, host):
    st, doc = cl.api(host, "GET", "/v1/engine/params", check_status=False)
    if st == 404:
        raise TbError(f"[{host}] the node has no /v1/engine/params (older than protocol minor 9)")
    if st != 200:
        raise TbError(f"[{host}] GET /v1/engine/params: {st} {doc}")
    return doc


def _value(doc, db_id, key):
    for d in doc.get("databases", []):
        if d["db_id"] == db_id:
            return d["values"].get(key, {})
    raise TbError(f"no database {db_id} in the engine params")


def _post(cl, host, body):
    st, r = cl.api(host, "POST", "/v1/engine/params", body, check_status=False)
    if st != 200:
        raise TbError(f"[{host}] POST /v1/engine/params {body}: {st} {r}")
    return r


def _pending(cl, host):
    st, s = cl.api(host, "GET", "/v1/status")
    return bool(s.get("pending_restart")) if st == 200 else None


def run(cl, a):
    res = Results("engineparams", vars(a).copy())
    master = cl.cfg.master
    dbs = cl.test_dbs(which="all")
    if len(dbs) < 2:
        raise TbError("engine params want at least two test databases (tb.py dbs prepare --count 2)")
    doc = _params(cl, master)
    count_key = "journal_segment_count"
    if not any(p["name"] == count_key for p in doc["catalog"]):
        raise TbError(f"[{master}] catalog has no {count_key}: engine {doc.get('engine')} is not 4/5")
    log(f"master engine {doc['engine']}, {len(doc['databases'])} database(s), section={doc['section']}")
    first, rest = dbs[0]["db_id"], [d["db_id"] for d in dbs[1:]]
    was = _value(doc, first, count_key)

    try:
        # A dry run changes nothing.
        r = _post(cl, master, {"target": {"db_ids": [first]}, "set": {count_key: "128"}, "dry_run": True})
        ok = r.get("restart_firebird") and _value(_params(cl, master), first, count_key) == was
        res.record("master dry run", "PASS" if ok else "FAIL", note=str(r)[:300])

        # One database: the file changes, the node does not restart Firebird.
        r = _post(cl, master, {"target": {"db_ids": [first]}, "set": {count_key: "128"}})
        v = _value(_params(cl, master), first, count_key)
        ok = r.get("pending_restart") and v.get("value") == "128" and v.get("source") == "database"
        res.record("master: value for one database", "PASS" if ok else "FAIL", note=str(r)[:300])
        time.sleep(30)
        res.record("master does not restart Firebird itself",
                   "PASS" if _pending(cl, master) else "FAIL")
        st, _ = cl.api(master, "POST", "/v1/firebird/restart", {"ignore_window": True, "reason": "tb engineparams"},
                       check_status=False, timeout=a.restart_timeout)
        res.record("master restart through the API", "PASS" if st in (200, 202) else "FAIL", note=str(st))

        # The node default reaches the other databases, not the first one.
        r = _post(cl, master, {"target": {"node_default": True}, "set": {count_key: "96"}})
        doc = _params(cl, master)
        ok = _value(doc, first, count_key).get("value") == "128" and all(
            _value(doc, i, count_key).get("value") == "96" for i in rest)
        res.record("master: node default", "PASS" if ok else "FAIL", note=str(r)[:300])

        # Replicas: a node default for all databases, restart in the window.
        for rep in pick_replicas(cl, a.replicas):
            rdoc = _params(cl, rep)
            if not any(p["name"] == "apply_idle_timeout" for p in rdoc["catalog"]):
                res.record(f"[{rep}] replica catalog", "FAIL", note="no apply_idle_timeout")
                continue
            r = _post(cl, rep, {"target": {"node_default": True}, "set": {"apply_idle_timeout": "20"}})
            ok = all(d["values"]["apply_idle_timeout"].get("value") == "20" for d in _params(cl, rep)["databases"])
            res.record(f"[{rep}] replica node default", "PASS" if ok else "FAIL", note=str(r)[:300])
            deadline = time.time() + a.restart_timeout
            while time.time() < deadline and _pending(cl, rep):
                time.sleep(10)
            res.record(f"[{rep}] replica restarts Firebird in its window",
                       "PASS" if not _pending(cl, rep) else "FAIL")
    finally:
        # Undo: every value back to the node's own.
        try:
            _post(cl, master, {"target": {"db_ids": [first]}, "unset": [count_key]})
            _post(cl, master, {"target": {"node_default": True}, "unset": [count_key]})
            back = _value(_params(cl, master), first, count_key)
            res.record("master unset", "PASS" if back.get("value") == was.get("value") else "FAIL", note=str(back))
            for rep in pick_replicas(cl, a.replicas):
                _post(cl, rep, {"target": {"node_default": True}, "unset": ["apply_idle_timeout"]})
            cl.api(master, "POST", "/v1/firebird/restart", {"ignore_window": True, "reason": "tb engineparams undo"},
                   check_status=False, timeout=a.restart_timeout)
        except TbError as e:
            res.record("undo", "FAIL", note=str(e))
    return res.finish()
