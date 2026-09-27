#!/usr/bin/env bash
# Module 90 (Linux): small host actions for tb.py and the tests.
#
#   90-hostctl.sh secure-file PATH
#   90-hostctl.sh node-api    --node-dir DIR --method GET --path /v1/status [--body-b64 B64] [--timeout 60]
#   90-hostctl.sh node-svc    --node-dir DIR --action stop|start|restart|kill
#   90-hostctl.sh fb-svc      --action stop|start|restart|status [--fb-service UNIT]
#   90-hostctl.sh counts      --db FILE [--fb-root /opt/firebird] [--port 3050]
#   90-hostctl.sh limbo       --db FILE [--fb-root /opt/firebird] [--port 3050]
#   90-hostctl.sh files       --glob 'PATTERN'
#   90-hostctl.sh remove-file --path FILE
#   90-hostctl.sh block-peer  --addr ADDR [--port PORT]      (iptables, both ways)
#   90-hostctl.sh unblock-peer --addr ADDR [--port PORT]
#   90-hostctl.sh tail        --path FILE [--lines 50]
#
# Results come as one "TBRESULT <json>" line.
source "$(dirname "$0")/common.sh"

CMD="${1:-}"; shift || true
if [[ "$CMD" == secure-file ]]; then chmod 600 "$1"; chown root:root "$1" 2>/dev/null || true; exit 0; fi
parse_args "$@"

NODE_DIR="$(arg node_dir /opt/hqclusternode)"
FB_ROOT="$(arg fb_root /opt/firebird)"
PORT="$(arg port 3050)"

isql_q() { # DB SQL -> stdout
  printf '%s\n' "$2" | "$(fb_tool "$FB_ROOT" isql)" -q -pag 0 "localhost/$PORT:$1"
}

case "$CMD" in
  node-api)
    args=(api "$(arg method GET)" "$(arg path /v1/status)" -i -timeout-sec "$(arg timeout 60)"
          -config "$NODE_DIR/node.json" -certs "$NODE_DIR/certs")
    tmp=""
    if [[ -n "$(arg body_b64)" ]]; then
      tmp="$(mktemp)"; base64 -d <<<"$(arg body_b64)" >"$tmp"; args+=(-body-file "$tmp")
    fi
    set +e; out="$("$NODE_DIR/hqclusternode" "${args[@]}" 2>&1)"; ec=$?; set -e
    [[ -z "$tmp" ]] || rm -f "$tmp"
    if [[ $ec -ne 0 ]]; then echo "$out" >&2; die "api call failed (exit $ec)"; fi
    python3 -c 'import json,sys; print("TBRESULT " + json.dumps(json.loads(sys.stdin.read())))' <<<"$out"
    ;;

  node-svc)
    case "$(arg action)" in
      stop|start) "$NODE_DIR/hqclusternode" svc "$(arg action)" -config "$NODE_DIR/node.json" ;;
      restart) "$NODE_DIR/hqclusternode" svc stop -config "$NODE_DIR/node.json" || true
               "$NODE_DIR/hqclusternode" svc start -config "$NODE_DIR/node.json" ;;
      # A crash: systemd restarts the node by itself (Restart=on-failure).
      kill) pkill -9 -f "$NODE_DIR/hqclusternode serve" || true ;;
      *) die "--action stop|start|restart|kill" ;;
    esac
    result "{\"node_svc\":\"$(arg action)\"}"
    ;;

  fb-svc)
    UNIT="$(arg fb_service)"; [[ -n "$UNIT" ]] || UNIT="$(detect_fb_unit)"
    [[ -n "$UNIT" ]] || die "cannot find the Firebird unit"
    case "$(arg action)" in
      stop|start|restart) systemctl "$(arg action)" "$UNIT" ;;
      status) ;;
      *) die "--action stop|start|restart|status" ;;
    esac
    result "{\"unit\":\"$UNIT\",\"active\":\"$(systemctl is-active "$UNIT" || true)\"}"
    ;;

  counts)
    # Row count of every user table. Keyed tables (with a primary key or a
    # unique index) must match between master and replica.
    load_secrets
    DB="$(arg db)"
    tables="$(isql_q "$DB" "set heading off;
select trim(r.rdb\$relation_name) || '|' ||
  iif(exists(select 1 from rdb\$indices i where i.rdb\$relation_name = r.rdb\$relation_name
             and i.rdb\$unique_flag = 1 and coalesce(i.rdb\$index_inactive, 0) = 0), 'K', 'N')
from rdb\$relations r
where coalesce(r.rdb\$system_flag, 0) = 0 and r.rdb\$view_blr is null
order by 1;")" || die "isql failed on $DB"
    sql="set heading off; set transaction read only ignore limbo;"
    first=1
    while IFS='|' read -r t k; do
      t="$(echo "$t" | tr -d '[:space:]')"; [[ -n "$t" ]] || continue
      [[ $first -eq 1 ]] && sql+=$'\nselect ' || sql+=$'\nunion all select '
      sql+="'RC|$t|$k|' || count(*) from \"$t\""
      first=0
    done <<<"$tables"
    [[ $first -eq 0 ]] || die "no user tables in $DB"
    out="$(isql_q "$DB" "$sql;")" || die "count query failed on $DB"
    python3 -c '
import json, sys
rows = {}
for line in sys.stdin.read().splitlines():
    line = line.strip()
    if line.startswith("RC|"):
        _, t, k, n = line.split("|")
        rows[t] = {"keyed": k == "K", "rows": int(n)}
print("TBRESULT " + json.dumps(rows))' <<<"$out"
    ;;

  limbo)
    load_secrets
    out="$("$(fb_tool "$FB_ROOT" gfix)" -list "localhost/$PORT:$(arg db)" 2>&1 || true)"
    n="$(grep -ciE 'transaction [0-9]+' <<<"$out" || true)"
    result "{\"limbo\":${n:-0}}"
    ;;

  files)
    # shellcheck disable=SC2086
    python3 -c 'import glob,json,sys,os; fs=sorted(glob.glob(sys.argv[1])); print("TBRESULT "+json.dumps([{"path":f,"size":os.path.getsize(f),"mtime":os.path.getmtime(f)} for f in fs]))' "$(arg glob)"
    ;;

  remove-file)
    P="$(arg path)"; [[ -f "$P" ]] || die "no file $P"
    rm -f "$P"; result "{\"removed\":\"$P\"}"
    ;;

  block-peer|unblock-peer)
    need_cmd iptables
    A="$(arg addr)"; [[ -n "$A" ]] || die "--addr is required"
    op=-I; [[ "$CMD" == unblock-peer ]] && op=-D
    if [[ -n "$(arg port)" ]]; then
      iptables $op INPUT -s "$A" -p tcp --dport "$(arg port)" -j DROP -m comment --comment tb-block 2>/dev/null || true
      iptables $op OUTPUT -d "$A" -p tcp --dport "$(arg port)" -j DROP -m comment --comment tb-block 2>/dev/null || true
    else
      iptables $op INPUT -s "$A" -j DROP -m comment --comment tb-block 2>/dev/null || true
      iptables $op OUTPUT -d "$A" -j DROP -m comment --comment tb-block 2>/dev/null || true
    fi
    result "{\"action\":\"$CMD\",\"addr\":\"$A\"}"
    ;;

  tail) tail -n "$(arg lines 50)" "$(arg path)" ;;

  *) die "usage: 90-hostctl.sh secure-file|node-api|node-svc|fb-svc|counts|limbo|files|remove-file|block-peer|unblock-peer|tail" ;;
esac
