#!/bin/bash
# hqbird50-shutdown-hang-connects.sh ISQL DATABASE [LOOPS]
#
# The frequent attachments of hqbird50-shutdown-hang-min.sh: LOOPS isql
# processes (default 4) attach to DATABASE again and again until the script
# is stopped (Ctrl+C, or a signal to its process group). Each isql reads an
# endless stream of CONNECT statements, so no process is started per
# attachment and an attachment is in progress almost all the time. In a full
# shutdown each one is rejected and traced as a failed attachment.
#
# ISQL: the isql binary. DATABASE: a connection string such as
# localhost/3071:/root/fbmin/run/test.fdb. User and password: ISC_USER and
# ISC_PASSWORD.

[ $# -ge 2 ] || { echo "usage: $0 ISQL DATABASE [LOOPS]" >&2; exit 64; }

connects() {
	while :; do
		while :; do echo "connect '$2';"; done | "$1" -q > /dev/null 2>&1
		sleep 0.2
	done
}

for ((i = 1; i <= ${3:-4}; i++)); do connects "$1" "$2" & done
wait
