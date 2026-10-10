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
    """Builds and runs rclone commands. Never shells out; never syncs or deletes."""

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
