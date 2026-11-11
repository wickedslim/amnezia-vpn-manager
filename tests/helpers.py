import base64
import json
import os
import subprocess
import sys
import shlex
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from manager.domain import ManagerError


def keypair():
    key = X25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(private).decode(), base64.b64encode(public).decode()


def fixture(root: Path, names=("office401", "office401")):
    root.mkdir(parents=True, exist_ok=True)
    private, public = keypair()
    config = f"[Interface]\nPrivateKey = {private}\nAddress = 10.30.0.1/24\nListenPort = 51820\nJc = 4\nS3 = 25\nI1 = <b 0x00>\n"
    runtime = {"public": public, "peers": {}, "params": {"jc": "4", "s3": "25", "i1": "<b 0x00>"}}
    table = []
    for i, name in enumerate(names, 2):
        _, peer = keypair()
        config += f"[Peer]\n# {name}\nPublicKey = {peer}\nAllowedIPs = 10.30.0.{i}/32\n"
        runtime["peers"][peer] = {"rx": 100, "tx": 200, "handshake": 0, "ips": f"10.30.0.{i}/32", "psk": "(none)"}
        table.append({"clientId": peer, "userData": {"clientName": name}})
    (root / "wg0.conf").write_text(config)
    (root / "clientsTable").write_text(json.dumps(table))
    (root / "runtime.json").write_text(json.dumps(runtime))
    binary = root / "bin"
    binary.mkdir(exist_ok=True)
    fake = binary / "awg"
    # A shebang cannot contain an interpreter path with spaces (common on macOS).
    program = binary / "fake_awg.py"
    program.write_text(FAKE_WG)
    fake.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(program))} \"$@\"\n")
    fake.chmod(0o755)
    return public


FAKE_WG = r'''
import base64, json, os, sys
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives import serialization
root = Path(os.environ["FAKE_VPN_ROOT"])
args = sys.argv[1:]
state = json.loads((root / "runtime.json").read_text())
with (root / "commands").open("a") as stream:
    stream.write(" ".join(args) + "\n")
if args[0] in {"genkey", "genpsk"}:
    print(base64.b64encode(os.urandom(32)).decode())
elif args[0] == "pubkey":
    key = X25519PrivateKey.from_private_bytes(base64.b64decode(sys.stdin.read().strip()))
    print(base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode())
elif args[:3] == ["show", "all", "dump"]:
    print("wg0\tPRIVATE\t" + state["public"] + "\t51820\t4\t40\t70\t0\t0\t1\t2\t3\t4\toff")
    for key, p in state["peers"].items():
        print("\t".join(map(str,["wg0",key,p.get("psk","(none)"),"(none)",p["ips"],p["handshake"],p["rx"],p["tx"],0])))
elif args[0] == "set":
    if (root / "fail_once").exists():
        (root / "fail_once").unlink()
        sys.exit(2)
    # Like AmneziaWG, every runtime change resets obfuscation parameters unless they are passed along.
    at = args.index("peer") if "peer" in args else len(args)
    state["params"] = {args[i]: args[i + 1] for i in range(2, at, 2)}
    if at < len(args):
        o = at - 2
        key = args[3 + o]
        if args[4 + o] != "remove" and (root / "fail_add_once").exists():
            (root / "fail_add_once").unlink()
            sys.exit(2)
        if args[4 + o] == "remove":
            state["peers"].pop(key, None)
        else:
            state["peers"][key] = {"psk": Path(args[5 + o]).read_text(), "ips": args[7 + o], "rx":0,"tx":0,"handshake":0}
    (root / "runtime.json").write_text(json.dumps(state))
elif args[0] == "addconf":
    values = {}
    for line in Path(args[2]).read_text().splitlines():
        if "=" in line:
            k,v = line.split("=",1)
            values[k.strip()] = v.strip()
    state["peers"][values["PublicKey"]] = {"ips":values["AllowedIPs"],"psk":values.get("PresharedKey","(none)"),"rx":0,"tx":0,"handshake":0}
    state["params"] = {}
    (root / "runtime.json").write_text(json.dumps(state))
else:
    sys.exit(3)
'''


class FakeRunner:
    def __init__(self, server, data_dir):
        self.server = server
        self.root = Path(server.config_path).parent
        self.closed = False

    def docker(self, script, data=None):
        # Execute the actual generated POSIX shell transaction against a fake VPN binary.
        script = script.replace("/opt/amnezia/awg/clientsTable", str(self.root / "clientsTable"))
        env = {**os.environ, "PATH": str(self.root / "bin") + os.pathsep + os.environ["PATH"], "FAKE_VPN_ROOT": str(self.root)}
        result = subprocess.run(["sh", "-c", script], input=data, capture_output=True, env=env, timeout=10)
        if result.returncode:
            if b"MANAGER_STALE" in result.stderr:
                raise ManagerError("Конфигурация изменилась другим приложением.")
            raise ManagerError("Ошибка тестового VPN")
        return result.stdout.decode()

    def close(self):
        self.closed = True
