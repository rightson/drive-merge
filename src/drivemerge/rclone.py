"""Thin, testable wrapper around the rclone binary (stdlib only).

Verified against rclone v1.75:
  * `lsjson --hash-type T` (repeatable) selects hashes and implies `--hash`.
  * A missing object or directory exits 3 ("directory not found"), a missing
    file may exit 4; any other failure (bad remote, bad flag, unknown hash)
    exits 1 or 2. `lsjson --stat` therefore maps exit 3/4 to "does not exist".
  * `copyto`/`moveto --ignore-existing` exit 0 and do nothing when the
    destination exists; callers must verify what actually happened.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence

Runner = Callable[[list[str]], tuple[int, str, str]]

# rclone exit codes meaning "directory not found" / "file not found".
_NOT_FOUND_CODES = frozenset({3, 4})
_STDERR_TAIL_LINES = 200
_ABOUT_UNSUPPORTED = re.compile(r"doesn.t support about|about.*not (?:supported|implemented)", re.I)


class RcloneError(Exception):
    """An rclone invocation failed.

    `args` is the full argv (binary first) as a list. It deliberately shadows
    BaseException.args, which stays the message tuple internally.
    """

    def __init__(self, message: str, returncode: int = -1, stderr: str = "", args: Sequence[str] = ()):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr
        self._argv = list(args)

    @property
    def args(self) -> list[str]:  # type: ignore[override]
        return self._argv

    def __reduce__(self):
        return (RcloneError, (str(self), self.returncode, self.stderr, self._argv))


class RcloneCancelled(RcloneError):
    """A streamed `rclone config` call was stopped through its cancel event."""

    def __reduce__(self):
        return (RcloneCancelled, (str(self), self.returncode, self.stderr, self._argv))


class RcloneTimeout(RcloneError):
    """A streamed `rclone config` call did not finish within its timeout and was killed."""

    def __reduce__(self):
        return (RcloneTimeout, (str(self), self.returncode, self.stderr, self._argv))


# rclone remote names this wrapper will pass to `config create/update/delete`: no leading '-', no ':' or '/'.
_REMOTE_NAME = re.compile(r"[A-Za-z0-9_.+@][A-Za-z0-9_.+@-]*")
MIN_RCLONE = (1, 69)
TEST_TIMEOUT_S = 30.0
# Where rclone's sign-in helper listens; only a URL there is ever handed to a browser.
AUTH_URL = re.compile(r"http://127\.0\.0\.1:53682/auth\?state=[A-Za-z0-9_.~%-]+")
_AUTH_EXPIRED = re.compile(
    r"invalid_grant|token (?:has )?(?:expired|been revoked)|expired.{0,20}token|couldn.t fetch token|oauth2:|"
    r"unauthori[sz]ed|\b401\b|invalid_client|refresh token|authentication failed|invalid (?:access )?token|"
    r"AuthenticationFailed|reauth", re.I)
_NETWORK = re.compile(
    r"no such host|dial tcp|i/o timeout|connection (?:refused|reset)|network is unreachable|TLS handshake|"
    r"timed out|context deadline exceeded|temporary failure in name resolution|server misbehaving|"
    r"unexpected EOF|proxyconnect", re.I)
_NOT_FOUND = re.compile(r"didn.t find section in config file|directory not found|object not found|"
                        r"couldn.t find root|unknown (?:remote|backend)", re.I)


def classify_failure(returncode: int, stderr: str) -> str:
    """AUTH_EXPIRED, NETWORK, NOT_FOUND or RCLONE_ERROR for a failed rclone call, from its exit code and stderr."""
    if _NOT_FOUND.search(stderr):
        return "NOT_FOUND"
    if _AUTH_EXPIRED.search(stderr):
        return "AUTH_EXPIRED"
    if _NETWORK.search(stderr):
        return "NETWORK"
    return "NOT_FOUND" if returncode in _NOT_FOUND_CODES else "RCLONE_ERROR"


def _scrub(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _tail(stderr: str) -> str:
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    return lines[-1][:500] if lines else "no output"


def _failure(sub: str, argv: list[str], rc: int, stderr: str) -> RcloneError:
    return RcloneError(f"rclone {sub} failed (exit {rc}): {_tail(stderr)}", rc, stderr, argv)


def _parse_items(lines: Iterable[str], argv: list[str]) -> Iterator[dict]:
    """Parse `lsjson` output: '[', one object per line with a trailing ',', ']'."""
    for line in lines:
        line = line.strip()
        if not line or line in ("[", "]"):
            continue
        if line.endswith(","):
            line = line[:-1]
        try:
            yield json.loads(line)
        except json.JSONDecodeError as e:
            raise RcloneError(f"rclone lsjson: unparseable output line ({e})", -1, line[:500], argv) from e


def _drain(stream, sink: deque) -> None:
    """Read stderr to EOF so a chatty rclone can never block on a full pipe."""
    try:
        for line in stream:
            sink.append(line.rstrip("\n"))
    except (OSError, ValueError):
        pass


class Rclone:
    """Builds and runs rclone commands. Never shells out and never syncs; the single
    delete it offers is `deletefile` (one file, used only by the executor's purge)."""

    def __init__(self, binary: str = "rclone", *, config: str | None = None,
                 extra_args: Sequence[str] = (), runner: Runner | None = None):
        self.binary = binary
        self.config = config
        self.extra_args = tuple(extra_args)
        self._runner = runner

    @staticmethod
    def join(remote: str, path: str) -> str:
        """'gd:' + 'a/b' -> 'gd:a/b'; 'gd:root' + 'a' -> 'gd:root/a'; '' path -> remote."""
        path = path.strip("/")
        if not path:
            return remote
        if not remote:
            return path
        if remote.endswith((":", "/")):
            return remote + path
        return f"{remote}/{path}"

    def _argv(self, sub: str, flags: Sequence[str] = (), operands: Sequence[str] = ()) -> list[str]:
        """binary [--config C] [global extra] sub flags... [--] operands...

        '--' is only added when an operand starts with '-', so a local path
        can never be mistaken for a flag.
        """
        argv = [self.binary]
        if self.config:
            argv += ["--config", self.config]
        argv += list(self.extra_args)
        argv.append(sub)
        argv += list(flags)
        if any(o.startswith("-") for o in operands):
            argv.append("--")
        argv += list(operands)
        return argv

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        if self._runner is not None:
            return self._runner(argv)
        try:
            p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
        except OSError as e:
            raise RcloneError(f"cannot run rclone: {e}", -1, str(e), argv) from e
        return p.returncode, p.stdout, p.stderr

    def _run_checked(self, sub: str, argv: list[str]) -> str:
        rc, out, err = self._run(argv)
        if rc != 0:
            raise _failure(sub, argv, rc, err)
        return out

    @staticmethod
    def _hash_flags(hash_types: Sequence[str]) -> list[str]:
        if not hash_types:
            return []
        flags = ["--hash"]
        for t in hash_types:
            flags += ["--hash-type", t]
        return flags

    def lsjson(self, remote: str, path: str = "", *, recursive: bool = True,
               hash_types: Sequence[str] = (), extra_args: Sequence[str] = ()) -> Iterator[dict]:
        """Yield one dict per listed object, streaming (memory stays flat).

        A non-zero exit raises RcloneError *after* the stream has been
        consumed, so a failed listing can never be mistaken for a complete
        one, and a missing root raises instead of yielding nothing.
        Closing the generator early kills the rclone process.
        """
        flags = (["--recursive"] if recursive else []) + self._hash_flags(hash_types) + list(extra_args)
        argv = self._argv("lsjson", flags, [self.join(remote, path)])
        if self._runner is not None:
            rc, out, err = self._runner(argv)
            yield from _parse_items(out.split("\n"), argv)  # not splitlines(): U+0085 etc. may occur raw in names
            if rc != 0:
                raise _failure("lsjson", argv, rc, err)
            return
        yield from self._stream(argv)

    def _stream(self, argv: list[str]) -> Iterator[dict]:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        except OSError as e:
            raise RcloneError(f"cannot run rclone: {e}", -1, str(e), argv) from e
        tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        drainer = threading.Thread(target=_drain, args=(proc.stderr, tail), daemon=True)
        drainer.start()
        try:
            yield from _parse_items(proc.stdout, argv)
            rc = proc.wait()
            drainer.join(timeout=10)
            if rc != 0:
                raise _failure("lsjson", argv, rc, "\n".join(tail))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            drainer.join(timeout=10)
            proc.stderr.close()

    def stat(self, remote: str, path: str, *, hash_types: Sequence[str] = ()) -> dict | None:
        """lsjson entry for one object (file or directory), or None if it does not exist.

        The returned Path/Name are rclone's own (the leaf name, '' for a
        directory stat), not the path that was asked for.
        """
        argv = self._argv("lsjson", ["--stat", *self._hash_flags(hash_types)], [self.join(remote, path)])
        rc, out, err = self._run(argv)
        if rc in _NOT_FOUND_CODES:
            return None
        if rc != 0:
            raise _failure("lsjson --stat", argv, rc, err)
        try:
            return json.loads(out)
        except json.JSONDecodeError as e:
            raise RcloneError(f"rclone lsjson --stat: unparseable output ({e})", rc, out[:500], argv) from e

    def copyto(self, src_remote: str, src_path: str, dst_remote: str, dst_path: str) -> None:
        """Copy one file; an existing destination is never overwritten.

        Runs with --ignore-existing: if the destination exists rclone exits 0
        and does nothing, so success does NOT prove the copy happened. The
        caller must stat the destination and compare size/hash.
        """
        argv = self._argv("copyto", ["--ignore-existing"],
                          [self.join(src_remote, src_path), self.join(dst_remote, dst_path)])
        self._run_checked("copyto", argv)

    def moveto(self, src_remote: str, src_path: str, dst_remote: str, dst_path: str) -> None:
        """Move one file; an existing destination is never overwritten.

        Same caveat as copyto: with --ignore-existing a pre-existing
        destination makes this a silent no-op that leaves the source in place
        and exits 0. Verify with stat() afterwards.
        """
        argv = self._argv("moveto", ["--ignore-existing"],
                          [self.join(src_remote, src_path), self.join(dst_remote, dst_path)])
        self._run_checked("moveto", argv)

    def deletefile(self, remote: str, path: str) -> None:
        """Permanently delete exactly one file (`rclone deletefile`).

        The only destructive call in this wrapper, used solely by the
        executor's purge. rclone refuses a directory and exits non-zero for a
        missing file, so a wrong path can never remove a tree. The path is
        validated here as well: it must name something below the remote root
        (never the root itself, never an absolute path or a '..' segment), so
        a caller bug cannot point the delete outside the remote.
        """
        if not path or path.startswith("/") or "\x00" in path:
            raise ValueError(f"deletefile needs a relative file path, got {path!r}")
        if any(seg in ("", ".", "..") for seg in path.split("/")):
            raise ValueError(f"deletefile path has an empty, '.' or '..' segment: {path!r}")
        argv = self._argv("deletefile", [], [self.join(remote, path)])
        self._run_checked("deletefile", argv)

    def download(self, remote: str, path: str, local_path: str) -> None:
        """Copy a remote file to a local scratch path, replacing whatever is there.

        Unlike copyto this overwrites: the destination is a local temp file
        owned by the caller (e.g. one made by tempfile), and skipping an
        existing empty temp file would silently yield wrong bytes. The path is
        made absolute so rclone cannot read 'a:b' as a remote.
        """
        local = os.path.abspath(local_path)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        argv = self._argv("copyto", [], [self.join(remote, path), local])
        self._run_checked("copyto", argv)

    def about(self, remote: str) -> dict | None:
        """{'total', 'used', 'free'} in bytes, or None if the backend has no About.

        A value rclone does not report (e.g. 'total' on unlimited storage) is None.
        """
        argv = self._argv("about", ["--json"], [remote])
        rc, out, err = self._run(argv)
        if rc != 0:
            if _ABOUT_UNSUPPORTED.search(err):
                return None
            raise _failure("about", argv, rc, err)
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            raise RcloneError(f"rclone about: unparseable output ({e})", rc, out[:500], argv) from e
        return {k: (int(data[k]) if data.get(k) is not None else None) for k in ("total", "used", "free")}

    def version(self) -> str:
        """rclone's version tag, e.g. 'v1.75.2'."""
        argv = self._argv("version")
        out = self._run_checked("version", argv)
        m = re.search(r"^rclone\s+(\S+)", out, re.M)
        if not m:
            raise RcloneError(f"rclone version: unrecognised output {out[:200]!r}", 0, out[:500], argv)
        return m.group(1)

    # ------------------------------------------------------------ setup: remotes and `rclone config`

    def version_info(self) -> dict | None:
        """{'version': 'v1.71.1', 'ok': True when at least 1.69}, or None when rclone cannot be run."""
        try:
            tag = self.version()
        except RcloneError:
            return None
        m = re.match(r"v?(\d+)\.(\d+)", tag)
        return {"version": tag, "ok": bool(m) and (int(m.group(1)), int(m.group(2))) >= MIN_RCLONE}

    def list_remotes(self) -> list[dict]:
        """[{'name', 'type'}] of every remote in rclone's config (`listremotes --long --json`, or the text form)."""
        argv = self._argv("listremotes", ["--long", "--json"])
        rc, out, err = self._run(argv)
        if rc == 0:
            try:
                data = json.loads(out or "[]")
                return [{"name": str(r["name"]), "type": str(r.get("type", ""))} for r in data]
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                raise RcloneError(f"rclone listremotes: unparseable output ({e})", rc, out[:500], argv) from e
        argv = self._argv("listremotes", ["--long"])           # an older rclone has no --json
        out = self._run_checked("listremotes", argv)
        found = []
        for line in out.splitlines():
            name, sep, rtype = line.partition(":")
            if sep and name.strip():
                found.append({"name": name.strip(), "type": rtype.strip()})
        return found

    @staticmethod
    def _check_name(name: str) -> str:
        if not _REMOTE_NAME.fullmatch(name):
            raise ValueError(f"invalid rclone remote name {name!r}")
        return name

    def config_create(self, name: str, rtype: str, options: dict[str, str] | None = None, *,
                      on_stderr: Callable[[str], None] | None = None, cancel: threading.Event | None = None,
                      timeout: float | None = None) -> dict:
        """`rclone config create NAME TYPE k=v... --non-interactive`: rclone's JSON {State, Option, Error, Result}.

        The remote's stanza is already in rclone's config when this returns. Streamed: see `_config_call`.
        """
        self._check_name(name)
        if not re.fullmatch(r"[a-z0-9]+", rtype):
            raise ValueError(f"invalid rclone remote type {rtype!r}")
        opts = dict(options or {})
        argv = self._argv("config", ["create", name, rtype, *[f"{k}={v}" for k, v in opts.items()],
                                     "--non-interactive"])
        shown = self._argv("config", ["create", name, rtype, *[f"{k}=***" for k in opts], "--non-interactive"])
        return self._config_call(argv, secrets=[v for v in opts.values() if len(v) >= 3], shown=shown,
                                 on_stderr=on_stderr, cancel=cancel, timeout=timeout)

    def config_continue(self, name: str, state: str, result: str, *,
                        on_stderr: Callable[[str], None] | None = None, cancel: threading.Event | None = None,
                        timeout: float | None = None) -> dict:
        """`rclone config update NAME --continue --state S --result R --non-interactive`; `result` may be a
        password or token, so it is kept out of every error (message, stderr and argv) and out of `on_stderr`."""
        self._check_name(name)
        argv = self._argv("config", ["update", name, "--continue", "--state", state, "--result", result,
                                     "--non-interactive"])
        shown = self._argv("config", ["update", name, "--continue", "--state", state, "--result", "***",
                                      "--non-interactive"])
        return self._config_call(argv, secrets=[result] if len(result) >= 3 else [], shown=shown,
                                 on_stderr=on_stderr, cancel=cancel, timeout=timeout)

    def config_delete(self, name: str) -> None:
        """Remove a remote from rclone's config. rclone exits 0 for a name that is not there."""
        self._check_name(name)
        argv = self._argv("config", ["delete", name])
        self._run_checked("config delete", argv)

    def test_remote(self, remote: str) -> RcloneError | None:
        """List the top of `remote` (`lsjson --max-depth 1`, 30 s): None when it works, else the error (returned)."""
        argv = self._argv("lsjson", ["--max-depth", "1"], [remote])
        try:
            if self._runner is not None:
                rc, out, err = self._runner(argv)
            else:
                p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=TEST_TIMEOUT_S)
                rc, err = p.returncode, p.stderr
        except subprocess.TimeoutExpired:
            return RcloneTimeout(f"rclone lsjson timed out after {TEST_TIMEOUT_S:g} s", -1, "timed out", argv)
        except OSError as e:
            return RcloneError(f"cannot run rclone: {e}", -1, str(e), argv)
        return None if rc == 0 else _failure("lsjson", argv, rc, err)

    def _config_call(self, argv: list[str], *, secrets: Sequence[str], shown: list[str],
                     on_stderr: Callable[[str], None] | None, cancel: threading.Event | None,
                     timeout: float | None) -> dict:
        """Run a `rclone config` call that may block (rclone waits for the browser) and return its JSON.

        stderr lines go to `on_stderr` as they arrive, so a caller can pick up the sign-in URL while rclone waits.
        `cancel` being set (or `timeout` seconds passing) kills the process: RcloneCancelled / RcloneTimeout.
        Errors carry `shown` (the argv with secrets masked) and a stderr from which `secrets` are removed.
        """
        if cancel is not None and cancel.is_set():
            raise RcloneCancelled("rclone config cancelled", -1, "", shown)

        def emit(line: str) -> None:
            if on_stderr is not None:
                try:
                    on_stderr(_scrub(line, secrets))
                except Exception:  # a broken callback must not wedge the pipe
                    pass

        if self._runner is not None:
            rc, out, err = self._runner(argv)
            for line in err.split("\n"):
                if line.strip():
                    emit(line)
            return self._config_result(rc, out, _scrub(err, secrets), shown)

        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace")
        except OSError as e:
            raise RcloneError(f"cannot run rclone: {e}", -1, str(e), shown) from e
        tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        chunks: list[str] = []
        why: list[str] = []

        def read_err() -> None:
            try:
                for line in proc.stderr:
                    line = line.rstrip("\n")
                    tail.append(_scrub(line, secrets))
                    emit(line)
            except (OSError, ValueError):
                pass

        def read_out() -> None:
            try:
                chunks.append(proc.stdout.read())
            except (OSError, ValueError):
                pass

        def watch() -> None:
            end = None if timeout is None else time.monotonic() + timeout
            while proc.poll() is None:
                if cancel is not None:
                    if cancel.wait(0.05):
                        why.append("cancel")
                else:
                    time.sleep(0.05)
                if not why and end is not None and time.monotonic() > end:
                    why.append("timeout")
                if why:
                    proc.kill()
                    return

        threads = [threading.Thread(target=f, daemon=True) for f in (read_err, read_out, watch)]
        for t in threads:
            t.start()
        try:
            rc = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        for t in threads:
            t.join(timeout=10)
        proc.stdout.close()
        proc.stderr.close()
        stderr = "\n".join(tail)
        if why and why[0] == "cancel":
            raise RcloneCancelled("rclone config cancelled", rc, stderr, shown)
        if why:
            raise RcloneTimeout(f"rclone config timed out after {timeout:g} s", rc, stderr, shown)
        return self._config_result(rc, "".join(chunks), stderr, shown)

    @staticmethod
    def _config_result(rc: int, out: str, stderr: str, shown: list[str]) -> dict:
        if rc != 0:
            raise _failure("config", shown, rc, stderr)
        if not out.strip():
            return {"State": "", "Option": None, "Error": "", "Result": ""}
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            raise RcloneError("rclone config: unparseable output", rc, "", shown) from e
        if not isinstance(data, dict):
            raise RcloneError("rclone config: unexpected output", rc, "", shown)
        return data
