#!/usr/bin/env bash
# Module 40 (Linux): put fb-loadgen on this host and check it with a 5 s run.
#
#   40-loadgen.sh install [--binary <stage>/bin/fb-loadgen]
#   40-loadgen.sh smoke   --db FILE [--host localhost] [--port 3050]
#                         [--profile write-heavy] [--seconds 5]
#
# tb.py builds the binary on the operator machine (from git or from a local
# checkout) and copies it to the stage folder; install moves it to
# <work>/loadgen/fb-loadgen.
#
# smoke passes when fb-loadgen exits 0, reaches its final report, and did at
# least one operation (the "Total: N" of the summary line is > 0).
source "$(dirname "$0")/common.sh"

CMD="${1:-}"; shift || true
parse_args "$@"

LG_DIR="$TB_WORK/loadgen"
LG="$LG_DIR/fb-loadgen"

case "$CMD" in
  install)
    BIN="$(arg binary "$TB_WORK/stage/bin/fb-loadgen")"
    [[ -f "$BIN" ]] || die "no binary $BIN"
    mkdir -p "$LG_DIR"
    install -m 0755 "$BIN" "$LG"
    "$LG" --help >/dev/null 2>&1 || die "fb-loadgen does not run on this host"
    log "installed $LG"
    result "{\"binary\":\"$LG\"}"
    ;;

  smoke)
    load_secrets
    [[ -x "$LG" ]] || die "fb-loadgen is not installed (run install)"
    DB="$(arg db)"; [[ -n "$DB" ]] || die "--db is required"
    SECS="$(arg seconds 5)"
    OUT="$LG_DIR/smoke"; mkdir -p "$OUT"; rm -f "$OUT"/smoke*
    log "smoke: $(arg profile write-heavy) for ${SECS}s on $DB"
    set +e
    timeout 120 "$LG" --profile "$(arg profile write-heavy)" \
      --dsn "$(arg host localhost)/$(arg port 3050):$DB" --user "$ISC_USER" --pass "$ISC_PASSWORD" \
      --warmup 0 --main "$SECS" --cooldown 0 --conn-min 1 --conn-max 2 --think-ms 0 \
      --extended-load=false --csv "$OUT/smoke.txt" >"$OUT/smoke.log" 2>&1
    ec=$?
    set -e
    summary="$(grep -E 'Total: [0-9]+' "$OUT/smoke.log" | tail -n1 || true)"
    total="$(sed -nE 's/.*Total: ([0-9]+).*/\1/p' <<<"$summary")"
    log "exit=$ec  $summary"
    if [[ $ec -ne 0 || -z "$total" || "$total" -eq 0 ]] || ! grep -q 'FINAL LOAD TEST REPORT' "$OUT/smoke.log"; then
      tail -n 30 "$OUT/smoke.log" >&2
      result "{\"ok\":false,\"exit\":$ec}"
      exit 1
    fi
    result "{\"ok\":true,\"total\":$total}"
    ;;

  *) die "usage: 40-loadgen.sh install|smoke [--key value ...]" ;;
esac
