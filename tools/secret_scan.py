#!/usr/bin/env python3
"""Refuse to publish secrets or concrete lab addresses.

Scans the files git would commit (tracked + staged + untracked, not ignored)
for:
  - every value of the local config that names a host, user, URL, pin or
    secret (config/testbed.local.json, if present);
  - IPv4 addresses other than 0.0.0.0 / 127.0.0.1;
  - private keys, GitHub/DigitalOcean tokens, "password": "<real value>".

  python tools/secret_scan.py            exit 0 = clean, 1 = findings
  git config core.hooksPath tools/hooks  run it before every push
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL = os.path.join(ROOT, "config", "testbed.local.json")

PATTERNS = [
    ("ipv4", re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("github token", re.compile(r"\bgh[opsu]_[A-Za-z0-9]{20,}")),
    ("digitalocean token", re.compile(r"\bdop_v1_[a-f0-9]{20,}")),
    ("password value", re.compile(r"""(?i)"(?:password|pass|token|firebird_password)"\s*:\s*"([^"<>]{3,})\"""")),
    ("masterkey", re.compile(r"(?i)\bmasterkey\b")),
]
ALLOWED_IPS = {"0.0.0.0", "127.0.0.1"}
GENERIC_USERS = {"root", "admin", "administrator", "ubuntu", "user", "firebird", "sysdba"}
SKIP_SUFFIX = (".png", ".jpg", ".gif", ".ico", ".zip", ".exe")


def local_values():
    """Strings from the local config that must never be published."""
    if not os.path.exists(LOCAL):
        return set()
    with open(LOCAL, encoding="utf-8") as f:
        cfg = json.load(f)
    out = set()
    sensitive = ("ssh", "addr", "url", "pin", "password", "token", "key", "client_cert",
                 "client_key", "user", "git_url", "local_src", "dir", "proxy_command")

    def walk(obj, key=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if not k.startswith("_"):
                    walk(v, k)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, key)
        elif isinstance(obj, str) and any(s in key for s in sensitive):
            v = obj.strip()
            if len(v) >= 4 and "<" not in v and v.lower() not in ("sysdba", "local", "stable", "direct", "main"):
                out.add(v)
                if "@" in v:
                    # user@host: the host always counts; the user only when
                    # it is not a generic account name.
                    user, _, hostpart = v.partition("@")
                    out.add(hostpart)
                    if len(user) >= 4 and user.lower() not in GENERIC_USERS:
                        out.add(user)

    walk(cfg.get("secrets", {}), "password")
    walk(cfg.get("hosts", {}))
    walk(cfg.get("goafts", {}))
    walk(cfg.get("ssh", {}))
    walk(cfg.get("loadgen", {}))
    walk(cfg.get("digitalocean", {}))
    walk(cfg.get("artifacts", {}).get("dir", ""), "dir")
    return out


def files():
    r = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                       cwd=ROOT, capture_output=True, text=True, check=True)
    return [f for f in r.stdout.split("\0") if f and not f.endswith(SKIP_SUFFIX)]


def main():
    values = local_values()
    findings = []
    for rel in files():
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path) or rel.startswith("tools/secret_scan.py"):
            continue
        try:
            text = open(path, encoding="utf-8").read()
        except UnicodeDecodeError:
            findings.append((rel, 0, "binary file", ""))
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for name, rx in PATTERNS:
                for m in rx.finditer(line):
                    if name == "ipv4" and m.group(0) in ALLOWED_IPS:
                        continue
                    findings.append((rel, n, name, m.group(0)[:40]))
            for v in values:
                if v in line:
                    findings.append((rel, n, "local config value", v[:4] + "..."))
    for rel, n, name, what in findings:
        print(f"{rel}:{n}: {name}: {what}")
    if findings:
        print(f"\n{len(findings)} finding(s). Move these values to config/testbed.local.json.")
        return 1
    print(f"clean ({len(values)} local values checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
