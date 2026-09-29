"""Test bed configuration: load the local JSON file, fill OS defaults, validate.

Real hosts and secrets come only from the local file (default
config/testbed.local.json, git-ignored). The repository holds only
config/testbed.example.json with placeholders.
"""
import copy
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(ROOT, "config", "testbed.local.json")
# Absolute: builds run in other folders (fb-loadgen) and write here.
STATE_DIR = os.path.abspath(os.environ.get("TB_STATE_DIR") or os.path.join(ROOT, "state"))

PLACEHOLDER = re.compile(r"<[^<>]+>")


class ConfigError(Exception):
    pass


# Port 0 means "find out on the host": the Firebird port from RemoteServicePort
# in firebird.conf (3050 when not set), the fbagent port from local_api.listen
# of an existing agent (13055, fbagent's own default, when not set).
# firebird.engine: "2.5", "3.0", "4" or "5"; empty = the node finds it.
LINUX_DEFAULTS = {
    "firebird": {"root": "/opt/firebird", "port": 0, "service": "", "engine": ""},
    "fbagent": {"mode": "install", "dir": "/opt/hqbird-fbagent", "port": 0,
                "instance_id": "", "service": "hqbirdfbagent"},
    "paths": {"work": "/opt/hqtb", "node": "/opt/hqclusternode",
              "rcm": "/opt/hqbirdrcm", "db_root": ""},
}

WINDOWS_DEFAULTS = {
    "firebird": {"root": "C:\\HQbird\\Firebird50", "port": 0, "service": "", "engine": "",
                 "copy_of": ""},     # a second instance: copy of this root (06-instance)
    "fbagent": {"mode": "existing", "dir": "C:\\hqtb\\fbagent", "port": 0,
                "instance_id": "", "service": "HQbirdFBAgent"},
    "paths": {"work": "C:\\hqtb", "node": "C:\\hqclusternode",
              "rcm": "C:\\hqbirdrcm", "db_root": ""},
}


def _merge(defaults, given):
    out = copy.deepcopy(defaults)
    for k, v in (given or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif v not in ("", None):
            out[k] = v
    return out


def _strip_comments(obj):
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


class Config:
    def __init__(self, data, path):
        self.path = path
        self.raw = _strip_comments(data)
        c = self.raw.get("cluster", {})
        self.master = c.get("master", "master")
        self.replicas = list(c.get("replicas", []))
        self.rcm_enabled = bool(c.get("rcm_enabled", True))
        self.rcm_host = c.get("rcm_host", self.master)
        self.secrets = self.raw.get("secrets", {})
        self.artifacts = self.raw.get("artifacts", {})
        self.goafts = self.raw.get("goafts", {})
        self.loadgen = self.raw.get("loadgen", {})
        self.ssh = _merge({"ssh_bin": "ssh", "scp_bin": "scp", "key": "",
                           "proxy_command": "", "connect_timeout": 15},
                          self.raw.get("ssh", {}))
        self.dbs = _merge({"subdir": "tb", "count": 2, "file_name": "employee.fdb",
                           "source": ""}, self.raw.get("dbs", {}))
        self.limits = self.raw.get("limits", {})
        self.windows = _merge({"master_restart_window": "always",
                               "replica_restart_window": "always",
                               "transfer_window": "always"},
                              self.raw.get("windows", {}))
        self.hosts = {}
        for name, h in self.raw.get("hosts", {}).items():
            self.hosts[name] = self._host(name, h)

    def _host(self, name, h):
        os_name = h.get("os", "linux").lower()
        if os_name not in ("linux", "windows"):
            raise ConfigError(f"hosts.{name}.os must be linux or windows")
        base = LINUX_DEFAULTS if os_name == "linux" else WINDOWS_DEFAULTS
        out = {"name": name, "os": os_name, "ssh": h.get("ssh", ""),
               "ssh_port": int(h.get("ssh_port", 22)), "sudo": bool(h.get("sudo", False)),
               "addr": h.get("addr", ""), "node_id": h.get("node_id", f"tb-{name}"),
               "node_port": int(h.get("node_port", 7051))}
        for section in ("firebird", "fbagent", "paths"):
            out[section] = _merge(base[section], h.get(section, {}))
        role = "master" if name == self.master else "replica"
        out["role"] = role
        sep = "/" if os_name == "linux" else "\\"
        if not out["paths"]["db_root"]:
            if os_name == "linux":
                out["paths"]["db_root"] = f"/databases/{role}"
            else:
                out["paths"]["db_root"] = out["paths"]["work"] + f"\\databases\\{role}"
        out["sep"] = sep
        return out

    # --- helpers -------------------------------------------------------------
    def host(self, name):
        if name not in self.hosts:
            raise ConfigError(f"unknown host '{name}' (known: {', '.join(self.hosts)})")
        return self.hosts[name]

    def node_hosts(self):
        return [self.master] + self.replicas

    def all_hosts(self):
        names = self.node_hosts()
        if self.rcm_enabled and self.rcm_host not in names:
            names.append(self.rcm_host)
        return names

    def select(self, spec):
        """'all' | 'master' | 'replicas' | comma list of host names."""
        if not spec or spec == "all":
            return self.all_hosts()
        if spec == "replicas":
            return list(self.replicas)
        out = []
        for part in spec.split(","):
            part = part.strip()
            if part == "replicas":
                out.extend(self.replicas)
            elif part:
                self.host(part)
                out.append(part)
        return out

    def validate(self, need=()):
        """Fail when a used value still holds a <placeholder>."""
        problems = []

        def walk(prefix, obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    walk(f"{prefix}.{k}" if prefix else k, v)
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    walk(f"{prefix}[{i}]", v)
            elif isinstance(obj, str) and PLACEHOLDER.search(obj):
                problems.append(prefix)

        for name in self.all_hosts():
            if name not in self.raw.get("hosts", {}):
                problems.append(f"hosts.{name} (missing)")
            else:
                walk(f"hosts.{name}", self.raw["hosts"][name])
        walk("secrets.firebird_password", self.secrets.get("firebird_password", ""))
        for section in need:
            walk(section, self.raw.get(section, {}))
        if problems:
            raise ConfigError("fill in these values in " + self.path + ":\n  " +
                              "\n  ".join(problems))


def load(path=None):
    path = path or os.environ.get("TB_CONFIG") or DEFAULT_CONFIG
    if not os.path.exists(path):
        raise ConfigError(f"no config file {path}. Copy config/testbed.example.json "
                          f"to config/testbed.local.json and fill it in.")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return Config(data, path)


def state_path(*parts):
    p = os.path.join(STATE_DIR, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def load_state():
    p = state_path("state.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(st):
    p = state_path("state.json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, sort_keys=True)
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
