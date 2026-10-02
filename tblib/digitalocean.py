"""DigitalOcean hosts for the test bed: create, list, destroy droplets.

Everything account-specific (token file, ssh key fingerprint, region, VPC)
comes from the "digitalocean" section of the local config. Droplets carry
the test bed tag; destroy deletes ONLY droplets with that tag.

create also writes the hosts' "ssh" (root@<public IP>) and "addr" (private
VPC IP) into the local config file.
"""
import json
import os
import socket
import time
import urllib.error
import urllib.request

from . import config as C
from .cluster import TbError, log

API = "https://api.digitalocean.com/v2"


class DO:
    def __init__(self, cfg):
        self.cfg = cfg
        d = cfg.raw.get("digitalocean") or {}
        if not d:
            raise TbError("no 'digitalocean' section in the local config")
        self.d = d
        tf = os.path.expanduser(d.get("token_file", ""))
        if not tf or not os.path.isfile(tf):
            raise TbError(f"digitalocean.token_file not found: {tf}")
        with open(tf, encoding="utf-8") as f:
            self.token = f.read().strip()
        self.tag = d.get("tag", "replication-testbed")
        self.prefix = d.get("name_prefix", "tb")

    def call(self, method, path, body=None, ok=(200, 201, 202, 204)):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code in ok:
                return {}
            raise TbError(f"DigitalOcean {method} {path}: HTTP {e.code} {e.read()[:300]!r}")

    def droplets(self):
        return self.call("GET", f"/droplets?tag_name={self.tag}&per_page=200").get("droplets", [])

    def names(self):
        return {h: f"{self.prefix}-{h}" for h in self.cfg.node_hosts() +
                ([self.cfg.rcm_host] if self.cfg.rcm_enabled and self.cfg.rcm_host not in self.cfg.node_hosts() else [])}

    @staticmethod
    def ip(drop, kind):
        for n in drop.get("networks", {}).get("v4", []):
            if n.get("type") == kind:
                return n.get("ip_address")
        return None

    def vpc(self):
        if self.d.get("vpc_uuid"):
            return self.d["vpc_uuid"]
        for v in self.call("GET", "/vpcs?per_page=200").get("vpcs", []):
            if v.get("region") == self.d["region"] and v.get("default"):
                return v["id"]
        raise TbError(f"no default VPC in region {self.d['region']}; set digitalocean.vpc_uuid")

    def operator_ip(self):
        ip = self.d.get("operator_ip", "auto")
        if ip and ip != "auto":
            return ip
        with urllib.request.urlopen("https://api.ipify.org", timeout=15) as r:
            return r.read().decode().strip()

    def firewall(self, droplet_ids):
        name = self.d.get("firewall_name", "replication-testbed")
        ports = sorted({str(self.cfg.host(h)["node_port"]) for h in self.cfg.node_hosts()} |
                       {str(self.cfg.host(h)["companion"]["node_port"]) for h in self.cfg.companion_hosts()} |
                       {"7443"})
        # operator_ports: more ports open to the operator IP only, e.g. an
        # HTTPS proxy in front of the RCM web UI (which listens on loopback).
        src = {"addresses": [self.operator_ip() + "/32"]}
        inbound = [{"protocol": "tcp", "ports": p, "sources": src}
                   for p in ["22"] + [str(x) for x in self.d.get("operator_ports", [])]]
        inbound += [{"protocol": "tcp", "ports": p, "sources": {"tags": [self.tag]}} for p in ports]
        outbound = [{"protocol": pr, "ports": "all" if pr != "icmp" else "0",
                     "destinations": {"addresses": ["0.0.0.0/0", "::/0"]}} for pr in ("tcp", "udp", "icmp")]
        for o in outbound:
            if o["protocol"] == "icmp":
                o.pop("ports")
        body = {"name": name, "tags": [self.tag], "inbound_rules": inbound, "outbound_rules": outbound}
        fws = [f for f in self.call("GET", "/firewalls?per_page=200").get("firewalls", []) if f["name"] == name]
        if fws:
            self.call("PUT", f"/firewalls/{fws[0]['id']}", body)
            log(f"DO firewall {name}: updated (ssh from operator IP, node ports within tag {self.tag})")
        else:
            self.call("POST", "/firewalls", body)
            log(f"DO firewall {name}: created")

    def create(self):
        d = self.d
        for k in ("region", "size", "image", "ssh_key_fingerprint"):
            if not d.get(k) or "<" in str(d.get(k)):
                raise TbError(f"set digitalocean.{k} in the local config")
        names = self.names()
        have = {x["name"]: x for x in self.droplets()}
        missing = [n for n in names.values() if n not in have]
        if missing:
            log(f"DO: create {', '.join(missing)} ({d['size']}, {d['image']}, {d['region']})")
            self.call("POST", "/droplets", {
                "names": missing, "region": d["region"], "size": d["size"], "image": d["image"],
                "ssh_keys": [d["ssh_key_fingerprint"]], "vpc_uuid": self.vpc(), "tags": [self.tag],
                "monitoring": False, "ipv6": False})
        else:
            log("DO: all droplets exist")
        end = time.time() + 600
        while True:
            have = {x["name"]: x for x in self.droplets()}
            ready = [n for n in names.values() if n in have and have[n]["status"] == "active"
                     and self.ip(have[n], "public") and self.ip(have[n], "private")]
            if len(ready) == len(names):
                break
            if time.time() > end:
                raise TbError("droplets not active with IPs after 10 min")
            time.sleep(10)
        self.firewall([have[n]["id"] for n in names.values()])
        self.write_hosts({h: have[n] for h, n in names.items()})
        for h, n in names.items():
            self.wait_ssh(self.ip(have[n], "public"))
        log("DO: droplets ready")

    def wait_ssh(self, ip, timeout=300):
        end = time.time() + timeout
        while time.time() < end:
            try:
                with socket.create_connection((ip, 22), timeout=5):
                    return
            except OSError:
                time.sleep(5)
        raise TbError(f"ssh port on {ip} not open after {timeout}s")

    def write_hosts(self, drops):
        """Put public/private IPs of the droplets into the local config file."""
        path = self.cfg.path
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        hosts = raw.setdefault("hosts", {})
        st = C.load_state()
        for h, drop in drops.items():
            e = hosts.setdefault(h, {})
            e["os"] = "linux"
            e["ssh"] = f"root@{self.ip(drop, 'public')}"
            e["addr"] = self.ip(drop, "private")
            st.setdefault("hosts", {}).setdefault(h, {})["do_droplet_id"] = drop["id"]
            log(f"  {h:10} {drop['name']:20} public {self.ip(drop, 'public'):16} private {self.ip(drop, 'private')}")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2)
        os.replace(tmp, path)
        C.save_state(st)

    def list(self):
        for x in self.droplets():
            log(f"  {x['id']}  {x['name']:20} {x['status']:8} public {self.ip(x, 'public')} "
                f"private {self.ip(x, 'private')} {x['size_slug']} {x['region']['slug']}")

    def destroy(self):
        drops = self.droplets()
        if not drops:
            log(f"DO: no droplets with tag {self.tag}")
            return
        for x in drops:
            log(f"DO: delete {x['name']} ({x['id']})")
        self.call("DELETE", f"/droplets?tag_name={self.tag}")
        st = C.load_state()
        for h in st.get("hosts", {}).values():
            h.pop("do_droplet_id", None)
            h.pop("installed", None)
        C.save_state(st)
