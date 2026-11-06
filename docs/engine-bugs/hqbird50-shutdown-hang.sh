#!/bin/bash
# hqbird50-shutdown-hang.sh FB_ROOT WORK_DIR ITERATIONS
#
# Repeats the switchover steps of hqbird50-shutdown-hang.en.md with Firebird
# tools only (isql, gfix, nbackup, gstat, fbtracemgr), cp and timeout; no
# cluster software. Two private SuperServer instances play the two hosts of a
# cluster service: copies of FB_ROOT under WORK_DIR, each with its own port
# (localhost only), lock directory, security database and replication.conf.
# The server of FB_ROOT itself is not touched.
#
# Each host has shop.fdb (the master with a journal on one host, its replica
# on the other) and REPLICAS idle replica databases. One iteration is one
# switchover from host X to host Y, then X back as the replica of Y:
#
#   quiesce X   gfix -shut full -force 0                          <- hang 1
#   apply       the last segments of X are applied on Y (SETTLE s)
#   promote Y   gfix -replica none (fails: the applier is attached),
#               gfix -shut single -force 0, gfix -replica none,
#               gfix -online normal; new GUID: gfix -shut single -force 0,
#               nbackup -L, nbackup -F, delta removed, gfix -online normal
#   restart Y   shop.fdb of Y is the master now; the load moves to Y
#   rejoin X    REJOIN_WAIT s after the restart: nbackup -L on Y  <- hang 2
#               copy, nbackup -N on Y; on X: nbackup -F -SEQUENCE,
#               gfix -replica read_only, restart X with the copy as replica
#   QUIESCE_GAP s after nbackup -N the next iteration quiesces Y.
#
# As the HQbird agent does, every POLL s each server gets service requests
# (fbsvcmgr action_lwmonitoring, info_svr_db_info2, fbtracemgr -list), and
# TRACE_DELAY s after each start two user trace sessions.
#
# Every tool, server stop and server start is bounded by TOOL_TIMEOUT. On the
# first timeout the script saves into WORK_DIR/hang-<time>/: gdb stacks of
# all server threads, the owners of the mutexes they wait for,
# /proc/<pid>/maps, gstat -h and the end of firebird.log. Then it prints the
# iteration number and stops. It removes its databases and stops its server
# and load processes at the end, also on Ctrl+C (KEEP=1 leaves the files).
#
# The password: ISC_PASSWORD (ISC_USER, default SYSDBA) must be valid in
# FB_ROOT/security5.fdb, which is copied. Run as a user that can read FB_ROOT
# and attach gdb to its own processes (root in a test container).
#
# Example:
#   ISC_PASSWORD=... ./hqbird50-shutdown-hang.sh /opt/firebird /root/fbhang 100

TOOL_TIMEOUT=${TOOL_TIMEOUT:-120} # s for one tool, server stop or start
PORT1=${PORT1:-3071}              # ports of the two private servers
PORT2=${PORT2:-3072}
REPLICAS=${REPLICAS:-2}           # idle replica databases per host
LOAD_CONN=${LOAD_CONN:-3}         # isql load connections (0: no load)
LOAD_THINK=${LOAD_THINK:-0.1}     # s between two transactions of one connection
LOADGEN=${LOADGEN:-}              # fb-loadgen binary: write-heavy load instead of isql (needs BASE_DB with the EMPLOYEE schema)
BASE_DB=${BASE_DB:-}              # start shop.fdb from a copy of this file instead of an empty database
SHOP_MB=${SHOP_MB:-200}           # fill shop.fdb with about this many MB before the first copy
COPY_MIN=${COPY_MIN:-8}           # s from nbackup -L to nbackup -N at least (a copy to another host takes time)
TRACE=${TRACE:-1}                 # 1: two user trace sessions per server, as the HQbird agent has
TRACE_DELAY=${TRACE_DELAY:-60}    # s from a server start to its trace sessions (the agent waits about 60 s)
POLL=${POLL:-3}                   # s between two service polls of a server, as the agent does (0: no polls)
NEWGUID=${NEWGUID:-1}             # 1: the promote gives shop.fdb a new GUID (nbackup -L/-F)
SETTLE=${SETTLE:-12}              # s from the quiesce to the promote
REJOIN_WAIT=${REJOIN_WAIT:-60}    # s from the restart of the new master to its nbackup -L
QUIESCE_GAP=${QUIESCE_GAP:-18}    # s from nbackup -N to the next quiesce
KEEP=${KEEP:-0}                   # 1: keep WORK_DIR/run after the end

set -u
[ $# -eq 3 ] || { echo "usage: $0 FB_ROOT WORK_DIR ITERATIONS" >&2; exit 64; }
FBROOT=$(cd "$1" && pwd) || exit 64
mkdir -p "$2" && WORK=$(cd "$2" && pwd) || exit 64
ITER=$3
[ -n "${ISC_PASSWORD:-}" ] || { echo "set ISC_PASSWORD to the SYSDBA password of $FBROOT/security5.fdb" >&2; exit 64; }
export ISC_USER=${ISC_USER:-SYSDBA} ISC_PASSWORD
[ -n "$LOADGEN" ] && LOAD_CONN=0

RUN=$WORK/run
H1=$RUN/h1
H2=$RUN/h2
LOG=$WORK/repro.log
IT=0
DONE=0
HUNG=
LAST_OUT=

log() { printf '%s it=%s %s\n' "$(date -u +%H:%M:%S)" "$IT" "$*" | tee -a "$LOG"; }
die() { log "FAILED: $*"; exit 3; }
port() { cat "$1/port"; }
spec() { echo "localhost/$(port "$1"):$2"; } # spec <host> <database>
fbenv() { local h=$1; shift; env FIREBIRD="$h/fb" FIREBIRD_LOCK="$h/lock" FIREBIRD_TMP="$h/tmp" "$@"; }
alive() { [ -n "$1" ] && [ -e "/proc/$1" ] && ! grep -q '^State:[[:space:]]*Z' "/proc/$1/status" 2>/dev/null; }
sleep_until() { local t=$(($1 - $(date +%s))); [ $t -gt 0 ] && sleep "$t"; return 0; }

# T <host> <tool> <args>: one Firebird tool of the host, bounded by
# TOOL_TIMEOUT. Its output is in LAST_OUT.
T() {
	local h=$1 tool=$2 t0 rc
	shift 2
	t0=$(date +%s%N)
	LAST_OUT=$(fbenv "$h" timeout -k 5 "$TOOL_TIMEOUT" "$h/fb/bin/$tool" "$@" 2>&1 < /dev/null)
	rc=$?
	log "  ${h##*/} $tool $* rc=$rc $((($(date +%s%N) - t0) / 1000000)) ms${LAST_OUT:+ | $(printf '%s' "$LAST_OUT" | tr -s '\n\t' '  ' | cut -c1-200)}"
	case $rc in 124 | 137) hang "$h" "$tool $*" ;; esac
	return $rc
}

# hang <host> <what>: save the state of the host's server, then stop.
hang() {
	local h=$1 what=$2 d pid m db
	d=$WORK/hang-$(date -u +%Y%m%d-%H%M%S)
	mkdir -p "$d"
	pid=$(cat "$h/fb.pid" 2>/dev/null)
	log "HANG in iteration $IT: '$what' on ${h##*/} did not end in $TOOL_TIMEOUT s; firebird pid $pid; saving $d"
	{
		echo "iteration $IT, host ${h##*/}, step: $what"
		date -u
		"$h/fb/bin/gstat" -z 2>&1 | grep -i version
		md5sum "$h/fb/bin/firebird" "$h/fb/plugins/libEngine13.so"
		uname -sr
		grep PRETTY_NAME /etc/os-release
		echo "server threads: $(ls "/proc/$pid/task" 2>/dev/null | wc -l)"
		echo "--- attach probes, 10 s each"
		for db in "$h/shop.fdb" "$h/r1.fdb" security.db; do
			echo "$db:"
			echo 'select 1 from rdb$database;' | fbenv "$h" timeout -k 2 10 "$h/fb/bin/isql" -q "$(spec "$h" "$db")" 2>&1 | head -3
			echo "rc=${PIPESTATUS[1]}"
		done
		echo "--- tool processes"
		ps -eo pid,etimes,args | grep -E '[g]fix|[n]backup|[i]sql|[g]stat' | grep -F "$RUN" | cut -c1-200
	} > "$d/summary.txt" 2>&1
	for db in "$h"/*.fdb; do
		echo "== $db"
		fbenv "$h" timeout -k 2 10 "$h/fb/bin/gstat" -h "$db" 2>&1
	done > "$d/gstat-h.txt"
	tail -n 300 "$h/fb/firebird.log" > "$d/firebird.log.tail" 2>&1
	tail -n 300 "$h/fb/replication.log" > "$d/replication.log.tail" 2>&1
	tail -n 50 "$h/poll.err" > "$d/poll.err.tail" 2>&1
	cp "/proc/$pid/maps" "$d/maps.txt" 2>/dev/null
	if command -v gdb > /dev/null; then
		DEBUGINFOD_URLS= timeout -k 5 300 gdb -p "$pid" -batch -ex 'set pagination off' -ex 'thread apply all bt' > "$d/stacks.txt" 2>&1
		for m in $(grep -o 'pthread_mutex_lock (mutex=0x[0-9a-f]*' "$d/stacks.txt" | grep -o '0x[0-9a-f]*$' | sort -u); do
			echo "printf \"mutex $m owner LWP %d\\n\", ((int*)$m)[2]"
		done > "$d/owners.gdb"
		[ -s "$d/owners.gdb" ] && DEBUGINFOD_URLS= timeout -k 5 120 gdb -p "$pid" -batch -x "$d/owners.gdb" 2>/dev/null | grep '^mutex' > "$d/mutex-owners.txt"
		log "  $(grep -c '^Thread ' "$d/stacks.txt") thread stacks saved"
	else
		echo "gdb not found: no stacks" >> "$d/summary.txt"
		log "  gdb not found: no stacks"
	fi
	cp "$LOG" "$d/repro.log"
	HUNG=$d
	echo "HANG in iteration $IT ($what on ${h##*/}); diagnostics: $d"
	exit 2
}

fb_start() { # fb_start <host>
	local h=$1 i
	fbenv "$h" "$h/fb/bin/firebird" >> "$h/server.out" 2>&1 < /dev/null &
	echo $! > "$h/fb.pid"
	for ((i = 0; i < TOOL_TIMEOUT; i++)); do
		(exec 3<> "/dev/tcp/127.0.0.1/$(port "$h")") 2>/dev/null && break
		sleep 1
	done
	[ "$i" -lt "$TOOL_TIMEOUT" ] || hang "$h" "server start"
	log "  ${h##*/} server started, pid $(cat "$h/fb.pid")"
	trace_start "$h"
}

fb_stop() { # fb_stop <host>: SIGTERM, as systemctl stop does
	local h=$1 pid i
	pid=$(cat "$h/fb.pid" 2>/dev/null) || return 0
	kill -TERM "$pid" 2>/dev/null
	for ((i = 0; i < TOOL_TIMEOUT; i++)); do
		alive "$pid" || break
		sleep 1
	done
	alive "$pid" && hang "$h" "server stop (SIGTERM)"
	wait "$pid" 2>/dev/null
	rm -f "$h/fb.pid"
	log "  ${h##*/} server stopped in $i s"
}

trace_start() { # trace_start <host>: the two trace sessions of the HQbird agent, TRACE_DELAY s later
	local h=$1
	[ "$TRACE" = 1 ] || return 0
	{
		local n pid
		pid=$(cat "$h/fb.pid")
		sleep "$TRACE_DELAY"
		[ -e "$RUN/on" ] && [ "$(cat "$h/fb.pid" 2>/dev/null)" = "$pid" ] || exit 0
		for n in 1 2; do
			fbenv "$h" "$h/fb/bin/fbtracemgr" -se "$(spec "$h" service_mgr)" -start -name "repro-$n" \
				-config "$RUN/trace$n.conf" > /dev/null 2>> "$h/trace.err" < /dev/null &
		done
		wait
	} &
	echo $! >> "$RUN/pids"
}

# poller <host>: the service requests the HQbird agent sends every few
# seconds. Failures and timeouts only go to <host>/poll.err.
poller() {
	local h=$1 s
	s=$(spec "$h" service_mgr)
	while [ -e "$RUN/on" ]; do
		{
			fbenv "$h" timeout -k 2 30 "$h/fb/bin/fbsvcmgr" "$s" action_lwmonitoring lwm_query 4
			fbenv "$h" timeout -k 2 30 "$h/fb/bin/fbsvcmgr" "$s" info_svr_db_info2
			fbenv "$h" timeout -k 2 30 "$h/fb/bin/fbtracemgr" -se "$s" -list
		} > /dev/null 2>> "$h/poll.err" < /dev/null
		sleep "$POLL"
	done
}

# write_repl <host>: replication.conf from the role of the host's shop.fdb.
write_repl() {
	local h=$1 i
	{
		printf 'database\n{\n}\n'
		for ((i = 1; i <= REPLICAS; i++)); do
			printf 'database = %s\n{\n    journal_source_directory = %s\n    verbose_logging = true\n    skip_upddel_without_pk = true\n}\n' \
				"$h/r$i.fdb" "$h/r$i.fdb.Incoming"
		done
		case $(cat "$h/shop.role") in
		master)
			printf 'database = %s\n{\n    journal_directory = %s\n    journal_archive_directory = %s\n' \
				"$h/shop.fdb" "$h/shop.fdb.ReplLog" "$h/shop.fdb.LogArch"
			printf '    journal_archive_timeout = 10\n    journal_segment_count = 64\n    log_errors = true\n    disable_on_error = true\n}\n'
			;;
		replica)
			printf 'database = %s\n{\n    journal_source_directory = %s\n    source_guid = "%s"\n' \
				"$h/shop.fdb" "$h/shop.fdb.Incoming" "$(cat "$h/shop.source_guid")"
			printf '    verbose_logging = true\n    skip_upddel_without_pk = true\n}\n'
			;;
		esac
	} > "$h/fb/replication.conf"
}

clean_journal() { # clean_journal <host>: empty journal, archive and incoming directories of shop.fdb
	local d
	for d in ReplLog LogArch Incoming; do
		rm -rf "$1/shop.fdb.$d"
		mkdir -p "$1/shop.fdb.$d"
	done
}

# The shipper copies archived segments of the master's shop.fdb into the
# replica's incoming directory, as the cluster node does. $RUN/ship holds
# "<archive dir> <incoming dir> <stamp dir>"; empty: paused.
shipper() {
	local src dst stamp f b now
	while [ -e "$RUN/on" ]; do
		if read -r src dst stamp < "$RUN/ship" 2>/dev/null; then
			now=$(date +%s)
			for f in "$src"/*; do
				[ -f "$f" ] || continue
				b=${f##*/}
				[ -e "$stamp/$b" ] && continue
				[ $((now - $(stat -c %Y "$f"))) -ge 2 ] || continue
				cp "$f" "$dst.stage" && mv "$dst.stage" "$dst/$b" && : > "$stamp/$b"
			done
		fi
		sleep 1
	done
}

ship_to() { # ship_to <master host> <replica host>
	local stamp
	stamp=$RUN/stamps/$(date +%s%N)
	mkdir -p "$stamp"
	echo "$1/shop.fdb.LogArch $2/shop.fdb.Incoming $stamp" > "$RUN/ship"
}

wait_applied() { # wait_applied <master host> <replica host>
	local i f left src dst stamp
	read -r src dst stamp < "$RUN/ship"
	for ((i = 0; i < 120; i++)); do
		left=0
		for f in "$1/shop.fdb.LogArch"/*; do
			[ -f "$f" ] && [ ! -e "$stamp/${f##*/}" ] && left=$((left + 1))
		done
		for f in "$2/shop.fdb.Incoming"/*; do
			[ -f "$f" ] && case ${f##*/} in '{'*) ;; *) left=$((left + 1)) ;; esac
		done
		[ $left -eq 0 ] && { log "  all segments applied on ${2##*/} ($i s)"; return 0; }
		sleep 1
	done
	log "  WARNING: $left segment(s) not applied on ${2##*/} in 120 s"
}

load_worker() { # load_worker <n>: small write transactions on the current master
	local n=$1 dsn
	while [ -e "$RUN/on" ]; do
		dsn=$(cat "$RUN/load.dsn")
		while [ -e "$RUN/on" ]; do
			echo "insert into LOADT (ID, W, TXT) values (gen_uuid(), $n, 'load row of worker $n'); update LOADC set N = N + 1 where W = $n; commit;"
			sleep "$LOAD_THINK"
		done | fbenv "$H1" "$H1/fb/bin/isql" -q -b "$dsn" > /dev/null 2>&1
		sleep 1
	done
}

load_start() { # load_start <host>
	spec "$1" "$1/shop.fdb" > "$RUN/load.dsn"
	[ -n "$LOADGEN" ] || return 0
	# fb-loadgen takes the password only as an argument.
	"$LOADGEN" --profile write-heavy --dsn "$(spec "$1" "$1/shop.fdb")" --user "$ISC_USER" --pass "$ISC_PASSWORD" \
		--tx-variants off --no-limbo --extended-load=false --conn-min 2 --conn-max 4 --think-ms 50 \
		--warmup 2 --main 604800 --cooldown 2 --report-every 10 --csv "$RUN/loadgen.csv" >> "$RUN/loadgen.out" 2>&1 < /dev/null &
	echo $! > "$RUN/loadgen.pid"
}

load_stop() {
	[ -f "$RUN/loadgen.pid" ] && kill "$(cat "$RUN/loadgen.pid")" 2>/dev/null
	rm -f "$RUN/loadgen.pid"
	return 0
}

load_count() { # load_count <host>: transactions of the isql load so far, in the log
	[ "$LOAD_CONN" -gt 0 ] || return 0
	T "$1" isql -q -i "$RUN/count.sql" "$(spec "$1" "$1/shop.fdb")" || die "load count"
	local n now
	n=$(printf '%s\n' "$LAST_OUT" | awk '$1 ~ /^[0-9]+$/ {print $1}' | tail -1)
	now=$(date +%s)
	[ -n "${LOAD_N:-}" ] && log "  load: $((n - LOAD_N)) transactions in $((now - LOAD_T)) s"
	LOAD_N=$n LOAD_T=$now
}

guid_of() { # guid_of <host> <database>: sets GUID from gstat -h
	T "$1" gstat -h "$2" || die "gstat -h $2"
	GUID=$(printf '%s\n' "$LAST_OUT" | awk '/Database GUID/ {print $3}')
	[ -n "$GUID" ] || die "no GUID in gstat -h $2"
}

promote() { # promote <host>: replica to master, as the cluster node does
	local h=$1 s e try
	s=$(spec "$h" "$h/shop.fdb")
	if ! T "$h" gfix -replica none "$s"; then
		case $LAST_OUT in *"in use"*) ;; *) die "gfix -replica none" ;; esac
		for ((try = 1; try <= 3; try++)); do
			T "$h" gfix -shut single -force 0 "$s" || die "gfix -shut single"
			T "$h" gfix -replica none "$s"
			e=$?
			T "$h" gfix -online normal "$s" || die "gfix -online normal"
			[ $e -eq 0 ] && break
			case $LAST_OUT in *"in use"*) sleep 2 ;; *) die "gfix -replica none" ;; esac
		done
		[ $e -eq 0 ] || die "gfix -replica none: still in use"
	fi
	[ "$NEWGUID" = 1 ] || return 0
	T "$h" gfix -shut single -force 0 "$s" || die "gfix -shut single (new GUID)"
	T "$h" nbackup -L "$s" || die "nbackup -L (new GUID)"
	T "$h" nbackup -F "$s" || die "nbackup -F (new GUID)"
	rm -f "$h/shop.fdb.delta"
	T "$h" gfix -online normal "$s" || die "gfix -online normal (new GUID)"
}

# rejoin <master host> <replica host>: the replica's shop.fdb from a copy of
# the master's. Sets T_N, the end of nbackup -N.
rejoin() {
	local m=$1 r=$2 s t0
	s=$(spec "$m" "$m/shop.fdb")
	T "$m" nbackup -L "$s" || die "nbackup -L"
	t0=$(date +%s)
	cp "$m/shop.fdb" "$r/shop.new.fdb" || die "copy"
	log "  copy of $(($(stat -c %s "$r/shop.new.fdb") / 1048576)) MB in $(($(date +%s) - t0)) s"
	sleep_until $((t0 + COPY_MIN))
	T "$m" nbackup -N "$s" || die "nbackup -N"
	T_N=$(date +%s)
	T "$r" nbackup -F "$(spec "$r" "$r/shop.new.fdb")" -SEQUENCE || die "nbackup -F -SEQUENCE"
	T "$r" gfix -replica read_only "$(spec "$r" "$r/shop.new.fdb")" || die "gfix -replica read_only"
	guid_of "$m" "$m/shop.fdb"
	fb_stop "$r"
	mv -f "$r/shop.new.fdb" "$r/shop.fdb"
	rm -f "$r/shop.fdb.delta"
	clean_journal "$r"
	echo replica > "$r/shop.role"
	echo "$GUID" > "$r/shop.source_guid"
	write_repl "$r"
	ship_to "$m" "$r"
	fb_start "$r"
}

cleanup() {
	local h pid i
	trap - INT TERM
	rm -f "$RUN/on"
	load_stop
	[ -f "$RUN/pids" ] && kill $(cat "$RUN/pids") 2>/dev/null
	for h in "$H1" "$H2"; do
		pid=$(cat "$h/fb.pid" 2>/dev/null) || continue
		kill -TERM "$pid" 2>/dev/null
		for ((i = 0; i < 20; i++)); do alive "$pid" || break; sleep 1; done
		alive "$pid" && kill -KILL "$pid" 2>/dev/null
	done
	kill $(jobs -p) 2>/dev/null
	sleep 2
	if [ "$KEEP" = 1 ]; then
		log "run directory kept: $RUN"
	elif [ -f "$RUN/.fbhang-run" ]; then
		rm -rf "$RUN"
	fi
	if [ -n "$HUNG" ]; then
		log "END: hang in iteration $IT of $ITER; diagnostics: $HUNG"
	else
		log "END: $DONE of $ITER iteration(s) done, no hang"
	fi
}

# --- setup
if [ -e "$RUN" ]; then
	[ -f "$RUN/.fbhang-run" ] || { echo "$RUN exists and is not a run directory of this script" >&2; exit 64; }
	for f in "$RUN"/h?/fb.pid; do
		[ -f "$f" ] && alive "$(cat "$f")" && { echo "a server of an earlier run still runs: $f" >&2; exit 64; }
	done
	rm -rf "$RUN"
fi
for p in "$PORT1" "$PORT2"; do
	(exec 3<> "/dev/tcp/127.0.0.1/$p") 2>/dev/null && { echo "port $p is in use: set PORT1/PORT2" >&2; exit 64; }
done
mkdir -p "$RUN/stamps" && : > "$RUN/.fbhang-run" && : > "$RUN/on" && : > "$RUN/ship" && : > "$RUN/pids"
trap cleanup EXIT
trap 'log "interrupted"; exit 130' INT TERM
command -v gdb > /dev/null || log "WARNING: gdb not found: a hang will be reported without stacks"

log "start: $("$FBROOT/bin/gstat" -z 2>&1 | grep -i version), $ITER iteration(s), work $WORK"
log "settings: TOOL_TIMEOUT=$TOOL_TIMEOUT REPLICAS=$REPLICAS LOAD_CONN=$LOAD_CONN LOAD_THINK=$LOAD_THINK" \
	"LOADGEN=${LOADGEN:+on} BASE_DB=${BASE_DB:+on} TRACE=$TRACE TRACE_DELAY=$TRACE_DELAY POLL=$POLL NEWGUID=$NEWGUID SETTLE=$SETTLE" \
	"REJOIN_WAIT=$REJOIN_WAIT QUIESCE_GAP=$QUIESCE_GAP SHOP_MB=$SHOP_MB COPY_MIN=$COPY_MIN"

printf 'database\n{\n\tenabled = true\n\ttime_threshold = 1\n\tlog_connections = true\n\tlog_statement_finish = true\n\tlog_errors = true\n\tprint_plan = true\n\tprint_perf = true\n\tmax_sql_length = 64000\n}\n' > "$RUN/trace1.conf"
printf 'database\n{\n\tenabled = true\n\ttime_threshold = 0\n\tinclude_table_modify = %%\n\tlog_statement_finish = true\n\tlog_errors = true\n\tprint_perf = true\n}\n' > "$RUN/trace2.conf"
printf 'set heading off;\nselect sum(N) from LOADC;\n' > "$RUN/count.sql"

for h in "$H1" "$H2"; do
	mkdir -p "$h/lock" "$h/tmp"
	# Owned by this user, without the sticky bit of the root directory:
	# with fs.protected_regular even root cannot write a file of another
	# user in a sticky directory.
	cp -RP --preserve=mode,timestamps "$FBROOT/." "$h/fb/" || die "copy of $FBROOT"
	chmod -t "$h/fb"
	rm -f "$h/fb/firebird.log" "$h/fb/replication.log"
	[ "$h" = "$H1" ] && echo "$PORT1" > "$h/port" || echo "$PORT2" > "$h/port"
	sed -i -E '/^[[:space:]]*(ServerMode|ParallelWorkers|MaxParallelWorkers|RemoteServicePort|RemoteAuxPort|RemoteBindAddress|SecurityDatabase)[[:space:]]*=/d' "$h/fb/firebird.conf"
	printf '%s\n' "ServerMode = Super" "ParallelWorkers = 1" "MaxParallelWorkers = 64" "RemoteServicePort = $(port "$h")" \
		"RemoteAuxPort = 0" "RemoteBindAddress = 127.0.0.1" "SecurityDatabase = $h/fb/security5.fdb" >> "$h/fb/firebird.conf"
	sed -i -E "s#^([[:space:]]*security\.db[[:space:]]*=).*#\1 $h/fb/security5.fdb#" "$h/fb/databases.conf"
	echo none > "$h/shop.role"
	clean_journal "$h"
	for ((i = 1; i <= REPLICAS; i++)); do mkdir -p "$h/r$i.fdb.Incoming"; done
	write_repl "$h"
	fb_start "$h"
	for ((i = 1; i <= REPLICAS; i++)); do
		printf "create database '%s' page_size 8192;\n" "$(spec "$h" "$h/r$i.fdb")" > "$RUN/create.sql"
		T "$h" isql -q -i "$RUN/create.sql" || die "create r$i.fdb"
		T "$h" gfix -replica read_only "$(spec "$h" "$h/r$i.fdb")" || die "replica r$i.fdb"
	done
done

if [ -n "$BASE_DB" ]; then
	cp "$BASE_DB" "$H1/shop.fdb" || die "copy of $BASE_DB"
else
	printf "create database '%s' page_size 8192;\n" "$(spec "$H1" "$H1/shop.fdb")" > "$RUN/create.sql"
	T "$H1" isql -q -i "$RUN/create.sql" || die "create shop.fdb"
fi
{
	echo "create table LOADT (ID char(16) character set octets not null primary key, W integer, TXT varchar(60), TS timestamp default localtimestamp);"
	echo "create table LOADC (W integer not null primary key, N bigint);"
	echo "commit;"
	for ((i = 1; i <= 8; i++)); do echo "insert into LOADC values ($i, 0);"; done
	echo "commit;"
	# About 900 rows of 1 KB per MB; filled before the publication, so not in the journal.
	echo "create table PAD (ID integer not null primary key, TXT varchar(1000));"
	echo "commit;"
	echo "set term ^;"
	echo "execute block as declare i integer = 0; begin"
	echo "  while (i < $((SHOP_MB * 900))) do begin"
	echo "    insert into PAD values (:i, rpad(uuid_to_char(gen_uuid()), 1000, uuid_to_char(gen_uuid())));"
	echo "    i = i + 1;"
	echo "  end"
	echo "end^"
	echo "set term ;^"
	echo "commit;"
	echo "alter database enable publication;"
	echo "alter database include all to publication;"
	echo "commit;"
} > "$RUN/schema.sql"
# The fill with forced writes takes minutes on a slow disk; the lab master has them on.
T "$H1" gfix -write async "$(spec "$H1" "$H1/shop.fdb")" || die "gfix -write async"
T "$H1" isql -q -b -i "$RUN/schema.sql" "$(spec "$H1" "$H1/shop.fdb")" || die "schema of shop.fdb"
T "$H1" gfix -write sync "$(spec "$H1" "$H1/shop.fdb")" || die "gfix -write sync"
echo master > "$H1/shop.role"
write_repl "$H1"
fb_stop "$H1"
fb_start "$H1"

shipper &
if [ "$POLL" != 0 ]; then
	poller "$H1" &
	poller "$H2" &
fi
load_start "$H1"
for ((i = 1; i <= LOAD_CONN; i++)); do load_worker "$i" & done
sleep 5
log "first copy of shop.fdb to h2"
rejoin "$H1" "$H2"
load_count "$H1"
sleep_until $((T_N + QUIESCE_GAP))

# --- the switchovers
A=$H1 # master
B=$H2 # replica
for ((IT = 1; IT <= ITER; IT++)); do
	log "== switchover ${A##*/} -> ${B##*/}"
	load_count "$A"
	T "$A" gfix -shut full -force 0 "$(spec "$A" "$A/shop.fdb")" || die "quiesce"
	sleep "$SETTLE"
	wait_applied "$A" "$B"
	: > "$RUN/ship"
	load_stop
	promote "$B"
	echo master > "$B/shop.role"
	clean_journal "$B"
	write_repl "$B"
	fb_stop "$B"
	fb_start "$B"
	T_START=$(date +%s)
	load_start "$B"
	LOAD_N=
	sleep_until $((T_START + REJOIN_WAIT))
	load_count "$B"
	rejoin "$B" "$A"
	DONE=$IT
	sleep_until $((T_N + QUIESCE_GAP))
	X=$A A=$B B=$X
done
IT=$ITER
