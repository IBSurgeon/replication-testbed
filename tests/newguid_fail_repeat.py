"""POST /v1/databases/{id}/newguid under an operator's nbackup lock, then
the repeat (hqcluster-node review 2026-10-22, finding C-08).

The bug: a newguid run that failed after nbackup -L brought the database
online with the lock still on. New transactions went into the delta. The
repeat saw the lock, ran nbackup -F and removed the delta: every transaction
committed in between was lost. Now a failed run merges the delta back
(nbackup -N) first, and a lock the node did not take is refused
(409 backup_lock: "an operator's backup; end it first").

The test makes the second half of that by hand: it takes the nbackup lock
itself and commits --rows rows, so they are in the delta only. Then:

  setup             TB_NEWGUID made (and published); our nbackup -L made a
                    delta; the rows are committed into it
  locked run        newguid is refused: 409 backup_lock is the PASS path.
                    500 newguid_failed (an interrupted-style error) also
                    passes when nothing below was lost; 200 is a FAIL
  nothing lost      the same GUID; the rows are there; the delta is still
                    there (or merged back by the node's own nbackup -N);
                    the database is online
  unlock            our nbackup -N merges the delta; the rows are there
  repeat            newguid answers 200: a new GUID, the header shows it,
                    no lock, no delta, online
  rows kept         the rows are there after the new GUID
  no alert          no newguid_interrupted alert of the database is left
  <replica>         the replicas need a reinit after a new GUID: each one is
                    reinitialized, then its rows match

Target (--target): auto takes a database the first companion master holds
(made by a promote, e.g. the guidpromote test), else a test database of
the main master. On a master node newguid needs "force": the test sends it.
A new GUID on the main master makes every replica of the database need a
reinit; the test does it. finally: our lock is taken off if it is still on,
TB_NEWGUID is dropped.
"""
import base64
import json
import time

from tblib import ops
from tblib.cluster import TbError, log
from tblib.results import Results

from ._common import converge_and_record, norm

HELP = "newguid refuses an operator's nbackup lock and keeps the delta; the repeat works (review C-08)"

TABLE = "TB_NEWGUID"
ALERT = "newguid_interrupted"


def add_args(p):
    p.add_argument("--target", default="auto", choices=["auto", "companion", "master"],
                   help="auto: a database of the first companion master, else the main master")
    p.add_argument("--db", default="",
                   help="master: a test database (db_id or folder), default the last one; "
                        "companion: the companion's db_id, default its first database")
    p.add_argument("--rows", type=int, default=200, help="rows committed while our lock is on")
    p.add_argument("--reinit-timeout", type=int, default=900)
    p.add_argument("--catchup-timeout", type=int, default=600)


def b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def G(g):
    return (g or "").upper().strip("{}")


class Target:
    """The node that runs newguid, and the database: host (Firebird and the
    file), API, db_id, path, and the replicas [(host, node_id)]."""

    def __init__(self, cl, kind, host, db_id, path, replicas):
        self.cl, self.kind, self.host, self.db_id, self.path = cl, kind, host, db_id, path
        self.replicas = replicas
        self.node_id = cl.h(host)["companion"]["node_id"] if kind == "companion" else cl.h(host)["node_id"]

    def api(self, method, path, body=None, timeout=60):
        if self.kind == "companion":
            return self.cl.companion_api(self.host, method, path, body, timeout=timeout, check_status=False)
        return self.cl.api(self.host, method, path, body, timeout=timeout, check_status=False)

    def label(self):
        return f"{self.kind} {self.node_id} {self.db_id} ({self.path})"


def pick_target(cl, a):
    by_id = {cl.h(r)["node_id"]: r for r in cl.cfg.replicas}
    if a.target in ("auto", "companion"):
        for host in cl.cfg.companion_hosts():
            st, dbs = cl.companion_api(host, "GET", "/v1/databases", check_status=False)
            for x in dbs if st == 200 and isinstance(dbs, list) else []:
                if not isinstance(x, dict) or x.get("state") == "ORPHANED":
                    continue
                if a.db and x.get("db_id") != a.db:
                    continue
                if not cl.hostctl(host, "files", {"glob": x.get("path") or ""}, check=False):
                    continue
                _, rows = cl.companion_api(host, "GET", "/v1/transfer", check_status=False)
                peers = sorted({r.get("peer_id") for r in rows or [] if isinstance(r, dict)
                                and r.get("db_id") == x["db_id"] and r.get("peer_id") in by_id})
                return Target(cl, "companion", host, x["db_id"], x["path"], [(by_id[p], p) for p in peers])
        if a.target == "companion":
            return None
        log("newguid_fail_repeat: no companion database (run guidpromote first); the main master is used")
    dbs = cl.test_dbs(which=a.db or "all")
    if not dbs:
        raise TbError("no test databases (run 'tb.py dbs prepare')")
    d = dbs[-1]
    return Target(cl, "master", cl.cfg.master, d["db_id"], d["path"],
                  [(r, cl.h(r)["node_id"]) for r in cl.cfg.replicas])


def s1(cl, host, cmd, args, timeout=None):
    rc, res, _, out = cl.module(host, "93-stage1", cmd, args, check=False, timeout=timeout)
    if rc != 0 or not isinstance(res, dict):
        raise TbError(f"[{host}] 93-stage1 {cmd}: exit {rc} {(out or '')[-400:]}")
    return res


def fb(cl, host):
    return {"fb_root": cl.h(host)["firebird"]["root"], "port": cl.fb_port(host)}


def sql(cl, t, text):
    r = s1(cl, t.host, "sql-utf8", dict(fb(cl, t.host), db=t.path, sql_b64=b64(text)), timeout=300)
    if not r.get("ok"):
        raise TbError(f"[{t.host}] isql failed: {str(r.get('out'))[-400:]}")
    return r


def rows(cl, t):
    r = s1(cl, t.host, "count-tables", dict(fb(cl, t.host), db=t.path, tables_b64=b64(json.dumps([TABLE]))))
    tabs = r.get("tables") or [{}]
    return tabs[0].get("rows")


def header(cl, t):
    return cl.hostctl(t.host, "db-header", {"db": t.path}, check=False) or {}


def delta(cl, t):
    return cl.hostctl(t.host, "files", {"glob": t.path + ".delta"}, check=False) or []


def lock_on(h):
    return "backup" in (h.get("attributes") or "").lower()


def offline(h):
    return "shutdown" in (h.get("attributes") or "").lower()


def err_code(body):
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return body["error"].get("code"), str(body["error"].get("message") or "")
    return None, str(body)[:300]


def alerts_of(cl, t, code=None):
    _, body = t.api("GET", "/v1/alerts")
    items = body if isinstance(body, list) else []
    return [x for x in items if isinstance(x, dict) and (code is None or x.get("code") == code)
            and x.get("database") in (t.db_id, "", None)]


def newguid(cl, t):
    return t.api("POST", f"/v1/databases/{t.db_id}/newguid", {"force": True}, timeout=660)


def reinit_companion(cl, t, rhost, rid, timeout):
    """A reinit from the companion master to one of its replicas."""
    body = {"to": rid, "mode": "standard", "ignore_window": True, "allow_restart": True,
            "hold_on_long_transactions": True}
    end = time.time() + 600
    while True:
        st, out = t.api("POST", f"/v1/databases/{t.db_id}/reinit", body)
        if st in (200, 202):
            break
        code, _ = err_code(out)
        if st == 409 and code in ("long_transactions", "reinit_lock_recovery_pending") and time.time() < end:
            time.sleep(20)
            continue
        raise TbError(f"reinit {t.db_id} -> {rid}: HTTP {st} {json.dumps(out)[:400]}")
    end = time.time() + timeout
    while time.time() < end:
        _, op = t.api("GET", f"/v1/operations/{out['operation_id']}")
        if isinstance(op, dict) and op.get("state") not in ("in_progress", "queued", "pending", None):
            return op
        time.sleep(5)
    raise TbError(f"reinit {t.db_id} -> {rid}: the operation did not end in {timeout}s")


def replicas_after(cl, res, a, t, n):
    """Every replica of the database gets a reinit (a new GUID needs one),
    then its rows must match."""
    if not t.replicas:
        res.record("replicas", "SKIP", note=f"{t.node_id} sends {t.db_id} to no replica")
        return
    for rhost, rid in t.replicas:
        try:
            if t.kind == "master":
                op = ops.reinit(cl, t.db_id, rhost, timeout=a.reinit_timeout)
            else:
                op = reinit_companion(cl, t, rhost, rid, a.reinit_timeout)
            ok = op.get("state") == "succeeded"
            res.record(f"{rhost}: reinit after the new GUID", "PASS" if ok else "FAIL",
                       note=f"op {op.get('state')} {str(op.get('error') or '')[:300]}")
        except TbError as e:
            res.record(f"{rhost}: reinit after the new GUID", "FAIL", note=str(e)[:400])
    if t.kind == "master":
        converge_and_record(cl, res, "converge", [t.path], a.catchup_timeout, [h for h, _ in t.replicas])
        return
    for rhost, rid in t.replicas:
        st, v = t.api("POST", f"/v1/databases/{t.db_id}/verify",
                      {"to": rid, "tables": TABLE, "wait_for_sync_sec": 120}, timeout=300)
        tabs = (v or {}).get("tables") or [] if isinstance(v, dict) else []
        mine = next((x for x in tabs if norm(x.get("table")).strip('"') == TABLE.lower()), {})
        ok = st == 200 and v.get("ok") and mine.get("replica_count") == n
        res.record(f"{rhost}: rows match (verify {TABLE})", "PASS" if ok else "FAIL",
                   note=f"HTTP {st} ok {v.get('ok') if isinstance(v, dict) else None}; "
                        f"master {mine.get('master_count')} replica {mine.get('replica_count')}; "
                        f"{str((v or {}).get('message') if isinstance(v, dict) else v)[:200]}")


def run(cl, a):
    res = Results("newguid_fail_repeat", vars(a).copy())
    t = pick_target(cl, a)
    if t is None:
        res.record("target", "SKIP", note="no companion master holds a database (run guidpromote first), "
                                          "or --target master")
        return res.finish()
    if ops.legacy(cl, t.host):
        res.record("target", "SKIP", note=f"{t.host}: HQbird {cl.fb_engine(t.host)}; the stage 1 lab is "
                                          f"Firebird 5 (nbackup -F of 2.5/3.0 is not covered here)")
        return res.finish()
    log(f"newguid_fail_repeat: {t.label()}, replicas {[h for h, _ in t.replicas]}")
    n = a.rows
    ours = False   # our nbackup lock may be on
    made = False
    try:
        # --- setup ---------------------------------------------------------
        h0 = header(cl, t)
        guid0 = G(h0.get("guid"))
        if not guid0 or lock_on(h0) or delta(cl, t):
            res.record("setup: the database is free", "FAIL",
                       note=f"guid '{guid0}', attributes '{h0.get('attributes')}', delta {delta(cl, t)}: "
                            f"a lock or a delta before the test; end it first")
            return res.finish()
        sql(cl, t, f"recreate table {TABLE} (ID integer not null primary key, V varchar(20));\ncommit;\n")
        made = True
        st, pub = t.api("POST", f"/v1/databases/{t.db_id}/publication", {})
        log(f"publication sync after the new table: HTTP {st}")
        ours = True
        lk = cl.hostctl(t.host, "nbackup-lock", {"db": t.path}, check=False) or {}
        sql(cl, t, "set term ^;\n"
                   "execute block as declare i integer = 1; begin\n"
                   f"  while (i <= {n}) do begin\n"
                   f"    insert into {TABLE} (ID, V) values (:i, 'row ' || :i);\n"
                   "    i = i + 1;\n"
                   "  end\n"
                   "end^\n"
                   "set term ;^\n"
                   "commit;\n")
        h1, d1, c1 = header(cl, t), delta(cl, t), rows(cl, t)
        ok = bool(lk.get("delta")) and lock_on(h1) and bool(d1) and c1 == n
        res.record("setup: rows committed into our delta", "PASS" if ok else "FAIL",
                   note=f"lock {lk or 'failed'}; attributes '{h1.get('attributes')}'; "
                        f"delta {[f['size'] for f in d1]} bytes; rows {c1}/{n}")
        if not ok:
            return res.finish()
        locked_alerts = [x.get("code") for x in alerts_of(cl, t) if x.get("code") != "firebird_log_error"]
        log(f"alerts while our lock is on: {locked_alerts}")

        # --- the run under our lock ---------------------------------------------
        st, out = newguid(cl, t)
        code, msg = err_code(out)
        if st == 409 and code == "backup_lock":
            res.record("locked run: refused", "PASS",
                       note=f"409 backup_lock (the PASS path: a lock the node did not take is refused): {msg[:200]}")
        elif st == 500 and code == "newguid_failed":
            res.record("locked run: refused", "PASS",
                       note=f"500 newguid_failed (interrupted-style; passes only if nothing below is lost): {msg[:300]}")
        elif st == 200:
            res.record("locked run: refused", "FAIL",
                       note=f"200: a new GUID under a lock the node did not take; {json.dumps(out)[:300]}")
        else:
            res.record("locked run: refused", "FAIL", note=f"HTTP {st} {code}: {msg[:300]}")
        h2, d2, c2 = header(cl, t), delta(cl, t), rows(cl, t)
        merged = not d2 and not lock_on(h2)
        ok = G(h2.get("guid")) == guid0 and c2 == n and (bool(d2) or merged) and not offline(h2)
        res.record("locked run: nothing lost", "PASS" if ok else "FAIL",
                   note=f"guid {G(h2.get('guid'))} (was {guid0}); rows {c2}/{n}; "
                        f"delta {'kept' if d2 else 'merged back by the node' if merged else 'GONE'}; "
                        f"attributes '{h2.get('attributes')}'")
        al = alerts_of(cl, t, ALERT)
        if st == 409:
            res.record("locked run: no newguid_interrupted", "FAIL" if al else "PASS",
                       note="; ".join(str(x.get("message"))[:200] for x in al) or "none (nothing was started)")
        else:
            log(f"after the locked run: {len(al)} {ALERT} alert(s): {[str(x.get('message'))[:160] for x in al]}")

        # --- our unlock -----------------------------------------------------------
        if lock_on(h2) or d2:
            u = cl.hostctl(t.host, "nbackup-unlock", {"db": t.path}, check=False) or {}
        else:
            u = {"unlocked": "by the node", "delta_left": False}
        ours = False
        h3, c3 = header(cl, t), rows(cl, t)
        ok = u.get("unlocked") and not u.get("delta_left") and not lock_on(h3) and c3 == n
        res.record("unlock: the delta merged, rows kept", "PASS" if ok else "FAIL",
                   note=f"{u or 'nbackup -N failed'}; attributes '{h3.get('attributes')}'; rows {c3}/{n}")

        # --- the repeat -------------------------------------------------------------
        st, out = newguid(cl, t)
        h4 = header(cl, t)
        new = G((out or {}).get("guid")) if isinstance(out, dict) else ""
        ok = (st == 200 and new and new != guid0 and G(h4.get("guid")) == new and not lock_on(h4)
              and not offline(h4) and not delta(cl, t))
        res.record("repeat: a new GUID", "PASS" if ok else "FAIL",
                   note=f"HTTP {st}; {guid0} -> {new} (header {G(h4.get('guid'))}); sequence "
                        f"{(out or {}).get('sequence') if isinstance(out, dict) else None}; delta_removed "
                        f"{(out or {}).get('delta_removed') if isinstance(out, dict) else None}; resumed "
                        f"{(out or {}).get('resumed') if isinstance(out, dict) else None}; attributes "
                        f"'{h4.get('attributes')}'" + ("" if st == 200 else f"; {err_code(out)}"),
                   steps=(out or {}).get("steps") if isinstance(out, dict) else None)
        c4 = rows(cl, t)
        res.record("repeat: rows kept", "PASS" if c4 == n else "FAIL", note=f"rows {c4}/{n}")
        al = alerts_of(cl, t, ALERT)
        res.record("repeat: no newguid_interrupted left", "FAIL" if al else "PASS",
                   note="; ".join(str(x.get("message"))[:200] for x in al) or "none")
        if st != 200:
            return res.finish()

        # --- replicas -----------------------------------------------------------------
        replicas_after(cl, res, a, t, n)
    except TbError as e:
        res.record("run", "FAIL", note=str(e)[:500])
    finally:
        try:
            h = header(cl, t)
            if ours or lock_on(h) or delta(cl, t):
                u = cl.hostctl(t.host, "nbackup-unlock", {"db": t.path}, check=False)
                log(f"cleanup: nbackup -N: {u}")
        except Exception as e:  # noqa: BLE001 - cleanup goes on
            log(f"cleanup: unlock: {e}")
        if made:
            try:
                sql(cl, t, f"drop table {TABLE};\ncommit;\n")
            except Exception as e:  # noqa: BLE001
                log(f"cleanup: drop {TABLE}: {e}")
    return res.finish()
