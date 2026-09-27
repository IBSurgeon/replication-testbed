# Adding a test

New replication tests go into this repository, as test bed tests. Rules:

1. **No secrets and no concrete hosts.** Never write a real host name, IP
   address, ssh user or port, URL, pin, password or token into a script, a
   doc or a default value. Read them from the local config through `cfg`
   (`cl.h(name)["addr"]`, `cl.cfg.secrets[...]`). If a test needs a new
   value, add a key with a `<placeholder>` to `config/testbed.example.json`.
2. **One file per test**: `tests/<name>.py` with `HELP`, `add_args(parser)`
   and `run(cluster, args)`. `tb.py test <name>` finds it by itself.
3. **Host work goes into a module.** When a test needs a new action on a
   host, add a command to `90-hostctl` (or a new `NN-name` module) for BOTH
   Linux (`.sh`) and Windows (`.ps1`), with the same name and arguments.
4. **Restore in `finally`.** A test that stops a service, blocks a port or
   deletes a file must undo it even when a check fails.
5. **Record every check** with `Results.record(case, "PASS"|"FAIL", note=...)`
   and return `res.finish()`.
6. Run `python tools/secret_scan.py` before you push.

Skeleton:

```python
"""What the test proves, in two lines."""
from tblib.cluster import TbError, log
from tblib.results import Results
from ._common import add_load_args, converge_and_record, ensure_load

HELP = "one line for 'tb.py test list'"


def add_args(p):
    p.add_argument("--db", default="all")
    add_load_args(p)


def run(cl, a):
    dbs = cl.test_dbs(which=a.db)
    if not dbs:
        raise TbError("no test databases (run 'tb.py dbs prepare')")
    res = Results("my_test", vars(a).copy())
    try:
        ensure_load(cl, dbs, a, "my_test")
        # ... act, check, res.record(...)
    finally:
        cl.load_stop("my_test")
    converge_and_record(cl, res, "converge", [d["path"] for d in dbs], 900)
    return res.finish()
```

Useful calls on the cluster object `cl`:

| Call | Does |
|---|---|
| `cl.api(host, "GET", "/v1/transfer")` | node API on that host; returns `(status, body)` |
| `cl.hostctl(host, "counts", {"db": path})` | a `90-hostctl` command; returns its result |
| `cl.test_dbs(which="db1")` | master records of the test databases |
| `cl.replica_path(replica, master_path)` | where the replica keeps its copy |
| `cl.load_start(paths, mode, tx, ...)` / `cl.load_stop(tag)` | fb-loadgen on the master |
| `cl.wait_converged(paths, timeout)` | poll row counts until the replicas match |
| `converge_and_record(cl, res, case, paths, timeout)` | rows, then no active transactions left in the replica control files |
| `ops.reinit(cl, db_id, replica, mode)` | reinit and wait for the operation |
