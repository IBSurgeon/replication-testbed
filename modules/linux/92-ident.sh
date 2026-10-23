#!/usr/bin/env bash
# Module 92 (Linux): host actions of the tests csrf and ghost_autoreinit
# (hqcluster review 2026-10-22: RCM same-origin rule, login throttle, Ghost
# rows and automatic reinit, reinit jobs across an RCM restart).
#
#   92-ident.sh rcm-http        --auth digest|session|none --method GET --path /v1/alerts
#                               [--body-b64 B64] [--content-type application/json|none]
#                               [--headers-b64 B64] [--login-headers same-origin|none|cross-site]
#       One request to the RCM operator port. --headers-b64: a JSON object of
#       extra request headers (base64). session: a form login first; with
#       --login-headers none the login has no Origin / Sec-Fetch-Site (as
#       90-hostctl rcm-web sends it), with cross-site it has a foreign Origin
#       and Sec-Fetch-Site cross-site; a refused login ends the call and the
#       answer only reports the login.
#       Result: status, the security headers of the answer, body; for a
#       session also login_status and login_cookie (true/false, never the value).
#   92-ident.sh rcm-digest-fail --count 11 [--user NAME] [--then-real true]
#       COUNT Digest attempts in a row with a user name RCM does not know and a
#       random password, each with a fresh challenge. Result: statuses, the last
#       body; with --then-real true also real_status (a Digest call of the real
#       user right after).
#   92-ident.sh rcm-svc         --action stop|start|restart|status [--unit hqbirdrcm]
#       After start/restart: waits until GET /v1/health answers (90 s).
#       Result: unit, active, answering, local_time (HH:MM, the host's clock:
#       RCM reads its time windows in it).
#   92-ident.sh rcm-block       --action on|off --port PORT [--unit hqbirdrcm]
#       on: REJECT every TCP connection of the RCM service's user to PORT
#       (iptables, owner match); off: removes every such rule. RCM then cannot
#       reach a node on that port; other users can.
#   92-ident.sh rcm-ghost       --action add|remove|list --rcm-dir DIR
#                               --donor-node ID --donor-db ID [--old-master-node ID
#                               --old-master-db ID --rel-path P --guid G --promoted-to-node ID]
#       add/remove: RCM stopped, its state.json (data_dir of rcm.json) gets or
#       loses a ghost_replicas tombstone (what a promote job writes), RCM started.
#   92-ident.sh rcm-reinits     --rcm-dir DIR
#       The reinit jobs of RCM's state.json (the last 100; RCM keeps running):
#       id, master_node, master_db, replica_node, command_id, status, phase,
#       error, created_at. The API has no list of reinit jobs.
#
# --rcm-addr HOST:PORT changes the RCM operator address (default 127.0.0.1:7444).
# The RCM login is TB_RCM_USER / TB_RCM_PASSWORD of secrets.env.
# Results come as one "TBRESULT <json>" line.
source "$(dirname "$0")/common.sh"

CMD="${1:-}"; shift || true
parse_args "$@"

ADDR="$(arg rcm_addr 127.0.0.1:7444)"
UNIT="$(arg unit hqbirdrcm)"

rcm_up() {
  python3 -c 'import sys, urllib.request; urllib.request.urlopen("http://" + sys.argv[1] + "/v1/health", timeout=5)' "$ADDR"
}

rcm_answering() { if rcm_up >/dev/null 2>&1; then echo true; else echo false; fi; }

rcm_uid() {
  local pid uid="" u
  pid="$(systemctl show -p MainPID --value "$UNIT" 2>/dev/null || echo 0)"
  if [[ -n "$pid" && "$pid" != 0 ]]; then uid="$(ps -o uid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"; fi
  if [[ -z "$uid" ]]; then
    u="$(systemctl show -p User --value "$UNIT" 2>/dev/null || true)"
    uid="$(id -u "${u:-root}" 2>/dev/null || true)"
  fi
  echo "$uid"
}

case "$CMD" in
  rcm-http|rcm-digest-fail)
    read_secrets
    python3 - "$CMD" "$ADDR" "$(arg auth digest)" "$(arg method GET)" "$(arg path /v1/alerts)" \
      "$(arg body_b64)" "$(arg content_type application/json)" "$(arg headers_b64)" \
      "$(arg login_headers same-origin)" "$(arg count 11)" "$(arg user tb-no-such-user)" \
      "$(arg then_real false)" <<'PY'
import base64, hashlib, http.client, json, os, re, sys, urllib.parse, uuid

(cmd, addr, auth, method, path, body_b64, ctype, hdr_b64, login_mode,
 count, bad_user, then_real) = sys.argv[1:13]
user, pw = os.environ.get("TB_RCM_USER", ""), os.environ.get("TB_RCM_PASSWORD", "")
host, port = addr.rsplit(":", 1)
KEEP = ("content-type", "x-frame-options", "content-security-policy", "referrer-policy",
        "x-content-type-options", "location")


def send(m, p, data=None, headers=None):
    c = http.client.HTTPConnection(host, int(port), timeout=60)
    try:
        c.request(m, p, body=data, headers=headers or {})
        r = c.getresponse()
        return r.status, [(k.lower(), v) for k, v in r.getheaders()], r.read()
    finally:
        c.close()


def md5(s):
    return hashlib.md5(s.encode()).hexdigest()


def challenge():
    # A request without credentials is not counted by the throttle.
    st, hs, _ = send("GET", "/v1/alerts", headers={"Accept": "application/json"})
    for k, v in hs:
        if k == "www-authenticate" and v.lower().startswith("digest "):
            return {m[0].lower(): (m[1] or m[2]) for m in
                    re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]*))', v[7:])}
    sys.exit(f"no Digest challenge from RCM on {addr} (HTTP {st})")


def digest(m, p, u, password):
    ch = challenge()
    realm, nonce = ch.get("realm", ""), ch.get("nonce", "")
    ha1 = md5(f"{u}:{realm}:{password}")
    ha2 = md5(f"{m}:{p}")
    if "auth" in [q.strip() for q in ch.get("qop", "").split(",")]:
        nc, cnonce = "00000001", uuid.uuid4().hex[:16]
        resp = md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        return (f'Digest username="{u}", realm="{realm}", nonce="{nonce}", uri="{p}", '
                f'algorithm=MD5, response="{resp}", qop=auth, nc={nc}, cnonce="{cnonce}"')
    resp = md5(f"{ha1}:{nonce}:{ha2}")
    return (f'Digest username="{u}", realm="{realm}", nonce="{nonce}", uri="{p}", '
            f'algorithm=MD5, response="{resp}"')


def login(mode):
    form = urllib.parse.urlencode({"username": user, "password": pw}).encode()
    h = {"Content-Type": "application/x-www-form-urlencoded"}
    if mode == "same-origin":
        h["Origin"] = "http://" + addr
        h["Sec-Fetch-Site"] = "same-origin"
    elif mode == "cross-site":
        h["Origin"] = "http://127.0.0.1:7999"
        h["Sec-Fetch-Site"] = "cross-site"
    st, hs, _ = send("POST", "/login", form, h)
    for k, v in hs:
        if k == "set-cookie" and v.startswith("rcm_session="):
            return st, v.split(";", 1)[0]
    return st, ""


def decode(raw, hs):
    ct = dict(hs).get("content-type", "")
    if "json" in ct:
        try:
            return json.loads(raw or b"null")
        except ValueError:
            pass
    return raw.decode(errors="replace")[:1500]


if not user or not pw:
    sys.exit("TB_RCM_USER / TB_RCM_PASSWORD are not set (secrets rcm_user, rcm_password)")

if cmd == "rcm-digest-fail":
    statuses, last = [], None
    for _ in range(int(count)):
        h = {"Accept": "application/json",
             "Authorization": digest("GET", "/v1/alerts", bad_user, uuid.uuid4().hex)}
        st, hs, raw = send("GET", "/v1/alerts", None, h)
        statuses.append(st)
        last = decode(raw, hs)
    out = {"statuses": statuses, "last_body": last, "user": bad_user}
    if then_real == "true":
        h = {"Accept": "application/json", "Authorization": digest("GET", "/v1/alerts", user, pw)}
        out["real_status"] = send("GET", "/v1/alerts", None, h)[0]
    print("TBRESULT " + json.dumps(out))
    sys.exit(0)

extra = json.loads(base64.b64decode(hdr_b64)) if hdr_b64 else {}
data = base64.b64decode(body_b64) if body_b64 else None
h = {"Accept": "application/json"}
if data is not None and ctype != "none":
    h["Content-Type"] = ctype
out = {"auth": auth}
if auth == "session":
    lst, cookie = login(login_mode)
    out["login_status"], out["login_cookie"] = lst, bool(cookie)
    if not cookie:
        out["status"] = None
        print("TBRESULT " + json.dumps(out))
        sys.exit(0)
    h["Cookie"] = cookie
elif auth == "digest":
    h["Authorization"] = digest(method, path, user, pw)
h.update(extra)
st, hs, raw = send(method, path, data, h)
out["status"] = st
out["headers"] = {k: v for k, v in hs if k in KEEP}
out["body"] = decode(raw, hs)
print("TBRESULT " + json.dumps(out))
PY
    ;;

  rcm-svc)
    case "$(arg action status)" in
      stop|start|restart) systemctl "$(arg action)" "$UNIT" ;;
      status) ;;
      *) die "--action stop|start|restart|status" ;;
    esac
    up=false
    case "$(arg action status)" in
      start|restart) if wait_until 90 rcm_up; then up=true; fi ;;
      *) up="$(rcm_answering)" ;;
    esac
    result "{\"unit\":\"$UNIT\",\"action\":\"$(arg action status)\",\"active\":\"$(systemctl is-active "$UNIT" 2>/dev/null || true)\",\"answering\":$up,\"local_time\":\"$(date +%H:%M)\"}"
    ;;

  rcm-block)
    need_cmd iptables
    P="$(arg port)"; [[ "$P" =~ ^[0-9]+$ ]] || die "--port is required"
    case "$(arg action)" in
      on)
        U="$(rcm_uid)"; [[ "$U" =~ ^[0-9]+$ ]] || die "cannot find the user of $UNIT"
        iptables -I OUTPUT -p tcp --dport "$P" -m owner --uid-owner "$U" \
          -j REJECT --reject-with tcp-reset -m comment --comment tb-ident-rcm-block
        result "{\"action\":\"on\",\"port\":$P,\"uid\":$U}"
        ;;
      off)
        n=0
        while true; do
          num="$(iptables -L OUTPUT --line-numbers -n 2>/dev/null | awk '/tb-ident-rcm-block/ {print $1; exit}' || true)"
          [[ -n "$num" ]] || break
          iptables -D OUTPUT "$num"; n=$((n + 1))
        done
        result "{\"action\":\"off\",\"removed\":$n}"
        ;;
      *) die "--action on|off" ;;
    esac
    ;;

  rcm-ghost)
    ACTION="$(arg action list)"
    case "$ACTION" in add|remove|list) ;; *) die "--action add|remove|list" ;; esac
    RCM_DIR="$(arg rcm_dir /opt/hqbirdrcm)"
    [[ -f "$RCM_DIR/rcm.json" ]] || die "no $RCM_DIR/rcm.json"
    [[ "$ACTION" == list ]] || systemctl stop "$UNIT"
    set +e
    out="$(python3 - "$ACTION" "$RCM_DIR" "$(arg donor_node)" "$(arg donor_db)" "$(arg old_master_node)" \
      "$(arg old_master_db)" "$(arg rel_path)" "$(arg guid)" "$(arg promoted_to_node)" <<'PY'
import datetime, json, os, sys

action, rcm_dir, donor_node, donor_db, om_node, om_db, rel, guid, promoted = sys.argv[1:10]
with open(os.path.join(rcm_dir, "rcm.json"), encoding="utf-8") as f:
    dd = json.load(f).get("data_dir") or "./rcm-data"
if not os.path.isabs(dd):
    dd = os.path.normpath(os.path.join(rcm_dir, dd))
path = os.path.join(dd, "state.json")
with open(path, encoding="utf-8") as f:
    st = json.load(f)
ghosts = st.get("ghost_replicas") or {}
key = donor_node + "|" + donor_db
if action == "add":
    if not (donor_node and donor_db and om_node and om_db):
        sys.exit("add needs --donor-node, --donor-db, --old-master-node, --old-master-db")
    rec = {"donor_node_id": donor_node, "donor_db_id": donor_db, "old_master_node_id": om_node,
           "old_master_db_id": om_db, "rel_path": rel, "promoted_to_node_id": promoted or donor_node,
           "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if guid:
        rec["guid"] = guid
    ghosts[key] = rec
elif action == "remove":
    ghosts.pop(key, None)
if action != "list":
    if ghosts:
        st["ghost_replicas"] = ghosts
    else:
        st.pop("ghost_replicas", None)
    info = os.stat(path)
    tmp = path + ".tb-ident"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, info.st_mode & 0o7777)
    os.chown(tmp, info.st_uid, info.st_gid)
    os.replace(tmp, path)
print("TBRESULT " + json.dumps({"action": action, "state": path, "ghosts": sorted(ghosts)}))
PY
)"
    ec=$?
    set -e
    [[ "$ACTION" == list ]] || systemctl start "$UNIT"
    up=false
    if wait_until 90 rcm_up; then up=true; fi
    [[ $ec -eq 0 ]] || { echo "$out" >&2; die "state.json not changed (exit $ec)"; }
    line="$(echo "$out" | grep '^TBRESULT ' | tail -n 1)"
    python3 -c 'import json, sys; d = json.loads(sys.argv[1]); d["answering"] = sys.argv[2] == "true"; print("TBRESULT " + json.dumps(d))' \
      "${line#TBRESULT }" "$up"
    ;;

  rcm-reinits)
    RCM_DIR="$(arg rcm_dir /opt/hqbirdrcm)"
    [[ -f "$RCM_DIR/rcm.json" ]] || die "no $RCM_DIR/rcm.json"
    python3 - "$RCM_DIR" <<'PY'
import json, os, sys

rcm_dir = sys.argv[1]
with open(os.path.join(rcm_dir, "rcm.json"), encoding="utf-8") as f:
    dd = json.load(f).get("data_dir") or "./rcm-data"
if not os.path.isabs(dd):
    dd = os.path.normpath(os.path.join(rcm_dir, dd))
path = os.path.join(dd, "state.json")
with open(path, encoding="utf-8") as f:
    st = json.load(f)
keys = ("id", "master_node", "master_db", "replica_node", "command_id", "status", "phase", "error", "created_at")
jobs = [{k: j.get(k) for k in keys} for j in st.get("reinits") or [] if isinstance(j, dict)][-100:]
print("TBRESULT " + json.dumps({"state": path, "reinits": jobs}))
PY
    ;;

  *) die "usage: 92-ident.sh rcm-http|rcm-digest-fail|rcm-svc|rcm-block|rcm-ghost|rcm-reinits [--key value ...]" ;;
esac
