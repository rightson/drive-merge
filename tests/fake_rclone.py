"""A fake `rclone` executable for the Setup tests: `config create/update --non-interactive` as a state machine
(verified against rclone v1.71.1), `listremotes --long [--json]`, `config delete`, `version` and `lsjson`.

    fake = FakeRclone(tmp_path)
    fake.binary        # path of the executable; pass it as the rclone binary
    fake.config        # the rclone config file it keeps (an ini file of [name] sections)
    fake.finish_browser()   # the person clicked Allow: a waiting sign-in completes
    fake.calls()       # every invocation as a list of argv, answers included (so tests can check where they went)

Behaviour that tests switch with files in `<config>.ctl/` is exposed as methods: `set_files`, `fail_lsjson`.
The sign-in step prints rclone's real stderr lines (including the auth URL on 127.0.0.1:53682) and then blocks
until `finish_browser()` or `deny_browser()` is called, or until it is killed.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

SCRIPT = r'''#!{python}
import json, os, sys, time

argv = sys.argv[1:]
cfg = None
rest = []
i = 0
while i < len(argv):
    if argv[i] == "--config":
        cfg = argv[i + 1]
        i += 2
        continue
    rest.append(argv[i])
    i += 1
if cfg is None:
    cfg = os.environ["FAKE_RCLONE_CONFIG"]
ctl = cfg + ".ctl"
os.makedirs(ctl, exist_ok=True)
with open(cfg + ".log", "a") as f:
    f.write(json.dumps(argv) + "\n")


def load():
    sections, cur = {}, None
    if os.path.exists(cfg):
        for line in open(cfg, encoding="utf-8"):
            line = line.rstrip("\n")
            if line.startswith("[") and line.endswith("]"):
                cur = line[1:-1]
                sections[cur] = {}
            elif " = " in line and cur is not None:
                k, v = line.split(" = ", 1)
                sections[cur][k] = v
    return sections


def save(sections):
    with open(cfg, "w", encoding="utf-8") as f:
        for name, kv in sections.items():
            f.write(f"[{name}]\n")
            for k, v in kv.items():
                f.write(f"{k} = {v}\n")
            f.write("\n")


def emit(state="", option=None, error=""):
    print(json.dumps({"State": state, "Option": option, "Error": error, "Result": ""}, indent="\t"))


def question(name, help, default="", examples=None, kind="string", exclusive=False, required=False, password=False):
    return {"Name": name, "FieldName": "", "Help": help, "Default": default, "Value": None,
            "Examples": examples, "Hide": 0, "Required": required, "IsPassword": password, "NoPrefix": False,
            "Advanced": False, "Exclusive": exclusive, "Sensitive": False, "DefaultStr": str(default),
            "ValueStr": str(default), "Type": kind}


def opt(args):
    out = {}
    for a in args:
        if "=" in a and not a.startswith("--"):
            k, v = a.split("=", 1)
            out[k] = v
    return out


def flag(args, name):
    return args[args.index(name) + 1]


def after_signin(name, sections):
    if sections[name]["type"] == "onedrive":
        emit("fake-onedrive-drive,,,", question(
            "config_driveid", "Select which drive to use.", "", exclusive=True, required=True, examples=[
                {"Value": "drive-personal", "Help": "OneDrive (personal)"},
                {"Value": "drive-docs", "Help": "Documents (business)"}]))
    else:
        emit("")


if not rest:
    sys.exit(0)
cmd = rest[0]

if cmd == "version":
    print("rclone " + os.environ.get("FAKE_RCLONE_VERSION", "v1.71.1"))
    print("- os/version: fake")
    sys.exit(0)

if cmd == "listremotes":
    if "--json" in rest and os.path.exists(ctl + "/no_json"):
        sys.stderr.write("Error: unknown flag: --json\n")
        sys.exit(1)
    sections = load()
    if "--json" in rest:
        print(json.dumps([{"name": n, "type": kv.get("type", ""), "source": "file", "description": ""}
                          for n, kv in sections.items()]))
    else:
        for n, kv in sections.items():
            print(f"{n}: {kv.get('type', '')}")
    sys.exit(0)

if cmd == "config" and rest[1] == "delete":
    sections = load()
    sections.pop(rest[2], None)
    save(sections)
    sys.exit(0)

if cmd == "config" and rest[1] == "create":
    name, rtype, args = rest[2], rest[3], rest[4:]
    options = opt(args[:])
    sections = load()
    if rtype == "iclouddrive" and not options.get("apple_id"):
        sys.stderr.write("NOTICE: Fatal error: a apple ID is required\n")
        sys.exit(1)
    kv = {"type": rtype}
    for k, v in options.items():
        kv[k] = "OBSCURED-" + v[::-1] if k == "password" else v      # rclone obscures passwords it is given
    sections[name] = kv
    save(sections)
    if os.path.exists(ctl + "/fail_create"):
        sys.stderr.write("CRITICAL: Failed to create remote: boom\n")
        sys.exit(1)
    if rtype == "iclouddrive":
        emit("fake-2fa,,,", question("config_2fa", "Enter the two-factor code sent to your devices.", "",
                                     required=True))
    elif rtype in ("drive", "dropbox", "onedrive"):
        state = "*oauth-islocal,teamdrive,," if rtype == "drive" else "*oauth-islocal,,,"
        emit(state, question("config_is_local", "Use web browser to automatically authenticate rclone with remote?",
                             True, kind="bool", exclusive=True,
                             examples=[{"Value": "true", "Help": "Yes"}, {"Value": "false", "Help": "No"}]))
    else:
        emit("")
    sys.exit(0)

if cmd == "config" and rest[1] == "update":
    name, args = rest[2], rest[3:]
    state, result = flag(args, "--state"), flag(args, "--result")
    sections = load()
    if name not in sections:
        sys.stderr.write("Error: remote not found\n")
        sys.exit(1)
    if os.path.exists(ctl + "/fail_continue"):
        sys.stderr.write("CRITICAL: Failed to configure: backend exploded\n")
        sys.exit(1)
    if state.startswith("*oauth-islocal"):
        if result == "true":
            for line in (
                'NOTICE: Make sure your Redirect URL is set to "http://localhost:53682/" in your custom config.',
                "ERROR : Failed to open browser automatically (exec: \"xdg-open\": executable file not found in "
                "$PATH) - please go to the following link: http://127.0.0.1:53682/auth?state=FAKESTATE123",
                "NOTICE: Log in and authorize rclone for access",
                "NOTICE: Waiting for code...",
            ):
                sys.stderr.write("2026/01/01 00:00:00 " + line + "\n")
                sys.stderr.flush()
            while not (os.path.exists(ctl + "/browser_done") or os.path.exists(ctl + "/browser_deny")):
                time.sleep(0.02)
            if os.path.exists(ctl + "/browser_deny"):
                sys.stderr.write("CRITICAL: Failed to configure token: access_denied\n")
                sys.exit(1)
            sections[name]["token"] = '{"access_token":"FAKE-ACCESS-TOKEN"}'
            save(sections)
            after_signin(name, sections)
        else:
            emit("*oauth-authorize,,,", question(
                "config_token", "Execute `rclone authorize` on a machine with a web browser, then paste the result.",
                "", required=True))
    elif state.startswith("*oauth-authorize"):
        if os.path.exists(ctl + "/echo_answer"):                # a misbehaving rclone that echoes its input
            sys.stderr.write("CRITICAL: Failed to parse token " + result + "\n")
            sys.exit(1)
        sections[name]["token"] = result
        save(sections)
        after_signin(name, sections)
    elif state.startswith("fake-onedrive-drive"):
        sections[name]["drive_id"] = result
        save(sections)
        emit("")
    elif state.startswith("fake-2fa"):
        if os.path.exists(ctl + "/echo_answer"):
            sys.stderr.write("CRITICAL: bad code " + result + "\n")
            sys.exit(1)
        if result == "123456":
            sections[name]["trust_token"] = "FAKE-TRUST"
            save(sections)
            emit("")
        else:
            emit("fake-2fa,,,", question("config_2fa", "Enter the two-factor code sent to your devices.", "",
                                         required=True), error="that code was not accepted")
    else:
        emit("")
    sys.exit(0)

if cmd == "lsjson":
    target = rest[-1]
    name = target.split(":", 1)[0]
    sections = load()
    if ":" in target and name not in sections and name != "":
        sys.stderr.write('CRITICAL: Failed to create file system for "%s": didn\'t find section in config file '
                         '("%s")\n' % (target, name))
        sys.exit(1)
    kind = sections.get(name, {}).get("fake_error")
    if kind == "auth":
        sys.stderr.write("CRITICAL: Failed to create file system: couldn't fetch token - maybe it has expired? "
                         "- refresh with \"rclone config reconnect\": oauth2: token expired and refresh token is "
                         "not set\n")
        sys.exit(1)
    if kind == "network":
        sys.stderr.write("ERROR : error listing: Get \"https://www.googleapis.com\": dial tcp: lookup "
                         "www.googleapis.com: no such host\n")
        sys.exit(1)
    if kind == "other":
        sys.stderr.write("CRITICAL: something odd happened\n")
        sys.exit(1)
    files = {}
    if os.path.exists(ctl + "/files.json"):
        files = json.load(open(ctl + "/files.json"))
    items = files.get(name, [])
    print("[")
    print(",\n".join(json.dumps(i) for i in items))
    print("]")
    sys.exit(0)

sys.stderr.write("fake rclone: unsupported command " + " ".join(rest) + "\n")
sys.exit(2)
'''


class FakeRclone:
    def __init__(self, tmp_path: Path):
        self.dir = Path(tmp_path) / "fake-rclone"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.binary = str(self.dir / "rclone")
        Path(self.binary).write_text(SCRIPT.replace("{python}", sys.executable), encoding="utf-8")
        os.chmod(self.binary, os.stat(self.binary).st_mode | stat.S_IXUSR)
        self.config = str(self.dir / "rclone.conf")
        self.ctl = Path(self.config + ".ctl")
        self.ctl.mkdir(parents=True, exist_ok=True)

    # ---- what a test can read
    def calls(self) -> list[list[str]]:
        log = Path(self.config + ".log")
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def text(self) -> str:
        return Path(self.config).read_text(encoding="utf-8") if os.path.exists(self.config) else ""

    def sections(self) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        cur = None
        for line in self.text().splitlines():
            if line.startswith("[") and line.endswith("]"):
                cur = out.setdefault(line[1:-1], {})
            elif " = " in line and cur is not None:
                k, v = line.split(" = ", 1)
                cur[k] = v
        return out

    def seed(self, name: str, rtype: str, **kv: str) -> None:
        """A remote the person already had in rclone."""
        sections = self.sections()
        sections[name] = {**sections.get(name, {}), "type": rtype, **kv}
        with open(self.config, "w", encoding="utf-8") as f:
            for n, items in sections.items():
                f.write(f"[{n}]\n" + "".join(f"{k} = {v}\n" for k, v in items.items()) + "\n")

    # ---- what a test can switch
    def finish_browser(self) -> None:
        (self.ctl / "browser_done").write_text("1")

    def deny_browser(self) -> None:
        (self.ctl / "browser_deny").write_text("1")

    def flag(self, name: str, on: bool = True) -> None:
        """fail_create, fail_continue, no_json, echo_answer."""
        path = self.ctl / name
        if on:
            path.write_text("1")
        elif path.exists():
            path.unlink()

    def set_files(self, remote: str, items: list[dict]) -> None:
        path = self.ctl / "files.json"
        files = json.loads(path.read_text()) if path.exists() else {}
        files[remote] = items
        path.write_text(json.dumps(files))

    def fail_lsjson(self, remote: str, kind: str) -> None:
        """Make listing `remote` fail the way an expired token ('auth'), a dead network or anything else does."""
        self.seed(remote, self.sections().get(remote, {}).get("type", "local"), fake_error=kind)

    def wait_for(self, predicate, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False
