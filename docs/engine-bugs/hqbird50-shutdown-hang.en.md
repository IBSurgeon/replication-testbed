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
is traced and the garbage collector thread. A build with a fix for it and
for two crashes in the same path passed every check (see "A tested fix"). It needs a **user trace
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

## A tested fix: build fix15t (2026-11-06)

Fix 1 above, plus two crash fixes that the shutdown path showed once the
deadlock was gone. The diff against the 5.0.5.1878 tree of the build with
the fix is `hqbird50-shutdown-hang-fix15t.diff` (three files):

| Change | File | What it fixes |
|---|---|---|
| Fix 1: a failed attachment does not map the trace session user (no `Mapping`, no `setDb`); the check uses the session user name | `jrd/trace/TraceManager.cpp` | the deadlock of hangs 1, 4 and 5 |
| `RefPtr<Cache> keep(cache)` while `populateMutex` is locked | `jrd/Mapping.cpp` | `clearCache` removed the cache meanwhile, `cache = nullptr` released the last reference, the destructor destroyed a locked mutex: `pthread_mutex_destroy` EBUSY, abort in `gfix -online normal` |
| `JAttachment::startTransaction` makes the `JTransaction` interface inside the `EngineContextHolder` scope | `jrd/jrd.cpp` | a forced shutdown (`gfix -shut full -force 0`) purged the attachment as soon as its lock was free, and the new transaction was already freed: SIGSEGV in `jrd_tra::getInterface` (`tra.cpp:3859`) |

Proposal 2 is not in the build: fix 1 alone breaks the cycle.

| | |
|---|---|
| libEngine13.so | md5 `63a599644b74727f03977433d528485f`, build-id `f76112f2773e776a563c9b5e2d340190a164c0a4` (debug file md5 `87c4eb89a2c682235095fe9832a3df07`) |
| Minimal script (`hqbird50-shutdown-hang-min.sh`, a user trace session) | original engine: hung in 11 of 11 runs; fix 1 only: no hang in 192 iterations, but aborts and the SIGSEGV; fix15t: **10 runs × 40 iterations, no hang, no abort, no crash** |
| The stand (ubuntu-1..3, fbagent trace tasks on again) | **40 switchovers under load: 40 of 40, 121 of 121 cases, 41-66 s each (median 50.5 s), no hang, no crash** |

## Second tested fix: fix 4, hvlad's variant (2026-11-06)

hvlad's idea: the garbage collector must not map users at its last commit.
`hqbird50-shutdown-hang-fix4.diff` (`jrd.cpp`, `TraceManager.h/.cpp`):
right after `dbb_init_fini->destroy()` in `JRD_shutdown_database()`, it
calls `att_trace_manager->stopNewSessions()` for every system attachment
(GC, cache writer, workers). Then `update_sessions()` adds no new trace
sessions for them, as `noNewSessions` already does for `att_purge_tid`.
Failed attachments are still traced. It is set before `VIO_fini()`, so it
also covers `TRA_start` in the GC.

The crash fix for `startTransaction` alone is
`hqbird50-shutdown-hang-fix3.diff` (AlexPeshkoff's variant: everything
after the `catch` moves into the `try`).

Results (minimal script, 40 iterations per run; Windows on 1 core):

| Build | Linux | Windows |
|---|---|---|
| no fix | hangs in iteration 1-2 | crash (freed `JTransaction`) in 4 of 5 runs |
| fix3 only | hangs in iteration 2 | 3 runs pass; run 4: crash of the GC after the 10 s wait (below) |
| fix3 + fix4 | **15 runs, no hang, no abort** (10 with default settings, 5 on 1 CPU) | **4 runs, no hang, no crash** |

**Windows has no hang, because of a 10 s wait.** On Windows,
`Thread::waitForCompletion()` (`common/ThreadStart.cpp:375`) waits only
10 s (`WaitForSingleObject(m_handle, 10000)`); Linux uses `pthread_join()`.
So `VIO_fini()` gives up on the GC that waits for `populateMutex`,
`JRD_shutdown_database()` goes on and deletes the `Database` with all its
pools, including the GC attachment. When the GC gets the mutex, it uses
freed memory: crash in `JAttachment::startTransaction` →
`Attachment::setupIdleTimer`, called from `Mapping::Cache::populate` in
the GC's last `TRA_commit`. The same 10 s limit can free a `Database`
under any GC that needs more than 10 s at close (from the code, not
tested). Details: `hqbird50-shutdown-hang-repro.ru.md`.

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
- A hung server is detected (node protocol minor 22, branch `switchover`):
  the node attaches to a database every minute (10 s allowed) and after one
  minute without an answer raises the alert `firebird_not_answering`; RCM
  moves the service to `failed`, and `hqbirdrcm resume` brings it back once
  Firebird answers again. The HAProxy TCP check alone does not see it.
- The stand ran with the fbagent trace tasks switched off (`trace_monitor`,
  `trace_monitor_conflicts`, `watch_trace_sessions`, `trace_watchdog`,
  `conflict_trace`) until the fix15t build was installed on 2026-11-06; they
  are on again.
