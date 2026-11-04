#!/usr/bin/env bash
# Module 94 (Linux): a cluster that RCM serves, without goafts (fbagent
# contract §7, "source rcm"): the host side of 'tb.py rcm' and of the
# nogoafts test.
#
#   94-rcmcluster.sh stage-rcm  --binary FILE [--fbagent-dir DIR]
#                               (the RCM build in the agent's staging folder
#                               update/hqbirdrcm/staged/, as the HQbird setup
#                               payload leaves it)
#   94-rcmcluster.sh init       --cluster-id ID [--address A] [--fbagent-dir DIR]
#                               (fbagent --cluster-init as the agent's user:
#                               rcm_url and ca_hash)
#   94-rcmcluster.sh join       --url URL --ca-hash H --token-b64 B64
#                               [--fbagent-dir DIR]
#                               (fbagent --cluster-join as the agent's user;
#                               --fbagent-dir can be a scratch agent)
#   94-rcmcluster.sh rcm        --args-b64 B64 [--rcm-dir DIR]
#                               (hqbirdrcm with the JSON list of arguments,
#                               RCM_USER / RCM_PASSWORD from the secrets file)
#   94-rcmcluster.sh release-import --file F --product P --version V
#                               [--platform linux-amd64] [--sig F] [--rcm-dir DIR]
#                               (hqbirdrcm release import as user hqbirdrcm)
#   94-rcmcluster.sh scratch-agent --name N [--agent-id ID] [--fbagent-dir DIR]
#                               (a copy of the agent's binary and config in
#                               /var/tmp/tb-agent-N, owned by the agent's user,
#                               for joins that must fail; no service)
#   94-rcmcluster.sh scratch-remove --name N
#   94-rcmcluster.sh facts      [--fbagent-dir DIR] [--rcm-dir DIR] [--node-dirs D1,D2]
#                               (services, their users, files, ports, versions)
#
# Results come as one "TBRESULT <json>" line. Every command runs as root;
# the agent's own commands run as the owner of the agent's folder.
source "$(dirname "$0")/common.sh"

CMD="${1:-}"; shift || true
parse_args "$@"
need_root

FBA_DIR="$(arg fbagent_dir /opt/hqbird-fbagent)"
RCM_DIR="$(arg rcm_dir /opt/hqbirdrcm)"

agent_user() { stat -c %U "$FBA_DIR"; }

# as_agent CMD...: run in the agent's folder as its user (the guide's
# "sudo -u firebird ./fbagent ...").
as_agent() {
  local u; u="$(agent_user)"
  (cd "$FBA_DIR" && runuser -u "$u" -- "$@")
}

# run_result CMD...: run, then TBRESULT {"rc": N, "out": "<last OUT_LIMIT chars>"}.
# QUIET=1: the output goes only into TBRESULT (a join token, a long document).
OUT_LIMIT=6000
QUIET=0
run_result() {
  local out ec
  set +e; out="$("$@" 2>&1)"; ec=$?; set -e
  [[ "$QUIET" == 1 ]] || printf '%s\n' "$out"
  python3 -c '
import json, sys
print("TBRESULT " + json.dumps({"rc": int(sys.argv[1]), "out": sys.argv[2][-int(sys.argv[3]):]}))' "$ec" "$out" "$OUT_LIMIT"
}

case "$CMD" in
  stage-rcm)
    BIN="$(arg binary)"
    [[ -f "$BIN" ]] || die "--binary $BIN not found"
    chmod 0755 "$BIN"
    VER="$("$BIN" version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1)"
    [[ -n "$VER" ]] || die "$BIN version: no version"
    DIR="$FBA_DIR/update/hqbirdrcm/staged"
    U="$(agent_user)"
    install -d -o "$U" -g "$U" -m 0750 "$FBA_DIR/update" "$FBA_DIR/update/hqbirdrcm" "$DIR"
    rm -f "$DIR"/hqbirdrcm-* "$DIR/staged.json"
    install -o "$U" -g "$U" -m 0755 "$BIN" "$DIR/hqbirdrcm-$VER"
    SUM="$(sha256sum "$DIR/hqbirdrcm-$VER" | cut -d' ' -f1)"
    echo "$SUM" >"$DIR/hqbirdrcm-$VER.sha256"
    SIZE="$(stat -c %s "$DIR/hqbirdrcm-$VER")"
    python3 - "$DIR/staged.json" "$VER" "$SUM" "$SIZE" <<'PY'
import datetime, json, sys
path, ver, s, size = sys.argv[1:5]
doc = {"version": ver, "sha256": s, "size": int(size), "platform": "linux-amd64",
       "staged_at_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
       "source": "replication test bed (lab build)"}
json.dump(doc, open(path, "w"), indent=2)
PY
    chown "$U:$U" "$DIR"/*
    log "staged hqbirdrcm $VER in $DIR"
    result "{\"version\":\"$VER\",\"sha256\":\"$SUM\"}"
    ;;

  init)
    CID="$(arg cluster_id)"; [[ -n "$CID" ]] || die "--cluster-id required"
    ADDR="$(arg address)"
    set +e
    if [[ -n "$ADDR" ]]; then
      out="$(as_agent ./fbagent --cluster-init "$CID" --address "$ADDR" 2>&1)"
    else
      out="$(as_agent ./fbagent --cluster-init "$CID" 2>&1)"
    fi
    ec=$?
    set -e
    printf '%s\n' "$out"
    python3 -c '
import json, re, sys
out = sys.argv[2]
def field(name):
    m = re.search(r"^" + name + r":\s*(\S+)", out, re.M)
    return m.group(1) if m else ""
print("TBRESULT " + json.dumps({"rc": int(sys.argv[1]), "rcm_url": field("RCM URL"),
                                "ca_hash": field("CA hash"), "out": out[-6000:]}))' "$ec" "$out"
    ;;

  join)
    URL="$(arg url)"; HASH="$(arg ca_hash)"
    [[ -n "$URL" && -n "$HASH" ]] || die "--url and --ca-hash required"
    U="$(agent_user)"
    TOK="$(mktemp /var/tmp/tb-token.XXXXXX)"
    trap 'rm -f "$TOK"' EXIT
    base64 -d <<<"$(arg token_b64)" >"$TOK" || die "bad --token-b64"
    chown "$U" "$TOK"; chmod 600 "$TOK"
    run_result as_agent ./fbagent --cluster-join "$URL" --ca-hash "$HASH" --token-file "$TOK"
    ;;

  rcm)
    read_secrets
    export RCM_USER="${TB_RCM_USER:-}" RCM_PASSWORD="${TB_RCM_PASSWORD:-}"
    mapfile -t ARGS < <(python3 -c '
import base64, json, sys
for a in json.loads(base64.b64decode(sys.argv[1]).decode()):
    print(a)' "$(arg args_b64)")
    OUT_LIMIT=1000000 QUIET=1
    run_result "$RCM_DIR/hqbirdrcm" "${ARGS[@]}"
    ;;

  release-import)
    F="$(arg file)"; [[ -f "$F" ]] || die "--file $F not found"
    # RCM's user must read the files: copy them where it can.
    D=/var/tmp/tb-releases
    install -d -m 0755 "$D"
    install -m 0644 "$F" "$D/$(basename "$F")"
    EXTRA=()
    SIG="$(arg sig)"
    if [[ -n "$SIG" ]]; then
      [[ -f "$SIG" ]] || die "--sig $SIG not found"
      install -m 0644 "$SIG" "$D/$(basename "$SIG")"
      EXTRA=(-sig "$D/$(basename "$SIG")")
    fi
    run_result runuser -u hqbirdrcm -- "$RCM_DIR/hqbirdrcm" release import -config "$RCM_DIR/rcm.json" \
      -file "$D/$(basename "$F")" -product "$(arg product)" -platform "$(arg platform linux-amd64)" \
      -version "$(arg version)" "${EXTRA[@]}"
    ;;

  scratch-agent)
    N="$(arg name)"; [[ "$N" =~ ^[a-z0-9-]+$ ]] || die "--name: a-z, 0-9 and - only"
    D="/var/tmp/tb-agent-$N"
    U="$(agent_user)"
    rm -rf "$D"
    install -d -o "$U" -g "$U" -m 0750 "$D"
    install -o "$U" -g "$U" -m 0755 "$FBA_DIR/fbagent" "$D/fbagent"
    AID="$(arg agent_id)"
    python3 - "$FBA_DIR/agent_config.json" "$D/agent_config.json" "$AID" <<'PY'
import json, sys
src, dst, aid = sys.argv[1:4]
cfg = json.load(open(src, encoding="utf-8"))
cfg.pop("cluster", None)
# No unit: a join must not restart the host's real agent.
(cfg.get("goafts") or {}).pop("auto_update", None)
if aid:
    cfg.setdefault("goafts", {})["agent_id"] = aid
    cfg.setdefault("agent", {})["id"] = aid
cfg["local_api"] = {"enabled": False}
json.dump(cfg, open(dst, "w", encoding="utf-8"), indent=2)
PY
    chown "$U:$U" "$D/agent_config.json"; chmod 640 "$D/agent_config.json"
    result "{\"dir\":\"$D\"}"
    ;;

  scratch-remove)
    N="$(arg name)"; [[ "$N" =~ ^[a-z0-9-]+$ ]] || die "--name: a-z, 0-9 and - only"
    rm -rf "/var/tmp/tb-agent-$N"
    result "{\"removed\":\"/var/tmp/tb-agent-$N\"}"
    ;;

  facts)
    FBA_DIR="$FBA_DIR" RCM_DIR="$RCM_DIR" NODE_DIRS="$(arg node_dirs)" python3 - <<'PY'
import glob, json, os, pwd, grp, re, stat, subprocess

def sh(*a):
    try:
        return subprocess.run(a, capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"

def owner(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return {"user": pwd.getpwuid(st.st_uid).pw_name, "group": grp.getgrgid(st.st_gid).gr_name,
            "mode": oct(stat.S_IMODE(st.st_mode))}

def unit(name):
    pid = sh("systemctl", "show", "-p", "MainPID", "--value", name)
    user = ""
    if pid and pid != "0":
        user = sh("ps", "-o", "user=", "-p", pid)
    return {"active": sh("systemctl", "is-active", name), "user": user,
            "unit_user": sh("systemctl", "show", "-p", "User", "--value", name)}

def version(binary):
    if not os.path.isfile(binary):
        return ""
    m = re.search(r"\d+\.\d+\.\d+(\.\d+)?", sh(binary, "version"))
    return m.group(0) if m else ""

fba, rcm = os.environ["FBA_DIR"], os.environ["RCM_DIR"]
out = {"hostname": sh("hostname", "-s")}
try:
    cfg = json.load(open(os.path.join(fba, "agent_config.json"), encoding="utf-8"))
except Exception:  # noqa: BLE001
    cfg = {}
out["agent_id"] = (cfg.get("goafts") or {}).get("agent_id", "")
out["cluster_source"] = cfg.get("cluster") or {}
out["agent_dir_owner"] = owner(fba)
out["clusterca"] = sorted(os.path.relpath(p, fba) for p in glob.glob(os.path.join(fba, "clusterca", "*", "*")))
out["rcm"] = {"unit": unit("hqbirdrcm"), "version": version(os.path.join(rcm, "hqbirdrcm")),
              "rcm_json": owner(os.path.join(rcm, "rcm.json")),
              "seed": os.path.isfile(os.path.join(rcm, "cluster-init.json")),
              "data_dir": owner(os.path.join(rcm, "rcm-data"))}
nodes = {}
for d in [x for x in os.environ.get("NODE_DIRS", "").split(",") if x]:
    info = {"version": version(os.path.join(d, "hqclusternode")),
            "node_json": owner(os.path.join(d, "node.json")),
            "managed": os.path.isfile(os.path.join(d, "cluster-managed.json"))}
    try:
        nj = json.load(open(os.path.join(d, "node.json"), encoding="utf-8"))
        info["node_id"] = nj.get("node_id", "")
        info["peers"] = [p.get("node_id") for p in nj.get("peers") or []]
        info["rcm_url"] = (nj.get("rcm") or {}).get("url", "") if isinstance(nj.get("rcm"), dict) else nj.get("rcm_url", "")
    except Exception:  # noqa: BLE001
        pass
    for u in glob.glob("/etc/systemd/system/hqclusternode-*.service"):
        if d + "/" in open(u, encoding="utf-8", errors="replace").read():
            info["unit"] = os.path.basename(u)[:-len(".service")]
            info["unit_state"] = unit(info["unit"])
    nodes[d] = info
out["nodes"] = nodes
out["listen"] = sorted(set(re.findall(r"[\d.*\[\]:]+:(7443|7444|7051|7052)\b", sh("ss", "-ltn"))))
out["hqbirdrcm_user"] = bool(sh("id", "-u", "hqbirdrcm"))
print("TBRESULT " + json.dumps(out))
PY
    ;;

  *) die "usage: 94-rcmcluster.sh stage-rcm|init|join|rcm|release-import|scratch-agent|scratch-remove|facts [--key value ...]" ;;
esac
