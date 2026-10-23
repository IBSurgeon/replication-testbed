"""The Channel A bandwidth limit and malformed JSON bodies (hqcluster-node
review 2026-10-22: A-02, B-05, E-02, A-09).

A-02: PUT /v1/limits/bandwidth with burst 0 (the CLI default) stopped every
segment push: the token bucket never filled. The value was not saved, so
GET /v1/config showed the old limit and a restart went back to it. Now the
node refuses a bad limit with 400 bad_request, burst 0 or omitted means the
configured burst (at least the limit), and the limit goes to node.json.
B-05: a push has a time limit, so a slow channel does not hang shipping.
E-02, A-09: a malformed or wrong-typed body on a route with an optional body
(scansync) ran the real operation; now it is 400 bad_json.

Steps, through the master's node API:
  1. refused limits: 0, negative, negative burst, burst below the limit, a
     fraction, above 2147483647, a string, {}, no body, truncated JSON:
     each 400 bad_request, and GET /v1/config still shows the old limits;
  2. a valid limit (--valid, default 3,7): the answer, GET /v1/config and
     GET /v1/stats (host.channel_a_mbps) show it;
  3. burst 0 and an omitted burst: 200, the burst is the configured one,
     raised to the limit when it is lower;
  4. node restart (unless --no-restart): GET /v1/config still shows the
     valid limit;
  5. malformed bodies: POST /v1/scansync (wrong type, truncated),
     POST /v1/shipping/resume (wrong type), PUT /v1/config (wrong type,
     truncated): 400 bad_json; POST /v1/scansync with no body still works;
  6. a tight limit (--tight-mbps, burst = limit) and a short load: the
     replicas match the master within --timeout.
The old limits are put back at the end.
"""
import base64
import time

from tblib.cluster import TbError, log
from tblib.results import Results

from ._common import converge_and_record
from .disasters import node_svc, wait_node

HELP = "Channel A limit: 400 for bad values, burst 0, saved over a restart; bad_json bodies (node A-02, B-05, E-02)"

TAG = "bwburst"
ROUTE = "/v1/limits/bandwidth"

REFUSED = [
    ("limit 0", '{"channel_a_mbps": 0}'),
    ("negative limit", '{"channel_a_mbps": -5}'),
    ("negative burst", '{"channel_a_mbps": 5, "channel_a_burst_mbps": -1}'),
    ("burst below the limit", '{"channel_a_mbps": 10, "channel_a_burst_mbps": 5}'),
    ("a fraction", '{"channel_a_mbps": 1.5}'),
    ("above the int32 range", '{"channel_a_mbps": 2147483648}'),
    ("absurdly large", '{"channel_a_mbps": 1e12, "channel_a_burst_mbps": 1e12}'),
    ("a string", '{"channel_a_mbps": "fast"}'),
    ("no field", '{}'),
    ("no body", ""),
    ("truncated JSON", '{"channel_a_mbps": 5'),
]

BAD_JSON = [
    ("POST", "/v1/scansync", '{"dry_run": "yes"}'),
    ("POST", "/v1/scansync", '{"dry_run": tru'),
    ("POST", "/v1/shipping/resume", '{"database": 5}'),
    ("PUT", "/v1/config", '{"limits": {"channel_a_mbps": "fast"}}'),
    ("PUT", "/v1/config", '{"limits": '),
]


def add_args(p):
    p.add_argument("--db", default="", help="test database for the load (db_id or folder name); default: the first")
    p.add_argument("--valid", default="3,7", help="a valid limit and burst, MiB/s")
    p.add_argument("--tight-mbps", type=int, default=1, help="the tight limit for the load step, MiB/s")
    p.add_argument("--load-seconds", type=int, default=30)
    p.add_argument("--timeout", type=int, default=600, help="seconds for the replicas to match under the tight limit")
    p.add_argument("--no-restart", action="store_true", help="skip the node restart step")


def raw(cl, method, path, text):
    """A request with exactly these body bytes ("" = no body). Returns (status, body)."""
    args = {"method": method, "path": path}
    if text:
        args["body_b64"] = base64.b64encode(text.encode()).decode()
    r = cl.hostctl(cl.cfg.master, "node-api", args, check=False)
    if not isinstance(r, dict) or "status" not in r:
        raise TbError(f"{method} {path}: no answer from the node")
    return int(r["status"]), r.get("body")


def err_code(body):
    if isinstance(body, dict):
        e = body.get("error")
        if isinstance(e, dict):
            return e.get("code")
        return body.get("code")
    return None


def limits(cl):
    _, doc = cl.api(cl.cfg.master, "GET", "/v1/config")
    doc = (doc or {}).get("config", doc) or {}
    lim = doc.get("limits") or {}
    return lim.get("channel_a_mbps"), lim.get("channel_a_burst_mbps")


def stats_mbps(cl, want, timeout=60):
    end = time.time() + timeout
    v = None
    while True:
        st, doc = cl.api(cl.cfg.master, "GET", "/v1/stats", check_status=False)
        v = ((doc or {}).get("host") or {}).get("channel_a_mbps") if st == 200 and isinstance(doc, dict) else None
        if v == want or time.time() >= end:
            return v
        time.sleep(5)


def put_limit(cl, mbps, burst):
    body = {"channel_a_mbps": mbps}
    if burst is not None:
        body["channel_a_burst_mbps"] = burst
    return cl.api(cl.cfg.master, "PUT", ROUTE, body, check_status=False)


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def restore(cl, old):
    mbps, burst = old
    if not mbps:
        return
    st, body = put_limit(cl, mbps, burst or 0)
    if st == 200:
        log(f"limits back to {mbps}/{burst}")
        return
    # An old burst below the limit is refused by the new rule: write node.json instead.
    log(f"PUT {ROUTE} {mbps}/{burst} -> {st} {err_code(body)}; restore through PUT /v1/config")
    cl.api(cl.cfg.master, "PUT", "/v1/config",
           {"limits": {"channel_a_mbps": mbps, "channel_a_burst_mbps": burst}}, check_status=False)


def run(cl, a):
    res = Results("bandwidth_burst0", vars(a).copy())
    m = cl.cfg.master
    old = (None, None)
    try:
        vm, vb = (int(x) for x in a.valid.split(","))
        old = limits(cl)
        log(f"limits now: channel_a_mbps={old[0]} channel_a_burst_mbps={old[1]}")
        if not old[0]:
            raise TbError("GET /v1/config shows no limits.channel_a_mbps")

        # 1. refused values
        for name, text in REFUSED:
            st, body = raw(cl, "PUT", ROUTE, text)
            now = limits(cl)
            ok = st == 400 and err_code(body) == "bad_request" and now == old
            res.record(f"refused: {name}", "PASS" if ok else "FAIL",
                       note=f"{st} {err_code(body)}; limits {old} -> {now}", body=body)
            if now != old:
                restore(cl, old)

        # 2. a valid limit
        st, body = put_limit(cl, vm, vb)
        got = (num((body or {}).get("channel_a_mbps")), num((body or {}).get("channel_a_burst_mbps"))) \
            if isinstance(body, dict) else (None, None)
        res.record("valid limit: the answer", "PASS" if st == 200 and got == (vm, vb) else "FAIL",
                   note=f"{st} {body}")
        now = limits(cl)
        res.record("valid limit: GET /v1/config", "PASS" if now == (vm, vb) else "FAIL", note=f"limits {now}")
        sv = stats_mbps(cl, vm)
        res.record("valid limit: GET /v1/stats host.channel_a_mbps", "PASS" if num(sv) == vm else "FAIL",
                   note=f"host.channel_a_mbps={sv}")

        # 3. burst 0 and an omitted burst
        st, body = put_limit(cl, vm + 2, 0)
        b = num((body or {}).get("channel_a_burst_mbps")) if isinstance(body, dict) else None
        want = max(vb, vm + 2)
        now = limits(cl)
        ok = st == 200 and b == want and now == (vm + 2, want)
        res.record("burst 0: the configured burst", "PASS" if ok else "FAIL",
                   note=f"{st} burst={b} (want {want}); config {now}")
        big = max(vb, vm + 2) + 3
        st, body = put_limit(cl, big, None)
        b = num((body or {}).get("channel_a_burst_mbps")) if isinstance(body, dict) else None
        now = limits(cl)
        ok = st == 200 and b == big and now == (big, big)
        res.record("burst omitted: raised to the limit", "PASS" if ok else "FAIL",
                   note=f"{st} burst={b} (want {big}); config {now}")

        # 4. saved over a node restart
        put_limit(cl, vm, vb)
        if not a.no_restart:
            node_svc(cl, m, "restart")
            if not wait_node(cl, m, 180):
                raise TbError("the master node did not come back after the restart")
            now = limits(cl)
            res.record("valid limit: kept over a node restart", "PASS" if now == (vm, vb) else "FAIL",
                       note=f"limits {now}")
            sv = stats_mbps(cl, vm)
            res.record("after the restart: GET /v1/stats host.channel_a_mbps", "PASS" if num(sv) == vm else "FAIL",
                       note=f"host.channel_a_mbps={sv}")

        # 5. malformed bodies
        for method, path, text in BAD_JSON:
            before = limits(cl)
            st, body = raw(cl, method, path, text)
            after = limits(cl)
            ok = st == 400 and err_code(body) == "bad_json" and before == after
            res.record(f"bad_json: {method} {path} {text}", "PASS" if ok else "FAIL",
                       note=f"{st} {err_code(body)}", body=body)
        st, body = raw(cl, "POST", "/v1/scansync", "")
        res.record("POST /v1/scansync with no body still works", "PASS" if st == 200 else "FAIL",
                   note=f"{st} {err_code(body) or (body or {}).get('status') if isinstance(body, dict) else body}")

        # 6. a tight limit and a short load
        dbs = cl.test_dbs(which=a.db or "all")
        if not dbs:
            raise TbError("no test database on the master (run 'dbs' first)")
        d = dbs[0]
        st, body = put_limit(cl, a.tight_mbps, a.tight_mbps)
        if st != 200:
            raise TbError(f"PUT {ROUTE} {a.tight_mbps}/{a.tight_mbps} -> {st} {body}")
        cl.load_stop(TAG)
        cl.load_start([d["path"]], mode="write", tx="off", conns="1:2", tag=TAG)
        time.sleep(a.load_seconds)
        cl.load_stop(TAG)
        t0 = time.time()
        ok = converge_and_record(cl, res, f"tight limit {a.tight_mbps} MiB/s: the replicas match",
                                 [d["path"]], a.timeout)
        log(f"converged={ok} in {int(time.time() - t0)}s under {a.tight_mbps} MiB/s")
    except TbError as e:
        res.record("bandwidth_burst0", "FAIL", note=str(e)[:400])
    except ValueError as e:
        res.record("bandwidth_burst0", "FAIL", note=f"--valid must be 'limit,burst': {e}")
    finally:
        cl.load_stop(TAG)
        try:
            restore(cl, old)
            log(f"limits now: {limits(cl)}")
        except TbError as e:
            log(f"restore the limits: {e}")
    return res.finish()
