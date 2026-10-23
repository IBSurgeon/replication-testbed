"""Publication of tables whose names need quotes (hqcluster-node review
2026-10-22, finding E-05).

The node's isql parser took only [A-Za-z0-9_$#] in a name, and its sessions
had no charset. A table with a space or a non-Latin letter broke the
publication sync, the table list and verify. The fix: UTF8 sessions, names
between markers, publication DDL that always quotes the name.

On one test database (Firebird 4/5; HQbird 2.5/3.0 have no publications, the
test is skipped), with two tables of the test's own, made with the UTF8
charset (the DDL replicates):
  CYR               "TbPubKirillitsa" written in Cyrillic letters, mixed case
                    ("cyrillic" in the notes)
  "TB Pub Space"    a space, mixed case ("space" in the notes)

  sync           POST /v1/publication/sync and POST .../publication: both
                 tables published, neither left in keyed_unpublished
  names          GET .../publication/tables: both names exactly as created,
                 published, keyed by the primary key; no cut or upper-cased
                 copy of a name
  rows           rows written on the master reach every replica
  exclude        PUT .../publication/tables takes the space table out; a
                 plain sync keeps it out; its new rows stay on the master
                 while the Cyrillic table's rows (same transaction) arrive
  verify         POST .../verify with both quoted names passes, marks the
                 space table not_published, the Cyrillic table matches
  put back       the earlier choice again: new rows of the space table arrive
  converge       the tables dropped, every replica matches the master

Rows are counted by 93-stage1 count-tables (quoted name, UTF8): hostctl
counts strips the space out of a name and then fails on the whole database,
and hostctl sql has no charset. That is also why the tables are dropped
before the converge step. finally: the database's earlier publication choice
comes back and the tables are dropped.
"""
import base64
import json
import time

from tblib.cluster import LEGACY_ENGINES, TbError, log
from tblib.results import Results

from ._common import converge_and_record, pick_replicas
from .pubtables import put_disabled

HELP = "publication, rows and verify of tables named with Cyrillic letters and a space (review E-05)"

# "TbPubKirillitsa" in Cyrillic letters. Escaped: the source and the console
# output stay ASCII.
CYR = "\u0422\u0431\u041f\u0443\u0431\u041a\u0438\u0440\u0438\u043b\u043b\u0438\u0446\u0430"
SPACE = "TB Pub Space"
NAMES = [CYR, SPACE]


def add_args(p):
    p.add_argument("--db", default="", help="db_id or folder; default: the first test database")
    p.add_argument("--replicas", default="all")
    p.add_argument("--rows", type=int, default=20, help="rows per table and step")
    p.add_argument("--catchup-timeout", type=int, default=300)


def q(name):
    """A quoted Firebird identifier."""
    return '"' + name.replace('"', '""') + '"'


def unq(name):
    name = name or ""
    if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
        return name[1:-1].replace('""', '"')
    return name


def s1(cl, host, cmd, args, timeout=None):
    """One command of module 93-stage1; its result, or TbError."""
    rc, res, _, out = cl.module(host, "93-stage1", cmd, args, check=False, timeout=timeout)
    if rc != 0 or not isinstance(res, dict):
        raise TbError(f"[{host}] 93-stage1 {cmd}: exit {rc} {(out or '')[-400:]}")
    return res


def fb(cl, host):
    return {"fb_root": cl.h(host)["firebird"]["root"], "port": cl.fb_port(host)}


def sql8(cl, host, db, script, check=True):
    """An isql script with the UTF8 charset (93-stage1 sql-utf8)."""
    r = s1(cl, host, "sql-utf8", dict(fb(cl, host), db=db,
                                      sql_b64=base64.b64encode(script.encode("utf-8")).decode()))
    if check and not r.get("ok"):
        raise TbError(f"[{host}] sql-utf8 on {db} failed: {r.get('out', '')[-400:]}")
    return r


def counts(cl, host, db, names):
    """{name: rows, or None when the count failed}, each name quoted."""
    b64 = base64.b64encode(json.dumps(names).encode("utf-8")).decode()
    r = s1(cl, host, "count-tables", dict(fb(cl, host), db=db, tables_b64=b64))
    return {t.get("name"): t.get("rows") for t in r.get("tables", [])}


def wait_counts(cl, host, db, want, timeout):
    """Poll until each table of `want` {name: rows} has its rows; the last counts."""
    end = time.time() + timeout
    while True:
        got = counts(cl, host, db, list(want))
        if all(got.get(t) == n for t, n in want.items()) or time.time() >= end:
            return got
        time.sleep(10)


def show(got):
    return f"cyrillic={got.get(CYR)} space={got.get(SPACE)}"


def insert(cl, db, tables, first, count):
    """Rows first..first+count-1 in every table, one transaction."""
    lines = [f"insert into {q(t)} (ID, V) values ({i}, 'r{i}');"
             for t in tables for i in range(first, first + count)]
    sql8(cl, cl.cfg.master, db, "\n".join(lines) + "\ncommit;")


def sync_one(cl, db_id):
    st, rep = cl.api(cl.cfg.master, "POST", f"/v1/databases/{db_id}/publication", {}, timeout=300,
                     check_status=False)
    return st, rep if isinstance(rep, dict) else {}


def run(cl, a):
    res = Results("pubtables_utf8", vars(a).copy())
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
    n = a.rows
    _, pub0 = cl.api(m, "GET", f"/v1/databases/{db_id}/publication", check_status=False)
    disabled0 = list((pub0 or {}).get("disabled") or []) if isinstance(pub0, dict) else []
    log(f"pubtables_utf8 on {db_id}; tables out of the publication before: {disabled0}")
    dropped = False
    try:
        # 1. The tables (UTF8 session, quoted names).
        sql8(cl, m, path, "".join(f"recreate table {q(t)} (ID integer not null primary key, V varchar(20));\n"
                                  for t in NAMES) + "commit;")

        # 2. Sync: the all-databases route, then the one-database route.
        st, out = cl.api(m, "POST", "/v1/publication/sync", {}, timeout=300, check_status=False)
        mine = [x for x in ((out or {}).get("results") or []) if x.get("db_id") == db_id] \
            if isinstance(out, dict) else []
        one = mine[0] if mine else {}
        pubd = (one.get("report") or {}).get("published") or []
        ok = bool(one.get("ok")) and all(t in pubd for t in NAMES)
        res.record("sync: POST /v1/publication/sync", "PASS" if ok else "FAIL",
                   note=f"HTTP {st}; this database ok={one.get('ok')} error={one.get('error') or '-'}; "
                        f"cyrillic published={CYR in pubd} space published={SPACE in pubd}", answer=out)
        st, rep = sync_one(cl, db_id)
        kun = rep.get("keyed_unpublished") or []
        ok = st == 200 and all(t in (rep.get("published") or []) for t in NAMES) and not any(t in kun for t in NAMES)
        res.record("sync: POST .../publication", "PASS" if ok else "FAIL",
                   note=f"HTTP {st}; keyed_unpublished has ours: {[t for t in NAMES if t in kun]}", answer=rep)

        # 3. The tables page keeps the names as they are.
        _, tbl = cl.api(m, "GET", f"/v1/databases/{db_id}/publication/tables")
        by = {t.get("name"): t for t in (tbl or {}).get("tables", [])}
        bad = []
        for t, label in ((CYR, "cyrillic"), (SPACE, "space")):
            x = by.get(t)
            if not x:
                bad.append(f"{label}: missing")
            elif not x.get("published") or (x.get("key") or {}).get("kind") != "primary_key":
                bad.append(f"{label}: published={x.get('published')} key={(x.get('key') or {}).get('kind')}")
        # A cut or upper-cased copy of a name (the old parser's way to fail).
        ours = {t.upper().replace(" ", "") for t in NAMES}
        mangled = [k for k in by if k not in NAMES and (k or "").upper().replace(" ", "") in ours
                   or k in ("TB", "PUB", "SPACE")]
        res.record("names: GET .../publication/tables", "FAIL" if bad or mangled else "PASS",
                   note="; ".join(bad) or (f"mangled names {mangled}" if mangled else "both names exact, published, keyed"),
                   tables=[t for t in by if t in NAMES or t in mangled])

        # 4. Rows reach every replica.
        insert(cl, path, NAMES, 1, n)
        for r in reps:
            got = wait_counts(cl, r, rpath[r], {t: n for t in NAMES}, a.catchup_timeout)
            ok = all(got.get(t) == n for t in NAMES)
            res.record(f"{r}: rows arrive", "PASS" if ok else "FAIL", note=f"want {n}: {show(got)}")

        # 5. The space table out of the publication.
        rep = put_disabled(cl, db_id, sorted(set(disabled0) | {SPACE}))
        ok = SPACE in (rep.get("disabled") or []) and SPACE not in (rep.get("published") or []) \
            and CYR in (rep.get("published") or [])
        res.record("exclude: PUT .../publication/tables", "PASS" if ok else "FAIL",
                   note=f"space disabled={SPACE in (rep.get('disabled') or [])}, "
                        f"space published={SPACE in (rep.get('published') or [])}, "
                        f"cyrillic published={CYR in (rep.get('published') or [])}")
        st, rep = sync_one(cl, db_id)
        res.record("exclude: a plain sync keeps it out", "PASS" if st == 200 and SPACE not in (rep.get("published") or [])
                   else "FAIL", note=f"HTTP {st}")
        insert(cl, path, NAMES, n + 1, n)
        for r in reps:
            wait_counts(cl, r, rpath[r], {CYR: 2 * n}, a.catchup_timeout)
            # One transaction: once the Cyrillic rows are there, published rows of
            # the space table would be there too.
            got = counts(cl, r, rpath[r], NAMES)
            ok = got.get(CYR) == 2 * n and got.get(SPACE) == n
            res.record(f"{r}: the excluded table's rows stay on the master", "PASS" if ok else "FAIL",
                       note=f"want cyrillic={2 * n} space={n}: {show(got)}")

        # 6. Verify with both quoted names.
        tables_arg = ",".join(q(t) for t in NAMES)
        for r in reps:
            st, ver = cl.api(m, "POST", f"/v1/databases/{db_id}/verify",
                             {"to": cl.h(r)["node_id"], "tables": tables_arg, "wait_for_sync_sec": 60},
                             timeout=300, check_status=False)
            ver = ver if isinstance(ver, dict) else {}
            rows = ver.get("tables") or []
            np = [unq(t.get("table")) for t in rows if t.get("not_published")]
            cyr = next((t for t in rows if unq(t.get("table")) == CYR), {})
            ok = st == 200 and ver.get("ok") and np == [SPACE] and cyr.get("match")
            res.record(f"{r}: verify with quoted names", "PASS" if ok else "FAIL",
                       note=f"HTTP {st} ok={ver.get('ok')} space not_published={np == [SPACE]} "
                            f"cyrillic match={cyr.get('match')} ({cyr.get('master_count')}/{cyr.get('replica_count')}) "
                            f"{ver.get('message') or ''}".strip(), report=ver)

        # 7. The earlier choice back: new rows of the space table arrive.
        rep = put_disabled(cl, db_id, disabled0)
        res.record("put back", "PASS" if SPACE in (rep.get("published") or []) else "FAIL",
                   note=f"disabled={len(rep.get('disabled') or [])} table(s)")
        insert(cl, path, NAMES, 2 * n + 1, n)
        for r in reps:
            # The rows written while it was out never come: n + n on the replica.
            got = wait_counts(cl, r, rpath[r], {CYR: 3 * n, SPACE: 2 * n}, a.catchup_timeout)
            ok = got.get(CYR) == 3 * n and got.get(SPACE) == 2 * n
            res.record(f"{r}: rows after put back arrive", "PASS" if ok else "FAIL",
                       note=f"want cyrillic={3 * n} space={2 * n}: {show(got)}")

        # 8. Drop (hostctl counts cannot count the space table), then converge.
        sql8(cl, m, path, "".join(f"drop table {q(t)};\n" for t in NAMES) + "commit;")
        dropped = True
        converge_and_record(cl, res, "converge", [path], a.catchup_timeout, reps)
    except TbError as e:
        # ASCII only: an isql error may quote the Cyrillic name.
        res.record("run", "FAIL", note=str(e)[:500].encode("ascii", "backslashreplace").decode())
    finally:
        try:
            _, cur = cl.api(m, "GET", f"/v1/databases/{db_id}/publication", check_status=False)
            if isinstance(cur, dict) and sorted(cur.get("disabled") or []) != sorted(disabled0):
                put_disabled(cl, db_id, disabled0)
        except Exception as e:  # noqa: BLE001 - cleanup goes on
            log(f"put back the publication choice: {e}")
        if not dropped:
            for t in NAMES:
                try:
                    sql8(cl, m, path, f"drop table {q(t)};\ncommit;", check=False)
                except Exception as e:  # noqa: BLE001
                    log(f"drop a test table: {e}")
    return res.finish()
