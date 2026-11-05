# Firebird 5.0.5 HQbird: the server hangs in a shutdown or a backup lock

For the HQbird engine team. Found with this test bed (`test switchover`,
LXD stand ubuntu-1..3), 2026-11-04. The full gdb stacks and the process maps
of the first hang are not in this repository: the operator sends them as a
separate archive.

## Summary

Twice in about 15 switchovers under load, the Firebird server of the host
that was being switched stopped answering:

| # | Time (UTC) | What the node ran | What happened |
|---|---|---|---|
| 1 | 2026-11-04 22:01:07 | `gfix -shut full -force 0` on the old master (18 s after an `nbackup -L`, copy, `nbackup -N` of the same file) | gfix did not end; the header showed `full shutdown`; the server took no new attachment at all, not even `security.db`; the replica appliers kept running; nothing in firebird.log |
| 2 | 2026-11-04 23:04:5x | `nbackup -L` on the new master, 1 minute after a `gfix -shut single -force 0`, `gfix -replica none`, `gfix -online normal` and a server restart | nbackup did not end in 30 minutes; the load (4 connections through HAProxy) stopped committing for the whole time; the database was not left in backup state |

Hang 1 ended with `kill -9` of `firebird` (the guardian restarted it) and
`gfix -online normal`. Hang 2 ended when the host was powered off by the
next test case. Six more switchovers right after, with the same load, had no
hang.

| | |
|---|---|
| Build | `LI-V5.0.5.1878 Firebird 5.0 HQbird`, SuperServer |
| Config | `ParallelWorkers = 1`, `MaxParallelWorkers = 64` |
| OS | Ubuntu 26.04.1 LTS x86_64 (LXD container) |
| Databases on the server | one master (`shop.fdb`, with a journal) and two replicas (`journal_source_directory`) |
| Load | fb-loadgen write-heavy, about 25 TPS, 2-4 SYSDBA connections through HAProxy |

## Hang 1: deadlock (gdb, all 82 threads)

- LWP 728979 holds mutexes 0x7f0ef03fca70 and 0x7f0eff5d5300 and waits in
  `pthread_join` for LWP 728894.
- LWP 728894 (an engine thread) waits for mutex 0x7f0ef03fca70.
- The other threads form a chain: 62 threads wait for mutex 0x7f0f00eb5868
  (owner LWP 729398) → 0x7f0f00eb4f68 (owner 728773) → 0x7f0f00e96060
  (owner 729396) → 0x7f0eff5d5300 (owner 728979).

Module offsets (libEngine13.so md5 78f5ffee48e67fc12e1b83c5db341d27,
firebird md5 86a379f162e08e39eec95511ced0c40d):

```
LWP 728979 (holds the mutex, joins 728894)
  pthread_join
  libEngine13.so+0x6548e7  +0x2b2d19  +0x8917b  +0x89451  +0x11587d
  libfbclient +0xc7ed2 +0x6fe5d          (y-valve call from inside the engine)
  libEngine13.so+0x163a06  +0x168e18  +0x169ea8  +0x59a96c  +0x59b135
                 +0x2a94ee  +0x2af97c  +0x891b4   +0x89451   +0x11587d
LWP 728894 (thread start at +0x6547b4)
  pthread_mutex_lock 0x7f0ef03fca70
  libEngine13.so+0x168de1  +0x169ea8  +0x59a372  +0x59b135
                 +0x349100  +0x36cae0  +0x36fa91  +0x6547b4
```

Both threads pass through +0x59b135 → +0x59a… → +0x169ea8 → +0x168…, where
mutex 0x7f0ef03fca70 is taken. One thread calls back into the y-valve from
inside the engine and then joins a thread that waits for a mutex it holds.

## Hang 2: no stacks

The host was powered off before the stacks were taken. After the restart
`gstat -h` showed only `force write` (no backup lock, no delta). The test bed
now has a watchdog for the next one: when `nbackup` or `gfix` runs longer
than 2 minutes, it takes the stacks of every Firebird thread and the mutex
owners.

## What the cluster node does about it

- A failed or stopped quiesce (`gfix -shut full`) runs `gfix -online normal`
  and says whether the database is online again (hqcluster-node, branch
  `switchover`).
- `nbackup -L` has a 30-minute limit. On a master under load, every writer
  waits while `-L` waits.
- A hung server is not detected yet: the node reports the replica in sync,
  and the HAProxy TCP check passes.
