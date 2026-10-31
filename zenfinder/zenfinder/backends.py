"""Where zenfinder looks: the Mac itself, or a host over ssh."""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import tempfile
from typing import Optional

from .browserpanel import Entry
from .sshargs import SshTarget, connection_options, control_dir, parse_ssh_G

PREVIEW_BYTES = 64 * 1024


class BackendError(Exception):
    """A failed operation; ``auth`` is set when ssh could not log in."""

    def __init__(self, message: str, auth: bool = False) -> None:
        super().__init__(message)
        self.auth = auth


class Exists(BackendError):
    pass


def sort_entries(entries: list[Entry]) -> list[Entry]:
    return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower(), e.name))


def visible(entries: list[Entry], show_hidden: bool) -> list[Entry]:
    return entries if show_hidden else [e for e in entries if not e.name.startswith(".")]


def unique_local_path(directory: str, name: str) -> str:
    path = os.path.join(directory, name)
    if not os.path.lexists(path):
        return path
    stem, ext = os.path.splitext(name)
    if stem.startswith(".") and not ext:
        stem, ext = name, ""
    n = 2
    while True:
        path = os.path.join(directory, f"{stem} ({n}){ext}")
        if not os.path.lexists(path):
            return path
        n += 1


def valid_name(name: str) -> str:
    """A single path component, or raise ``ValueError``."""
    name = name.strip()
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise ValueError("a file name may not be empty, '.', '..' or contain '/'")
    if len(name.encode()) > 255:
        raise ValueError("that name is longer than 255 bytes")
    return name


class Backend:
    kind = "?"
    label = "?"

    def home(self) -> str:
        raise NotImplementedError

    def join(self, directory: str, name: str) -> str:
        return directory.rstrip("/") + "/" + name if directory != "/" else "/" + name

    def parent(self, directory: str) -> str:
        if directory in ("/", "~"):
            return directory
        p = directory.rstrip("/").rsplit("/", 1)[0]
        return p or "/"

    def display(self, directory: str) -> str:
        return directory

    async def list(self, directory: str) -> tuple[str, list[Entry]]:
        raise NotImplementedError

    async def read_head(self, path: str, n: int = PREVIEW_BYTES) -> bytes:
        raise NotImplementedError

    async def exists(self, path: str) -> bool:
        raise NotImplementedError

    async def write_bytes(self, directory: str, name: str, data: bytes, overwrite: bool = False) -> str:
        raise NotImplementedError

    async def upload(self, local_path: str, directory: str, name: str, overwrite: bool = False) -> str:
        raise NotImplementedError

    async def mkdir(self, directory: str, name: str) -> str:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# local


class LocalBackend(Backend):
    kind = "local"
    label = "this Mac"

    def home(self) -> str:
        return os.path.expanduser("~")

    def display(self, directory: str) -> str:
        h = self.home()
        if directory == h or directory.startswith(h + "/"):
            return "~" + directory[len(h):]
        return directory

    async def list(self, directory: str) -> tuple[str, list[Entry]]:
        return await asyncio.to_thread(self._list, directory)

    def _list(self, directory: str) -> tuple[str, list[Entry]]:
        d = os.path.realpath(os.path.expanduser(directory))
        out = []
        try:
            with os.scandir(d) as it:
                for de in it:
                    try:
                        is_link = de.is_symlink()
                        is_dir = de.is_dir()  # follows links
                        st = de.stat(follow_symlinks=True) if not is_dir else de.stat(follow_symlinks=False)
                        out.append(Entry(de.name, is_dir, None if is_dir else st.st_size, st.st_mtime, is_link))
                    except OSError:
                        out.append(Entry(de.name, False, None, None, True))
        except OSError as e:
            raise BackendError(f"{self.display(d)}: {e.strerror or e}") from e
        return d, sort_entries(out)

    async def read_head(self, path: str, n: int = PREVIEW_BYTES) -> bytes:
        def read() -> bytes:
            with open(path, "rb") as f:
                return f.read(n)

        try:
            return await asyncio.to_thread(read)
        except OSError as e:
            raise BackendError(f"{path}: {e.strerror or e}") from e

    async def exists(self, path: str) -> bool:
        return os.path.lexists(path)

    async def write_bytes(self, directory: str, name: str, data: bytes, overwrite: bool = False) -> str:
        dest = os.path.join(directory, valid_name(name))

        def write() -> str:
            if not overwrite and os.path.lexists(dest):
                raise Exists(f"{name} already exists")
            fd, tmp = tempfile.mkstemp(prefix=".zenfinder-", dir=directory)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.chmod(tmp, 0o644 & ~_umask())
                os.replace(tmp, dest)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return dest

        try:
            return await asyncio.to_thread(write)
        except Exists:
            raise
        except OSError as e:
            raise BackendError(f"cannot write {name}: {e.strerror or e}") from e

    async def upload(self, local_path: str, directory: str, name: str, overwrite: bool = False) -> str:
        dest = os.path.join(directory, valid_name(name))

        def copy() -> str:
            if os.path.realpath(local_path) == os.path.realpath(dest):
                raise Exists(f"{name} is already here")
            if os.path.lexists(dest):
                if not overwrite:
                    raise Exists(f"{name} already exists")
                if os.path.isdir(dest) and not os.path.islink(dest):
                    shutil.rmtree(dest)
                else:
                    os.unlink(dest)
            if os.path.isdir(local_path) and not os.path.islink(local_path):
                shutil.copytree(local_path, dest, symlinks=True)
            else:
                shutil.copy2(local_path, dest, follow_symlinks=False)
            return dest

        try:
            return await asyncio.to_thread(copy)
        except Exists:
            raise
        except OSError as e:
            raise BackendError(f"cannot copy {name}: {e.strerror or e}") from e

    async def mkdir(self, directory: str, name: str) -> str:
        dest = os.path.join(directory, valid_name(name))
        try:
            os.mkdir(dest)
        except FileExistsError as e:
            raise Exists(f"{name} already exists") from e
        except OSError as e:
            raise BackendError(f"cannot create {name}: {e.strerror or e}") from e
        return dest


def _umask() -> int:
    m = os.umask(0)
    os.umask(m)
    return m


# ---------------------------------------------------------------------------
# ssh

LIST_SCRIPT = r"""
d=$1
case $d in "~") d=$HOME;; "~/"*) d=$HOME/${d#"~/"};; esac
cd -- "$d" || exit 3
pwd
if find . -maxdepth 0 -printf '' >/dev/null 2>&1; then
  find . -mindepth 1 -maxdepth 1 -printf '%y\t%Y\t%s\t%T@\t%f\0'
else
  for f in .[!.]* ..?* *; do
    [ -e "$f" ] || [ -L "$f" ] || continue
    y=f; [ -d "$f" ] && y=d; Y=$y; [ -L "$f" ] && y=l
    s=$(stat -f %z -- "$f" 2>/dev/null || stat -c %s -- "$f" 2>/dev/null)
    m=$(stat -f %m -- "$f" 2>/dev/null || stat -c %Y -- "$f" 2>/dev/null)
    printf '%s\t%s\t%s\t%s\t%s\0' "$y" "$Y" "$s" "$m" "$f"
  done
fi
"""

WRITE_SCRIPT = r"""
[ -e "$1" ] && [ "$2" != 1 ] && exit 17
cat > "$1.zenfinder-part" && mv -f -- "$1.zenfinder-part" "$1"
"""

UNTAR_SCRIPT = r"""
[ -e "$1/$2" ] && [ "$3" != 1 ] && exit 17
[ "$3" = 1 ] && rm -rf -- "$1/$2"
tar -C "$1" -xf -
"""

EXISTS_SCRIPT = r'[ -e "$1" ] || [ -L "$1" ]'

MKDIR_SCRIPT = r'[ -e "$1" ] && exit 17; mkdir -- "$1"'


def parse_listing(raw: bytes) -> tuple[str, list[Entry]]:
    head, _, body = raw.partition(b"\n")
    cwd = head.decode("utf-8", "surrogateescape")
    out = []
    for rec in body.split(b"\0"):
        if not rec:
            continue
        parts = rec.split(b"\t", 4)
        if len(parts) != 5:
            continue
        y, Y, s, m, name = (p.decode("utf-8", "surrogateescape") for p in parts)
        try:
            size = int(s) if s else None
        except ValueError:
            size = None
        try:
            mtime = float(m) if m else None
        except ValueError:
            mtime = None
        is_dir = Y == "d"
        out.append(Entry(name, is_dir, None if is_dir else size, mtime, y == "l"))
    return cwd, sort_entries(out)


def remote_sh(script: str, *args: str) -> str:
    """A remote command line running ``script`` under ``sh`` with ``args``."""
    return " ".join(["sh", "-c", shlex.quote(script.strip()), "zenfinder", *(shlex.quote(a) for a in args)])


class RemoteBackend(Backend):
    kind = "ssh"

    def __init__(self, target: SshTarget) -> None:
        self.target = target
        self.label = target.label
        self._effective: Optional[dict[str, str]] = None

    def home(self) -> str:
        return "~"

    def display(self, directory: str) -> str:
        return f"{self.label}:{directory}"

    async def effective_config(self) -> dict[str, str]:
        if self._effective is None:
            p = await asyncio.create_subprocess_exec(
                "ssh", "-G", *self.target.options, self.target.destination,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, stdin=asyncio.subprocess.DEVNULL,
            )
            out, _ = await p.communicate()
            self._effective = parse_ssh_G(out.decode("utf-8", "replace")) if p.returncode == 0 else {}
        return self._effective

    async def base(self) -> list[str]:
        os.makedirs(control_dir(), mode=0o700, exist_ok=True)
        return ["ssh", *self.target.options, *connection_options(await self.effective_config()), "-T", self.target.destination]

    async def run(self, remote: str, stdin: Optional[bytes] = None, stdin_fd: Optional[int] = None,
                  stdout_fd: Optional[int] = None, timeout: float = 60) -> bytes:
        cmd = await self.base() + [remote]
        p = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=stdin_fd if stdin_fd is not None else (asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL),
            stdout=stdout_fd if stdout_fd is not None else asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(p.communicate(stdin), timeout)
        except asyncio.TimeoutError:
            p.kill()
            raise BackendError(f"{self.label}: timed out") from None
        except asyncio.CancelledError:
            p.kill()
            raise
        self._check(p.returncode, err)
        return out or b""

    def _check(self, code: Optional[int], err: bytes) -> None:
        if code == 0:
            return
        msg = err.decode("utf-8", "replace").strip().splitlines()
        text = msg[-1] if msg else f"exit status {code}"
        if code == 17:
            raise Exists("already exists")
        if code == 255:
            low = (err or b"").decode("utf-8", "replace").lower()
            auth = any(s in low for s in ("permission denied", "authentication", "password", "keyboard-interactive", "host key verification"))
            raise BackendError(f"{self.label}: {text}", auth=auth)
        raise BackendError(text)

    async def list(self, directory: str) -> tuple[str, list[Entry]]:
        raw = await self.run(remote_sh(LIST_SCRIPT, directory), timeout=30)
        if not raw:
            raise BackendError(f"{self.label}: no listing for {directory}")
        return parse_listing(raw)

    async def read_head(self, path: str, n: int = PREVIEW_BYTES) -> bytes:
        return await self.run(" ".join(["head", "-c", str(int(n)), "--", shlex.quote(path)]), timeout=30)

    async def exists(self, path: str) -> bool:
        try:
            await self.run(remote_sh(EXISTS_SCRIPT, path), timeout=20)
            return True
        except Exists:
            return True
        except BackendError as e:
            if e.auth:
                raise
            return False

    async def write_bytes(self, directory: str, name: str, data: bytes, overwrite: bool = False) -> str:
        dest = self.join(directory, valid_name(name))
        await self.run(remote_sh(WRITE_SCRIPT, dest, "1" if overwrite else "0"), stdin=data, timeout=600)
        return dest

    async def upload(self, local_path: str, directory: str, name: str, overwrite: bool = False) -> str:
        name = valid_name(name)
        dest = self.join(directory, name)
        if os.path.isdir(local_path) and not os.path.islink(local_path):
            await self._upload_dir(local_path, directory, name, overwrite)
            return dest
        try:
            fd = os.open(local_path, os.O_RDONLY)
        except OSError as e:
            raise BackendError(f"{local_path}: {e.strerror or e}") from e
        try:
            await self.run(remote_sh(WRITE_SCRIPT, dest, "1" if overwrite else "0"), stdin_fd=fd, timeout=3600)
        finally:
            os.close(fd)
        return dest

    async def _upload_dir(self, local_path: str, directory: str, name: str, overwrite: bool) -> None:
        parent = os.path.dirname(os.path.abspath(local_path))
        base = os.path.basename(os.path.abspath(local_path))
        if base != name:
            raise BackendError("folders are uploaded under their own name")
        r, w = os.pipe()
        env = dict(os.environ, COPYFILE_DISABLE="1")  # no ._ AppleDouble files
        tar = await asyncio.create_subprocess_exec(
            "tar", "-C", parent, "-cf", "-", base, stdout=w, stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL, env=env,
        )
        os.close(w)
        try:
            await self.run(remote_sh(UNTAR_SCRIPT, directory, name, "1" if overwrite else "0"), stdin_fd=r, timeout=3600)
        finally:
            os.close(r)
            if tar.returncode is None:
                try:
                    tar.kill()
                except ProcessLookupError:
                    pass
            await tar.wait()

    async def download(self, path: str, is_dir: bool, local_dir: str) -> str:
        """Copy ``path`` into ``local_dir`` (never overwriting); returns the local path."""
        os.makedirs(local_dir, exist_ok=True)
        name = os.path.basename(path.rstrip("/")) or "download"
        dest = unique_local_path(local_dir, name)
        if not is_dir:
            part = dest + ".zenfinder-part"
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                await self.run("cat -- " + shlex.quote(path), stdout_fd=fd, timeout=3600)
            except BaseException:
                os.close(fd)
                os.unlink(part)
                raise
            os.close(fd)
            os.replace(part, dest)
            return dest
        staging = tempfile.mkdtemp(prefix=".zenfinder-", dir=local_dir)
        try:
            r, w = os.pipe()
            untar = await asyncio.create_subprocess_exec(
                "tar", "-C", staging, "-xf", "-", stdin=r, stderr=asyncio.subprocess.PIPE,
            )
            os.close(r)
            try:
                await self.run(
                    " ".join(["tar", "-C", shlex.quote(self.parent(path)), "-cf", "-", "--", shlex.quote(name)]),
                    stdout_fd=w, timeout=3600,
                )
            finally:
                os.close(w)
            _out, err = await untar.communicate()
            if untar.returncode != 0:
                raise BackendError("tar: " + err.decode("utf-8", "replace").strip())
            os.replace(os.path.join(staging, name), dest)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return dest

    async def mkdir(self, directory: str, name: str) -> str:
        dest = self.join(directory, valid_name(name))
        await self.run(remote_sh(MKDIR_SCRIPT, dest), timeout=20)
        return dest
