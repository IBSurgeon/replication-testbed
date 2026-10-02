"""RCM helpers for the tests: the operator API (Digest login, 90-hostctl
rcm-api), the Databases page parts (web login, 90-hostctl rcm-web) and a
small reader of the database tables of that page:

  m1  master-oriented: one master node, a column per replica (node, slot)
  m2  replica-oriented: a block (<tbody>) per master database, a row per replica
  m3  combined: m2 plus "not paired" rows with Initialize and a master checkbox

The login is secrets.rcm_user / rcm_password of the local config.
"""
import base64
import html
import json
import re
import time

from tblib.cluster import TbError, log


def ready(cl):
    s = cl.cfg.secrets
    return cl.cfg.rcm_enabled and all(s.get(k) and not s[k].startswith("<") for k in ("rcm_user", "rcm_password"))


def api(cl, method, path, body=None, then_restart=False):
    """RCM operator API on the RCM host. Returns (status, body)."""
    args = {"method": method, "path": path, "then_restart": then_restart}
    if body is not None:
        args["body_b64"] = base64.b64encode(json.dumps(body).encode()).decode()
    r = cl.hostctl(cl.cfg.rcm_host, "rcm-api", args, check=False)
    if not isinstance(r, dict):
        raise TbError(f"RCM {method} {path} failed (is secrets.rcm_user / rcm_password set?)")
    return r.get("status"), r.get("body")


def web(cl, path):
    """A Databases page part, as the page asks for it. Returns (status, body)."""
    r = cl.hostctl(cl.cfg.rcm_host, "rcm-web", {"path": path}, check=False)
    if not isinstance(r, dict):
        raise TbError(f"RCM web {path} failed (login?)")
    return r.get("status"), r.get("body")


def poll_now(cl):
    api(cl, "POST", "/v1/poll-now", {})


def table(cl, tab, master=""):
    q = f"/partials/db-table?tab={tab}" + (f"&master={master}" if master else "")
    st, body = web(cl, q)
    if st != 200 or not isinstance(body, str):
        raise TbError(f"RCM {q}: HTTP {st}")
    return body


def node_ids(cl):
    st, body = api(cl, "GET", "/v1/nodes")
    rows = body.get("nodes", body) if isinstance(body, dict) else body
    return {n.get("node_id"): n for n in rows or [] if isinstance(n, dict)}


def alerts(cl, code=None):
    _, body = api(cl, "GET", "/v1/alerts")
    rows = body.get("alerts", body) if isinstance(body, dict) else body
    rows = [a for a in rows or [] if isinstance(a, dict) and not a.get("cleared")]
    return [a for a in rows if code is None or a.get("code") == code]


def wait_job(cl, path, timeout=1800, poll=5, running=("running",)):
    """Poll an RCM job (GET path) until its status leaves `running`."""
    end = time.time() + timeout
    job = None
    while time.time() < end:
        st, job = api(cl, "GET", path)
        if st == 200 and isinstance(job, dict) and job.get("status") not in running:
            return job
        time.sleep(poll)
    raise TbError(f"RCM job {path} still running after {timeout}s: {json.dumps(job)[:300]}")


def reinit(cl, master_node, db_id, to, mode="standard", timeout=1800):
    """RCM reinit / Initialize (what the page's button sends). Returns
    (http_status, answer_or_job): the finished job when RCM started one."""
    st, body = api(cl, "POST", f"/v1/nodes/{master_node}/reinit",
                   {"db_id": db_id, "to": to, "ignore_window": True, "mode": mode,
                    "hold_on_long_transactions": True})
    rid = body.get("reinit_id") if isinstance(body, dict) else None
    if st != 202 or not rid:
        return st, body
    return st, wait_job(cl, f"/v1/reinit/{rid}", timeout=timeout)


# ------------------------------------------------------------ table reader --
_ATTR = re.compile(r'([\w-]+)="([^"]*)"')


def _attrs(tag):
    return {k: html.unescape(v) for k, v in _ATTR.findall(tag)}


def blocks(page):
    """The blocks of an m2/m3 table: {key: {"master_node", "master_db", "rows": [...]}}.
    A row: {"kind": "replica"|"not_paired"|"none", "node", "db_id", "name",
    "status", "promote", "attrs"} (attrs: the <tr>'s data-*)."""
    out = {}
    for m in re.finditer(r'<tbody class="topo-block" data-key="([^"]*)">(.*?)</tbody>', page, re.S):
        key, body = html.unescape(m.group(1)), m.group(2)
        rows = []
        for part in re.split(r'(?=<tr class="topo-row")', body):
            if not part.startswith('<tr class="topo-row"'):
                continue
            tr = _attrs(part[:part.index(">")])
            row = {"attrs": tr, "promote": 'data-act="promote"' in part}
            npd = re.search(r'class="not-paired" data-side="r" data-rn="([^"]*)"', part)
            cell = re.search(r'<td class="actions" data-side="r"([^>]*)>', part)
            if npd:
                row.update(kind="not_paired", node=html.unescape(npd.group(1)),
                           init_to=(re.search(r'data-act="init" data-to="([^"]*)"', part) or [None, None])[1])
            elif cell:
                a = _attrs(cell.group(1))
                row.update(kind="replica", node=a.get("data-rn"), db_id=a.get("data-rd"),
                           name=a.get("data-rname"), status=a.get("data-rstatus"))
            else:
                row.update(kind="none")
            rows.append(row)
        first = rows[0]["attrs"] if rows else {}
        out[key] = {"master_node": first.get("data-mn", ""), "master_db": first.get("data-md", ""),
                    "rows": rows}
    return out


def replica_rows(block, node=None, kind="replica"):
    return [r for r in block["rows"] if r["kind"] == kind and (node is None or r.get("node") == node)]


def m1_columns(page):
    """Node ids of the replica column groups of an m1 table, in order (a node
    that holds two replicas of one database has two)."""
    return [html.unescape(x) for x in
            re.findall(r'class="topo-grp-replica">[^<]*<span class="topo-grp-node">([^<]*)</span>', page)]


def wait_table(cl, tab, cond, timeout=240, poll=15, what=""):
    """Ask RCM to poll the nodes, read the table, until cond(page) is true.
    Returns (ok, page)."""
    end = time.time() + timeout
    page = ""
    while True:
        try:
            poll_now(cl)
        except TbError:
            pass
        page = table(cl, tab)
        if cond(page):
            return True, page
        if time.time() >= end:
            return False, page
        log(f"RCM {tab} table: waiting for {what or 'the change'}")
        time.sleep(poll)
