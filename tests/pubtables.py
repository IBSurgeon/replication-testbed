"""Publication per table, on/off, and sync under load (hqcluster-node 2027.4.4).

The node's publication sync runs only the difference, in one transaction
(plan item N4): a table that stays published is never out of the
publication, so a sync during load loses no row. The operator takes tables
out of a master database's publication and puts them back, and turns the
publication off and on (N5); every later sync keeps that choice.

Steps, on one test database (Firebird 4/5; HQbird 2.5/3.0 have no
publications and the test is skipped):
  1. two tables of the test's own, TB_PUB_ON and TB_PUB_OFF, made on the
     master (DDL replicates); a sync publishes them;
  2. GET .../publication/tables: every keyed table published, with its key;
  3. TB_PUB_OFF taken out: rows written to it do not reach the replicas,
     rows of TB_PUB_ON do; a plain sync keeps it out;
  4. put back: rows written after that reach the replicas;
  5. under load, ten plain syncs and two real changes (TB_PUB_OFF out and
     in): the replicas still match the master on every load table;
  6. publication off: the report says off by the operator, the alert
     master_publication_off is info; on again;
  7. verify with TB_PUB_OFF out: not_published, and the report passes.
The two tables are dropped at the end (the drop replicates).
"""
import base64
import time

from tblib.cluster import LEGACY_ENGINES, TbError, log
from tblib.results import Results

from ._common import add_load_args, converge_and_record, pick_replicas

HELP = "publication per table, on/off, sync under load (node 2027.4.4)"

ON, OFF = "TB_PUB_ON", "TB_PUB_OFF"


def add_args(p):
    p.add_argument("--db", default="", help="db_id; default: the first test database")
    p.add_argument("--replicas", default="all")
    p.add_argument("--minutes", type=int, default=2, help="load in step 5")
    p.add_argument("--catchup-timeout", type=int, default=600)
    add_load_args(p, tx="off")


def sql(cl, host, db, script):
    r = cl.hostctl(host, "sql", {"db": db, "sql_b64": base64.b64encode(script.encode()).decode()}, check=False)
    if not r or not r.get("ok"):
        raise TbError(f"[{host}] sql on {db} failed: {(r or {}).get('out', '')[-400:]}")
    return r.get("out", "")


def rows(cl, host, db, table):
    counts = cl.hostctl(host, "counts", {"db": db}) or {}
    return (counts.get(table) or {}).get("rows")


def wait_rows(cl, host, db, table, want, timeout):
    end = time.time() + timeout
    n = None
    while time.time() < end:
        n = rows(cl, host, db, table)
        if n == want:
            return n
        time.sleep(10)
    return n


def insert(cl, db, table, first, count):
    sql(cl, cl.cfg.master, db, "\n".join(
        f"insert into {table} (ID, V) values ({i}, 'x');" for i in range(first, first + count)) + "\ncommit;")


def put_disabled(cl, db_id, disabled):
    st, rep = cl.api(cl.cfg.master, "PUT", f"/v1/databases/{db_id}/publication/tables",
                     {"disabled": disabled}, check_status=False)
    if st != 200:
        raise TbError(f"PUT publication/tables {disabled}: HTTP {st} {rep}")
    return rep


def run(cl, a):
    res = Results("pubtables", vars(a).copy())
    m = cl.cfg.master
    if cl.fb_engine(m) in LEGACY_ENGINES:
        res.record("engine", "SKIP", note=f"HQbird {cl.fb_engine(m)} has no publications")
        return res.finish()
    dbs = cl.test_dbs(which=a.db or "all")
    if not dbs:
        raise TbError("no test databases (run 'tb.py dbs prepare')")
    d = dbs[0]
    db_id, path = d["db_id"], d["path"]
    reps = pick_replicas(cl, a.replicas)
    rpath = {r: cl.replica_path(r, path) for r in reps}
    tag = "pubtables"
    try:
        # 1. The test's own tables; the sync publishes them (auto-enable is off).
        sql(cl, m, path, f"recreate table {ON} (ID integer not null primary key, V varchar(10));\n"
                         f"recreate table {OFF} (ID integer not null primary key, V varchar(10));\ncommit;")
        put_disabled(cl, db_id, [])
        st, out = cl.api(m, "POST", f"/v1/databases/{db_id}/publication", {})
        res.record("sync publishes the new tables", "PASS" if not out.get("keyed_unpublished") else "FAIL",
                   note=f"keyed_unpublished={out.get('keyed_unpublished')}")

        # 2. The tables page.
        _, tbl = cl.api(m, "GET", f"/v1/databases/{db_id}/publication/tables")
        by = {t["name"]: t for t in tbl.get("tables", [])}
        bad = [t["name"] for t in tbl.get("tables", []) if t.get("key") and not t.get("published")]
        keyless_pub = [t["name"] for t in tbl.get("tables", []) if not t.get("key") and t.get("published")]
        ok = by.get(ON, {}).get("key", {}).get("kind") == "primary_key" and not bad and not keyless_pub
        res.record("tables: keys and publication", "PASS" if ok else "FAIL",
                   note=f"{len(by)} tables; keyed unpublished {bad}; keyless published {keyless_pub}")

        # 3. TB_PUB_OFF out: its rows stay on the master.
        rep = put_disabled(cl, db_id, [OFF])
        res.record("take a table out", "PASS" if OFF in (rep.get("disabled") or []) and OFF not in (rep.get("published") or []) else "FAIL",
                   note=f"disabled={rep.get('disabled')}")
        cl.api(m, "POST", f"/v1/databases/{db_id}/publication", {})
        _, chk = cl.api(m, "GET", f"/v1/databases/{db_id}/publication")
        res.record("a plain sync keeps it out", "PASS" if OFF not in (chk.get("published") or []) else "FAIL")
        insert(cl, path, ON, 1, 50)
        insert(cl, path, OFF, 1, 50)
        for r in reps:
            n_on = wait_rows(cl, r, rpath[r], ON, 50, a.catchup_timeout)
            n_off = rows(cl, r, rpath[r], OFF)
            res.record(f"{r}: rows of the table out of the publication do not arrive",
                       "PASS" if n_on == 50 and n_off == 0 else "FAIL", note=f"{ON}={n_on} {OFF}={n_off}")
            # 7. verify marks it not_published and does not fail on it.
            st, ver = cl.api(m, "POST", f"/v1/databases/{db_id}/verify",
                             {"to": cl.h(r)["node_id"], "tables": f"{ON},{OFF}"}, timeout=300, check_status=False)
            np = [t["table"] for t in (ver or {}).get("tables", []) if t.get("not_published")]
            res.record(f"{r}: verify passes with the table out of the publication",
                       "PASS" if st == 200 and ver.get("ok") and np == [OFF] else "FAIL",
                       note=f"HTTP {st} ok={(ver or {}).get('ok')} not_published={np}")

        # 4. Back in: rows written from now on arrive.
        rep = put_disabled(cl, db_id, [])
        res.record("put it back", "PASS" if OFF in (rep.get("published") or []) else "FAIL")
        insert(cl, path, ON, 51, 50)
        insert(cl, path, OFF, 51, 50)
        for r in reps:
            n_on = wait_rows(cl, r, rpath[r], ON, 100, a.catchup_timeout)
            n_off = wait_rows(cl, r, rpath[r], OFF, 50, a.catchup_timeout)
            res.record(f"{r}: rows after putting it back arrive",
                       "PASS" if n_on == 100 and n_off == 50 else "FAIL", note=f"{ON}={n_on} {OFF}={n_off}")

        # 5. Syncs under load: no row of the load tables is lost.
        if not a.no_load:
            cl.load_stop(tag)
            cl.load_start([path], mode=a.load_mode, tx=a.tx, conns=a.conns, tag=tag)
            end = time.time() + a.minutes * 60
            n = 0
            while time.time() < end:
                cl.api(m, "POST", f"/v1/databases/{db_id}/publication", {})
                n += 1
                if n in (3, 6):
                    put_disabled(cl, db_id, [OFF] if n == 3 else [])
                time.sleep(max(5, a.minutes * 60 // 12))
            cl.load_stop(tag)
            log(f"{n} publication syncs under load")
        # TB_PUB_OFF differs on purpose (rows 1-50 never arrived): drop both
        # tables before comparing; the drop replicates.
        sql(cl, m, path, f"drop table {OFF};\ndrop table {ON};\ncommit;")
        converge_and_record(cl, res, "replicas match after syncs under load", [path], a.catchup_timeout, reps)

        # 6. Off and on.
        st, rep = cl.api(m, "PUT", f"/v1/databases/{db_id}/publication/state", {"enabled": False}, check_status=False)
        ok = st == 200 and not rep.get("publication_enabled") and rep.get("off_by_operator")
        res.record("publication off", "PASS" if ok else "FAIL", note=f"HTTP {st} {rep if not ok else ''}")
        lvl = None
        for _ in range(12):        # pubwatch checks an off database within two minutes
            _, al = cl.api(m, "GET", "/v1/alerts")
            hit = [x for x in (al or []) if x.get("code") == "master_publication_off" and x.get("database") == db_id]
            if hit:
                lvl = hit[0].get("severity")
                break
            time.sleep(15)
        res.record("master_publication_off is info when the operator turned it off",
                   "PASS" if lvl == "info" else "FAIL", note=f"severity={lvl}")
        st, rep = cl.api(m, "PUT", f"/v1/databases/{db_id}/publication/state", {"enabled": True}, check_status=False)
        res.record("publication on", "PASS" if st == 200 and rep.get("publication_enabled") and not rep.get("off_by_operator") else "FAIL")
    finally:
        if not a.no_load:
            cl.load_stop(tag)
        try:
            put_disabled(cl, db_id, [])
            cl.api(m, "PUT", f"/v1/databases/{db_id}/publication/state", {"enabled": True}, check_status=False)
        except TbError as e:
            log(f"cleanup: {e}")
    return res.finish()
