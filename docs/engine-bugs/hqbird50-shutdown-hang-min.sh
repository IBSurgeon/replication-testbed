#!/bin/bash
# hqbird50-shutdown-hang-min.sh FB_ROOT WORK_DIR ITERATIONS
#
# Minimal repro of the deadlock in hqbird50-shutdown-hang.en.md (hangs 1, 4
# and 5). A private SuperServer runs from a copy of FB_ROOT on
# 127.0.0.1:PORT; the server of FB_ROOT is not touched. Around test.fdb:
#
#   - a new user trace session in each iteration: fbtracemgr over TCP as
#     SYSDBA, so it has an auth block and is not an admin session. The
#     garbage collector reads the trace sessions when it starts; at its last
#     commit it maps the user (and waits for populateMutex) only for a
#     session that is new to it, as after a session start by fbagent;
#   - WRITERS isql loops delete and insert rows, so the garbage collector
#     thread opens a transaction;
#   - hqbird50-shutdown-hang-connects.sh (next to this script) attaches
#     again and again with CONNECTS isql processes.
#
# One iteration: start the load, ONLINE_S s, a new trace session (the one of
# the previous iteration is stopped), 1 s, gfix -shut full -force 0,
# BURST_S s of rejected attachments, stop the load, SHUT_S s of quiet, a
# probe attachment to probe.fdb, gfix -online normal.
#
# The load must stop in the shutdown: the cycle can form only when the last
# attachment leaves the Database object of test.fdb, and a constant stream
# of attachments keeps that object open. (gfix -online normal under load can
# also crash the server: pthread_mutex_destroy failed, error code 16.)
#
# In the deadlock no new attachment gets an answer, so a tool runs into
# TIMEOUT. Then the script saves gdb stacks, a core, gstat -h and
# firebird.log into WORK_DIR/hang-<time>/ and stops.
#
# ISC_PASSWORD (ISC_USER, default SYSDBA) must be valid in
# FB_ROOT/security5.fdb, which is copied. Needs gdb; run as root in a test
# container.
#
# Example:
#   ISC_PASSWORD=... ./hqbird50-shutdown-hang-min.sh /opt/firebird /root/fbmin 100
# Control runs: TRACE=once (one session from the start), TRACE=0 (none) or
# SHUT_MODE=multi. Workarounds: FBCONF='TraceAll = true'; INTAKE=closed (no
# new attachments from just before the shutdown on); TRACE=stop (a session
# starts only while test.fdb is closed, one known session stops in each
# iteration).

PORT=${PORT:-3071}            # private server, 127.0.0.1 only
WRITERS=${WRITERS:-2}         # isql loops that delete and insert
CONNECTS=${CONNECTS:-4}       # isql processes that only attach
ONLINE_S=${ONLINE_S:-3}       # s of load before each shutdown
BURST_S=${BURST_S:-1}         # s of load after the shutdown (rejected)
SHUT_S=${SHUT_S:-2}           # s without load before the probe
SHUT_MODE=${SHUT_MODE:-full}  # full: the hang; multi: control
TRACE=${TRACE:-1}             # 1: a new session per iteration; stop, once, 0
INTAKE=${INTAKE:-open}        # closed: no new attachments from the shutdown on
FBCONF=${FBCONF:-}            # more firebird.conf lines, e.g. 'TraceAll = true'
TIMEOUT=${TIMEOUT:-30}        # s for one tool; longer is a hang
KEEP=${KEEP:-0}               # 1: keep the server and its files
CPUS=${CPUS:-}                # CPU list for the server (taskset -c), e.g. 0

set -u
[ $# -eq 3 ] || { echo "usage: $0 FB_ROOT WORK_DIR ITERATIONS" >&2; exit 64; }
[ -n "${ISC_PASSWORD:-}" ] || { echo "set ISC_PASSWORD" >&2; exit 64; }
command -v gdb > /dev/null || { echo "gdb is needed for the stacks" >&2; exit 64; }
FBROOT=$(cd "$1" && pwd) || exit 64
mkdir -p "$2" && WORK=$(cd "$2" && pwd) || exit 64
ITER=$3
HERE=$(cd "$(dirname "$0")" && pwd)
RUN=$WORK/run
FB=$RUN/fb
DB=localhost/$PORT:$RUN/test.fdb
LOG=$WORK/repro.log
IT=0 PID= CPID= HUNG=
export ISC_USER=${ISC_USER:-SYSDBA} ISC_PASSWORD FIREBIRD=$FB FIREBIRD_LOCK=$RUN/lock FIREBIRD_TMP=$RUN/tmp

log() { printf '%s it=%s %s\n' "$(date -u +%T)" "$IT" "$*" | tee -a "$LOG"; }

# run <tool> <args>: a Firebird tool of the private server. A timeout is a
# hang; another error stops the script.
run() {
	local out rc
	out=$(timeout -k 5 "$TIMEOUT" "$FB/bin/$1" "${@:2}" 2>&1 < /dev/null)
	rc=$?
	log "  $* rc=$rc${out:+ | $(printf '%s' "$out" | tr -s '\n\t' '  ' | cut -c1-160)}"
	case $rc in
	0) ;;
	124 | 137) hang "$*" ;;
	*) kill -0 "$PID" 2>/dev/null || log "the server process is gone"; log "FAILED"; exit 3 ;;
	esac
}

hang() {
	HUNG=$WORK/hang-$(date -u +%Y%m%d-%H%M%S)
	mkdir -p "$HUNG"
	log "HANG in iteration $IT: '$1' got no answer in $TIMEOUT s; saving $HUNG"
	{ "$FB/bin/gstat" -z; md5sum "$FB/bin/firebird" "$FB/plugins/libEngine13.so"; uname -sr; } > "$HUNG/version.txt" 2>&1
	timeout 10 "$FB/bin/gstat" -h "$RUN/test.fdb" > "$HUNG/gstat-h.txt" 2>&1
	cp "/proc/$PID/maps" "$HUNG/maps.txt"
	DEBUGINFOD_URLS= timeout 600 gdb -p "$PID" -batch -ex 'set pagination off' -ex 'thread apply all bt' \
		-ex "gcore $HUNG/core" > "$HUNG/stacks.txt" 2>&1
	log "  $(grep -c '^Thread ' "$HUNG/stacks.txt") thread stacks and a core saved"
	exit 2
}

stop_connects() { [ -n "$CPID" ] && kill -- -"$CPID" 2>/dev/null; CPID=; }

cleanup() {
	local rc=$?
	trap - INT TERM
	rm -f "$RUN"/on.*
	stop_connects
	for j in $(jobs -p); do [ "$j" = "$PID" ] || kill "$j" 2>/dev/null; done # not the server
	cp "$FB/firebird.log" "${HUNG:-$WORK}/" 2>/dev/null
	[ -n "$HUNG" ] && cp "$LOG" "$HUNG/"
	if [ "$KEEP" = 1 ]; then
		log "kept: $RUN; the server still runs: kill -9 $PID"
	elif [ -f "$RUN/.fbmin-run" ]; then
		pkill -9 -f -- "^$FB/bin/" # the server and the isql processes
		rm -rf "$RUN"
	fi
	case $rc in
	0) log "END: $((IT - 1)) of $ITER iterations, no hang" ;;
	2) log "END: hang in iteration $IT; see $HUNG" ;;
	*) log "END: stopped (exit $rc) in iteration $IT, no hang before" ;;
	esac
}

tracemgr() { timeout -k 5 "$TIMEOUT" "$FB/bin/fbtracemgr" -se "localhost/$PORT:service_mgr" "$@"; }

# trace_start <name>: start a session (fbtracemgr runs in the background)
# and check its flags.
trace_start() {
	local flags
	"$FB/bin/fbtracemgr" -se "localhost/$PORT:service_mgr" -start -name "$1" -config "$RUN/trace.conf" > /dev/null 2>&1 &
	sleep 1
	flags=$(tracemgr -list 2>&1 | awk -v n="$1" '$1 == "name:" {s = ($2 == n)} s && $1 == "flags:"' | cut -c1-60)
	[ "$IT" -le 1 ] && log "  trace session $1:${flags:- missing}"
	[ -n "$flags" ] || { log "FAILED: no trace session $1"; exit 3; }
}

trace_stop() { # trace_stop <name>
	tracemgr -stop -id "$(tracemgr -list 2>&1 | awk -v n="$1" '/Session ID/ {id = $3} $1 == "name:" && $2 == n {print id}')" > /dev/null 2>&1
	[ "$IT" -le 2 ] && log "  trace session $1 stopped; sessions: $(tracemgr -list 2>&1 | grep -c 'Session ID')"
}

# writer <n> <file>: runs while <file> exists; reconnects after an error
# while <file>.r exists
writer() {
	while [ -e "$2.r" ]; do
		while [ -e "$2" ]; do
			echo "delete from T where W = $1; insert into T values ($1, 'row'); commit;"
			sleep 0.05
		done | "$FB/bin/isql" -q -b "$DB" > /dev/null 2>&1
		sleep 0.2
	done
}

# --- a private server
if [ -e "$RUN" ]; then
	[ -f "$RUN/.fbmin-run" ] || { echo "$RUN is not a run directory of this script" >&2; exit 64; }
	pgrep -f -- "^$FB/bin/firebird" > /dev/null && { echo "the server of an earlier run still runs" >&2; exit 64; }
	rm -rf "$RUN"
fi
(exec 3<> "/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && { echo "port $PORT is in use: set PORT" >&2; exit 64; }
mkdir -p "$RUN/lock" "$RUN/tmp" && : > "$RUN/.fbmin-run"
trap cleanup EXIT
trap 'log "interrupted"; exit 130' INT TERM

# Owned by this user and without the sticky bit of FB_ROOT: with
# fs.protected_regular even root cannot write a file of another user there.
cp -RP --preserve=mode,timestamps "$FBROOT/." "$FB/" && chmod -t "$FB" || exit 3
rm -f "$FB/firebird.log"
: > "$FB/replication.conf"
sed -i -E '/^[[:space:]]*(ServerMode|RemoteServicePort|RemoteAuxPort|RemoteBindAddress|SecurityDatabase)[[:space:]]*=/d' "$FB/firebird.conf"
printf '%s\n' "ServerMode = Super" "RemoteServicePort = $PORT" "RemoteAuxPort = 0" \
	"RemoteBindAddress = 127.0.0.1" "SecurityDatabase = $FB/security5.fdb" >> "$FB/firebird.conf"
while IFS= read -r line; do # an FBCONF line replaces the same key
	[ -n "$line" ] || continue
	sed -i -E "/^[[:space:]]*${line%%[[:space:]=]*}[[:space:]]*=/d" "$FB/firebird.conf"
	printf '%s\n' "$line" >> "$FB/firebird.conf"
done <<< "$FBCONF"
sed -i -E "s#^([[:space:]]*security\.db[[:space:]]*=).*#\1 $FB/security5.fdb#" "$FB/databases.conf"

${CPUS:+taskset -c "$CPUS"} "$FB/bin/firebird" >> "$RUN/server.out" 2>&1 < /dev/null &
PID=$!
for ((i = 0; i < 30; i++)); do (exec 3<> "/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && break; sleep 1; done
log "start: $("$FB/bin/gstat" -z 2>&1 | grep -io 'LI-V.*'), server pid $PID, $ITER iterations"
log "settings: WRITERS=$WRITERS CONNECTS=$CONNECTS ONLINE_S=$ONLINE_S BURST_S=$BURST_S SHUT_S=$SHUT_S SHUT_MODE=$SHUT_MODE TRACE=$TRACE INTAKE=$INTAKE TIMEOUT=$TIMEOUT${FBCONF:+ FBCONF='$FBCONF'}${CPUS:+ CPUS=$CPUS}"

# --- databases, trace session, load
for f in test probe; do
	echo "create database 'localhost/$PORT:$RUN/$f.fdb';" > "$RUN/create.sql"
	run isql -q -i "$RUN/create.sql"
done
echo "create table T (W integer, TXT varchar(100)); commit;" > "$RUN/schema.sql"
run isql -q -i "$RUN/schema.sql" "$DB"
echo 'select 1 from rdb$database;' > "$RUN/one.sql"
printf '%s\n' "set heading off;" "select 'garbage collector transactions: ' || count(*)" \
	"from MON\$TRANSACTIONS t join MON\$ATTACHMENTS a on a.MON\$ATTACHMENT_ID = t.MON\$ATTACHMENT_ID" \
	"where a.MON\$USER = 'Garbage Collector';" > "$RUN/gc.sql"

printf 'database\n{\n\tenabled = true\n\tlog_connections = true\n}\n' > "$RUN/trace.conf"
case $TRACE in
once) trace_start repro ;;
stop) trace_start repro && trace_start repro-0 ;;
esac

# --- the shutdowns
for ((IT = 1; IT <= ITER; IT++)); do
	: > "$RUN/on.$IT" && : > "$RUN/on.$IT.r"
	for ((i = 1; i <= WRITERS; i++)); do writer "$i" "$RUN/on.$IT" & done
	setsid bash "$HERE/hqbird50-shutdown-hang-connects.sh" "$FB/bin/isql" "$DB" "$CONNECTS" > /dev/null 2>&1 < /dev/null &
	CPID=$!
	sleep "$ONLINE_S"
	case $TRACE in
	1) trace_start "repro-$IT" && [ "$IT" -gt 1 ] && trace_stop "repro-$((IT - 1))" ;;
	stop) trace_stop "repro-$((IT - 1))"; sleep 1 ;;
	esac
	run isql -q -i "$RUN/gc.sql" "$DB"
	if [ "$INTAKE" = closed ]; then # the open attachments stay until the shutdown
		rm -f "$RUN/on.$IT.r"
		stop_connects
		sleep 1
	fi
	run gfix -shut "$SHUT_MODE" -force 0 "$DB"
	sleep "$BURST_S"
	rm -f "$RUN/on.$IT" "$RUN/on.$IT.r"
	stop_connects
	sleep "$SHUT_S"
	[ "$TRACE" = stop ] && trace_start "repro-$IT" # test.fdb is closed now
	run isql -q -i "$RUN/one.sql" "localhost/$PORT:$RUN/probe.fdb"
	run gfix -online normal "$DB"
done
