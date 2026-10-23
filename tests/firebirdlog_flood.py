"""A flood of error lines in firebird.log (hqcluster-node review 2026-10-22,
finding D-03).

The node scans firebird.log every 30 s. Before the fix every error or
fatal line raised an alert of its own: a burst of network errors from
clients (a few thousand lines) filled the alert list, pushed the standing
alerts out, and after each restart the node read the log from the start
and raised old lines again. Now there is one rolling alert,
firebird_log_error, whose message has the count and the last line; the
scan starts at the end of the log, so the history before a start is not
replayed.

The test appends --lines (2000) error entries in Firebird's own format
(host and time, then the message on the next line, indented) with other
client addresses and ports, to the node's firebird.log (GET /v1/config:
firebird.firebird_log). Module 93-stage1 fblog-inject only appends: the
log is never cut. Then:

  one alert        within --alert-timeout (60 s) there is exactly one
                   firebird_log_error, and its last line is our last entry
  detail           what the message says about the count: it must count
                   every line (the count before + --lines; more when
                   Firebird wrote error lines of its own meanwhile)
  alert list       the node's alert list grew by --max-new-alerts at most,
                   and no standing alert was pushed out
  restart          the node is restarted; for --quiet-wait (70 s, two scan
                   ticks) no firebird_log_error with our lines comes back
  fresh line       one new entry after the restart raises the alert again,
                   with a small count and the new line

finally: nothing to put back (the log only grew); the rolling alert is
removed (DELETE /v1/alerts/firebird_log_error) when it still shows our
lines, so it does not stay for the hour the node keeps it.
"""
import random
import re
import time

from tblib.cluster import TbError, log
from tblib.results import Results

from .disasters import node_svc, wait_node

HELP = "2000 firebird.log error lines give one rolling alert; no replay after a restart (review D-03)"

CODE = "firebird_log_error"
COUNT_RX = re.compile(r"has (\d+) error")
EVICT_AT = 1000   # the node keeps this many alerts


def add_args(p):
    p.add_argument("--host", default="", help="node host whose firebird.log gets the lines; default the master")
    p.add_argument("--lines", type=int, default=2000)
    p.add_argument("--alert-timeout", type=int, default=60,
                   help="seconds for the alert to show (the scan runs every 30 s)")
    p.add_argument("--max-new-alerts", type=int, default=5)
    p.add_argument("--quiet-wait", type=int, default=70, help="seconds watched after the restart")
    p.add_argument("--no-restart", action="store_true", help="skip the restart part")


def s1(cl, host, cmd, args, timeout=None):
    rc, res, _, out = cl.module(host, "93-stage1", cmd, args, check=False, timeout=timeout)
    if rc != 0 or not isinstance(res, dict):
        raise TbError(f"[{host}] 93-stage1 {cmd}: exit {rc} {(out or '')[-400:]}")
    return res


def alerts(cl, host):
    _, body = cl.api(host, "GET", "/v1/alerts", check_status=False)
    return [x for x in body if isinstance(x, dict)] if isinstance(body, list) else []


def ours(cl, host, mark=None):
    return [x for x in alerts(cl, host) if x.get("code") == CODE
            and (mark is None or mark in str(x.get("message") or ""))]


def count_of(al):
    m = COUNT_RX.search(str((al or {}).get("message") or ""))
    return int(m.group(1)) if m else None


def wait_alert(cl, host, mark, timeout):
    """Poll until a firebird_log_error shows `mark`. Returns (seconds,
    every firebird_log_error at that moment)."""
    t0 = time.time()
    al = []
    while time.time() < t0 + timeout:
        al = ours(cl, host)
        if any(mark in str(x.get("message") or "") for x in al):
            break
        time.sleep(3)
    return time.time() - t0, al


def inject(cl, host, path, count, tag):
    r = s1(cl, host, "fblog-inject", {"path": path, "count": count, "tag": tag}, timeout=300)
    log(f"firebird.log {path}: {r.get('size_before')} -> {r.get('size_after')} bytes, {r.get('entries')} entries")
    return r


def run(cl, a):
    res = Results("firebirdlog_flood", vars(a).copy())
    host = a.host or cl.cfg.master
    if host not in cl.cfg.node_hosts():
        raise TbError(f"'{host}' is not a node host ({', '.join(cl.cfg.node_hosts())})")
    _, c = cl.api(host, "GET", "/v1/config")
    path = (((c or {}).get("config") or {}).get("firebird") or {}).get("firebird_log") or ""
    if not path:
        res.record("setup", "SKIP", note=f"{host}: firebird.firebird_log is empty, the node does not scan the log")
        return res.finish()
    tag = "tbflood-%06x" % random.randrange(1 << 24)
    try:
        # --- the flood ---------------------------------------------------------
        before = alerts(cl, host)
        fl0 = [x for x in before if x.get("code") == CODE]
        c0 = count_of(fl0[0]) if fl0 else 0
        log(f"{host}: {len(before)} alerts before; firebird_log_error count {c0}")
        r = inject(cl, host, path, a.lines, tag)
        ok = r.get("entries") == a.lines and (r.get("size_after") or 0) > (r.get("size_before") or 0)
        res.record("inject: entries appended", "PASS" if ok else "FAIL",
                   note=f"{r.get('entries')} entries, {r.get('size_before')} -> {r.get('size_after')} bytes; "
                        f"last: {str(r.get('last_line'))[:160]}")
        sec, al = wait_alert(cl, host, f"{tag}-last", a.alert_timeout)
        mine = [x for x in al if f"{tag}-last" in str(x.get("message") or "")]
        cnt = count_of(mine[0]) if mine else None
        res.record("one alert", "PASS" if len(al) == 1 and mine else "FAIL",
                   note=f"{len(al)} firebird_log_error alert(s) after {sec:.0f}s; ours {'found' if mine else 'NOT found'}",
                   alerts=al[:5])
        want = c0 + a.lines
        if cnt is None:
            st, why = "FAIL", "no count in the message"
        elif cnt < want:
            st, why = "FAIL", f"{cnt} < {want}: lines were not counted"
        elif cnt == want:
            st, why = "PASS", f"{cnt}: every line counted (before {c0} + {a.lines})"
        else:
            st, why = "PASS", f"{cnt}: our {a.lines} + {c0} before + {cnt - want} from Firebird itself"
        res.record("detail: the count", st, note=f"{why}; message: {str((mine or [{}])[0].get('message'))[:300]}")

        after = alerts(cl, host)
        ids0 = {x.get("alert_id") for x in before}
        ids1 = {x.get("alert_id") for x in after}
        new = [x.get("code") for x in after if x.get("alert_id") not in ids0]
        gone = [x.get("code") for x in before if x.get("alert_id") not in ids1 and x.get("code") != CODE]
        ok = len(after) <= len(before) + a.max_new_alerts and not (gone and len(after) >= EVICT_AT)
        res.record("alert list: small, standing alerts kept", "PASS" if ok else "FAIL",
                   note=f"{len(before)} -> {len(after)} alerts; new {new}; "
                        + (f"gone {gone} ({'pushed out' if len(after) >= EVICT_AT else 'cleared by their own condition'})"
                           if gone else "none gone"))

        # --- restart: no replay ------------------------------------------------
        if a.no_restart:
            res.record("restart: no replay", "SKIP", note="--no-restart")
        else:
            node_svc(cl, host, "restart")
            if not wait_node(cl, host, 180):
                raise TbError(f"{host}: the node did not come back after the restart")
            t1 = time.time()
            replay, seen = None, set()
            while time.time() < t1 + a.quiet_wait and not replay:
                for x in ours(cl, host):
                    n = count_of(x)
                    seen.add(n)
                    if tag in str(x.get("message") or "") or (n or 0) >= a.lines:
                        replay = x
                time.sleep(5)
            res.record("restart: no replay", "FAIL" if replay else "PASS",
                       note=(f"replayed: {str(replay.get('message'))[:300]}" if replay else
                             f"watched {a.quiet_wait}s; counts seen {sorted(x for x in seen if x is not None)} "
                             f"(a small count is Firebird's own new lines)"))
            # The first scan after a start only finds the end of the log: a
            # line written before it is history. Two ticks have passed.
            time.sleep(max(0.0, t1 + 65 - time.time()))

        # --- one fresh line ------------------------------------------------------
        tag2 = tag + "-fresh"
        inject(cl, host, path, 1, tag2)
        sec, al = wait_alert(cl, host, f"{tag2}-last", a.alert_timeout + 30)
        mine = [x for x in al if f"{tag2}-last" in str(x.get("message") or "")]
        cnt2 = count_of(mine[0]) if mine else None
        if a.no_restart:
            ok = bool(mine) and len(al) == 1 and (cnt2 or 0) >= want + 1
        else:
            ok = bool(mine) and len(al) == 1 and cnt2 is not None and cnt2 < a.lines
        res.record("fresh line: the alert again", "PASS" if ok else "FAIL",
                   note=f"after {sec:.0f}s: {len(al)} alert(s); count {cnt2}; "
                        f"{str((mine or al or [{}])[0].get('message'))[:300]}")
    except TbError as e:
        res.record("run", "FAIL", note=str(e)[:500])
    finally:
        # Nothing to put back: the log only grew. The rolling alert stays for
        # an hour; it goes now when it shows our lines.
        try:
            if ours(cl, host, tag):
                st, _ = cl.api(host, "DELETE", f"/v1/alerts/{CODE}", check_status=False)
                log(f"cleanup: DELETE /v1/alerts/{CODE}: HTTP {st}")
        except Exception as e:  # noqa: BLE001 - cleanup goes on
            log(f"cleanup: {e}")
        log(f"firebird.log keeps the {a.lines + 1} test entries (tag {tag}); the test never cuts the log")
    return res.finish()
