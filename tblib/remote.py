"""Run module scripts on test bed hosts over ssh (or locally).

Linux hosts run  modules/linux/<script>.sh  with bash.
Windows hosts run modules/windows/<script>.ps1 with Windows PowerShell 5.1.
The module folder and the secrets file are copied to <paths.work> first.
"""
import base64
import os
import shlex
import shutil
import subprocess
import tempfile

from .config import ROOT


class RemoteError(Exception):
    def __init__(self, host, cmd, rc, out, err):
        self.host, self.cmd, self.rc, self.out, self.err = host, cmd, rc, out, err
        tail = (err or out or "").strip().splitlines()[-15:]
        super().__init__(f"[{host}] exit {rc}: {cmd}\n  " + "\n  ".join(tail))


def b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class Host:
    def __init__(self, cfg, name, log=print):
        self.cfg = cfg
        self.h = cfg.host(name)
        self.name = name
        self.os = self.h["os"]
        self.local = self.h["ssh"] == "local"
        self.log = log
        self.work = self.h["paths"]["work"]

    # --- low level -----------------------------------------------------------
    def _ssh_base(self):
        s = self.cfg.ssh
        cmd = [s["ssh_bin"], "-p", str(self.h["ssh_port"]),
               "-o", "BatchMode=yes",
               "-o", f"ConnectTimeout={s['connect_timeout']}",
               "-o", "ServerAliveInterval=30",
               "-o", "StrictHostKeyChecking=accept-new"]
        if s.get("key"):
            cmd += ["-i", os.path.expanduser(s["key"])]
        if s.get("proxy_command"):
            cmd += ["-o", "ProxyCommand=" + s["proxy_command"]]
        return cmd

    def _scp_base(self):
        s = self.cfg.ssh
        cmd = [s["scp_bin"], "-q", "-P", str(self.h["ssh_port"]),
               "-o", "BatchMode=yes",
               "-o", f"ConnectTimeout={s['connect_timeout']}",
               "-o", "StrictHostKeyChecking=accept-new"]
        if s.get("key"):
            cmd += ["-i", os.path.expanduser(s["key"])]
        if s.get("proxy_command"):
            cmd += ["-o", "ProxyCommand=" + s["proxy_command"]]
        return cmd

    def _remote_path(self, p):
        # Windows OpenSSH scp takes C:/dir/file.
        return p.replace("\\", "/") if self.os == "windows" else p

    def join(self, *parts):
        sep = self.h["sep"]
        out = parts[0].rstrip("/\\")
        for p in parts[1:]:
            out += sep + p.strip("/\\")
        return out

    def run_raw(self, command, check=True, timeout=None, stream=False, sudo=True):
        """command: a shell string for Linux, or an argv list (quoted here).
        sudo=False: do not prefix 'sudo -n' (the command handles it itself)."""
        if self.local:
            if isinstance(command, str):
                argv = ["bash", "-c", command] if self.os == "linux" else ["cmd", "/c", command]
            else:
                argv = command
        else:
            if isinstance(command, list):
                command = self._quote(command)
            if self.os == "linux" and self.h["sudo"] and sudo:
                command = "sudo -n " + command
            argv = self._ssh_base() + [self.h["ssh"], command]
        return self._exec(argv, check, timeout, stream, command)

    def _quote(self, argv):
        if self.os == "linux":
            return " ".join(shlex.quote(a) for a in argv)
        out = []
        for a in argv:
            a = str(a)
            if '"' in a:
                raise ValueError("double quote in a Windows argument; pass it base64-encoded")
            out.append(f'"{a}"' if (not a or any(c in a for c in " \t&|<>^()")) else a)
        return " ".join(out)

    def _exec(self, argv, check, timeout, stream, shown):
        if stream:
            p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                 errors="replace")
            lines = []
            for line in p.stdout:
                line = line.rstrip("\r\n")
                lines.append(line)
                if line.startswith("TBSECRET "):
                    line = "TBSECRET <hidden>"
                elif line.startswith("TBRESULT ") and len(line) > 400:
                    line = line[:400] + " ..."
                self.log(f"  [{self.name}] {line}")
            rc = p.wait(timeout=timeout)
            out, err = "\n".join(lines), ""
        else:
            p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
            rc, out, err = p.returncode, p.stdout, p.stderr
        if check and rc != 0:
            raise RemoteError(self.name, _short(shown), rc, out, err)
        return rc, out, err

    def put(self, local, remote):
        if self.local:
            if os.path.isdir(local):
                shutil.copytree(local, remote, dirs_exist_ok=True)
            else:
                os.makedirs(os.path.dirname(remote) or ".", exist_ok=True)
                shutil.copy2(local, remote)
            return
        argv = self._scp_base()
        if os.path.isdir(local):
            argv.append("-r")
        argv += [local, f"{self.h['ssh']}:{self._remote_path(remote)}"]
        self._exec(argv, True, 1800, False, f"scp {os.path.basename(local)} -> {remote}")

    def get(self, remote, local):
        if self.local:
            if os.path.isdir(remote):
                shutil.copytree(remote, local, dirs_exist_ok=True)
            else:
                shutil.copy2(remote, local)
            return
        argv = self._scp_base() + ["-r", f"{self.h['ssh']}:{self._remote_path(remote)}", local]
        self._exec(argv, True, 1800, False, f"scp {remote} -> {local}")

    def ssh_user(self):
        return self.h["ssh"].split("@")[0] if "@" in self.h["ssh"] else ""

    def mkdir(self, path):
        if self.os == "linux" and self.h["sudo"] and not self.local:
            # scp writes as the ssh user: the folder must be theirs.
            q = shlex.quote(path)
            self.run_raw(f"sudo -n mkdir -p {q} && sudo -n chown \"$(id -un)\" {q}", sudo=False)
        elif self.os == "linux":
            self.run_raw(["mkdir", "-p", path])
        elif self.local:
            os.makedirs(path, exist_ok=True)
        else:
            self.run_raw(["powershell", "-NoProfile", "-Command",
                          f"New-Item -ItemType Directory -Force -Path '{path}' | Out-Null"])

    # --- modules -------------------------------------------------------------
    def sync_modules(self):
        """Copy modules/<os>/ to <work>/modules on the host."""
        src = os.path.join(ROOT, "modules", self.os)
        dest = self.join(self.work, "modules")
        self.mkdir(dest)
        if self.local:
            shutil.copytree(src, dest, dirs_exist_ok=True)
            return
        # scp -r copies the folder itself; send the files one level down.
        argv = self._scp_base() + [os.path.join(src, f) for f in sorted(os.listdir(src))
                                   if not f.startswith(".")]
        argv.append(f"{self.h['ssh']}:{self._remote_path(dest)}/")
        self._exec(argv, True, 600, False, f"scp modules -> {dest}")

    def push_secrets(self, values):
        """Write KEY=VALUE lines to <work>/secrets.env, readable by the admin only."""
        text = "".join(f"{k}={v}\n" for k, v in values.items())
        fd, tmp = tempfile.mkstemp(prefix="tb-secrets-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            self.mkdir(self.work)
            dest = self.join(self.work, "secrets.env")
            if self.os == "linux" and self.h["sudo"] and not self.local:
                self.run_raw(["rm", "-f", dest])      # root-owned from the last run
            self.put(tmp, dest)
        finally:
            os.remove(tmp)
        self.module("90-hostctl", ["secure-file", dest])

    def module(self, script, args=(), check=True, timeout=None, stream=True):
        """Run one module script with argv-style args."""
        args = [str(a) for a in args]
        if self.os == "linux":
            path = self.join(self.work, "modules", script + ".sh")
            argv = ["bash", path] + args
        else:
            path = self.join(self.work, "modules", script + ".ps1")
            argv = ["powershell", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", path] + args
        return self.run_raw(argv, check=check, timeout=timeout, stream=stream)


def _short(cmd):
    s = cmd if isinstance(cmd, str) else " ".join(map(str, cmd))
    return s if len(s) < 300 else s[:300] + " ..."
