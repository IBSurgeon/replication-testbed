"""Helpers for the state machine tests (tests/states.py, tests/gaps.py).

The node writes every state change of a database to its journal
(journal.jsonl, next to node.json) before it acts on it. A Trace reads those
changes for one database on one node from a mark on, so a test sees every
state the database went through, short ones too, not only the one a poll
happens to catch.

State names follow hqcluster-node (internal/status): UNCONFIGURED,
PENDING_RESTART, CONFIGURED, PUBLISHING, SEEDING, IN_SYNC, LAGGING,
NEEDS_ATTENTION, NEEDS_REINIT, DISABLED, ORPHANED, FAILED. A state with a
reason is written "STATE/reason" (LAGGING/blocked_on_segment).
"""
import base64
import json
import threading
import time

from tblib import ops
from tblib.cluster import TbError, log

from ._common import replica_record
from .disasters import fb_svc, node_svc, wait_node

TAG = "sm"


class Trace:
    """State changes of one database on one node since this object was made.
    The state at the moment of creation is part of the chain: it is an
    observed fact, and the transitions below read as FROM it — without the
    seed a scenario opening on a database that has been IN_SYNC since the
    previous scenario's recovery would record no IN_SYNC entry at all, and
    an IN_SYNC->LAGGING adjacency could never match."""

    def __init__(self, cl, host, db_id=""):
        self.cl, self.host, self.db_id = cl, host, db_id
        full = self._read(0)
        self.start = full["lines"]
        self.seed = ""
        last = [e for e in full.get("events", []) if e.get("type") == "db_state"]
        if last:
            e = last[-1]
            self.seed = (e.get("state") or "") + (("/" + e["reason"]) if e.get("reason") else "")

    def _read(self, start):
        r = self.cl.hostctl(self.host, "statelog", {"db_id": self.db_id or "-", "from": start}, check=False)
        return r or {"lines": 0, "events": []}

    def events(self):
        return [e for e in self._read(self.start)["events"] if e.get("type") == "db_state"]

    def chain(self, head=True):
        """The states in order, repeats of the same state and reason folded,
        starting with the seed state when there was one (head False: only the
        changes after the mark — a wait for a state the seed already is)."""
        out = [self.seed] if self.seed and head else []
        for e in self.events():
            s = e.get("state") or "?"
            if e.get("reason"):
                s += "/" + e["reason"]
            if not out or out[-1] != s:
                out.append(s)
        return out


def _match(item, pattern):
    """pattern: "*", "STATE", "STATE/reason", or alternatives "A|B"."""
    for p in pattern.split("|"):
        if p == "*" or item == p or ("/" not in p and item.split("/")[0] == p):
            return True
    return False


def has_step(chain, frm, to):
    """frm -> to as two neighbours of the chain. frm "^" means the first
    state of a new record; frm "*" means `to` anywhere."""
    if frm == "^":
        return bool(chain) and _match(chain[0], to)
    if frm == "*":
        return any(_match(s, to) for s in chain)
    return any(_match(chain[i], frm) and _match(chain[i + 1], to) for i in range(len(chain) - 1))


class SM:
    """One test database and the replicas it goes to."""

    def __init__(self, cl, res, a, reps):
        self.cl, self.res, self.a = cl, res, a
        self.m = cl.cfg.master
        self.reps = reps
        self.rep = reps[0]
        self.db_id = self.path = None

    def use(self, db):
        self.db_id, self.path = db["db_id"], db["path"]

    # --- what the nodes say ----------------------------------------------------
    def rid(self, rep):
        """The replica's record of this database (the same db_id as on the master)."""
        rec = replica_record(self.cl, rep, self.path)
        return rec["db_id"] if rec else self.db_id

    def rpath(self, rep):
        rec = replica_record(self.cl, rep, self.path)
        return (rec or {}).get("path") or self.cl.replica_path(rep, self.path)

    def rec(self, host, db_id=None):
        st, r = self.cl.api(host, "GET", f"/v1/databases/{db_id or self.db_id}", check_status=False)
        return r if st == 200 and isinstance(r, dict) else None

    def state(self, host, db_id=None):
        r = self.rec(host, db_id) or {}
        s = r.get("state") or "(none)"
        return s + ("/" + r["state_reason"] if r.get("state_reason") else "")

    def wait(self, host, want, timeout=300, db_id=None, poll=5):
        """Wait until the state matches one of `want` ("A|B" patterns).
        Returns the state seen last."""
        end = time.time() + timeout
        while True:
            s = self.state(host, db_id)
            if _match(s, want) or time.time() >= end:
                return s
            time.sleep(poll)

    def wait_not(self, host, pattern, timeout, db_id=None, poll=10):
        """Wait until the state no longer matches. Returns (left, seconds, last)."""
        t0 = time.time()
        while True:
            s = self.state(host, db_id)
            if not _match(s, pattern):
                return True, int(time.time() - t0), s
            if time.time() - t0 >= timeout:
                return False, int(time.time() - t0), s
            time.sleep(poll)

    def ledger(self, rep):
        """The master's delivery ledger row for this database and replica."""
        nid = self.cl.h(rep)["node_id"]
        for r in self.cl.transfer():
            if r.get("db_id") == self.db_id and r.get("peer_id") == nid:
                return r
        return {}

    def node_config(self, host):
        """node.json as the node runs it (GET /v1/config wraps it in "config";
        a bare-config answer is tolerated too)."""
        _, c = self.cl.api(host, "GET", "/v1/config")
        c = c or {}
        return c.get("config") if isinstance(c.get("config"), dict) else c

    def replication_log(self, host):
        p = (self.node_config(host).get("firebird") or {}).get("replication_log")
        if not p:
            raise TbError(f"[{host}] the node has no firebird.replication_log")
        return p

    def peer_addr(self, rep):
        return f"{self.cl.h(rep)['addr']}:{self.cl.h(rep)['node_port']}"

    # --- actions ---------------------------------------------------------------
    def load_on(self):
        self.cl.load_stop(TAG)
        self.cl.load_start([self.path], mode=self.a.load_mode, tx=self.a.tx, conns=self.a.conns, tag=TAG)

    def load_off(self):
        self.cl.load_stop(TAG)

    def load(self, seconds):
        self.load_on()
        try:
            time.sleep(seconds)
        finally:
            self.load_off()

    def set_limits(self, host, limits):
        """PUT limits, restart the node, wait for it. Returns the old values."""
        old = {k: (self.node_config(host).get("limits") or {}).get(k) for k in limits}
        self.cl.api(host, "PUT", "/v1/config", {"limits": limits})
        node_svc(self.cl, host, "restart")
        if not wait_node(self.cl, host, 180):
            raise TbError(f"{host}: the node did not come back after the restart")
        return old

    def inject_log(self, host, db_path, message, count=1, role="replica"):
        """Firebird-style ERROR blocks in the host's replication.log: how a test
        makes the node see an error Firebird cannot be made to log on demand."""
        self.cl.hostctl(host, "replog-inject", {
            "path": self.replication_log(host), "db": db_path, "role": role, "level": "ERROR",
            "count": count, "message_b64": base64.b64encode(message.encode()).decode()})

    def push_segment(self, rep, meta, file=None, random_bytes=0):
        """A segment sent to the replica from the master host, with the master's
        certificate: the replica takes it for one the master sent."""
        args = {"addr": self.peer_addr(rep), "meta_b64": base64.b64encode(json.dumps(meta).encode()).decode()}
        if file:
            args["file"] = file
        else:
            args["random"] = random_bytes or 4096
        r = self.cl.hostctl(self.m, "peer-push", args)
        if not isinstance(r, dict):
            raise TbError(f"peer push to {rep} failed")
        return r

    def reinit(self, rep, mode="standard"):
        try:
            return ops.reinit(self.cl, self.db_id, rep, mode=mode, timeout=1800, refusal_timeout=300)
        except TbError as e:
            return {"state": "error", "error": str(e)}

    def start_reinit(self, rep, mode="standard"):
        """POST a reinit and return (status, body) without waiting for it."""
        body = {"to": self.cl.h(rep)["node_id"], "mode": mode, "ignore_window": True, "allow_restart": True}
        return self.cl.api(self.m, "POST", f"/v1/databases/{self.db_id}/reinit", body, check_status=False)

    def node_on_file(self, event, action, timeout=900):
        """Stop or kill the master node when the database's nbackup lock file
        appears (event "locked") or goes away again ("unlocked"). Runs in a
        thread: start it, then start the reinit. Returns the thread; its
        result is in thread.result."""
        t = threading.Thread(daemon=True, target=lambda: setattr(t, "result", self.cl.hostctl(
            self.m, "node-on-file", {"path": self.path + ".delta", "event": event, "action": action,
                                     "timeout": timeout}, check=False, timeout=timeout + 120)))
        t.result = None
        t.start()
        time.sleep(3)           # the watcher is running before the reinit starts
        return t

    def recover(self, why=""):
        """Reinit to every replica until the master and the replicas are
        IN_SYNC again; records the outcome. Starts a stopped node or Firebird
        first."""
        for h in [self.m] + self.reps:
            if not wait_node(self.cl, h, 10):
                node_svc(self.cl, h, "start")
                wait_node(self.cl, h, 180)
        ok, notes = True, []
        for rep in self.reps:
            op = self.reinit(rep)
            good = op.get("state") == "succeeded"
            ok = ok and good
            notes.append(f"{rep}: {op.get('state')}{' ' + str(op.get('error'))[:120] if not good else ''}")
        s = self.wait(self.m, "IN_SYNC", 300)
        ok = ok and s == "IN_SYNC"
        self.res.record(f"recover after {why}" if why else "recover", "PASS" if ok else "FAIL",
                        note="; ".join(notes) + f"; master {s}")
        return ok


def fb_start_all(cl, hosts):
    for h in hosts:
        try:
            fb_svc(cl, h, "start")
        except Exception as e:  # noqa: BLE001 - best effort in a finally
            log(f"[{h}] Firebird start: {e}")
