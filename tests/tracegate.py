"""HQbird 3.0 replicas and trace sessions. HQbird 3.0.15 aborts on the next
segment apply after a user trace session appears on a replica (engine bug,
docs/engine-bugs/hqbird30-replica-trace-abort.en.md). fbagent 2.59.0 does not
start its trace tasks on an HQbird 3.0 instance with a replica node.

  agent    the replica's agent runs no trace session of its own; the
           master's agent still does (noted)
  apply    under load the replica applies every segment: rows converge,
           the server's PID does not change, firebird.log gets no
           "terminated abnormally"
  repro    (--repro) one trace session started by hand with fbtracemgr on
           the replica: the next applied segment aborts the server (PID
           changes or the firebird.log line) - the engine bug, reproduced
           without fbagent. The session is stopped, the replica converges.

HQbird 3.0 replicas only; Linux hosts only."""
import time

from tblib import ops
from tblib.results import Results

from ._common import converge_and_record

HELP = "HQbird 3.0 replica: fbagent runs no trace session there; apply under load; --repro: the engine abort by hand"


def add_args(p):
    p.add_argument("--replica", default="", help="replica host; default the first one")
    p.add_argument("--seconds", type=int, default=60, help="load on the master")
    p.add_argument("--repro", action="store_true", help="also start a trace session by hand and expect the abort")


def trace(cl, host, op, name=""):
    args = {"op": op}
    if name:
        args["name"] = name
    return cl.hostctl(host, "trace", args, check=False) or {}


def aborts(cl, host):
    log = cl.host(host).join(cl.h(host)["firebird"]["root"], "firebird.log")
    _, out, _ = cl.host(host).run_raw(["grep", "-c", "terminated abnormally", log], check=False)
    try:
        return int((out or "0").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 0


def run(cl, a):
    res = Results("tracegate", vars(a).copy())
    rep = a.replica or cl.cfg.replicas[0]
    if cl.fb_engine(rep) != "3.0":
        res.record("tracegate", "SKIP", note=f"{rep} is not HQbird 3.0")
        return res.finish()
    dbs = cl.test_dbs(which="all")
    paths = [d["path"] for d in dbs]
    m = cl.cfg.master

    # --- agent ---------------------------------------------------------------------
    t = trace(cl, rep, "list")
    own = [s for s in t.get("sessions") or [] if s.startswith("FBAgent-")]
    res.record("agent: no trace session of the agent on the replica", "FAIL" if own or "sessions" not in t else "PASS",
               note=f"sessions {t.get('sessions')}" if "sessions" in t else "the session list could not be read")
    tm = trace(cl, m, "list")
    res.record("agent: the master's agent traces (noted)", "PASS", note=f"sessions {tm.get('sessions')}")

    # --- apply under load ------------------------------------------------------------------
    pid0, ab0 = t.get("pid"), aborts(cl, rep)
    cl.load_start(paths, mode="write", tx="off", conns="1:2", tag="tracegate")
    time.sleep(a.seconds)
    cl.load_stop("tracegate")
    converge_and_record(cl, res, "apply: the replica converges under load", paths, 900, replicas=[rep])
    t1 = trace(cl, rep, "pid")
    ok = t1.get("pid") == pid0 and aborts(cl, rep) == ab0
    res.record("apply: the server did not abort (same PID, no new firebird.log line)", "PASS" if ok else "FAIL",
               note=f"pid {pid0} -> {t1.get('pid')}; aborts {ab0} -> {aborts(cl, rep)}")

    # --- the engine bug by hand ----------------------------------------------------------
    if a.repro:
        trace(cl, rep, "start", "tbrepro")
        st = trace(cl, rep, "list")
        started = "tbrepro" in st.get("sessions", [])
        cl.load_start(paths[:1], mode="write", tx="off", conns="1:1", tag="tracegate")
        time.sleep(40)
        cl.load_stop("tracegate")
        end, aborted = time.time() + 180, False
        while not aborted and time.time() < end:
            time.sleep(15)
            p = trace(cl, rep, "pid").get("pid")
            aborted = (p and p != st.get("pid")) or aborts(cl, rep) > ab0
        res.record("repro: a trace session started by hand aborts the replica's server on the next segment",
                   "PASS" if started and aborted else "FAIL",
                   note=f"session started {started}; pid {st.get('pid')} -> {trace(cl, rep, 'pid').get('pid')}; "
                        f"aborts {ab0} -> {aborts(cl, rep)}")
        trace(cl, rep, "stop", "tbrepro")
        converge_and_record(cl, res, "repro: the replica converges after the session is gone", paths, 900,
                            replicas=[rep])
    return res.finish()
