#!/usr/bin/env bash
# Module 50 (Linux): run fb-loadgen load on test databases.
#
#   50-load.sh start  (--dbs FILE1,FILE2 | --dir DIR) [--host localhost] [--port 3050]
#                     [--mode write|read|mixed|spike|oltp-emul]
#                     [--tx off|emul-safe|full] [--limbo false] [--extended true]
#                     [--conns 1:4] [--minutes 0] [--think-ms 50] [--tag load]
#   50-load.sh stop   [--tag TAG|all]
#   50-load.sh status [--tag TAG|all]
#
# One fb-loadgen process per database (mixed: two per database, one
# write-heavy and one read-heavy). --dir takes every *.fdb below DIR.
#
# --tx off        the same transaction kind all the time
# --tx emul-safe  isolation / wait / completion change per transaction
# --tx full       as emul-safe, plus consistency isolation and infinite wait
# --limbo false   (default) no prepare-then-die transactions: CLI fb-loadgen
#                 cannot resolve limbo on EMPLOYEE databases, and limbo
#                 crashed Firebird 5.0.5 on Windows.
# --minutes 0     run until stop.
#
# Note: fb-loadgen takes the password only as --pass, so it is visible in
# the process list of this host.
source "$(dirname "$0")/common.sh"

CMD="${1:-}"; shift || true
parse_args "$@"

LG="$TB_WORK/loadgen/fb-loadgen"
RUN_DIR="$TB_WORK/load"
TAG="$(arg tag load)"
[[ "$TAG" =~ ^[A-Za-z0-9_.-]+$ ]] || die "--tag: letters, digits, _ . - only"

pids_of() { # TAG|all -> pid files
  if [[ "$1" == all ]]; then ls "$RUN_DIR"/*/*.pid 2>/dev/null || true
  else ls "$RUN_DIR/$1"/*.pid 2>/dev/null || true; fi
}

case "$CMD" in
  start)
    load_secrets
    [[ -x "$LG" ]] || die "fb-loadgen is not installed (40-loadgen.sh install)"
    if [[ -n "$(arg dbs)" ]]; then IFS=',' read -r -a DBS <<<"$(arg dbs)"
    else
      DIR="$(arg dir)"; [[ -n "$DIR" ]] || die "--dbs or --dir is required"
      mapfile -t DBS < <(find "$DIR" -type f \( -name '*.fdb' -o -name '*.FDB' \) | sort)
    fi
    [[ ${#DBS[@]} -gt 0 ]] || die "no databases to load"
    MODE="$(arg mode write)"
    case "$MODE" in
      write) PROFILES=(write-heavy) ;;
      read) PROFILES=(read-heavy) ;;
      mixed) PROFILES=(write-heavy read-heavy) ;;
      spike) PROFILES=(spike) ;;
      oltp-emul) PROFILES=(oltp-emul) ;;
      *) die "--mode: write|read|mixed|spike|oltp-emul" ;;
    esac
    TX="$(arg tx off)"
    case "$TX" in off|emul-safe|full) ;; *) die "--tx: off|emul-safe|full" ;; esac
    CONNS="$(arg conns 1:4)"; CMIN="${CONNS%%:*}"; CMAX="${CONNS##*:}"
    MIN="$(arg minutes 0)"; MAIN=$(( MIN > 0 ? MIN * 60 : 604800 ))
    EXTRA=()
    [[ "$(arg limbo false)" == "true" ]] || EXTRA+=(--no-limbo)
    EXTRA+=("--extended-load=$(arg extended true)")
    OUT="$RUN_DIR/$TAG"; mkdir -p "$OUT"
    if [[ -n "$(pids_of "$TAG")" ]]; then die "tag '$TAG' is already running; stop it first"; fi
    n=0
    for db in "${DBS[@]}"; do
      [[ -f "$db" ]] || die "no database $db"
      base="$(basename "$(dirname "$db")")-$(basename "$db" | tr '.' '_')"
      for prof in "${PROFILES[@]}"; do
        name="$base-$prof"
        setsid nohup "$LG" --profile "$prof" --dsn "$(arg host localhost)/$(arg port 3050):$db" \
          --user "$ISC_USER" --pass "$ISC_PASSWORD" --tx-variants "$TX" "${EXTRA[@]}" \
          --conn-min "$CMIN" --conn-max "$CMAX" --think-ms "$(arg think_ms 50)" \
          --warmup 5 --main "$MAIN" --cooldown 5 --report-every 30 \
          --csv "$OUT/$name.txt" >"$OUT/$name.log" 2>&1 </dev/null &
        echo $! >"$OUT/$name.pid"
        log "started $name pid $!"
        n=$((n + 1))
      done
    done
    sleep 3
    dead=0
    for p in "$OUT"/*.pid; do kill -0 "$(cat "$p")" 2>/dev/null || { warn "$(basename "$p" .pid) exited early"; tail -n 15 "${p%.pid}.log" >&2; dead=$((dead + 1)); }; done
    result "{\"tag\":\"$TAG\",\"processes\":$n,\"exited_early\":$dead}"
    (( dead == 0 )) || exit 1
    ;;

  stop)
    stopped=0
    for p in $(pids_of "$(arg tag all)"); do
      pid="$(cat "$p")"
      if kill -0 "$pid" 2>/dev/null; then
        kill -INT "$pid" 2>/dev/null || true
        for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
        kill -9 "$pid" 2>/dev/null || true
        stopped=$((stopped + 1))
      fi
      rm -f "$p"
    done
    log "stopped $stopped process(es)"
    result "{\"stopped\":$stopped}"
    ;;

  status)
    running=0; total=0
    for p in $(pids_of "$(arg tag all)"); do
      total=$((total + 1))
      if kill -0 "$(cat "$p")" 2>/dev/null; then running=$((running + 1)); st=running; else st=exited; fi
      last="$(grep -E 'Status:|Total:' "${p%.pid}.log" 2>/dev/null | tail -n1 | tr -d '"\r' || true)"
      echo "  $(basename "$p" .pid): $st  $last"
    done
    result "{\"running\":$running,\"total\":$total}"
    ;;

  *) die "usage: 50-load.sh start|stop|status [--key value ...]" ;;
esac
