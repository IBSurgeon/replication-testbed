"""The replconf.hqbird file of HQbird 2.5/3.0 (hqcluster-node
internal/replconf/codec.go): enough to read the valid date of a file and to
write a file as DataGuard does. The data line holds the replica's
user:password: never log a decoded record, only dates and counts."""
import os
import struct

HEADER = "HQBird replication configuration "
MARKER = "HQBirdReplicationConf"
CRLF = "\r\n"
KEY = (0x370E4825, 0x90ABCDEF, 0x12345678, 0xA6D20EE8)
DELTA = 0x9E3779B9
M = 0xFFFFFFFF


def _sar(v, n):
    # The plugin works on signed int: an arithmetic right shift.
    if v & 0x80000000:
        v -= 1 << 32
    return (v >> n) & M


def _decrypt(b):
    v0, v1 = struct.unpack(">II", b)
    s = (DELTA << 5) & M
    for _ in range(32):
        v1 = (v1 - ((((v0 << 4) + KEY[2]) & M) ^ ((v0 + s) & M) ^ ((_sar(v0, 5) + KEY[3]) & M))) & M
        v0 = (v0 - ((((v1 << 4) + KEY[0]) & M) ^ ((v1 + s) & M) ^ ((_sar(v1, 5) + KEY[1]) & M))) & M
        s = (s - DELTA) & M
    return struct.pack(">II", v0, v1)


def _encrypt(b):
    v0, v1 = struct.unpack(">II", b)
    s = 0
    for _ in range(32):
        s = (s + DELTA) & M
        v0 = (v0 + ((((v1 << 4) + KEY[0]) & M) ^ ((v1 + s) & M) ^ ((_sar(v1, 5) + KEY[1]) & M))) & M
        v1 = (v1 + ((((v0 << 4) + KEY[2]) & M) ^ ((v0 + s) & M) ^ ((_sar(v0, 5) + KEY[3]) & M))) & M
    return struct.pack(">II", v0, v1)


def decode(data):
    """{format, reg_name, valid_till, records} of a file, or {error}."""
    lines = data.decode("latin-1").split("\n")
    out = {"format": "", "reg_name": "", "valid_till": "", "records": 0}
    first = lines[0].strip() if lines else ""
    if first not in (HEADER + "v1.2", HEADER + "v2.0"):
        return {"error": f"unknown version line {first[:60]!r}"}
    out["format"] = first[len(HEADER):]
    for line in lines[1:]:
        token, _, value = line.strip().partition("=")
        if token == "RegName":
            out["reg_name"] = value.strip()
        elif token == "RCIs":
            raw = bytes.fromhex(value.strip()[: len(value.strip()) // 2 * 2])
            if out["format"] == "v1.2":
                if not raw or len(raw) % 8:
                    return {"error": "the data length is not a multiple of 8"}
                raw = b"".join(_decrypt(raw[i:i + 8]) for i in range(0, len(raw), 8))
                raw = raw[raw[0]:]
            plain = raw.decode("latin-1")
            if not plain.endswith(MARKER):
                return {"error": "no check marker"}
            out["valid_till"] = plain[:10]
            out["records"] = sum(1 for r in plain[10:-len(MARKER)].replace("\r", "").split("\n\n")
                                 if any("=" in x for x in r.split("\n")))
    return out


def encode(valid_till, reg_name="HQBird empty project", fmt="v1.2"):
    """A file without records, laid out as DataGuard writes it."""
    data = (valid_till + CRLF + CRLF + MARKER).encode("latin-1")
    if fmt == "v1.2":
        k = 1
        if (len(data) + 1) % 8:
            k = 8 - (len(data) + 1) % 8 + 1
        buf = bytes([k]) + os.urandom(k - 1) + data
        data = b"".join(_encrypt(buf[i:i + 8]) for i in range(0, len(buf), 8))
    text = (HEADER + fmt + CRLF + "RegName=" + reg_name + CRLF + "ValidTill=" + valid_till + CRLF
            + "RCIs=" + data.hex().upper() + CRLF)
    return text.encode("latin-1")
