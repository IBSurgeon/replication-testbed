"""Node restarts under load replay nothing (hqcluster-node review 2026-10-22:
B-01, B-02, B-03, B-11).

The node keeps its state in state.json and an event journal (journal.jsonl,
next to node.json). A clean stop writes state.json and moves the journal
aside (journal.jsonl.<UTC>.bak). A start after a crash replays the journal
once and compacts it. Before the fix every start replayed the whole journal
onto a snapshot that already held it: the receive watermark (last_received)
and the applied sequence (last_segment) could go back to old values, and the
master then skipped segments.

Under a small write load the test restarts the replica node (--restarts clean
stop/start rounds, one kill -9), stops it once while state.json cannot be
written (B-03), and restarts the master node once. After each restart:
  states   no record of a test database changes state (IN_SYNC and LAGGING
           count as one), conf, generation or path; no NEEDS_REINIT
  applied  on the restarted replica the applied sequence (record last_segment,
           stats last_segment_number, Firebird's control file) and the receive
           watermark (last_received, received_generation) do not go back, and
           last_received is not above the master's newest segment
  ledger   the master's last_shipped, last_acked, last_applied per pair do not
           go back
  journal  a clean stop leaves no journal (or an empty one) and a new .bak;
           after the start the journal holds only events newer than the stop;
           at most limits.journal_backups_keep .bak files; no request replay
           entry in state.json older than 24 h (B-11)
  blocked  (B-03) with state.json unwritable the stop keeps the journal and
           leaves state.json as it was; the next start replays it: no state lost
The received/applied counters of GET /v1/stats are in the notes (they count
from the node's start, so a restart sets them back to 0 by design).
At the end the load stops and every replica must match the master.
"""
import time

from tblib.cluster import TbError, log
from tblib.results import Results

from ._common import add_load_args, converge_and_record, ensure_load, norm, pick_replicas, replica_control
from .disasters import node_svc, wait_node

HELP = "replica/master node restarts under load: no state change, no replay, journal rotated (B-01/02/03/11)"

TAG = "restart_replay"
MOD = "91-journal"
HEALTHY = {"IN_SYNC", "LAGGING"}
BAD = {"NEEDS_REINIT", "ORPHANED", "FAILED"}
DAY = 24 * 3600


def add_args(p):
    p.add_argument("--db", default="all", help="all | db1,db2")
    p.add_argument("--replica", default="", help="the replica to restart; default: the first")
    p.add_argument("--restarts", type=int, default=2, help="clean stop/start rounds of the replica node")
    p.add_argument("--no-kill", action="store_true", help="skip the kill -9 round")
    p.add_argument("--no-blocked-stop", action="store_true", help="skip the stop with state.json unwritable (B-03)")
    p.add_argument("--no-master", action="store_true", help="do not restart the master node")
    p.add_argument("--settle", type=int, default=40, help="seconds after a start before the checks")
    p.add_argument("--keep", type=int, default=0,
                   help="journal .bak files allowed; 0: limits.journal_backups_keep of the node (default 5)")
    p.add_argument("--catchup-timeout", type=int, default=900)
    add_load_args(p)


# --- host calls --------------------------------------------------------------
def jcall(cl, host, cmd, args=None, timeout=None):
    """One 91-journal command on the host: its result, or None when it failed."""
    a = {"node_dir": cl.h(host)["paths"]["node"]}
    a.update(args or {})
    rc, res, _, out = cl.module(host, MOD, cmd, a, check=False, timeout=timeout)
    if rc != 0:
        log(f"[{host}] {MOD} {cmd}: exit {rc}: {(out or '')[-300:]}")
        return None
    return res


def journal(cl, host):
    return jcall(cl, host, "journal-info") or {}


def backups_keep(cl, host, given):
    if given > 0:
        return given
    try:
        _, c = cl.api(host, "GET", "/v1/config")
        cfg = (c or {}).get("config", c) or {}
        return int((cfg.get("limits") or {}).get("journal_backups_keep") or 0) or 5
    except TbError:
        return 5


# --- what the nodes say ------------------------------------------------------
def snapshot(cl, dbs, hosts, ctl_reps):
    """Records, stats, ledger and control files of the test databases.
    recs[host][master db_id] is the host's record of that database (a replica
    record is found by its path)."""
    out = {"recs": {}, "stats": {}, "ledger": {}, "ctl": {}}
    m = cl.cfg.master
    for h in hosts:
        all_recs = cl.databases(h)
        recs = {}
        for d in dbs:
            if h == m:
                hit = [r for r in all_recs if r.get("db_id") == d["db_id"]]
            else:
                want = norm(cl.replica_path(h, d["path"]))
                hit = [r for r in all_recs if norm(r.get("path")) == want]
            if hit:
                recs[d["db_id"]] = hit[0]
        out["recs"][h] = recs
        st, doc = cl.api(h, "GET", "/v1/stats", check_status=False)
        by_id = {s.get("db_id"): s for s in ((doc or {}).get("databases") or [])} if st == 200 else {}
        out["stats"][h] = {mid: by_id.get(r.get("db_id"), {}) for mid, r in recs.items()}
    ids = {d["db_id"] for d in dbs}
    for r in cl.transfer():
        if r.get("db_id") in ids:
            out["ledger"][(r["db_id"], r.get("peer_id"))] = r
    for rep in ctl_reps:
        out["ctl"][rep] = {}
        for d in dbs:
            seqs = [int(c.get("sequence") or 0) for c in replica_control(cl, rep, d["path"]) if "sequence" in c]
            out["ctl"][rep][d["db_id"]] = max(seqs) if seqs else None
    return out


def _st(rec):
    s = (rec or {}).get("state") or "(none)"
    return s + ("/" + rec["state_reason"] if (rec or {}).get("state_reason") else "")


def state_diffs(before, after):
    """Records whose state, conf, generation or path changed, and records in a
    bad state now."""
    diffs = []
    for h, recs in before["recs"].items():
        for mid, b in recs.items():
            a = after["recs"].get(h, {}).get(mid)
            if a is None:
                diffs.append(f"{h} {mid}: record gone")
                continue
            sb, sa = b.get("state"), a.get("state")
            if sa in BAD:
                diffs.append(f"{h} {mid}: {_st(a)}")
            elif sb != sa and not (sb in HEALTHY and sa in HEALTHY):
                diffs.append(f"{h} {mid}: {_st(b)} -> {_st(a)}")
            for k in ("conf", "generation", "path"):
                if b.get(k) and a.get(k) != b.get(k):
                    diffs.append(f"{h} {mid}: {k} {b.get(k)} -> {a.get(k)}")
    return diffs


def wait_states(cl, dbs, hosts, ctl_reps, before, timeout):
    """Snapshot until no state differs from before (a LAGGING that turns
    IN_SYNC again is fine), or the timeout. Returns (snapshot, diffs)."""
    end = time.time() + timeout
    while True:
        after = snapshot(cl, dbs, hosts, ctl_reps)
        diffs = state_diffs(before, after)
        if not diffs or time.time() >= end:
            return after, diffs
        log(f"waiting for states to settle: {diffs[:3]}")
        time.sleep(10)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def sequence_problems(before, after, rep):
    """The restarted replica's applied and received positions must not go back,
    and last_received must not pass the master's newest segment."""
    bad, note = [], []
    for mid, b in before["recs"].get(rep, {}).items():
        a = after["recs"].get(rep, {}).get(mid) or {}
        for k in ("last_segment", "last_received"):
            vb, va = _int(b.get(k)), _int(a.get(k))
            if vb and (va or 0) < vb and a.get("received_generation") == b.get("received_generation"):
                bad.append(f"{mid} {k} {vb} -> {va}")
        if b.get("received_generation") != a.get("received_generation"):
            bad.append(f"{mid} received_generation {b.get('received_generation')} -> {a.get('received_generation')}")
        sb = before["stats"].get(rep, {}).get(mid, {})
        sa = after["stats"].get(rep, {}).get(mid, {})
        vb, va = _int(sb.get("last_segment_number")), _int(sa.get("last_segment_number"))
        if vb and va is not None and va < vb:
            bad.append(f"{mid} stats last_segment_number {vb} -> {va}")
        cb, ca = before["ctl"].get(rep, {}).get(mid), after["ctl"].get(rep, {}).get(mid)
        if cb and ca is not None and ca < cb:
            bad.append(f"{mid} control file sequence {cb} -> {ca}")
        note.append(f"{mid}: last_segment {b.get('last_segment')}->{a.get('last_segment')}, "
                    f"last_received {b.get('last_received')}->{a.get('last_received')}, "
                    f"control {cb}->{ca}, received/applied {sb.get('received')}/{sb.get('applied')}"
                    f"->{sa.get('received')}/{sa.get('applied')}")
    return bad, note


def above_master(cl, after, rep):
    """last_received of the replica above the master's newest segment: an old
    value came back (B-01). Only where the master knows its newest segment."""
    bad = []
    mst = after["stats"].get(cl.cfg.master, {})
    for mid, a in after["recs"].get(rep, {}).items():
        top = _int(mst.get(mid, {}).get("last_segment_number"))
        got = _int(a.get("last_received"))
        if top and got and got > top:
            bad.append(f"{mid} last_received {got} > master newest {top}")
    return bad


def ledger_problems(before, after):
    bad = []
    for key, b in before["ledger"].items():
        a = after["ledger"].get(key)
        if a is None:
            bad.append(f"{key[0]}|{key[1]}: ledger row gone")
            continue
        for f in ("last_shipped", "last_acked", "last_applied"):
            vb, va = _int(b.get(f)), _int(a.get(f))
            if vb and (va or 0) < vb:
                bad.append(f"{key[0]}|{key[1]} {f} {vb} -> {va}")
    return bad


# --- journal checks ----------------------------------------------------------
def check_stopped(res, case, before, down, keep, blocked=False):
    """The journal while the node is down after a stop."""
    j, s = down.get("journal") or {}, down.get("state") or {}
    jb, sb = before.get("journal") or {}, before.get("state") or {}
    new_baks = sorted({b["path"] for b in down.get("baks") or []} - {b["path"] for b in before.get("baks") or []})
    if blocked:
        ok = j.get("exists") and j.get("size", 0) > 0 and s.get("mtime") == sb.get("mtime") and not new_baks
        res.record(f"{case}: journal kept at a failed stop (B-03)", "PASS" if ok else "FAIL",
                   note=f"journal {j.get('size')} B / {j.get('lines')} lines, state.json mtime "
                        f"{sb.get('mtime')} -> {s.get('mtime')}, new .bak {new_baks}")
        return
    problems = []
    if j.get("exists") and j.get("size", 0) > 0:
        problems.append(f"journal left: {j.get('lines')} lines")
    if jb.get("size", 0) > 0 and not new_baks:
        problems.append("no new .bak")
    if s.get("mtime", 0) < before.get("now", 0) - 2:
        problems.append("state.json not written at the stop")
    if down.get("bak_count", 0) > keep:
        problems.append(f"{down.get('bak_count')} .bak files > {keep}")
    res.record(f"{case}: journal at the stop", "FAIL" if problems else "PASS",
               note="; ".join(problems) or f"journal {jb.get('size')} B rotated to {new_baks[-1:]}, "
                                           f"{down.get('bak_count')} .bak")
    rp = down.get("replies") or {}
    old = rp.get("key") == "request_replay" and rp.get("untimed", 0) > 0
    stale = rp.get("oldest_age_sec", 0) > DAY + 600
    res.record(f"{case}: request replay entries (B-11)", "FAIL" if (old or stale) else "PASS",
               note=f"key {rp.get('key') or '-'}, {rp.get('count', 0)} entries, oldest "
                    f"{rp.get('oldest_age_sec', 0)}s, untimed {rp.get('untimed', 0)}")


def check_started(res, case, mark, up, keep, crash_journal):
    """The journal after the start: only events after `mark` (host time)."""
    j = up.get("journal") or {}
    problems = []
    if j.get("lines") and j.get("first_epoch") and j["first_epoch"] < mark - 2:
        problems.append(f"journal starts at {j.get('first_ts')}, before the restart")
    if up.get("bak_count", 0) > keep:
        problems.append(f"{up.get('bak_count')} .bak files > {keep}")
    if crash_journal and not up.get("new_bak"):
        problems.append("the journal left by the stop was not compacted")
    res.record(f"{case}: journal after the start", "FAIL" if problems else "PASS",
               note="; ".join(problems) or f"{j.get('lines', 0)} lines / {j.get('size', 0)} B since the start, "
                                           f"{up.get('bak_count')} .bak")


# --- one restart -------------------------------------------------------------
def restart(cl, res, a, dbs, hosts, ctl_reps, rep, host, kind, n, keep):
    case = f"{kind} {host} #{n}"
    log(f"--- {case}")
    before = snapshot(cl, dbs, hosts, ctl_reps)
    jb = journal(cl, host)
    crash_journal = False
    blocked = False
    mark = 0.0
    try:
        if kind == "kill":
            node_svc(cl, host, "kill")
            jd = journal(cl, host)
            mark = jd.get("now", 0)
            crash_journal = (jd.get("journal") or {}).get("size", 0) > 0
            if not wait_node(cl, host, 60):
                log(f"{host}: not back after the kill; starting it")
                node_svc(cl, host, "start")
        else:
            if kind == "blocked-stop":
                r = jcall(cl, host, "snapshot-block", {"state": "on"}) or {}
                blocked = bool(r.get("blocked"))
                if not blocked:
                    res.record(f"{case}: block state.json", "FAIL", note="could not make state.json.tmp a folder")
                jb = journal(cl, host)
            node_svc(cl, host, "stop")
            jd = journal(cl, host)
            mark = jd.get("now", 0)
            check_stopped(res, case, jb, jd, keep, blocked=blocked)
            crash_journal = blocked
            if blocked:
                jcall(cl, host, "snapshot-block", {"state": "off"})
                blocked = False
            node_svc(cl, host, "start")
    finally:
        if blocked:
            jcall(cl, host, "snapshot-block", {"state": "off"})
    if not wait_node(cl, host, 180):
        node_svc(cl, host, "start")
        if not wait_node(cl, host, 120):
            res.record(f"{case}: node back", "FAIL", note="the node did not come back")
            raise TbError(f"{host}: the node did not come back")
    time.sleep(a.settle)
    up = journal(cl, host)
    up["new_bak"] = bool({b["path"] for b in up.get("baks") or []} - {b["path"] for b in jb.get("baks") or []})
    check_started(res, case, mark, up, keep, crash_journal)

    after, diffs = wait_states(cl, dbs, hosts, ctl_reps, before, a.settle * 2)
    res.record(f"{case}: states", "FAIL" if diffs else "PASS",
               note="; ".join(diffs[:6]) or f"{sum(len(r) for r in after['recs'].values())} records unchanged")
    bad, note = sequence_problems(before, after, rep)
    bad += above_master(cl, after, rep)
    res.record(f"{case}: applied and received do not go back", "FAIL" if bad else "PASS",
               note="; ".join(bad[:6]) or " | ".join(note)[:600])
    lb = ledger_problems(before, after)
    res.record(f"{case}: master ledger monotonic", "FAIL" if lb else "PASS",
               note="; ".join(lb[:6]) or f"{len(after['ledger'])} rows")
    log(f"{case}: journal {(jb.get('journal') or {}).get('size')} B before, "
        f"{(up.get('journal') or {}).get('size')} B after")


def run(cl, a):
    dbs = cl.test_dbs(which=a.db)
    if not dbs:
        raise TbError("no test databases (run 'tb.py dbs prepare')")
    reps = pick_replicas(cl, a.replica) if a.replica else list(cl.cfg.replicas[:1])
    if len(reps) != 1:
        raise TbError(f"--replica names one replica, got {reps}")
    rep = reps[0]
    m = cl.cfg.master
    hosts = [m] + list(cl.cfg.replicas)
    res = Results("restart_replay", vars(a).copy())
    keep_rep, keep_m = backups_keep(cl, rep, a.keep), backups_keep(cl, m, a.keep)

    plan = [("restart", rep, i + 1) for i in range(max(0, a.restarts))]
    if not a.no_kill:
        plan.append(("kill", rep, 1))
    if not a.no_blocked_stop:
        plan.append(("blocked-stop", rep, 1))
    if not a.no_master:
        plan.append(("restart", m, 1))
    log(f"restart_replay: {len(dbs)} db, replica {rep}, plan {[(k, h) for k, h, _ in plan]}")
    try:
        if not a.no_load:
            ensure_load(cl, dbs, a, TAG)
            time.sleep(30)          # segments flowing before the first snapshot
        for kind, host, n in plan:
            restart(cl, res, a, dbs, hosts, [rep], rep, host, kind, n, keep_m if host == m else keep_rep)
            if not a.no_load:
                ensure_load(cl, dbs, a, TAG)
    finally:
        if not a.no_load:
            cl.load_stop(TAG)
        for h in hosts:             # a failed check must not leave a node down or blocked
            jcall(cl, h, "snapshot-block", {"state": "off"})
            if not wait_node(cl, h, 10):
                node_svc(cl, h, "start")
                wait_node(cl, h, 180)
    converge_and_record(cl, res, "converge", [d["path"] for d in dbs], a.catchup_timeout, list(cl.cfg.replicas))
    return res.finish()
