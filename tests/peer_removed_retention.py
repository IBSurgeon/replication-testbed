"""A peer removed at runtime stops holding the archive back; added again, it
holds it until it has applied (hqcluster-node review 2026-10-22, findings
B-07 and B-16).

B-07: DELETE /v1/peers/{id} left the peer's delivery rows. Retention and the
apply-lag check waited for that peer for good. Now its rows go (journal event
delivery_remove_peer) and only configured peers count.
B-16: a configured peer without a ledger row counted as "nothing to wait
for". Now it counts as applied 0: the archive stays until it reports.

One test database and two or more replicas. X is the removed replica
(--peer, default the last one), L a replica that stays.

  setup               shipping to X paused for the database (POST
                      /v1/shipping/pause), a short load: L applies segments
                      X has not got
  retention waits     the archive aged past retention_days (93-stage1
                      age-files), the master node restarted (its archive
                      index keeps file times in memory), POST
                      /v1/cleanuparchive: no segment X lacks is deleted
  remove              DELETE /v1/peers/X: 200; X gone from GET /v1/peers and
                      GET /v1/config, config_version changed
  no ledger row       GET /v1/transfer has no row of X
  journal             the master's journal has delivery_remove_peer for X
  retention moves on  POST /v1/cleanuparchive: the aged segments only X
                      lacked are gone (all but the retention_min_kept_segments
                      tail)
  no apply-lag alert  no retention_blocked_by_apply for the database
  add back            a second short load with X out; POST /v1/peers with X's
                      old entry; the new segments aged; the master restarted:
                      X is still a peer
  re-added peer holds POST /v1/cleanuparchive keeps every segment of the
  the archive         second load: X has applied none of them (B-16)
  catch up <db>       shipping to X resumed; X matches, or reports a gap (the
                      segments deleted above): then a reinit of X for that
                      database
  converge            every replica matches the master on every test database

finally: the load stops, shipping to X resumes, and the master's peer list is
put back as it was (PUT /v1/config peers) when it differs. A run stopped
between "remove" and "add back" can leave X needing a reinit.
"""
import re
import time

from tblib import ops
from tblib.cluster import TbError, log
from tblib.results import Results

from ._common import converge_and_record, replica_record
from .disasters import node_svc, wait_node

HELP = "a removed peer stops holding the archive; added back, it holds it until it applies (review B-07, B-16)"

SEG = re.compile(r"\.(?:journal|arch)-(\d+)$", re.I)
GAP_CODES = ("sequence_gap", "segment_unavailable", "segment_missing")
TAG = "peerrm"


def add_args(p):
    p.add_argument("--db", default="", help="db_id or folder; default: the first test database")
    p.add_argument("--peer", default="", help="the replica to remove; default: the last one")
    p.add_argument("--load-seconds", type=int, default=45, help="length of each of the two short loads")
    p.add_argument("--load-mode", default="write", choices=["write", "mixed", "spike", "oltp-emul"])
    p.add_argument("--conns", default="2:4")
    p.add_argument("--apply-timeout", type=int, default=180, help="wait for the staying replica to apply")
    p.add_argument("--catchup-timeout", type=int, default=600)


def s1(cl, host, cmd, args, timeout=None):
    """One command of module 93-stage1; its result, or TbError."""
    rc, res, _, out = cl.module(host, "93-stage1", cmd, args, check=False, timeout=timeout)
    if rc != 0 or not isinstance(res, dict):
        raise TbError(f"[{host}] 93-stage1 {cmd}: exit {rc} {(out or '')[-400:]}")
    return res


def seq_of(path):
    g = SEG.search(path or "")
    return int(g.group(1)) if g else None


def segs(cl, m, path):
    """Sequence numbers of the segments in <db>.LogArch."""
    out = set()
    for f in cl.hostctl(m, "files", {"glob": path + ".LogArch/*"}) or []:
        s = seq_of(f.get("path"))
        if s is not None:
            out.add(s)
    return out


def ledger(cl, db_id, peer_id):
    return next((r for r in cl.transfer() if r.get("db_id") == db_id and r.get("peer_id") == peer_id), None)


def top(row):
    """The highest segment a ledger row has seen (shipped, acked or applied)."""
    row = row or {}
    return max(int(row.get(k) or 0) for k in ("last_shipped", "last_acked", "last_applied"))


def config(cl, m):
    _, c = cl.api(m, "GET", "/v1/config")
    return (c or {}).get("config") or {}, (c or {}).get("config_version")


def peer_ids(cl, m):
    _, lst = cl.api(m, "GET", "/v1/peers")
    return [p.get("node_id") for p in lst or [] if isinstance(p, dict)]


def cleanup(cl, m, db_id):
    """POST /v1/cleanuparchive for one database: (HTTP status, its result)."""
    st, out = cl.api(m, "POST", "/v1/cleanuparchive", {"database": db_id}, timeout=300, check_status=False)
    r = ((out or {}).get("results") or {}).get(db_id) if isinstance(out, dict) else None
    return st, r or {}


def short_load(cl, path, a):
    cl.load_start([path], mode=a.load_mode, tx="off", conns=a.conns, tag=TAG)
    time.sleep(a.load_seconds)          # the load itself runs this long
    cl.load_stop(TAG)


def wait_applied(cl, db_id, peer_id, floor, timeout):
    """Poll the ledger until the peer applied past `floor` and all it got."""
    end = time.time() + timeout
    row = {}
    while time.time() < end:
        row = ledger(cl, db_id, peer_id) or {}
        ap = int(row.get("last_applied") or 0)
        if ap > floor and ap >= int(row.get("last_shipped") or 0):
            return row
        time.sleep(10)
    return row


def restart_master(cl, m):
    node_svc(cl, m, "restart")
    if not wait_node(cl, m, 180):
        raise TbError(f"{m}: the node did not come back after the restart")


def age(cl, m, path, days):
    r = s1(cl, m, "age-files", {"glob": path + ".LogArch/*", "days": days})
    return {s for s in (seq_of(f) for f in r.get("files") or []) if s is not None}


def gap_signal(cl, d, x, xid):
    """Why X cannot catch up on database d by itself, or ""."""
    row = ledger(cl, d["db_id"], xid) or {}
    if row.get("gap_at"):
        return f"ledger gap_at {row.get('gap_at')}"
    for al in ops.node_alerts(cl, cl.cfg.master, db_id=d["db_id"]):
        if al.get("code") in GAP_CODES and al.get("database") == d["db_id"]:
            return f"master alert {al.get('code')}"
    rec = replica_record(cl, x, d["path"])
    if rec:
        for al in ops.node_alerts(cl, x, db_id=rec.get("db_id")):
            if al.get("code") in GAP_CODES and al.get("database") == rec.get("db_id"):
                return f"{x} alert {al.get('code')}"
        if "NEEDS_REINIT" in str(rec.get("state") or ""):
            return f"{x} record {rec.get('state')}"
    return ""


def catch_up(cl, res, dbs, x, xid, timeout):
    """X catches up on every database; one with a gap gets a reinit."""
    todo = {d["db_id"]: d for d in dbs}
    end = time.time() + timeout
    while todo and time.time() < end:
        for db_id, d in list(todo.items()):
            ok, _ = cl.compare(d["path"], [x])
            if ok:
                res.record(f"catch up {db_id}", "PASS", note=f"{x} matches without a reinit")
                del todo[db_id]
                continue
            why = gap_signal(cl, d, x, xid)
            if why:
                log(f"{db_id}: {why}; reinit of {x}")
                op = ops.reinit(cl, db_id, x, mode="standard", timeout=timeout)
                res.record(f"catch up {db_id}", "PASS" if op.get("state") == "succeeded" else "FAIL",
                           note=f"{why}; reinit {op.get('state')} {op.get('error') or ''}".strip())
                del todo[db_id]
        if todo:
            time.sleep(20)
    for db_id in todo:
        res.record(f"catch up {db_id}", "FAIL", note=f"{x} neither matched nor reported a gap in {timeout}s")


def run(cl, a):
    res = Results("peer_removed_retention", vars(a).copy())
    m = cl.cfg.master
    reps = list(cl.cfg.replicas)
    if len(reps) < 2:
        res.record("replicas", "SKIP", note="needs two replicas: one is removed, one stays")
        return res.finish()
    x = a.peer or reps[-1]
    if x not in reps:
        raise TbError(f"'{x}' is not a replica (replicas: {', '.join(reps)})")
    stay = next(r for r in reps if r != x)
    xid, lid = cl.h(x)["node_id"], cl.h(stay)["node_id"]
    dbs = cl.test_dbs(which=a.db or "all")
    all_dbs = cl.test_dbs()
    if not dbs:
        raise TbError("no test databases (run 'tb.py dbs prepare')")
    d = dbs[0]
    db_id, path = d["db_id"], d["path"]
    cfg0, ver0 = config(cl, m)
    peers0 = [dict(p) for p in cfg0.get("peers") or []]
    entry = next((p for p in peers0 if p.get("node_id") == xid), None)
    if not entry:
        raise TbError(f"{xid} is not a peer of the master (GET /v1/config peers)")
    limits = cfg0.get("limits") or {}
    days = int(limits.get("retention_days") or 0)
    days = days if days > 0 else 2
    keep = int(limits.get("retention_min_kept_segments") or 0)
    node_dir = cl.h(m)["paths"]["node"]
    j0 = s1(cl, m, "journal", {"node_dir": node_dir, "from": -1})["lines"]
    log(f"remove {xid} ({x}), keep {lid} ({stay}); database {db_id}; retention_days {days}, min kept {keep}")
    paused = False
    try:
        # 1. X falls behind on this database.
        st, out = cl.api(m, "POST", "/v1/shipping/pause", {"database": db_id, "peer": xid, "reason": "tb-peer-removed"},
                         check_status=False)
        if st != 200:
            raise TbError(f"pause shipping to {xid}: HTTP {st} {out}")
        paused = True
        short_load(cl, path, a)
        x_hi = top(ledger(cl, db_id, xid))
        l1 = int(wait_applied(cl, db_id, lid, x_hi, a.apply_timeout).get("last_applied") or 0)
        x_hi = max(x_hi, top(ledger(cl, db_id, xid)))
        behind = sorted(s for s in segs(cl, m, path) if x_hi < s <= l1)
        if not behind:
            res.record("setup: segments X lacks", "FAIL",
                       note=f"{stay} applied {l1}, {x} saw {x_hi}: no archived segment between (a longer --load-seconds?)")
            raise TbError("nothing to test retention with")
        res.record("setup: segments X lacks", "PASS",
                   note=f"{len(behind)} segment(s) {behind[0]}..{behind[-1]}; {stay} applied {l1}, {x} saw {x_hi}")

        # 2.-3. Aged; retention still waits for X.
        aged = age(cl, m, path, days + 1)
        restart_master(cl, m)
        blocked0 = [al for al in ops.node_alerts(cl, m, "retention_blocked_by_apply", db_id)
                    if al.get("database") == db_id]
        st, r = cleanup(cl, m, db_id)
        left = segs(cl, m, path)
        lost = [s for s in behind if s not in left]
        res.record("retention waits for a configured peer", "PASS" if st == 200 and not lost else "FAIL",
                   note=f"HTTP {st}; stage {r.get('stage')}, deleted {len(r.get('deleted') or [])}; "
                        f"deleted although {x} lacks them: {lost[:10]}; retention_blocked_by_apply: {len(blocked0)}")

        # 4. Remove X.
        st, out = cl.api(m, "DELETE", f"/v1/peers/{xid}", check_status=False)
        cfg1, ver1 = config(cl, m)
        in_list = xid in peer_ids(cl, m)
        in_cfg = any(p.get("node_id") == xid for p in cfg1.get("peers") or [])
        gone = out.get("removed") if isinstance(out, dict) else None
        ok = st == 200 and gone == xid and not in_list and not in_cfg and ver1 != ver0
        res.record("remove: DELETE /v1/peers/{id}", "PASS" if ok else "FAIL",
                   note=f"HTTP {st} {out}; in GET /v1/peers: {in_list}; in config: {in_cfg}; config_version {ver0} -> {ver1}")

        # 5. No ledger row.
        rows_x = [row for row in cl.transfer() if row.get("peer_id") == xid]
        res.record("no ledger row of the removed peer", "FAIL" if rows_x else "PASS",
                   note=f"{len(rows_x)} row(s): {sorted({row.get('db_id') for row in rows_x})}" if rows_x else "none")

        # 6. Journal.
        ev = s1(cl, m, "journal", {"node_dir": node_dir, "from": j0, "types": "delivery_remove_peer"}).get("events") or []
        mine = [e for e in ev if (e.get("fields") or {}).get("peer_id") == xid]
        res.record("journal: delivery_remove_peer", "PASS" if mine else "FAIL",
                   note=f"{len(mine)} event(s) for {xid}", events=mine[:3])

        # 7. Retention moves on.
        now = sorted(segs(cl, m, path))
        tail = set(now[-keep:]) if keep > 0 else set()
        expect = [s for s in behind if s in aged and s not in tail]
        st, r = cleanup(cl, m, db_id)
        left = segs(cl, m, path)
        kept = [s for s in expect if s in left]
        if not expect:
            res.record("retention no longer waits for the removed peer", "SKIP",
                       note=f"every segment X lacked is in the kept tail of {keep}")
        else:
            res.record("retention no longer waits for the removed peer", "PASS" if st == 200 and not kept else "FAIL",
                       note=f"HTTP {st}; stage {r.get('stage')}; {len(expect) - len(kept)} of {len(expect)} gone; "
                            f"still there: {kept[:10]}")

        # 8. No apply-lag alert (the retention tick withdraws one within 30 s).
        end = time.time() + 70
        while True:
            al = [x_ for x_ in ops.node_alerts(cl, m, "retention_blocked_by_apply", db_id) if x_.get("database") == db_id]
            if not al or time.time() >= end:
                break
            time.sleep(10)
        res.record("no retention_blocked_by_apply", "FAIL" if al else "PASS",
                   note=f"before the removal: {len(blocked0)}, now: {len(al)}")

        # 9. A second load with X out; X back; aged; restarted.
        l1 = int((ledger(cl, db_id, lid) or {}).get("last_applied") or l1)
        short_load(cl, path, a)
        l2 = int(wait_applied(cl, db_id, lid, l1, a.apply_timeout).get("last_applied") or 0)
        second = sorted(s for s in segs(cl, m, path) if l1 < s <= l2)
        st, out = cl.api(m, "POST", "/v1/peers", entry, check_status=False)
        res.record("add back: POST /v1/peers", "PASS" if st == 200 and xid in peer_ids(cl, m) else "FAIL",
                   note=f"HTTP {st} {out}")
        age(cl, m, path, days + 1)
        restart_master(cl, m)
        res.record("the peer is still there after a restart", "PASS" if xid in peer_ids(cl, m) else "FAIL")

        # 10. B-16: X holds the archive until it reports.
        xrow = ledger(cl, db_id, xid)
        st, r = cleanup(cl, m, db_id)
        left = segs(cl, m, path)
        x_ap = int((xrow or {}).get("last_applied") or 0)
        lost = [s for s in second if s not in left and s > x_ap]
        row_note = "no ledger row" if not xrow else \
            f"ledger row: applied {x_ap}, not_subscribed {bool(xrow.get('not_subscribed'))}"
        if not second:
            res.record("re-added peer holds the archive", "SKIP", note=f"the second load archived nothing new; {row_note}")
        elif lost and xrow and xrow.get("not_subscribed"):
            res.record("re-added peer holds the archive", "SKIP",
                       note=f"{row_note}: retention skips a peer with no copy; deleted {lost[:10]}")
        else:
            res.record("re-added peer holds the archive", "FAIL" if lost or st != 200 else "PASS",
                       note=f"HTTP {st}; {row_note}; {len(second)} segment(s) of the second load, "
                            f"deleted: {lost[:10]}")

        # 11.-12. X catches up (a reinit on a gap); every replica matches. The
        # pause may have gone with X's ledger rows; resumed here in any case.
        st, out = cl.api(m, "POST", "/v1/shipping/resume", {"database": db_id, "peer": xid}, check_status=False)
        log(f"resume shipping to {xid}: HTTP {st} {str(out)[:200]}")
        paused = False
        catch_up(cl, res, all_dbs, x, xid, a.catchup_timeout)
        converge_and_record(cl, res, "converge", [dd["path"] for dd in all_dbs], a.catchup_timeout, reps)
    except TbError as e:
        res.record("run", "FAIL", note=str(e)[:500])
    finally:
        cl.load_stop(TAG)
        try:
            cur, _ = config(cl, m)
            if [p.get("node_id") for p in cur.get("peers") or []] != [p.get("node_id") for p in peers0]:
                cl.api(m, "PUT", "/v1/config", {"peers": peers0})
                log(f"master peer list put back: {[p.get('node_id') for p in peers0]}")
        except Exception as e:  # noqa: BLE001 - cleanup goes on
            log(f"put back the peer list: {e}")
        if paused:
            try:
                cl.api(m, "POST", "/v1/shipping/resume", {"database": db_id, "peer": xid}, check_status=False)
            except Exception as e:  # noqa: BLE001
                log(f"resume shipping to {xid}: {e}")
    return res.finish()
