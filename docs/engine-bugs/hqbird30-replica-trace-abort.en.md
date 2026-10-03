# HQbird 3.0.15: a replica aborts on segment apply when a trace session starts

For the HQbird engine team. Found and analysed with this test bed, 2026-10-01 to
2026-10-03. The core dump, the full backtraces, `firebird.log`,
`replication.log` and the configs are not in this repository: the operator
sends them as a separate archive (`hqbird30-replica-trace-abort.zip`, about
17 MB).

## Summary

On an HQbird 3.0 **replica** (asynchronous replication, `ReplServer`), the
server aborts (`SIGABRT`) on the next segment apply after a **new user trace
session** is created. `firebird.log`:

```
Operating system call pthread_mutex_unlock failed. Error code 1
/opt/firebird/bin/fbguard: /opt/firebird/bin/firebird terminated abnormally (-1)
```

The guardian restarts the server, the segment is applied after the restart,
and the next apply aborts again while the trace session lives. With
`BugCheckAbort = 1` every abort writes a core of 650-700 MB.

| | |
|---|---|
| Builds | `3.0.15.33885-HQbird-8962ddc`; `3.0.15.33885-HQbird-03e5152` (2026-10-02, only `libEngine12.so` differs) - same stack |
| OS | Ubuntu 22.04 x86_64, SuperServer, installed by `fb_hqbird-30.sh` |
| Not affected | the master (it does not apply segments); HQbird 2.5 replicas; Firebird/HQbird 4.0 and 5.0 replicas with the same trace sessions |

## How to reproduce

1. Master and replica on HQbird 3.0.15, asynchronous replication configured
   (`replconf.hqbird` / replication plugin), a test table published.
2. Start Firebird on the replica. Wait until the replication thread has
   attached the replica database and applied at least one segment.
3. On the replica, start any user trace session, for example:

   ```
   fbtracemgr -se localhost:service_mgr -user SYSDBA -password *** \
       -start -name t1 -config trace.conf
   ```

   with `trace.conf`:

   ```
   database
   {
       enabled = true
       log_statement_finish = true
   }
   ```

4. On the master, insert one row and commit; wait for the segment to be
   shipped and applied on the replica.
5. The replica server aborts with the `firebird.log` lines above.

Verified 2026-10-03 exactly so, without fbagent's trace tasks (they were off
on the replica): one session started with `fbtracemgr` as above, one row
inserted on the master, and the replica server aborted on that segment (new
PID, one more "terminated abnormally" line). After the session was stopped
the replica applied the rest and matched the master (test bed `test
tracegate --repro`, HQbird 3.0.15.33885 on Ubuntu 22.04).

What we checked on the stand (one change at a time):

| Change | Abort |
|---|---|
| No trace sessions | no |
| One trace session (either of two different configs) | yes |
| A trace session for `security3.fdb` only | yes |
| `MonitoringPlugin = fblwmon` removed, trace session on | yes |
| Segment applied right after the server start, before the agent created its sessions | no |

`libcluster.so` is not loaded into the server process. The replconf plugin
content and its valid date do not matter.

## Stack (replication thread)

```
#5-7   system_call_failed <- Mutex::leave            pthread_mutex_unlock, EPERM
#8-9   StableAttachmentPart::Sync::leave <- EngineCheckout
#10    TraceManager::update_session                 TraceManager.cpp:289
#11-12 TraceManager::update_sessions <- needs
#13    TRA_start                                    tra.cpp:1716
#14    ReplicationSlave::startTransaction           Replication.cpp:2227
#15-20 REPL_replicate <- JAttachment::replicate <- fb_replicate
#21-23 replicate <- process_archive <- process_thread   ReplServer.cpp
```

## Analysis from the core

- `ReplicationSlave::startTransaction` (Replication.cpp:1186) leaves the
  mutex of the attachment that called `fb_replicate` (the caller attachment)
  and enters the mutex of the replica's internal attachment.
- The `TraceManager` used by `TRA_start` belongs to the internal attachment
  but points at the **caller** attachment
  (`TraceManager::attachment->att_attachment_id` is the caller's id).
- When a new trace session appeared since the last check,
  `update_sessions` -> `update_session` creates an `EngineCheckout` for that
  attachment (TraceManager.cpp:289) and leaves the caller attachment's mutex
  a second time.
- In frame 8 the mutex has no owner (`__owner = 0`, `__lock = 0`,
  `threadId = 0`, `currentLocksCounter = 0`). `pthread_mutex_unlock` on an
  error-checking mutex returns `EPERM`, `system_call_failed::raise` follows,
  and with `BugCheckAbort = 1` the server calls `abort()`.
- The session being updated in the dump is the agent's second trace session
  (`ses_id = 3`, `ses_flags = 2`, name `FBAgent-<host>-conflicts`). The agent
  starts its sessions about 30 s after the server starts; the segment applied
  at start passes, the next one aborts.

Probable fix directions (for the engine team to judge): let the slave's
internal attachment have its own `TraceManager` (or skip trace session
updates for it), or do the session update without `EngineCheckout` on an
attachment whose mutex the thread does not hold.

## Workaround in the field

Do not create trace sessions on an HQbird 3.0 replica. fbagent 2.59.0 does
not start its trace tasks (`trace_monitor`, `trace_monitor_conflicts`) on an
HQbird 3.0 instance that hosts a replica database.

## Archive contents (sent separately)

| File | What |
|---|---|
| `core.firebird.<pid>.xz` | core of build `8962ddc` (xz, about 18 MB; 650 MB unpacked) |
| `bt.txt`, `bt-full.txt`, `bt-all.txt` | crashing thread, with locals, all threads |
| `bt-03e5152.txt` | the same abort with build `03e5152` |
| `inspect*.gdb` | gdb scripts used for the analysis above |
| `firebird.log`, `replication.log`, `firebird.conf` | replica logs and config |
| `trace.conf`, `trace_conflict.conf` | the two trace configs fbagent uses |
| `BUILD.txt` | build id; debug symbols matched by the `.gnu_debuglink` CRC |
