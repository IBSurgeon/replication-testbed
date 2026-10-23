"""The RCM same-origin rule, the JSON body rule, anti-framing headers and the
Digest login throttle (hqcluster RCM review 2026-10-22: G-01, G-02, G-12).

G-01: a request signed in by the session cookie that changes state must come
from a page of RCM itself (Sec-Fetch-Site same-origin or none; without it,
an Origin equal to Host); otherwise 403 csrf. The login form has the same
rule (403, a short text page). A body must be application/json, else 415
unsupported_media_type. Digest (the CLI) is not checked for the origin.
G-02: Digest failures count in the login throttle: 10 failures in 5 minutes
per user and address (per address for a user RCM does not know), then 429
throttled.
G-12: every answer has X-Frame-Options DENY and Content-Security-Policy
frame-ancestors 'none'.

The mutating request is POST /v1/alerts/tb-no-such-alert/ack: an alert id
RCM does not have, so the normal answer is 404 not_found and nothing changes.
All calls go from the RCM host to the operator port (module 92-ident
rcm-http, rcm-digest-fail). The throttle step uses a user name RCM does not
know, so the real operator is not locked out; the address key stays blocked
for unknown names for 5 minutes (or until RCM restarts).
"""
import base64
import json

from tblib.cluster import TbError, log
from tblib.results import Results

from . import _rcm

HELP = "RCM: 403 csrf for cross-site session requests, 415 for non-JSON bodies, anti-framing, Digest throttle (RCM G-01, G-02, G-12)"

ACK = "/v1/alerts/tb-no-such-alert/ack"
FOREIGN = "http://127.0.0.1:7999"
ATTEMPTS = 11


def add_args(p):
    p.add_argument("--no-throttle", action="store_true", help="skip the Digest throttle step")


def b64(text):
    return base64.b64encode(text.encode()).decode()


def rcm_http(cl, auth, method, path, body=None, ctype="application/json", headers=None, login="same-origin"):
    args = {"auth": auth, "method": method, "path": path, "content_type": ctype, "login_headers": login}
    if body is not None:
        args["body_b64"] = b64(body)
    if headers:
        args["headers_b64"] = b64(json.dumps(headers))
    rc, res, _, out = cl.module(cl.cfg.rcm_host, "92-ident", "rcm-http", args, check=False)
    if rc != 0 or not isinstance(res, dict):
        raise TbError(f"rcm-http {auth} {method} {path}: {(out or '')[-300:]}")
    return res


def code(res):
    b = res.get("body")
    if isinstance(b, dict) and isinstance(b.get("error"), dict):
        return b["error"].get("code")
    return None


def brief(res):
    b = res.get("body")
    text = json.dumps(b) if isinstance(b, (dict, list)) else str(b or "")
    return f"HTTP {res.get('status')} {code(res) or ''} {text[:160]}".strip()


def expect(res, case, r, status, err=None):
    ok = r.get("status") == status and (err is None or code(r) == err)
    res.record(case, "PASS" if ok else "FAIL", note=f"want {status} {err or ''}; got {brief(r)}")
    return ok


def frame_headers(res, case, r):
    h = r.get("headers") or {}
    xfo = h.get("x-frame-options", "")
    csp = h.get("content-security-policy", "")
    ok = xfo.upper() == "DENY" and "frame-ancestors 'none'" in csp
    res.record(case, "PASS" if ok else "FAIL",
               note=f"HTTP {r.get('status')}; X-Frame-Options={xfo or '-'}; CSP={csp or '-'}; "
                    f"Referrer-Policy={h.get('referrer-policy', '-')}; "
                    f"X-Content-Type-Options={h.get('x-content-type-options', '-')}")


def run(cl, a):
    res = Results("csrf", vars(a).copy())
    if not _rcm.ready(cl):
        res.record("csrf", "SKIP", note="RCM is off or secrets.rcm_user / rcm_password are not set")
        return res.finish()
    try:
        # The login form (G-01: a login forced from another site).
        r = rcm_http(cl, "session", "GET", "/v1/alerts", login="none")
        ok = r.get("login_status") == 403 and not r.get("login_cookie")
        res.record("login without Origin / Sec-Fetch-Site: 403", "PASS" if ok else "FAIL",
                   note=f"login HTTP {r.get('login_status')}, session cookie {r.get('login_cookie')} "
                        "(90-hostctl rcm-web logs in like this)")
        r = rcm_http(cl, "session", "GET", "/v1/alerts", login="cross-site")
        ok = r.get("login_status") == 403 and not r.get("login_cookie")
        res.record("login from another site: 403", "PASS" if ok else "FAIL",
                   note=f"login HTTP {r.get('login_status')}, session cookie {r.get('login_cookie')}")
        r = rcm_http(cl, "session", "GET", "/v1/alerts")
        ok = r.get("login_cookie") and r.get("login_status") in (200, 302, 303) and r.get("status") == 200
        res.record("login from the same origin works", "PASS" if ok else "FAIL",
                   note=f"login HTTP {r.get('login_status')}, then GET /v1/alerts HTTP {r.get('status')}")
        if not ok:
            raise TbError("no RCM session: the other session cases cannot run")

        # Session + a request that changes state (G-01).
        same = {"Sec-Fetch-Site": "same-origin"}
        r = rcm_http(cl, "session", "POST", ACK, "{}", headers={"Origin": FOREIGN})
        expect(res, "session POST, foreign Origin: 403 csrf", r, 403, "csrf")
        r = rcm_http(cl, "session", "POST", ACK, "{}", headers={"Sec-Fetch-Site": "cross-site"})
        expect(res, "session POST, Sec-Fetch-Site cross-site: 403 csrf", r, 403, "csrf")
        r = rcm_http(cl, "session", "POST", ACK, "{}", headers={"Sec-Fetch-Site": "same-site", "Origin": FOREIGN})
        expect(res, "session POST, Sec-Fetch-Site same-site (another port): 403 csrf", r, 403, "csrf")
        r = rcm_http(cl, "session", "POST", ACK, "{}")
        expect(res, "session POST, neither Origin nor Sec-Fetch-Site: 403 csrf", r, 403, "csrf")
        r = rcm_http(cl, "session", "POST", ACK, "{}", ctype="text/plain", headers=same)
        expect(res, "session POST, same origin, text/plain body: 415", r, 415, "unsupported_media_type")
        r = rcm_http(cl, "session", "POST", ACK, "{}", headers=same)
        expect(res, "session POST, same origin, JSON: the normal answer (404)", r, 404, "not_found")
        # The module calls 127.0.0.1:7444, so that is the Host header RCM sees.
        r = rcm_http(cl, "session", "POST", ACK, "{}", headers={"Origin": "http://127.0.0.1:7444"})
        expect(res, "session POST, Origin equal to Host only: the normal answer (404)", r, 404, "not_found")
        r = rcm_http(cl, "session", "GET", "/v1/alerts", headers={"Origin": FOREIGN, "Sec-Fetch-Site": "cross-site"})
        expect(res, "session GET, foreign Origin: still 200", r, 200)

        # Digest, as the CLI calls RCM: no origin rule, the JSON rule still holds.
        r = rcm_http(cl, "digest", "POST", ACK, "{}")
        expect(res, "Digest POST, no Origin: the normal answer (404)", r, 404, "not_found")
        r = rcm_http(cl, "digest", "POST", ACK, "{}", headers={"Origin": FOREIGN, "Sec-Fetch-Site": "cross-site"})
        expect(res, "Digest POST, foreign Origin: not checked (404)", r, 404, "not_found")
        r = rcm_http(cl, "digest", "POST", ACK, "{}", ctype="text/plain")
        expect(res, "Digest POST, text/plain body: 415", r, 415, "unsupported_media_type")

        # Anti-framing headers (G-12).
        r = rcm_http(cl, "none", "GET", "/login", headers={"Accept": "text/html"})
        frame_headers(res, "GET /login: anti-framing headers", r)
        r = rcm_http(cl, "session", "GET", "/databases", headers={"Accept": "text/html"})
        frame_headers(res, "GET /databases (page): anti-framing headers", r)
        r = rcm_http(cl, "digest", "GET", "/v1/alerts")
        frame_headers(res, "GET /v1/alerts (API): anti-framing headers", r)

        # The Digest throttle (G-02).
        if not a.no_throttle:
            # RCM allows 10 failures in 5 minutes: the 11th attempt is refused.
            rc, out, _, raw = cl.module(cl.cfg.rcm_host, "92-ident", "rcm-digest-fail",
                                        {"count": ATTEMPTS, "then_real": True}, check=False)
            if rc != 0 or not isinstance(out, dict):
                raise TbError(f"rcm-digest-fail: {(raw or '')[-300:]}")
            sts = out.get("statuses") or []
            last = out.get("last_body")
            last_code = (last.get("error") or {}).get("code") if isinstance(last, dict) else None
            first429 = sts.index(429) + 1 if 429 in sts else None
            ok = (len(sts) == ATTEMPTS and all(s == 401 for s in sts[:-1])
                  and sts[-1] == 429 and last_code == "throttled")
            note = f"statuses {sts}; first 429 at attempt {first429}; last code {last_code}"
            if sts and sts[0] == 429:
                res.record("Digest: wrong passwords are throttled (429)", "SKIP",
                           note="still throttled by an earlier run (5 minutes, or restart RCM); " + note)
            else:
                res.record("Digest: wrong passwords are throttled (429)", "PASS" if ok else "FAIL", note=note)
            rs = out.get("real_status")
            res.record("Digest: the real operator is not locked out", "PASS" if rs == 200 else "FAIL",
                       note=f"real user GET /v1/alerts HTTP {rs}")
            log("the address key of unknown Digest users stays throttled for 5 minutes")
    except TbError as e:
        res.record("csrf", "FAIL", note=str(e)[:400])
    return res.finish()
