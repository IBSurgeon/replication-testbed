"""Offline self-check (no hosts, no goafts): python tools/selftest_csr.py

Checks of ops.approve_pending: exact name, source address, age, one per host."""
import os
import sys
import time
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tblib import ops  # noqa: E402

now = time.time()
iso = lambda t: datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
PENDING = [
    {"request_id": "r-prefix", "hostname": "tb-master2", "source_ip": "192.0.2.10", "created_at": iso(now)},
    {"request_id": "r-good", "hostname": "tb-master", "source_ip": "192.0.2.10", "created_at": iso(now)},
    {"request_id": "r-wrongip", "hostname": "tb-replica1", "source_ip": "198.51.100.7", "created_at": iso(now)},
    {"request_id": "r-old", "hostname": "tb-replica2", "source_ip": "192.0.2.12", "created_at": iso(now - 3600)},
    {"request_id": "r-dup1", "hostname": "tb-replica3", "source_ip": "192.0.2.13", "created_at": iso(now)},
    {"request_id": "r-dup2", "hostname": "tb-replica3", "source_ip": "192.0.2.13", "created_at": iso(now)},
]
calls = []


def fake_admin(cl, method, path, body=None):
    if method == "GET":
        return 200, {"items": PENDING}
    calls.append(path)
    return 200, {}


ops.admin_call = fake_admin
expect = {
    "master": {"hostnames": {"tb-master"}, "ips": {"192.0.2.10"}, "since": now - 10},
    "replica1": {"hostnames": {"tb-replica1"}, "ips": {"192.0.2.11"}, "since": now - 10},
    "replica2": {"hostnames": {"tb-replica2"}, "ips": {"192.0.2.12"}, "since": now - 10},
    "replica3": {"hostnames": {"tb-replica3"}, "ips": {"192.0.2.13"}, "since": now - 10},
}
approved, warned = set(), set()
n = ops.approve_pending(None, expect, approved, warned)
print("approved:", calls, approved)
assert calls == ["/v1/admin/csr-requests/r-good/approve"], calls
assert approved == {"master"}
# second poll: nothing new is approved, warnings are not repeated
n2 = ops.approve_pending(None, expect, approved, warned)
assert n2 == 0 and len(calls) == 1
print("CSR approval check: PASS")
