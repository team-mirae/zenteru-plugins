"""Understanding the ssh session a terminal is running.

zenfinder doesn't share the session's ssh connection. It opens its own,
built from the same command line: destination plus the options that
decide *where* and *how* to connect (port, user, identity, jump host,
config file, ``-o`` options). Options about the interactive session
itself (forwardings, tty, agent, background) are dropped.

To avoid a second login, connections go through an OpenSSH control
master (``ControlMaster=auto``). If ``~/.ssh/config`` already sets a
``ControlPath``, that is reused, so when the terminal's own ssh is the
master zenfinder piggybacks on it with no authentication at all.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Optional

#: ssh options that take an argument (OpenSSH 9.x ``ssh -h``).
ARG_OPTS = set("BbcDEeFIiJLlmOoPpQRSWw")
#: Options with an argument that zenfinder keeps for its own connections.
KEEP_ARG_OPTS = set("BbcFiJlmop") | {"P"}
#: Flag options zenfinder keeps.
KEEP_FLAGS = set("46Cq")
#: ``-o`` options that only concern the interactive session.
DROP_O_OPTIONS = {
    "localforward", "remoteforward", "dynamicforward", "requesttty", "remotecommand",
    "forwardagent", "forwardx11", "forwardx11trusted", "permitlocalcommand", "localcommand",
    "sessiontype", "stdinnull", "forkafterauthentication", "clearallforwardings",
    "exitonforwardfailure", "controlmaster", "controlpath", "controlpersist", "batchmode",
}


@dataclass
class SshTarget:
    destination: str
    options: list[str] = field(default_factory=list)
    #: Remote command given on the command line (None for a login shell).
    command: Optional[list[str]] = None

    @property
    def label(self) -> str:
        d = self.destination
        if d.startswith("ssh://"):
            d = d[len("ssh://"):]
        return d

    def key(self) -> tuple:
        return (self.destination, tuple(self.options))


def parse_ssh_argv(argv: list[str]) -> Optional[SshTarget]:
    """Parse an ``ssh`` command line (``argv[0]`` is the program).

    Returns None if there is no destination (``ssh -V``, ``ssh -G``,
    ``ssh -O exit``) or for modes that aren't a login (``-N``, ``-W``).
    """
    if not argv:
        return None
    args = argv[1:]
    opts: list[str] = []
    dest: Optional[str] = None
    i = 0
    while i < len(args):
        a = args[i]
        if dest is None and a == "--":
            i += 1
            if i < len(args):
                dest = args[i]
                i += 1
            break
        if dest is None and a.startswith("-") and len(a) > 1:
            j = 1
            while j < len(a):
                c = a[j]
                if c in ARG_OPTS:
                    val = a[j + 1:]
                    if not val:
                        i += 1
                        if i >= len(args):
                            return None
                        val = args[i]
                    if c in ("W", "O"):
                        return None
                    if c == "o":
                        name = re.split(r"[\s=]", val.strip(), maxsplit=1)[0].lower()
                        if name not in DROP_O_OPTIONS:
                            opts += ["-o", val]
                    elif c in KEEP_ARG_OPTS:
                        opts += [f"-{c}", val]
                    break
                if c in ("V", "G", "N", "f"):
                    if c in ("V", "G", "N"):
                        return None
                elif c in KEEP_FLAGS:
                    opts.append(f"-{c}")
                j += 1
            i += 1
            continue
        dest = a
        i += 1
        break
    if not dest:
        return None
    rest = args[i:]
    return SshTarget(dest, opts, rest or None)


# ---------------------------------------------------------------------------
# finding the foreground ssh of a tty


def parse_ps(output: str) -> list[tuple[int, str, str]]:
    """``ps -o pid=,stat=,args=`` → ``[(pid, stat, args)]``."""
    rows = []
    for line in output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        rows.append((int(parts[0]), parts[1], parts[2]))
    return rows


def foreground_ssh(ps_rows: list[tuple[int, str, str]]) -> Optional[SshTarget]:
    """The newest foreground (``+`` in stat) ssh process, parsed."""
    for _pid, stat, args in sorted(ps_rows, key=lambda r: -r[0]):
        if "+" not in stat:
            continue
        try:
            argv = shlex.split(args)
        except ValueError:
            argv = args.split()
        if not argv or os.path.basename(argv[0]) not in ("ssh", "autossh"):
            continue
        t = parse_ssh_argv(argv)
        if t is not None:
            return t
    return None


# ---------------------------------------------------------------------------
# remote working directory from the prompt

_PROMPTS = [
    # user@host:~/src$   user@host:/etc#   (Debian/Ubuntu bash, many zsh themes)
    re.compile(r"(?:^|\s)[\w.\-]+@[\w.\-]+:(?P<p>~[^\s]*|/[^\s]*)\s?[$#%>❯]\s*$"),
    # [user@host ~/src]$ (only when the full path is shown)
    re.compile(r"\[[\w.\-]+@[\w.\-]+\s+(?P<p>~[^\s\]]*|/[^\s\]]*)\][$#%]\s*$"),
    # user@host ~/src %  (zsh)
    re.compile(r"(?:^|\s)[\w.\-]+@[\w.\-]+\s+(?P<p>~[^\s]*|/[^\s]*)\s?[$#%>❯]\s*$"),
]


def remote_cwd_from_prompt(line: str) -> Optional[str]:
    """Guess the remote directory from a prompt line, or None."""
    line = line.rstrip()
    for rx in _PROMPTS:
        m = rx.search(line)
        if m:
            return m.group("p")
    return None


# ---------------------------------------------------------------------------
# commands


def control_dir() -> str:
    """Short, private directory for control sockets (sun_path is ~104 bytes)."""
    return f"/tmp/zenfinder-{os.getuid()}"


def parse_ssh_G(output: str) -> dict[str, str]:
    cfg: dict[str, str] = {}
    for line in output.splitlines():
        k, _, v = line.partition(" ")
        if k and k not in cfg:
            cfg[k.lower()] = v.strip()
    return cfg


def connection_options(effective: dict[str, str]) -> list[str]:
    """``-o`` options for zenfinder's own non-interactive connections."""
    opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15"]
    cp = effective.get("controlpath", "none")
    if cp and cp.lower() != "none":
        # Reuse the user's control path: the terminal's ssh may be the master.
        opts += ["-o", "ControlMaster=auto", "-o", "ControlPersist=600"]
    else:
        opts += [
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={control_dir()}/%C",
            "-o", "ControlPersist=600",
        ]
    return opts


def auth_command(target: SshTarget, effective: dict[str, str]) -> list[str]:
    """An interactive ssh that authenticates once, then backgrounds as master."""
    opts = list(target.options)
    cp = effective.get("controlpath", "none")
    if not cp or cp.lower() == "none":
        opts += ["-o", f"ControlPath={control_dir()}/%C"]
    return ["ssh", *opts, "-o", "ControlMaster=yes", "-o", "ControlPersist=600", "-f", "-N", target.destination]


CONFIG_SNIPPET = """\
Host *
    ControlMaster auto
    ControlPath ~/.ssh/cm-%C
    ControlPersist 10m"""
