# Firebird 5.0.5 HQbird: the server hangs in a shutdown or a backup lock

For the HQbird engine team. Found with this test bed (`test switchover`,
LXD stand ubuntu-1..3), 2026-11-04 and 2026-11-05. The gdb stacks, the
process maps and the cores are not in this repository: the operator sends
them as a separate archive.

## Summary

Five times in about 60 switchovers under load with the fbagent trace
sessions on, the Firebird server of the host that was being switched
stopped answering:

| # | Time (UTC) | What the node ran | What happened |
|---|---|---|---|
| 1 | 2026-11-04 22:01:07 | `gfix -shut full -force 0` on the old master (18 s after an `nbackup -L`, copy, `nbackup -N` of the same file) | gfix did not end; the header showed `full shutdown`; the server took no new attachment at all, not even `security.db`; the replica appliers kept running; nothing in firebird.log |
| 2 | 2026-11-04 23:04:5x | `nbackup -L` on the new master, 1 minute after a `gfix -shut single -force 0`, `gfix -replica none`, `gfix -online normal` and a server restart | nbackup did not end in 30 minutes; the load (4 connections through HAProxy) stopped committing for the whole time; the database was not left in backup state |
| 3 | 2026-11-05 15:26 | `nbackup -N` (reinit) on the new master, 14 s after the promote restart | another picture: a chain of mutexes that ends in a condition wait at `libEngine13.so+0x222bd4`; not seen again with the build with the fix |
| 4 | 2026-11-05 18:2x | `gfix -shut full -force 0` on the old master, build with the fix | the same deadlock as hang 1 |
| 5 | 2026-11-05 18:56:57 | `gfix -shut full -force 0` on the old master, build with the fix, engine debug info installed | the same deadlock; full analysis below |

Hangs 1, 4 and 5 are one bug: a deadlock between a failed attachment that
is traced and the garbage collector thread. It needs a **user trace
session** on the server. On the stand, fbagent runs these sessions
(`fbtracemgr`). With the fbagent trace tasks switched off, 40 switchovers
under the same load had no hang (40 of 40, 39-94 s each). A minimal script
without fbagent and without a trace session did not hang in 40 iterations.

| | |
|---|---|
| Build | `LI-V5.0.5.1878 Firebird 5.0 HQbird`, SuperServer, default `GCPolicy` |
| libEngine13.so | hangs 1-3: md5 `78f5ffee48e67fc12e1b83c5db341d27`; hangs 4-5: build with the fix, md5 `a5d7a9773fa246640546624e06aa7492`, build-id `8af11883d6c49b7dd58826d291d0c877c1afc099` |
| firebird | md5 `86a379f162e08e39eec95511ced0c40d` (official, no debug info) |
| Config | `ParallelWorkers = 1`, `MaxParallelWorkers = 64` |
| OS | Ubuntu 26.04.1 LTS x86_64 (LXD container) |
| Databases on the server | one master (`shop.fdb`, with a journal) and two replicas (`journal_source_directory`); all use the default `security5.fdb` |
| Load | fb-loadgen write-heavy, about 25 TPS, 2-4 SYSDBA connections through HAProxy |
| Trace | two fbagent `fbtracemgr` sessions (fbagent tasks `trace_monitor`, `trace_monitor_conflicts`), user SYSDBA |

## Hang 5: the deadlock with debug info

Source lines are from the 5.0.5.1878 tree of the build with the fix.

### State at the time of the hang

- `gstat -h`: `full shutdown`. firebird.log, 18:56:57: `Automatic sweep
  error`, `database /databases/svc/shop.fdb shutdown`. Stacks at 18:59:40.
- Core, `Database` of shop.fdb: `dbb_ast_flags = 0x88` (`DBB_shutdown` +
  `DBB_shutdown_full`; no `DBB_shut_attach`, `DBB_shut_tran`,
  `DBB_shut_force`), so the shutdown is complete. `dbb_attachments = 0x0`,
  `dbb_sys_attachments` = the garbage collector attachment, `DBB_new` and
  `DBB_garbage_collector` are clear, `DBB_gc_background` is set.
- Trace session 3, `FBAgent-ubuntu-2261104`, `ses_user = SYSDBA`,
  `ses_flags = 0x2` (`trs_active` only), auth block of 114 bytes. The
  session is not `trs_admin` although the user is SYSDBA: when the service
  has an auth block, `TraceSvcJrd` sets `m_admin = false`
  (`jrd/trace/TraceService.cpp:89-95`).

### The two threads in the cycle

```
LWP 959356 (fb-loadgen attachment, holds populateMutex 0x7fb801b47df0
            and the Database existence mutex 0x7fb833bd9650)
  pthread_join (garbage collector thread, LWP 958759)
  Thread::waitForCompletion                  common/ThreadStart.cpp:198
  ThreadFinishSync<Database*>::waitForCompletion
  JRD_shutdown_database(dbb, flags = 1)
  unwindAttach("shop")                        nested attachment
  JProvider::internalAttach("shop")           SYSDBA, sec_attach, map_attach
  y-valve (libfbclient)
  Mapping::DbHandle::attach("shop")           jrd/Mapping.cpp:200
  Mapping::ensureCachePresence                jrd/Mapping.cpp:1331
  Mapping::setDb                              jrd/Mapping.cpp:1411
  TraceManager::update_session                filename branch
  TraceManager::update_sessions               jrd/trace/TraceManager.cpp:251
  TraceManager::needs                         jrd/trace/TraceManager.h:144
  trace_failed_attach("shop")
  unwindAttach("shop")
  JProvider::internalAttach("shop")           remote, fb-loadgen, isc_shutdown

LWP 958759 (garbage collector of shop.fdb)
  pthread_mutex_lock(populateMutex 0x7fb801b47df0)
  Mapping::ensureCachePresence                jrd/Mapping.cpp:1326
  Mapping::setDb                              jrd/Mapping.cpp:1411
  TraceManager::update_session                jrd/trace/TraceManager.cpp:312
  TraceManager::update_sessions               jrd/trace/TraceManager.cpp:251
  TraceManager::needs(TRANSACTION_END)        jrd/trace/TraceManager.h:144
  TraceTransactionEnd::TraceTransactionEnd
  TRA_commit
  Database::garbage_collector
```

### Step by step

1. The node runs `gfix -shut full -force 0`. The gfix attachment
   (`isc_dpb_shutdown = 0x48`: full + force, delay 0) runs `SHUT_database`:
   all attachments are closed and the database goes to full shutdown. Then
   the same attachment gets `isc_shutdown` (`jrd/jrd.cpp:2047-2073`; the
   comment there says this is expected when entering full shutdown).
2. fb-loadgen reconnects all the time; HAProxy still sends it to this host.
   The `Database` object of shop.fdb is still in memory and is not new. Its
   garbage collector thread is still running.
3. Each new attachment is rejected with `isc_shutdown`. `unwindAttach`
   first releases the attachment and calls `JRD_shutdown_database`, which
   returns at once while other attachments exist. Then it calls
   `trace_failed_attach`.
4. `trace_failed_attach` creates a temporary `TraceManager` with
   `failedAttach = flags & UNWIND_NEW`. The `Database` is not new, so
   `failedAttach` is false. `needs()` calls `update_sessions()`, then
   `update_session()` for the fbagent session. The session is not admin or
   system and `TraceAll` is off, so the engine maps the session user. In the
   `filename` branch, `mapping.setDb()` is called because `failedAttach` is
   false.
5. `Mapping::ensureCachePresence` finds the mapping cache of shop.fdb empty.
   It takes `cache->populateMutex` (`Mapping.cpp:1326`) and, while it holds
   it, attaches to the same database to read its `RDB$AUTH_MAPPING`
   (`:1331`, `DbHandle::attach`: embedded SYSDBA, `isc_dpb_sec_attach`,
   `isc_dpb_map_attach`, `isc_dpb_no_db_triggers`, providers
   `Remote Engine13`). This is the database itself, not `security5.fdb`.
6. The nested attachment finds the same `Database`, takes its existence
   mutex and is rejected: SYSDBA is a locksmith, but full shutdown denies
   access always (`jrd/jrd.cpp:2054`).
7. The nested `unwindAttach` releases its attachment. Now there are no
   attachments at all, so `JRD_shutdown_database(dbb, 1)` really shuts the
   `Database` down. Linger does not apply in shutdown. It stops the
   garbage collector (`DBB_garbage_collector` is cleared, the thread is
   woken) and waits for it in `pthread_join`. The thread still holds
   `populateMutex`. (`trace_failed_attach` of the nested attachment returns
   early because of `dpb_map_attach`, but this is too late.)
8. The garbage collector thread commits its transaction. `TRA_commit` →
   `TraceTransactionEnd` → `needs()` → `update_session()` for the same
   session → `Mapping::setDb()` with the same database cache → it waits for
   `populateMutex`.
9. Cycle: LWP 959356 waits for the end of LWP 958759, and LWP 958759 waits
   for the mutex that LWP 959356 holds. Neither wait has a timeout.

### How the hang spreads

| LWP | Who | Waits for | Holds |
|---|---|---|---|
| 959356 | fb-loadgen, then the nested SYSDBA attachment | end of the garbage collector thread | populateMutex 0x7fb801b47df0, existence mutex 0x7fb833bd9650 |
| 958759 | garbage collector of shop.fdb | populateMutex | - |
| 958715 | gfix (`isc_dpb_shutdown = 0x48`) | populateMutex, in `trace_failed_attach` | - |
| 958605, 958722, 958774, 958776, 959360 | fb-loadgen | populateMutex, in `trace_failed_attach` | - |
| 958775 | fb-loadgen, a new attachment | existence mutex 0x7fb833bd9650, in `initAttachment` | `dbInitMutex` 0x7fb83628b060 |
| 958717 | embedded SYSDBA attachment to `security5.fdb` (password check of a new network login) | `dbInitMutex` (`jrd/jrd.cpp:1663`) | 0x7fb8362a9f68 (code in `firebird`, no symbols) |
| 959517 | network server thread (`firebird`, no symbols) | 0x7fb8362a9f68 | 0x7fb8362aa868 |
| 19 more | network server threads | 0x7fb8362aa868 | - |

Every attachment to any database first takes `dbInitMutex`, so the server
takes no new attachment at all. The process lives and port 3050 is open, so
the guardian and a TCP health check see nothing. gfix never gets an answer;
the node kills it after 30 s, but the server thread stays blocked.

Hang 1 has the same shape: 62 threads → 0x7f0f00eb5868 → 0x7f0f00eb4f68 →
0x7f0f00e96060 → a thread that holds two mutexes and joins an engine thread
that waits for one of them. The low parts of the addresses (`…868`,
`…f68`, `…060`) are the same as in hang 5. The module offsets of hang 4
resolve to the same functions as in hang 5 (`Thread::waitForCompletion`,
`Mapping::ensureCachePresence`, `Mapping::setDb`,
`TraceManager::update_session`, `TraceManager::update_sessions`).

### Conditions

All of these at the same time:

- A user trace session with an auth block (not `trs_admin`, not
  `trs_system`) and `TraceAll` off. A session started by SYSDBA over the
  network is such a session. The HQbird "Firebird Monitoring" session is
  `trs_system` and does not take part.
- The database is in **full** shutdown, so the nested SYSDBA attachment is
  rejected too. From the code, not tested: in `multi` shutdown the nested
  attachment is allowed and the cycle does not form.
- The `Database` object is still in memory and is not new, and its mapping
  cache is empty.
- A rejected attachment is the last one, so its nested attachment shuts the
  `Database` down and joins the garbage collector.
- At that time the garbage collector thread enters `TRA_commit`, and the
  trace check sends it to the mapping.

This is a short time window; it explains about one hang in 10-20
switchovers.

### Possible fixes

Ideas for the engine team; any one of them breaks the cycle:

1. Do not call `Mapping::setDb()` in `TraceManager::update_session()` for
   any failed attachment, not only when the `Database` is new
   (`UNWIND_NEW`), or at least not after `isc_shutdown`.
2. Do not hold `cache->populateMutex` while `ensureCachePresence()`
   attaches to the database, or do not attach when the database is shut
   down.
3. Do not run `JRD_shutdown_database()` from `unwindAttach()` of an
   attachment with `dpb_map_attach`; leave the `Database` to the outer
   attachment.

## Hang 2: no stacks

The host was powered off before the stacks were taken. After the restart
`gstat -h` showed only `force write` (no backup lock, no delta). For hangs
3-5 the operator ran a watchdog script next to the test (not in this
repository): when `nbackup` or `gfix` runs longer than 20 s and a new
attachment gets no answer in 10 s, it takes the stacks of every Firebird
thread, the mutex owners and a core.

## What the cluster node does about it

- A failed or stopped quiesce (`gfix -shut full`) runs `gfix -online normal`
  and says whether the database is online again (hqcluster-node, branch
  `switchover`).
- `nbackup -L` stops after 2 minutes (was 30): on a master under load,
  every writer waits while `-L` waits. The reinit then fails and can be
  started again.
- A hung server is not detected yet: the node reports the replica in sync,
  and the HAProxy TCP check passes.
- Workaround on the stand: the fbagent trace tasks are switched off
  (`trace_monitor`, `trace_monitor_conflicts`, `watch_trace_sessions`,
  `trace_watchdog`, `conflict_trace`).
