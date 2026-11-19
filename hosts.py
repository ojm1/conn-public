"""Host inventory and remote probing for the server panel.

Everything that touches ~/.ssh/config, sshfs or a remote box lives here, so
tui.py only ever deals with plain dicts. Nothing in this module blocks for
longer than PROBE_TIMEOUT -- the UI runs it on worker threads and would
otherwise freeze on a box that is powered off.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

import agent_state
import connlog

# CONN_SSH_CONFIG points everything here that reads or edits the config at
# another file. It exists for the tests: pointed at a config of their own they
# can run without probing the real fleet or rewriting the real file. ssh
# itself never sees the variable -- only this module's parsing and editing.
SSH_CONFIG = Path(os.environ.get("CONN_SSH_CONFIG")
                  or Path.home() / ".ssh" / "config")
# Mountpoints live in the runtime directory, not the home folder. They hold
# nothing -- a mountpoint is an empty hook to hang a filesystem on -- so a
# folder in ~ for each server you once looked at was pure clutter. The runtime
# directory is tmpfs and is cleared at logout, which also settles the leftovers
# for good: a reboot with a mount still up can no longer strand a directory,
# because the directory does not survive either.
MNT_ROOT = Path(os.environ.get("XDG_RUNTIME_DIR")
                or Path.home() / ".cache") / "conn" / "mnt"

# The machine the panel itself runs on, listed as a host under this reserved
# name. It is not "ssh to yourself": run_argv hands the same scripts straight
# to bash, so the local rows cost no daemon, no key and no round trip.
LOCAL = "local"

CONNECT_TIMEOUT = 6      # seconds to get a TCP + auth handshake
CAPTURE_LINES = 200     # lines of each remote screen to bring back;
                        # the panel renders as many as it can fit
PROBE_TIMEOUT = 15       # hard ceiling on the whole probe


class HostError(RuntimeError):
    """Something the user needs to read, not a traceback."""


# ---------------------------------------------------------------------------
# Transport: where a script runs
# ---------------------------------------------------------------------------

def is_local(host: str) -> bool:
    return host == LOCAL


_OWN_SESSION: str | None = None


def own_session() -> str:
    """The local tmux session the panel is running inside, or "".

    Normally "" -- conn gets a window of its own. Started from inside tmux it
    is a real session, and listing it would offer to attach to the session you
    are already in: the one place an attach cannot go. So it is left out.
    """
    global _OWN_SESSION
    if _OWN_SESSION is None:
        _OWN_SESSION = ""
        if os.environ.get("TMUX"):
            try:
                out = subprocess.run(["tmux", "display-message", "-p", "#S"],
                                     stdin=subprocess.DEVNULL,
                                     capture_output=True, text=True, timeout=5)
                _OWN_SESSION = out.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
    return _OWN_SESSION


def run_argv(host: str, opts: list[str] | None = None) -> list[str]:
    """argv that runs, on `host`, the script the caller then feeds to stdin.

    Every probe, watch and send below is POSIX shell that never mentions how it
    got there, so this is the only place that knows the difference: locally the
    script goes to bash directly, remotely to `sh` over ssh. One copy of each
    script, two transports.

    The script travels on stdin rather than as ssh's command argument. The
    argument is handed to the remote *login* shell, and a fish or tcsh user's
    shell rejects POSIX syntax at parse time -- the host then read as down
    with a syntax error for a tooltip. "exec sh" is the one command line every
    shell reads the same way. Feeding stdin ourselves also keeps ssh off the
    terminal: left attached it reads the panel's keystrokes, which is what -n
    used to be here for. A script run this way must not read its own stdin,
    or it eats the rest of its text.

    "--" so a host name is never read as an option, whatever it starts with.
    """
    if is_local(host):
        return ["bash"]

    argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_TIMEOUT}"]
    argv += list(opts or [])
    return argv + ["--", host, "exec sh"]


# ---------------------------------------------------------------------------
# ~/.ssh/config
# ---------------------------------------------------------------------------

def _usable_alias(name: str) -> bool:
    """Whether a config alias is one the panel can act on.

    Wildcards are patterns, not hosts. A slash is dropped too -- MNT_ROOT /
    host would follow it out of the mountpoint tree -- and so is a leading
    dash, which reads as an option to ssh, sshfs and everything else an
    alias is handed to.

    bin/ssh-connect carries the attach side's guard: everything this lists
    it must accept, so its rejects are these plus only what breaks its own
    sinks. Change either and check the other.
    """
    return not re.search(r"[*?!/]", name) and not name.startswith("-")


def list_hosts() -> list[str]:
    """Host aliases in the order they appear in the config.

    File order beats alphabetical here -- people group related boxes together,
    and that grouping is information the panel should preserve.
    """
    try:
        text = SSH_CONFIG.read_text()
    except OSError:
        text = ""

    names: list[str] = []
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0].lower() == "host":
            for name in parts[1:]:
                if _usable_alias(name) and name not in names:
                    names.append(name)

    # This machine leads the list: it is the one host that is always up, and
    # the sessions on it are the ones you are most likely to be mid-thought in.
    # A config that already defines "local" keeps it -- an alias the user can
    # actually ssh to beats one the panel invented. CONN_NO_LOCAL=1 hides it.
    if LOCAL not in names and os.environ.get("CONN_NO_LOCAL") != "1":
        names.insert(0, LOCAL)
    return names


def resolve(host: str) -> dict:
    """Ask ssh itself what a name expands to, rather than re-parsing config.

    This picks up Match blocks, Include files and the Host * defaults, so the
    panel shows the connection that will actually be made.
    """
    info = {"user": "", "hostname": "", "port": "", "identityfile": "",
            "proxyjump": ""}
    if is_local(host):
        # Nothing to resolve, and no connection to describe. Saying so beats
        # printing a loopback address that no ssh will ever be made to.
        return info | {"hostname": "this machine", "port": "22"}
    try:
        out = subprocess.run(["ssh", "-G", host], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return info

    for line in out.splitlines():
        key, _, value = line.strip().partition(" ")
        key = key.lower()
        if key in info and value and not info[key]:
            info[key] = value

    # Fall back only where ssh told us nothing, so a real HostName always wins
    # over the alias we were asked about.
    info["hostname"] = info["hostname"] or host
    info["port"] = info["port"] or "22"
    return info


def target(info: dict) -> str:
    port = info.get("port", "22")
    suffix = "" if port == "22" else f":{port}"
    user = info.get("user", "")
    return f"{user}@{info.get('hostname', '')}{suffix}" if user else f"{info.get('hostname','')}{suffix}"


def backup_config() -> Path:
    """Copy the config aside before touching it. Cheap, and it means an edit
    that goes wrong is a one-line recovery rather than a retype."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = SSH_CONFIG.with_suffix(f".bak.{stamp}")
    shutil.copy2(SSH_CONFIG, dest)
    return dest


# The one reference for what may be written into a Host block. These values
# become lines in a file whose directives run commands (ProxyCommand), and the
# alias is later a shell argument and a path segment -- so what cannot be
# written safely is refused, not quoted. A pasted "hostname" with a newline in
# it was one ProxyCommand away from local code execution. add_host and
# update_host both go through here, so the two agree on what is safe.

def _check_alias(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9_.-]*", name):
        raise HostError("Name must be one word of letters, digits, . _ or -, "
                        "not starting with a dash.")


def _check_hostname(hostname: str) -> None:
    if not hostname:
        raise HostError("Hostname or IP is required.")
    if re.search(r"[^A-Za-z0-9_.:%-]", hostname) or hostname.startswith("-"):
        raise HostError("Hostname can only use letters, digits and . : % _ -, "
                        "and cannot start with a dash.")


def _check_user(user: str) -> None:
    if user and (re.search(r"[^A-Za-z0-9_.@-]", user)
                 or user.startswith("-")):
        raise HostError("User can only use letters, digits and . @ _ -, "
                        "and cannot start with a dash.")


def _check_port(port: str) -> None:
    # Empty is allowed: it means "no Port directive", the default of 22.
    if port and not port.isdigit():
        raise HostError("Port must be a number.")


def _check_identityfile(path: str) -> None:
    # Only the injection is refused -- a newline or other control character
    # would let the value forge a second directive line. Everything else is a
    # path we do not second-guess. Empty means "no IdentityFile directive".
    if re.search(r"[\x00-\x1f\x7f]", path):
        raise HostError("Identity file path cannot contain control characters.")


def _check_proxyjump(value: str, name: str) -> None:
    # Empty means "no ProxyJump directive" -- connect directly. "none" is ssh's
    # own word for the same, written when a Host * default would otherwise
    # apply. Anything else is a comma-separated chain of [user@]host[:port]
    # hops, usually just another alias from this file; whitespace is refused
    # because it would end the value and start a second argument.
    if not value or value == "none":
        return
    for hop in value.split(","):
        if (not hop or hop.startswith("-")
                or re.search(r"[^A-Za-z0-9_.@:%-]", hop)):
            raise HostError("Connect through must be a server name, or "
                            "user@host:port hops separated by commas.")
        if hop == name:
            raise HostError(f"'{name}' cannot connect through itself.")


def add_host(name: str, hostname: str, user: str, port: str = "22") -> Path:
    """Insert a new Host block above the Host * defaults.

    Order matters to ssh: it is first-match-wins, so a block placed after
    Host * would have the defaults applied before its own settings.
    """
    name = name.strip()
    hostname = hostname.strip()
    user = user.strip()
    port = (port or "22").strip()

    _check_alias(name)
    _check_hostname(hostname)
    _check_user(user)
    if name in list_hosts():
        raise HostError(f"'{name}' is already in ~/.ssh/config.")
    _check_port(port)

    block = [f"Host {name}", f"    HostName {hostname}"]
    if user:
        block.append(f"    User {user}")
    if port != "22":
        block.append(f"    Port {port}")

    backup = backup_config()
    lines = SSH_CONFIG.read_text().splitlines()

    insert_at = _above_defaults(lines)
    merged = lines[:insert_at] + [""] + block + lines[insert_at:]
    SSH_CONFIG.write_text("\n".join(merged).rstrip("\n") + "\n")
    SSH_CONFIG.chmod(0o600)
    return backup


def _above_defaults(lines: list[str]) -> int:
    """Where a block goes to sit above the wildcard defaults: the first Host
    line with a * in it, walked back over the blank lines and the comment
    banner introducing it -- the banner belongs to Host *, so a new block goes
    above it, not underneath a heading that says "keep this last"."""
    for index, line in enumerate(lines):
        parts = line.strip().split()
        if (len(parts) >= 2 and parts[0].lower() == "host"
                and any("*" in p for p in parts[1:])):
            while index > 0:
                above = lines[index - 1].strip()
                if above and not above.startswith("#"):
                    break
                index -= 1
            return index
    return len(lines)


def config_hosts() -> list[str]:
    """Only the aliases the file actually names.

    list_hosts() invents "local" when the config has no such entry, which is
    right for the panel and wrong for anything that edits the file: you cannot
    remove a line that was never written.
    """
    try:
        text = SSH_CONFIG.read_text()
    except OSError:
        return []
    names: list[str] = []
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0].lower() == "host":
            for name in parts[1:]:
                if _usable_alias(name) and name not in names:
                    names.append(name)
    return names


def remove_host(name: str) -> Path:
    """Take a Host block back out of ~/.ssh/config.

    The counterpart add_host() never had, so every server the panel ever added
    was permanent as far as it was concerned.

    A Host line may name several aliases. Dropping one of those is an edit to
    that line, not the removal of a block the other names still depend on --
    deleting it wholesale would quietly take unrelated servers with it.
    """
    name = name.strip()
    if not name or re.search(r"[*?!\s]", name):
        raise HostError("Name must be a single word with no * ? or spaces.")

    try:
        lines = SSH_CONFIG.read_text().splitlines()
    except OSError as exc:
        raise HostError(f"Cannot read ~/.ssh/config: {exc}") from exc

    def aliases(line: str) -> list[str] | None:
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0].lower() == "host":
            return parts[1:]
        return None

    start = None
    for index, line in enumerate(lines):
        named = aliases(line)
        if named and name in named:
            start = index
            break
    if start is None:
        raise HostError(f"'{name}' is not in ~/.ssh/config.")

    backup = backup_config()
    shared = [n for n in aliases(lines[start]) if n != name]

    if shared:
        lines[start] = "Host " + " ".join(shared)
    else:
        end = start + 1
        while end < len(lines) and aliases(lines[end]) is None:
            end += 1
        # Blank lines and comments sitting directly above the next block
        # introduce it, not us -- add_host() walks over the same banner going
        # the other way. Leave them with the block they belong to.
        while end > start + 1:
            above = lines[end - 1].strip()
            if above and not above.startswith("#"):
                break
            end -= 1
        # The blank line above ours is the separator add_host() wrote.
        while start > 0 and not lines[start - 1].strip():
            start -= 1
        del lines[start:end]

    SSH_CONFIG.write_text("\n".join(lines).rstrip("\n") + "\n")
    SSH_CONFIG.chmod(0o600)
    return backup


# Which ssh directive a Site Manager field is written as, and the order new
# ones are inserted in. The keys are what the GUI and host_config() speak; the
# values are how ssh spells them (case is cosmetic -- ssh reads them either
# way, but a file a person also edits should look like add_host wrote it).
_DIRECTIVE = {"hostname": "HostName", "user": "User",
              "port": "Port", "identityfile": "IdentityFile",
              "proxyjump": "ProxyJump"}


def _host_aliases(line: str) -> list[str] | None:
    """The aliases a 'Host' line names, or None if the line is not one."""
    parts = line.strip().split()
    if len(parts) >= 2 and parts[0].lower() == "host":
        return parts[1:]
    return None


def _starts_block(line: str) -> bool:
    """Whether a line opens a new scope -- the boundary a Host block ends at.

    ssh applies a block's directives until the next Host *or Match* line, so a
    block that runs up to the next Host alone would reach into a Match block
    sitting between the two: an edit meant for one host could then rewrite or
    delete a directive that belongs to the Match.
    """
    first = line.strip().split()[:1]
    return bool(first) and first[0].lower() in ("host", "match")


def _find_block(lines: list[str], name: str) -> tuple[int, int] | None:
    """(start, end) of the block `name` opens: its Host line, to the line that
    begins the next block. None if no Host line names `name`."""
    start = None
    for index, line in enumerate(lines):
        named = _host_aliases(line)
        if named and name in named:
            start = index
            break
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and not _starts_block(lines[end]):
        end += 1
    return start, end


def host_config(name: str) -> dict:
    """The block's OWN hostname/user/port/identityfile/proxyjump, as written.

    For editing the file, not connecting: Host * defaults and Match blocks are
    deliberately left out, so what comes back is only what this block sets and
    an empty string where it sets nothing. resolve() is the other question --
    what ssh will actually use -- and folds those in.
    """
    name = name.strip()
    if name not in config_hosts():
        raise HostError(f"'{name}' is not in ~/.ssh/config.")
    lines = SSH_CONFIG.read_text().splitlines()
    start, end = _find_block(lines, name)
    fields = {"hostname": "", "user": "", "port": "", "identityfile": "",
              "proxyjump": ""}
    for line in lines[start + 1:end]:
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            key = parts[0].lower()
            # First wins, the way ssh reads a repeated directive within a block.
            if key in fields and not fields[key]:
                fields[key] = parts[1].strip()
    return fields


def host_line_aliases(name: str) -> list[str]:
    """The other aliases sharing `name`'s Host line, [] if it stands alone.

    A block is edited by the Host line it opens, and that line may name several
    hosts -- the GUI shows these so an edit or a probe reads as applying to all
    of them, which it does.
    """
    name = name.strip()
    try:
        lines = SSH_CONFIG.read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        named = _host_aliases(line)
        if named and name in named:
            return [alias for alias in named if alias != name]
    raise HostError(f"'{name}' is not in ~/.ssh/config.")


def update_host(name: str, fields: dict) -> Path:
    """Set, replace or remove directives in `name`'s block, in place.

    `fields` may carry any of hostname/user/port/identityfile/proxyjump. A
    validated
    non-empty value sets or replaces that directive; an empty one removes it if
    present. Port "22" removes it too -- 22 is ssh's default, which add_host
    also leaves unwritten. Every other line in the block -- comments,
    ProxyCommand, directives we do not touch -- and every other block is kept
    exactly. Values are checked before the file is opened, so a bad one refuses
    rather than being quoted into a line that could run a command.

    The block edited is the one this alias's Host line opens, even when that
    line is shared; that is correct ssh semantics, and host_line_aliases() is
    how the GUI surfaces the sharing.
    """
    name = name.strip()
    if name not in config_hosts():
        raise HostError(f"'{name}' is not in ~/.ssh/config.")

    changes: dict[str, str] = {}
    for key in ("hostname", "user", "port", "identityfile", "proxyjump"):
        if key not in fields:
            continue
        value = (fields[key] or "").strip()
        if key == "hostname" and value:
            _check_hostname(value)
        elif key == "user" and value:
            _check_user(value)
        elif key == "port":
            _check_port(value)
            if value == "22":
                value = ""      # the default -- carried by absence, not a line
        elif key == "identityfile" and value:
            _check_identityfile(value)
        elif key == "proxyjump":
            _check_proxyjump(value, name)
        changes[key] = value

    backup = backup_config()
    lines = SSH_CONFIG.read_text().splitlines()
    start, end = _find_block(lines, name)
    body = lines[start + 1:end]

    inserts: list[str] = []
    for key, value in changes.items():
        # Every occurrence, not the first: ssh is first-match-wins, so leaving
        # a stale second copy of a directive behind would quietly override the
        # edit -- clearing HostName but leaving a duplicate line is a rename
        # that did not take. A block may legitimately repeat IdentityFile too.
        matches = [i for i, line in enumerate(body)
                   if line is not None
                   and (line.strip().split(None, 1)[:1] or [""])[0].lower() == key]
        new_line = f"    {_DIRECTIVE[key]} {value}"
        if value and not matches:
            inserts.append(new_line)
        elif value:
            body[matches[0]] = new_line          # the first carries the value
            for extra in matches[1:]:
                body[extra] = None               # the rest are stale copies
        else:
            for index in matches:                # remove: every occurrence goes
                body[index] = None
    body = [line for line in body if line is not None]

    # New directives go directly under the Host line, in the order above, so
    # the result reads the way add_host lays a fresh block out.
    merged = lines[:start + 1] + inserts + body + lines[end:]
    SSH_CONFIG.write_text("\n".join(merged).rstrip("\n") + "\n")
    SSH_CONFIG.chmod(0o600)
    return backup


# The default route: one wildcard block, "Host * !via" with "ProxyJump via",
# that sends every server through `via` unless the server's own block says
# otherwise -- ssh is first-match-wins, and the server blocks sit above it.
# The "!via" keeps the jump host itself out of it, or it would try to reach
# itself through itself. conn writes its explanation inside the block, so
# taking the block out takes the comment with it.

def _wildcard_only(line: str) -> list[str] | None:
    """A Host line's patterns if every one is * or a !negation, else None."""
    named = _host_aliases(line)
    if named and "*" in named and all(a == "*" or a.startswith("!")
                                      for a in named):
        return named
    return None


def default_jump() -> str:
    """The server every host connects through unless it says otherwise, or ""
    for direct. Read from the first wildcard block that sets ProxyJump."""
    try:
        lines = SSH_CONFIG.read_text().splitlines()
    except OSError:
        return ""
    in_wild = False
    for line in lines:
        if _starts_block(line):
            in_wild = _wildcard_only(line) is not None
            continue
        parts = line.strip().split(None, 1)
        if in_wild and len(parts) == 2 and parts[0].lower() == "proxyjump":
            value = parts[1].strip()
            return "" if value == "none" else value
    return ""


def set_default_jump(via: str) -> Path:
    """Send every server through `via` by default, or straight out for "".

    Any ProxyJump in a wildcard block goes first: a "Host * !x" block of
    conn's goes whole, and a hand-written one in the plain Host * defaults
    loses just that line, so there is only ever one default. Then, for a
    `via`, conn's block is written just above the defaults. A server's own
    ProxyJump -- "none" is Direct -- still wins, since its block is above.
    """
    via = via.strip()
    if via and via not in config_hosts():
        raise HostError(f"'{via}' is not in ~/.ssh/config.")

    backup = backup_config()
    lines = SSH_CONFIG.read_text().splitlines()
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        named = _wildcard_only(line)
        if named is None:
            kept.append(line)
            index += 1
            continue
        end = index + 1
        while end < len(lines) and not _starts_block(lines[end]):
            end += 1
        body = lines[index + 1:end]
        directives = [b for b in body
                      if b.strip() and not b.strip().startswith("#")]
        jumps = [b for b in directives
                 if b.strip().split(None, 1)[0].lower() == "proxyjump"]
        if any(a.startswith("!") for a in named) and directives == jumps:
            # A route block: nothing in it but the jump. Its Host line and its
            # indented lines go; unindented ones after it -- the banner over
            # the Host * defaults, the blank line before it -- belong to what
            # follows, and stay.
            while kept and not kept[-1].strip():
                kept.pop()
            kept.extend(b for b in body if b[:1] not in (" ", "\t"))
        else:
            kept.append(line)
            kept.extend(b for b in body if b not in jumps)
        index = end

    if via:
        at = _above_defaults(kept)
        block = [f"Host * !{via}",
                 "    # conn: the default route (Site Manager > All servers",
                 "    # connect). A server's own ProxyJump overrides it --",
                 "    # ProxyJump none is Direct. One-off: ssh -J none <host>",
                 f"    ProxyJump {via}"]
        while at > 0 and not kept[at - 1].strip():
            at -= 1
        kept = kept[:at] + [""] + block + kept[at:]
    SSH_CONFIG.write_text("\n".join(kept).rstrip("\n") + "\n")
    SSH_CONFIG.chmod(0o600)
    return backup


def drop_master(host: str) -> None:
    """Close the shared connection ssh keeps open to `host`, if there is one.

    ~/.ssh/config multiplexes (ControlMaster/ControlPersist), so after a route
    changes -- a ProxyJump added or taken away -- every new ssh would keep
    riding the old connection for up to ControlPersist. Closing it makes the
    next one take the new route. No master is not an error.
    """
    try:
        subprocess.run(["ssh", "-O", "exit", "--", host],
                       stdin=subprocess.DEVNULL, capture_output=True,
                       timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def slept_seconds() -> float:
    """Total time this machine has spent suspended since boot.

    CLOCK_BOOTTIME counts suspend and CLOCK_MONOTONIC does not, so their
    difference only grows while the laptop sleeps -- a jump in it between two
    looks is a wake, with no D-Bus and nothing to subscribe to."""
    try:
        return (time.clock_gettime(time.CLOCK_BOOTTIME)
                - time.clock_gettime(time.CLOCK_MONOTONIC))
    except (AttributeError, OSError):
        return 0.0


def local_addresses() -> frozenset[str]:
    """This machine's global addresses, IPv4 and IPv6. A change is a new
    network: every TCP connection bound to an old one is dead."""
    try:
        out = subprocess.run(["ip", "-o", "addr", "show", "up", "scope",
                              "global"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    found = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) > 3 and parts[2] in ("inet", "inet6"):
            found.add(parts[3].split("/")[0])
    return frozenset(found)


def control_sockets() -> list[Path]:
    """Every ssh ControlMaster socket, from where ~/.ssh/config puts them."""
    try:
        out = subprocess.run(["ssh", "-G", "conn-mux-check.invalid"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    path = ""
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        if key == "controlpath":
            path = value.strip()
    if not path or path == "none":
        return []
    folder = Path(os.path.expanduser(path)).parent
    try:
        return sorted(p for p in folder.iterdir() if p.is_socket())
    except OSError:
        return []


MASTER_CHECK_SECONDS = 6


def revive_masters(sockets: list[Path] | None = None,
                   timeout: float = MASTER_CHECK_SECONDS,
                   run=subprocess.run, log=None) -> list[str]:
    """Close every shared ssh connection that no longer answers; return the
    names of those closed.

    After a sleep or a network change a master's TCP is usually dead but
    nothing has told it: `ssh -O check` still says "Master running", and
    every new ssh -- a stream, a probe, the ProxyJump hop under all of them
    -- hands its request to it and waits, for minutes, until keepalives
    finally give up. So each one is asked to run `true`, for real, through
    the socket; one that cannot answer inside `timeout` is told to exit
    (which works however dead its TCP is -- the request is local), and the
    next ssh makes a fresh connection. One that answers is left alone, with
    any terminal riding it. All at once, so the whole check costs one
    timeout, not one per master.

    Each socket's outcome goes to `log` (a callable taking one line;
    default the connection-event log, see connlog).
    """
    say = log or connlog.event
    started = time.monotonic()
    sockets = control_sockets() if sockets is None else sockets
    say(f"masters: checking {len(sockets)} socket"
        f"{'' if len(sockets) == 1 else 's'}")
    dropped: list[str] = []
    finished: set[str] = set()
    lock = threading.Lock()
    # A host name that cannot resolve: if the socket has no master behind
    # it, ssh would otherwise make a connection of its own to somewhere.
    nowhere = "conn-mux-check.invalid"

    def one(sock: Path) -> None:
        base = ["ssh", "-o", f"ControlPath={sock}", "-o", "ControlMaster=no",
                "-o", "BatchMode=yes", "-o", "ConnectTimeout=3"]
        # Dead means ssh itself failed (255) or no answer in time -- and a
        # slow one gets a second, longer go first. Any other exit is the
        # far side answering (a forced command, a refused extra session,
        # a shell that dislikes `true`): the link is fine.
        name = connlog.clean(sock.name, 120)
        alive = False
        tried: list[str] = []
        began = time.monotonic()
        for limit in (timeout, timeout * 2):
            began = time.monotonic()
            try:
                code = run(base + [nowhere, "true"], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=limit).returncode
            except subprocess.TimeoutExpired:
                tried.append(f"timed out after {limit:g}s")
                continue
            except (OSError, subprocess.SubprocessError) as exc:
                tried.append(f"error {type(exc).__name__}")
                break
            took = time.monotonic() - began
            alive = code != 255
            tried.append(f"exit {code} in {took:.1f}s")
            break
        if alive:
            say(f"master {name}: alive ({', '.join(tried)})")
            with lock:
                finished.add(sock.name)
            return
        try:
            done = run(["ssh", "-O", "exit", "-o", f"ControlPath={sock}",
                        nowhere],
                       stdin=subprocess.DEVNULL, capture_output=True,
                       timeout=5)
            closed = f"ssh -O exit -> {done.returncode}"
        except (OSError, subprocess.SubprocessError) as exc:
            # A master too wedged to take even the local exit request: end
            # the process that owns the socket.
            pids = _end_socket_owner(sock)
            closed = (f"ssh -O exit failed ({type(exc).__name__}) -> SIGKILL "
                      + (",".join(map(str, pids)) if pids else "no owner found"))
        say(f"master {name}: dead ({', '.join(tried) or 'no answer'}); {closed}")
        with lock:
            dropped.append(sock.name)
            finished.add(sock.name)

    threads = [threading.Thread(target=one, args=(s,), daemon=True,
                                name="master-check")
               for s in sockets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout * 3 + 7)
    with lock:
        stuck = [connlog.clean(s.name, 120) for s in sockets
                 if s.name not in finished]
    for name in stuck:
        say(f"master {name}: check still running, abandoned")
    say(f"masters: {len(sockets)} checked, {len(dropped)} closed in "
        f"{time.monotonic() - started:.1f}s")
    return sorted(dropped)


def _jump_target() -> tuple[str, int] | None:
    """(hostname, port) of the default ProxyJump hop, if there is one."""
    def conf(host: str) -> dict[str, str]:
        try:
            out = subprocess.run(["ssh", "-G", host], capture_output=True,
                                 text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return {}
        return {k: v.strip() for k, _, v in
                (line.partition(" ") for line in out.splitlines())}
    jump = conf("conn-mux-check.invalid").get("proxyjump", "none")
    if not jump or jump == "none":
        return None
    hop = conf(jump.split(",")[0].split("@")[-1])
    try:
        return hop.get("hostname", ""), int(hop.get("port", "22"))
    except ValueError:
        return None


def network_ready(wait: float = 90.0, sleep=time.sleep, log=None) -> bool:
    """True once there is a default route and (when everything goes through
    a jump host) that host's ssh port takes a TCP connection; False if that
    has not happened within `wait`.

    Right after a wake Wi-Fi is often still associating, and judging ssh
    masters then would condemn every one of them -- and every terminal
    riding them -- for a network that is about to come back.

    The answer, how long it took and what was in the way go to `log`
    (default the connection-event log)."""
    import socket
    say = log or connlog.event
    started = time.monotonic()
    target = _jump_target()
    jump = (f"{connlog.clean(target[0], 80)}:{target[1]}"
            if target and target[0] else "none")
    say(f"network: waiting for a route and the jump host ({jump}), "
        f"up to {wait:g}s")
    deadline = started + wait
    tries = 0
    why = "no default route"
    while time.monotonic() < deadline:
        tries += 1
        try:
            route = subprocess.run(["ip", "route", "show", "default"],
                                   capture_output=True, text=True,
                                   timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            route = ""
        if route:
            if target is None or not target[0]:
                say(f"network: ready after {time.monotonic() - started:.1f}s "
                    f"({tries} tries; route, no jump host)")
                return True
            try:
                with socket.create_connection(target, timeout=4):
                    say(f"network: ready after "
                        f"{time.monotonic() - started:.1f}s ({tries} tries; "
                        f"route, {jump} takes TCP)")
                    return True
            except OSError as exc:
                why = (f"route up, {jump} refused TCP: "
                       f"{connlog.clean(exc, 80)}")
        else:
            why = "no default route"
        sleep(1.0)
    say(f"network: NOT ready after {time.monotonic() - started:.1f}s "
        f"({tries} tries; last: {why}) -- masters left to ServerAlive")
    return False


def _end_socket_owner(sock: Path) -> list[int]:
    """SIGKILL whichever of our ssh processes is listening on `sock`; return
    the pids killed."""
    killed: list[int] = []
    try:
        out = subprocess.run(["ss", "-xlpn"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return killed
    for line in out.splitlines():
        if str(sock) in line:
            for pid in re.findall(r'"ssh",pid=(\d+)', line):
                try:
                    os.kill(int(pid), 9)
                    killed.append(int(pid))
                except OSError:
                    pass
    try:
        sock.unlink()
    except OSError:
        pass
    return killed


def rename_host(old: str, new: str) -> Path:
    """Swap the `old` alias token for `new` on its Host line, nothing else.

    Any other alias on the line stays, and the block's directives are
    untouched -- this is a relabel, not a move. The one exception is other
    blocks that connect through it: a ProxyJump hop naming `old` is pointed at
    `new`, and so is a "!old" that keeps it out of a wildcard default jump --
    or renaming a shared jump host would quietly cut off every server behind
    it, or have it try to jump through itself. The config is only half the
    rename: the keyring secrets, stars and hand-arranged order are filed under
    the old name and are the caller's to migrate (migrate_secrets does the
    keyring half).
    """
    old = old.strip()
    new = new.strip()
    _check_alias(new)
    if new in list_hosts():
        raise HostError(f"'{new}' is already in ~/.ssh/config.")

    try:
        lines = SSH_CONFIG.read_text().splitlines()
    except OSError as exc:
        raise HostError(f"Cannot read ~/.ssh/config: {exc}") from exc

    target_line = None
    for index, line in enumerate(lines):
        named = _host_aliases(line)
        if named and old in named:
            target_line = index
            break
    if target_line is None:
        raise HostError(f"'{old}' is not in ~/.ssh/config.")

    backup = backup_config()
    swapped = [new if alias == old else alias
               for alias in _host_aliases(lines[target_line])]
    lines[target_line] = "Host " + " ".join(swapped)
    for index, line in enumerate(lines):
        named = _host_aliases(line)
        if named and f"!{old}" in named:
            lines[index] = "Host " + " ".join(
                f"!{new}" if alias == f"!{old}" else alias for alias in named)
            continue
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "proxyjump":
            hops = parts[1].strip().split(",")
            if old in hops:
                indent = line[:len(line) - len(line.lstrip())]
                lines[index] = (f"{indent}{parts[0]} "
                                + ",".join(new if h == old else h for h in hops))
    SSH_CONFIG.write_text("\n".join(lines).rstrip("\n") + "\n")
    SSH_CONFIG.chmod(0o600)
    return backup


# ---------------------------------------------------------------------------
# Probing a live host
# ---------------------------------------------------------------------------

# One round trip collects everything the panel shows. Each section is opened
# by a marker line so the parse below cannot be confused by a value that
# happens to contain a newline -- and every marker carries __BOUND__, a random
# token minted fresh for each run and never shown to the far side's screens.
# The capture sections replay whatever a watched terminal chooses to display,
# and a screen that merely *prints* marker text -- an editor open on this
# file, an agent fed hostile output -- must not be able to end a section,
# speak as another session, or forge one that does not exist.
#
# The pane list rides inside the capture loop, under the same guarded
# markers, so the session name never shares a '|'-delimited line with fields
# that are free to contain '|' themselves.
#
# The chat metadata (META_FORMAT) is one list-sessions call ahead of the loop,
# before the first SESSION marker, where nothing is attributed to a session.
# Linux and macOS both: a Mac has no /proc/loadavg, nproc, free or
# uptime -p, so each of those falls back to sysctl and vm_stat there.
REMOTE_PROBE = r"""
echo "###__BOUND__:HOME";   printf '%s\n' "$HOME"
echo "###__BOUND__:HOSTNAME"; uname -n 2>/dev/null
echo "###__BOUND__:UPTIME"; uptime -p 2>/dev/null || uptime 2>/dev/null | sed -E 's/.* up +//; s/, *[0-9]+ users?.*//; s/, *load average.*//'
echo "###__BOUND__:LOAD";   if [ -r /proc/loadavg ]; then cut -d' ' -f1-3 /proc/loadavg; else sysctl -n vm.loadavg 2>/dev/null | tr -d '{}' | awk '{print $1" "$2" "$3}'; fi
echo "###__BOUND__:CPUS";   nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null
echo "###__BOUND__:MEM";    if command -v free >/dev/null 2>&1; then free -m | awk '/^Mem:/{print $3" "$2}'; else vm_stat 2>/dev/null | awk -v t="$(sysctl -n hw.memsize 2>/dev/null)" -v ps="$(sysctl -n hw.pagesize 2>/dev/null)" '/^Pages active/{a=$3} /^Pages wired down/{w=$4} /^Pages occupied by compressor/{c=$5} END{if (t && ps) print int((a+w+c)*ps/1048576)" "int(t/1048576)}'; fi
echo "###__BOUND__:DISK";   df -h / 2>/dev/null | tail -1 | awk '{print $3" "$2" "$5}'
echo "###__BOUND__:TMUX";   tmux list-sessions -F "#{session_name}|#{session_windows}|#{?session_attached,attached,detached}|#{session_activity}" 2>/dev/null
echo "###__BOUND__:CLIENTS"; tmux list-clients -F "#{client_activity}|#{client_session}" 2>/dev/null
echo "###__BOUND__:TYPED";  tmux show-options -gv @conn_typed 2>/dev/null
echo "###__BOUND__:NOW";    date +%s
echo "###__BOUND__:CAPTURE"
tmux list-sessions -F "__META__" 2>/dev/null
tmux list-sessions -F "#{session_name}" 2>/dev/null | while read -r s; do
  printf '%s\n' "@@@__BOUND__:SESSION:$s"
  tmux capture-pane -p -e -t "$s" 2>/dev/null | tail -__CAPLINES__
  printf '%s\n' "@@@__BOUND__:PANES:$s"
  tmux list-panes -t "$s" -F "#{pane_current_command}|#{pane_current_path}" 2>/dev/null
done
echo "###__BOUND__:END"
"""

SECTIONS = ("HOME", "HOSTNAME", "UPTIME", "LOAD", "CPUS", "MEM", "DISK", "TMUX",
            "CLIENTS", "TYPED", "NOW", "CAPTURE", "END")

# The chat metadata of every session, in one tmux call: the SPEC's @conn_*
# user options plus what the title, project and recency fall back on. In a
# session context tmux resolves pane_* against the active pane of the current
# window, which is the pane the chat is about.
#
# A record is fenced by token markers, and so is every field inside it,
# because these values are free text in every sense: a title can hold '|',
# a tab, or a raw newline (tmux passes all three through untouched), and
# pane_title is whatever a program on that screen chose to print. Only the
# token cannot appear in them -- it is minted per run and never shown to the
# far side -- so a value can neither end its record early nor speak as
# another session. The script templates splice this in for __META__ before
# __BOUND__ is replaced, so it carries the same token as everything else.
META_FIELDS = (
    ("name", "#{session_name}"),
    ("created", "#{session_created}"),
    ("project", "#{@conn_project}"),
    ("title", "#{@conn_title}"),
    ("task", "#{@conn_task}"),
    ("last_event", "#{@conn_last_event}"),
    ("starred", "#{@conn_starred}"),
    ("source", "#{@conn_source}"),
    # Not "name": that key is the session name, and records are filed by it.
    ("conn_name", "#{@conn_name}"),
    # Set by `conn --restore-sessions` on a session it rebuilt after a reboot.
    ("restored", "#{@conn_restored}"),
    # Conn's title for what the chat is about now (retitle.py).
    ("autotitle", "#{@conn_autotitle}"),
    ("autotitle_for", "#{@conn_autotitle_for}"),
    ("pane_title", "#{pane_title}"),
    ("path", "#{pane_current_path}"),
)
META_FORMAT = ("@@@__BOUND__:REC"
               + "@@@__BOUND__:F".join(fmt for _key, fmt in META_FIELDS)
               + "@@@__BOUND__:EOR")

# The options set_chat_option may touch: the SPEC's set, and nothing that
# would let a caller reach tmux's own options through the same door.
CHAT_OPTIONS = ("project", "title", "task", "last_event", "starred", "source",
                "name", "autotitle", "autotitle_for")


def _parse_meta(text: str, token: str) -> dict[str, dict]:
    """Every META_FORMAT record in `text` -> {session: {field: str}}.

    Records are found by their token alone, wherever they sit, so the caller
    need not cut out a block first: nothing but the metadata call can print
    them. A record cut off before its end marker, or with the wrong number
    of fields, is dropped rather than guessed at -- a session without
    metadata reads as one with no options set, which is merely less helpful.
    """
    rec, sep, eor = (f"@@@{token}:REC", f"@@@{token}:F", f"@@@{token}:EOR")
    found: dict[str, dict] = {}
    for chunk in text.split(rec)[1:]:
        body, ended, _rest = chunk.partition(eor)
        if not ended:
            continue
        fields = body.split(sep)
        if len(fields) != len(META_FIELDS):
            continue
        record = dict(zip((key for key, _fmt in META_FIELDS), fields))
        # Stripped as the SESSION marker's name is, or the two never meet.
        found[record["name"].strip()] = record
    return found


def _int_or(text: str, default):
    # isascii too: isdigit() is True for '²' and '١', which int() rejects or
    # reads as something no client meant -- and these values come from any
    # client that sets an option, while probe() promises never to raise.
    text = (text or "").strip()
    return int(text) if text.isascii() and text.isdigit() else default


def _session_meta(record: dict | None) -> dict:
    """One session's metadata in the shape probe and parse_frame both hand
    out. Missing means unset: "" for text, None for a last_event that was
    never written (the caller falls back to `created`), False for a star."""
    record = record or {}
    return {
        "options": {
            "project": record.get("project", ""),
            "title": record.get("title", ""),
            "task": record.get("task", ""),
            "last_event": _int_or(record.get("last_event", ""), None),
            "starred": record.get("starred", "").strip() == "1",
            "source": record.get("source", ""),
            "name": record.get("conn_name", ""),
            "restored": _int_or(record.get("restored", ""), 0),
            "autotitle": record.get("autotitle", ""),
            "autotitle_for": record.get("autotitle_for", ""),
        },
        "pane_title": record.get("pane_title", ""),
        "path": record.get("path", ""),
        "created": _int_or(record.get("created", ""), 0),
    }


def _split_captures(text: str, token: str) -> tuple[dict[str, str],
                                                    dict[str, list[dict]]]:
    """Pull the per-session screen grabs and pane lists out of the raw probe
    output, keyed by session name.

    Unlike every other section the screens must survive verbatim: blank lines
    and leading spaces are part of what makes a terminal screen readable, and
    the state classifier reads them. capture-pane runs with -e so they keep
    their colour too: dim is the only thing telling a suggestion sitting in
    the input box apart from something you typed and left there.

    A pane line is command then path. The command goes first because it is
    the one the classifier depends on, and a path is the field with '|' in
    the wild -- partition keeps whatever follows the first '|' as the path,
    '|'s and all.
    """
    start = text.find(f"###{token}:CAPTURE")
    if start < 0:
        return {}, {}
    body = text[start:]
    end = body.find(f"###{token}:END")
    if end >= 0:
        body = body[:end]

    session_mark = f"@@@{token}:SESSION:"
    panes_mark = f"@@@{token}:PANES:"
    screens: dict[str, list[str]] = {}
    panes: dict[str, list[dict]] = {}
    current = None
    mode = None
    for line in body.splitlines():
        if line.startswith(session_mark):
            current = line[len(session_mark):].strip()
            screens.setdefault(current, [])
            mode = "screen"
        elif line.startswith(panes_mark):
            current = line[len(panes_mark):].strip()
            panes.setdefault(current, [])
            mode = "panes"
        elif current is not None and mode == "screen":
            screens[current].append(line.rstrip())
        elif current is not None and mode == "panes" and line.strip():
            cmd, _, path = line.partition("|")
            panes[current].append({"cmd": cmd, "path": path})
    return ({name: "\n".join(lines).strip("\n")
             for name, lines in screens.items()}, panes)


def _split_sections(text: str, token: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {name: [] for name in SECTIONS}
    prefix = f"###{token}:"
    current = None
    for line in text.split(f"{prefix}CAPTURE")[0].splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix) and stripped[len(prefix):] in found:
            current = stripped[len(prefix):]
            continue
        if current and stripped:
            found[current].append(stripped)
    return found


def blank(host: str) -> dict:
    """The row shape, before anything is known about the host."""
    return {
        "host": host, "state": "unknown", "target": "", "error": "",
        "uptime": "", "load": "", "cpus": "", "mem": "", "disk": "",
        "sessions": [], "checked": 0.0, "mounted": False, "home": "",
        "hostname": "",
        # Set with state "unverified" when the key ssh refused is not new but
        # *different* from the one known_hosts pinned -- see key_changed().
        "keychanged": False,
    }


# What ssh prints, above "Host key verification failed.", when the key it was
# offered contradicts the one it has on file -- as opposed to having none.
KEY_CHANGED_MARKS = ("REMOTE HOST IDENTIFICATION HAS CHANGED",
                     "has changed and you have requested strict checking")


def key_changed(stderr: str) -> bool:
    """Whether a failed probe's ssh complained of a *changed* host key.

    Both a new key and a changed one end on the same last line, "Host key
    verification failed.", so the last line alone cannot tell them apart --
    and they want opposite advice. A new key is first contact: answer the
    fingerprint prompt. A changed one is either a rebuilt server or someone
    in the middle, and ssh will not even offer the prompt; the old entry has
    to go first, and only a person comparing fingerprints should decide that.
    """
    return any(mark in (stderr or "") for mark in KEY_CHANGED_MARKS)


def _probe_failure(last: str, host: str,
                   stderr: str = "") -> tuple[str, str]:
    """The state and reason for a probe that reached ssh but never opened the
    session -- three problems with three different fixes.

    A key ssh will not trust yet is first contact, not a dead box: a server you
    just added answers, but the probe runs BatchMode and will not accept the
    key for you, so it gets its own state and says how to log in -- rather than
    sitting there red as "down", which reads as broken. Being let to the door
    and refused (Permission denied) is the "no key" state: the box is up, the
    login is not. Everything else is genuinely down. There is no authentication
    on this machine, so a local failure is never a key problem.
    """
    if "Host key verification failed" in last and key_changed(stderr):
        # Still "unverified" -- every consumer of the state (dark, needs a
        # login, the phone's needs_login) is right about it -- with
        # row["keychanged"] set by probe() for the one place that differs:
        # the fix offered.
        return ("unverified",
                "host key changed -- the server offered a different key from "
                "the one ssh has on file. Rebuilt, or someone in the middle: "
                "use Check new key in the Servers tab to compare them.")
    if "Host key verification failed" in last:
        return ("unverified",
                "new server -- ssh does not trust its key yet. Use Trust\u2026 "
                "on its row in the Servers tab to answer the fingerprint "
                "prompt and log in; every probe after that rides the key you "
                "accept.")
    if "Permission denied" in last and not is_local(host):
        return ("nokey", last)
    return ("down", last)


def probe(host: str) -> dict:
    """Query one host. Never raises -- failure is a state, not an exception,
    because a dashboard that crashes on an offline box is useless."""
    row = blank(host)
    row["target"] = target(resolve(host))
    row["mounted"] = is_mounted(host)
    row["checked"] = time.time()

    token = secrets.token_hex(16)
    script = (REMOTE_PROBE.replace("__META__", META_FORMAT)
                          .replace("__CAPLINES__", str(CAPTURE_LINES))
                          .replace("__BOUND__", token))
    # StrictHostKeyChecking=yes, never accept-new: this path runs unattended
    # for every configured host, and silently pinning whatever key the
    # network offers is the one trust decision ssh exists to put in front of
    # a human. An unknown key is a down-state with the fix in the tooltip;
    # the fingerprint prompt itself belongs to the first interactive connect.
    try:
        done = subprocess.run(
            run_argv(host, opts=["-o", "StrictHostKeyChecking=yes"]),
            input=script,
            capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        row["state"] = "down"
        row["error"] = "timed out"
        return row
    except OSError as exc:
        row["state"] = "down"
        row["error"] = str(exc)
        return row

    if f"###{token}:END" not in done.stdout:
        stderr = (done.stderr or "").strip().splitlines()
        last = stderr[-1] if stderr else "unreachable"
        row["state"], row["error"] = _probe_failure(last, host,
                                                    done.stderr or "")
        row["keychanged"] = (row["state"] == "unverified"
                             and key_changed(done.stderr or ""))
        return row

    parts = _split_sections(done.stdout, token)
    row["state"] = "up"
    # The remote $HOME, which a project folder written "~" is relative to.
    row["home"] = _first(parts["HOME"])
    # The machine's own name (gethostname, as uname -n reports it), which is
    # what tmux titles a pane until something on it sets a title -- so a chat
    # whose agent has not titled it yet is not called after the box.
    row["hostname"] = _first(parts["HOSTNAME"])
    row["uptime"] = _first(parts["UPTIME"]).removeprefix("up ")
    row["load"] = _first(parts["LOAD"])
    row["cpus"] = _first(parts["CPUS"])

    mem = _first(parts["MEM"]).split()
    if len(mem) == 2:
        row["mem"] = f"{_gib(mem[0])} / {_gib(mem[1])}"

    # When someone last typed on this server, by its own clock (NOW is read
    # alongside, so skew does not matter): a key into an attached terminal
    # (client_activity), or a reply or new chat sent from Conn's own box
    # (@conn_typed, which starting.py sets). How conn-agent tells you are at
    # a keyboard; None when neither has ever happened.
    now = _int_or(_first(parts["NOW"]), 0)
    typed = [_int_or(line.split("|", 1)[0], 0) for line in parts["CLIENTS"]]
    typed += [_int_or(_first(parts["TYPED"]), 0)]
    typed = [t for t in typed if t]
    row["typed_ago"] = (max(0, now - max(typed)) if now and typed else None)

    disk = _first(parts["DISK"]).split()
    if len(disk) == 3:
        row["disk"] = f"{disk[0]} / {disk[1]} ({disk[2]})"

    screens, panes = _split_captures(done.stdout, token)
    meta = _parse_meta(done.stdout, token)
    mine = own_session() if is_local(host) else ""
    for line in parts["TMUX"]:
        # From the right: the name is the one field free to contain '|', and
        # it comes first, so the three fixed fields are peeled off the end.
        bits = line.rsplit("|", 3)
        if len(bits) < 3:
            continue
        name = bits[0]
        if name == mine:
            continue
        raw = screens.get(name, "")
        screen = agent_state.strip_ansi(raw)
        commands = [pane["cmd"] for pane in panes.get(name, [])]
        # The session set is the TMUX section's, as it always was; metadata
        # only decorates it. A session born between the two tmux calls just
        # goes without until the next look, rather than becoming a session
        # the stream does not know about.
        extra = _session_meta(meta.get(name))
        if not extra["path"] and panes.get(name):
            extra["path"] = panes[name][0]["path"]
        row["sessions"].append({
            "name": name, "windows": bits[1],
            "attached": bits[2] == "attached",
            "activity": _int_or(bits[3], 0) if len(bits) > 3 else 0,
            "panes": panes.get(name, []),
            "screen": screen,
            "agent": agent_state.classify(screen, commands, raw),
            **extra,
        })
    return row


def _first(lines: list[str]) -> str:
    return lines[0] if lines else ""


def _gib(megabytes: str) -> str:
    try:
        value = int(megabytes)
    except ValueError:
        return megabytes
    return f"{value/1024:.1f}G" if value >= 1024 else f"{value}M"


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

# Where the kernel's table is read from. A name so the tests can hand these
# checks a table of their own.
MOUNTS_TABLE = "/proc/self/mounts"


def _in_mounts_table(path: Path) -> bool:
    """Whether the kernel lists `path` as a mountpoint.

    Deliberately not os.path.ismount(): that stats the path, and a stat on a
    FUSE mount whose sshfs has hung blocks indefinitely -- on the main thread
    that froze the whole window. It also answers wrongly for an sshfs that
    died: the stat fails with ENOTCONN and ismount says False, yet the mount
    is still held and unmounting is the one thing left to do with it. The
    table has neither problem: reading it never enters the filesystem, and a
    dead mount stays listed until it is released.
    """
    # The table octal-escapes space, tab, newline and backslash, so the
    # comparison is made in its terms.
    wanted = (str(path).replace("\\", "\\134").replace(" ", "\\040")
              .replace("\t", "\\011").replace("\n", "\\012"))
    try:
        with open(MOUNTS_TABLE) as table:
            for line in table:
                fields = line.split()
                if len(fields) > 1 and fields[1] == wanted:
                    return True
    except OSError:
        pass
    return False


def is_mounted(host: str) -> bool:
    # This machine's files are already here, which is the state "mounted"
    # exists to describe, so f opens them and u has nothing to undo.
    if is_local(host):
        return True
    return _in_mounts_table(MNT_ROOT / host)


def files_root(host: str) -> Path:
    """Where this host's files are on disk, once they are reachable."""
    return Path.home() if is_local(host) else MNT_ROOT / host


def files_label(host: str) -> str:
    """That path as a prompt would write it."""
    return "~" if is_local(host) else "files"


def mount(host: str) -> str:
    if is_local(host):
        return str(files_root(host))
    # TimeoutExpired is a SubprocessError, not an OSError: left unconverted
    # it escapes the (HostError, OSError) net every caller holds and kills
    # the worker thread, and the click looks like it did nothing.
    try:
        done = subprocess.run(["ssh-mount", host], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=40)
    except subprocess.TimeoutExpired:
        raise HostError("no answer from sshfs after 40s")
    except subprocess.SubprocessError as exc:
        raise HostError(str(exc))
    if done.returncode != 0:
        message = (done.stderr or done.stdout or "").strip()
        raise HostError(message.splitlines()[-1] if message else "sshfs failed")
    return str(MNT_ROOT / host)


def unmount(host: str) -> None:
    if is_local(host):
        raise HostError("local is this machine -- nothing to unmount.")
    try:
        done = subprocess.run(["ssh-mount", "-u", host], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        raise HostError("no answer from fusermount after 20s")
    except subprocess.SubprocessError as exc:
        raise HostError(str(exc))
    if done.returncode != 0:
        message = (done.stderr or done.stdout or "").strip()
        raise HostError(message.splitlines()[-1] if message else "unmount failed")


def tidy_mounts() -> list[str]:
    """Take away mountpoints with nothing mounted on them.

    ~/mnt/<host> is created before sshfs runs and outlives it: a mount that
    fails leaves the empty directory behind, and so does a reboot with one
    still up, since no unmount ever runs to tidy it. What is left looks
    exactly like a mount that worked until you open it.

    rmdir only ever removes an empty directory, so this cannot take anything
    with it. If files were written into a mountpoint while it was unmounted --
    the one case where this would be destructive -- the directory is not empty
    and is left exactly where it is.
    """
    gone: list[str] = []
    try:
        entries = sorted(MNT_ROOT.iterdir())
    except OSError:
        return gone
    for path in entries:
        # Table first: anything mounted -- alive, dead or hung -- is skipped
        # before a stat can touch it, so what remains stats plain tmpfs.
        if _in_mounts_table(path):
            continue
        try:
            if not path.is_dir():
                continue
            path.rmdir()
            gone.append(path.name)
        except OSError:
            continue        # in use, not empty, or not ours to remove
    return gone


def open_files(path: str) -> None:
    subprocess.Popen(["xdg-open", path],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


# Attaching on this machine is the session ssh-connect builds on the far side
# with the transport taken out: same name, same F12, same banner, so a local
# chat and a remote one behave identically once you are inside them.
#
# unset TMUX is what makes it work when the panel itself was started from
# inside tmux. tmux refuses a nested attach outright; without this the key
# would look like it did nothing. Unset, it becomes a second client on the
# same server -- which works, at the cost of the inner status line being drawn
# under the outer one.
LOCAL_CONNECT = r"""
printf '\033]0;%s\007' 'local/__SESSION__'
unset TMUX
exec tmux new-session -A -s '__SESSION__' \
  \; bind-key -n F12 detach-client \
  \; set-option -t '__SESSION__' status-left-length 40 \
  \; set-option -t '__SESSION__' status-left '[local/__SESSION__] ' \
  \; set-option -t '__SESSION__' display-time 5000 \
  \; display-message 'F12  or  ctrl-b d   =   back to the server panel'
"""


SESSION_NAME = re.compile(r"[^A-Za-z0-9_-]")


def session_name(wanted: str) -> str:
    """The name tmux will actually use for a session asked for as `wanted`.

    Session names are interpolated into shell scripts, so every path filters
    them to [A-Za-z0-9_-] rather than quoting: a session name is a label, and
    one that needs quoting is a mistake. The filter lives here, once, so the
    panel can file a view under the name the session really gets -- type
    "web app" and the view, the row and the tmux session all agree on
    "web_app", instead of the view waiting forever on a name that never
    exists.
    """
    return SESSION_NAME.sub("_", wanted.strip()) or "shell"


def connect_argv(host: str, session: str) -> list[str]:
    """How to attach to one session, wherever it lives.

    Both halves are argv for a real terminal -- suspend the panel over it, or
    hand it to launch() for a window of its own.
    """
    name = session_name(session)
    if is_local(host):
        return ["bash", "-c", LOCAL_CONNECT.replace("__SESSION__", name)]
    return ["ssh-connect", host, name]


SESSION_APP_ID = "org.omarchy.conn-session"
PANEL_APP_ID = "org.omarchy.conn"


# NOTE: no hyprctl window manipulation here on purpose. On this setup every
# `hyprctl dispatch` that takes an argument fails ("hl.dispatch(...)" Lua parse
# error), so focus/move calls look like they work and silently do nothing.
# Window placement is left to the compositor's own rules.


def launch(argv: list[str], app_id: str = SESSION_APP_ID) -> None:
    """Open a terminal window running argv, detached from this process."""
    if shutil.which("omarchy-launch-tui"):
        command = ["omarchy-launch-tui", f"--app-id={app_id}"] + argv
    else:
        command = ["foot", "-a", app_id] + argv
    subprocess.Popen(command, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


# Where ssh-connect leaves a note when a window died on the way up. Watching the
# launcher's own exit would tell us nothing: omarchy-launch-tui is `exec setsid
# uwsm-app ...`, so it is gone within milliseconds whether the session came up
# or not, and the real error is on the far side of a terminal we do not own.
LAUNCH_ERROR = Path(os.environ.get("XDG_CACHE_HOME",
                                   Path.home() / ".cache")) / "conn" / "last-launch-error"


def launch_error(since: float) -> str:
    """The failure a just-launched window reported, or "" if it is running.

    `since` is when the launch started, so an old note from yesterday's failure
    cannot be mistaken for this one.
    """
    try:
        stamp, target, reason = LAUNCH_ERROR.read_text().strip().split("\t", 2)
        # `date +%s` truncates to the second, so a note written 200ms after the
        # launch can carry a stamp a fraction *before* it. Without the second
        # of slack, the fastest failures -- the ones worth reporting -- would
        # be the ones dismissed as stale.
        if float(stamp) < since - 1:
            return ""
    except (OSError, ValueError):
        return ""
    return f"{target}: {reason}"


def wait_launch_error(since: float, timeout: float = 6.0) -> str:
    """Give a launch long enough to fail, then say how. A window that is still
    up when the time is out has not failed, so this returns "".
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        error = launch_error(since)
        if error:
            return error
        time.sleep(0.25)
    return ""


# A long-lived loop on the far side that re-dumps every session's screen and
# only speaks when something actually changed. One held-open ssh channel beats
# polling: a poll pays the round trip on every single check,
# while this pays it once and then streams.
#
# The markers carry the same per-invocation token as the probe's, for the
# same reason: a frame boundary a watched screen can print is a frame
# boundary it can forge. The ALIVE line every tick is the liveness the
# transport cannot give -- ServerAliveInterval is ignored when ssh is a mux
# client riding the user's ControlMaster, so a master whose TCP has silently
# died leaves the reader waiting on a pipe that will never speak again. The
# heartbeat makes silence finite: a reader that has heard nothing for the
# stall budget knows the channel is dead, not idle. Every tick, not every
# few: a tick is the sleep plus the whole dump -- some twenty process spawns
# -- so on a slow remote a multi-tick beat could outrun the reader's
# WATCH_STALL budget and get a healthy stream killed as stalled. One printf
# a second is nothing next to the dump it follows.
#
# The chat metadata is one more tmux call per tick for every session at once
# (META_FORMAT), ahead of the per-session loop, and it sits inside $out, so
# the cksum covers it: a star or a title set from another client is a change
# like any screen change, and the frame goes out. session_activity is
# deliberately not in it -- it moves on every spinner redraw and would turn
# an idle-looking stream into a frame a second.
WATCH_SCRIPT = r"""
last=""
while :; do
  out=$(tmux list-sessions -F "__META__" 2>/dev/null
        tmux list-sessions -F "#{session_name}" 2>/dev/null | while read -r s; do
          printf '%s\n' "@@@__BOUND__:SESSION:$s"
          tmux capture-pane -p -e -t "$s" 2>/dev/null | tail -__CAPLINES__
          printf '%s\n' "@@@__BOUND__:PANES:$s"
          tmux list-panes -t "$s" -F "#{pane_current_command}" 2>/dev/null
        done)
  now=$(printf '%s' "$out" | cksum)
  if [ "$now" != "$last" ]; then
    last=$now
    printf '%s\n###__BOUND__:FRAME\n' "$out"
  fi
  echo "###__BOUND__:ALIVE"
  sleep __INTERVAL__
done
"""


def watch_screens(host: str, interval: float = 1.0) -> subprocess.Popen:
    """Start a streaming watcher for one host. Caller owns the process.

    stdin carries the script and nothing else -- it is a pipe of ours, never
    the terminal, so ssh cannot compete with the panel for keystrokes the way
    an inherited stdin used to let it.

    The process comes back carrying its invocation's marker strings: `frame`
    ends a frame, `alive` is the idle heartbeat, and `boundary` is the token
    parse_frame needs. They live on the handle because they are only good for
    this one stream -- the next connection mints its own.

    stdout is a raw, unbuffered pipe. The reader select()s on the fd to put a
    ceiling on silence, and a buffered text wrapper would hold lines a select
    on the fd can no longer see.
    """
    token = secrets.token_hex(16)
    script = (WATCH_SCRIPT.replace("__META__", META_FORMAT)
                          .replace("__INTERVAL__", str(interval))
                          .replace("__CAPLINES__", str(CAPTURE_LINES))
                          .replace("__BOUND__", token))
    proc = subprocess.Popen(
        run_argv(host, opts=["-o", "ServerAliveInterval=15"]),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        bufsize=0)
    try:
        proc.stdin.write(script.encode())
        proc.stdin.close()
    except OSError:
        pass        # died at spawn: the reader sees EOF and backs off
    proc.boundary = token
    proc.frame = f"###{token}:FRAME"
    proc.alive = f"###{token}:ALIVE"
    return proc


def parse_frame(lines: list[str], host: str = "",
                token: str = "") -> dict[str, dict]:
    """One frame -> {session: {"screen": str, "raw": str, "commands": [str],
    "options": {...}, "pane_title": str, "path": str, "created": int}}.

    `token` is the boundary the frame arrived under -- proc.boundary from the
    watch_screens that produced it. A marker without it is screen content.

    The session set is the SESSION markers' and nothing else, exactly as
    probe() takes its set from list-sessions rather than from the metadata:
    the metadata call runs a moment before the loop, and letting a record for
    a session that has since gone (or not yet arrived) add or drop a session
    would make the stream and the probe disagree, which the panel answers
    with a re-probe. The host's $HOME is not here: it does not change while a
    stream runs, so the panel keeps the probe's.

    Our own session is dropped here too, not just in probe(). The panel treats
    "the stream and the probe disagree about which sessions exist" as a session
    having appeared, so filtering one and not the other would re-probe on every
    single frame.
    """
    session_mark = f"@@@{token}:SESSION:"
    panes_mark = f"@@@{token}:PANES:"
    sessions: dict[str, dict] = {}
    current = None
    mode = None
    # Everything before the first SESSION marker is the metadata block; a
    # value with a newline in it spans lines, so it is put back together and
    # read by token, not line by line.
    head: list[str] = []
    for line in lines:
        if current is None and not line.startswith((session_mark, panes_mark)):
            head.append(line)
        if line.startswith(session_mark):
            current = line[len(session_mark):].strip()
            sessions.setdefault(current, {"screen": [], "commands": []})
            mode = "screen"
        elif line.startswith(panes_mark):
            current = line[len(panes_mark):].strip()
            sessions.setdefault(current, {"screen": [], "commands": []})
            mode = "commands"
        elif current and mode == "screen":
            sessions[current]["screen"].append(line.rstrip())
        elif current and mode == "commands" and line.strip():
            sessions[current]["commands"].append(line.strip())

    # Only ours, and only here: a remote box may well have a session with the
    # same name, and it is a different session on a different machine.
    mine = own_session() if is_local(host) else ""
    meta = _parse_meta("\n".join(head), token)
    frames = {}
    for name, data in sessions.items():
        if name == mine:
            continue
        raw = "\n".join(data["screen"]).strip("\n")
        frames[name] = {"screen": agent_state.strip_ansi(raw), "raw": raw,
                        "commands": data["commands"],
                        **_session_meta(meta.get(name))}
    return frames


# ---------------------------------------------------------------------------
# The agent, and the secrets beside it
# ---------------------------------------------------------------------------

# Where a desktop keeps its ssh agent, in the order worth trying.
AGENT_SOCKETS = ("gcr/ssh",            # gnome-keyring, via gcr
                 "ssh-agent.socket",   # systemd's own user agent
                 "keyring/ssh")        # older gnome-keyring


def ensure_agent() -> str:
    """Find the agent when nothing in the environment points at one.

    SSH_AUTH_SOCK is usually exported by a shell profile, so a panel started
    from a launcher rather than a terminal has never heard of it. That is not
    obvious from the outside: ssh keeps working for a while on the
    multiplexed connections ControlPersist is holding open, and only once
    those age out does every host start refusing at once.

    Setting it here means every ssh, ssh-add and secret-tool spawned below
    inherits it, whichever way conn was started.
    """
    current = os.environ.get("SSH_AUTH_SOCK", "")
    try:
        if current and Path(current).is_socket():
            return current
    except OSError:
        pass

    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    for name in AGENT_SOCKETS:
        candidate = runtime / name
        try:
            if candidate.is_socket():
                os.environ["SSH_AUTH_SOCK"] = str(candidate)
                return str(candidate)
        except OSError:
            continue
    return ""


def agent_keys() -> int | None:
    """How many identities the ssh agent is holding, or None if there is no
    agent to ask.

    This is the whole of conn's involvement with your passphrase. The key is
    unlocked once -- by the desktop keyring at login, or by the button below
    -- and every ssh spawned from here rides the agent. Holding the passphrase
    ourselves would add a second copy of it and protect nothing.
    """
    try:
        done = subprocess.run(["ssh-add", "-l"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode == 2:          # could not talk to an agent at all
        return None
    if done.returncode == 1:          # agent is there, holding nothing
        return 0
    return len([line for line in done.stdout.splitlines() if line.strip()])


def unlock_agent() -> None:
    """Ask ssh-add for the passphrase, in a terminal of its own.

    Deliberately not a dialog of ours: the prompt belongs to ssh-add and the
    keyring, so the passphrase goes from your keyboard to them without conn
    ever being on the path.
    """
    launch(["bash", "-lc",
            "ssh-add; echo; read -rsn1 -p 'Press any key to close...'"])


# Secrets live in the desktop keyring, which your login password already
# unlocks -- so "one password for everything" is not a feature to build here,
# it is the arrangement that exists. conn stores nothing itself and reads a
# value only when you ask to see it.
SECRET_APP = "conn"


def secret_names(host: str) -> list[str]:
    """What is filed against this host. Names only -- the values stay put
    until something asks for one by name."""
    try:
        done = subprocess.run(
            ["secret-tool", "search", "--all",
             "application", SECRET_APP, "host", host],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []

    # secret-tool prints the attributes on stderr and everything else on
    # stdout, so both have to be read or the listing comes back empty while
    # every individual lookup works.
    names = []
    for line in (done.stdout + "\n" + done.stderr).splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "attribute.name":
            name = value.strip()
            if name and name not in names:
                names.append(name)
    return sorted(names)


def secret_value(host: str, name: str) -> str:
    try:
        done = subprocess.run(
            ["secret-tool", "lookup", "application", SECRET_APP,
             "host", host, "name", name],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostError(str(exc))
    if done.returncode != 0:
        message = (done.stderr or "").strip()
        raise HostError(message.splitlines()[-1] if message
                        else "no such secret")
    return done.stdout


def secret_store(host: str, name: str, value: str) -> None:
    """Hand a value to the keyring. It arrives on stdin rather than in the
    command line, which anything on the machine can read."""
    if not name.strip():
        raise HostError("A name is required.")
    try:
        done = subprocess.run(
            ["secret-tool", "store", "--label", f"conn: {host}/{name}",
             "application", SECRET_APP, "host", host, "name", name],
            input=value, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostError(str(exc))
    if done.returncode != 0:
        message = (done.stderr or "").strip()
        raise HostError(message.splitlines()[-1] if message
                        else "could not store")


def secret_clear(host: str, name: str) -> None:
    try:
        done = subprocess.run(
            ["secret-tool", "clear", "application", SECRET_APP,
             "host", host, "name", name],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostError(str(exc))
    if done.returncode != 0:
        message = (done.stderr or "").strip()
        raise HostError(message.splitlines()[-1] if message
                        else "could not remove")


def migrate_secrets(old: str, new: str) -> None:
    """Re-file every keyring secret from host=old to host=new.

    The keyring is a separate store from the config, keyed by the alias:
    rename_host moves the Host line, this moves the secrets that were filed
    against the old name so they still answer once it is renamed. Store before
    clear, so a run interrupted between the two leaves a copy under both names
    rather than none. A keyring with nothing to move is not an error.
    """
    for name in secret_names(old):
        secret_store(new, name, secret_value(old, name))
        secret_clear(old, name)


# What a URL looks like, minus the punctuation that ends a sentence rather
# than an address.
URL = re.compile(r"(?:https?://|ftp://|file://|mailto:)"
                 r"[^\s<>\"'`{}|\\^\[\]]*[^\s<>\"'`{}|\\^\[\].,;:!?)]")


def screen_links(screen: str) -> list[str]:
    """Every URL on a screen, including ones wrapped across rows.

    A terminal can normally join a wrapped line back together, because it
    knows it wrapped it. Inside tmux nothing does: tmux redraws a pane row by
    row, and a long URL arrives as several lines with no record that they were
    ever one. That is why clicking a login link works until the link is long
    enough to matter.

    What survives is its shape -- a row ending without a space, followed by a
    row that is one unbroken token starting hard against the left margin --
    and continuation is only considered while the line so far is already a
    URL. Two ordinary full-width lines do not meet that.
    """
    found: list[str] = []
    for url, _first, _last in screen_link_spans(screen):
        if url not in found:
            found.append(url)
    return found


def screen_link_spans(screen: str) -> list[tuple[str, int, int]]:
    """screen_links(), with the rows each URL covers: (url, first, last),
    rows counted from 0. What lets a click on the second or third row of a
    wrapped login link open the whole of it."""
    joined: list[tuple[str, int, int]] = []
    buffer, first = "", 0
    for row, raw in enumerate(screen.splitlines()):
        line = raw.rstrip()
        token = line.strip()
        if (buffer and "://" in buffer and token and " " not in token
                and not raw[:1].isspace()):
            buffer += token
            continue
        if buffer:
            joined.append((buffer, first, row - 1))
        buffer, first = line, row
    if buffer:
        joined.append((buffer, first, len(screen.splitlines()) - 1))

    spans: list[tuple[str, int, int]] = []
    for line, start, end in joined:
        for match in URL.finditer(line):
            spans.append((match.group(0), start, end))
    return spans


def link_on_row(screen: str, row: int, fragment: str = "") -> str:
    """The whole URL a click on this row of the screen is on: the one
    whose rows include it -- the one containing what the terminal itself
    matched there, when there are several."""
    hits = [url for url, first, last in screen_link_spans(screen)
            if first <= row <= last]
    if fragment:
        hits = [u for u in hits if fragment in u] or hits
    return max(hits, key=len) if hits else ""


def kill_session(host: str, session: str) -> None:
    """End a tmux session and everything running in it.

    There is no undo and no scrollback afterwards: whatever the agent was
    part-way through is gone. Callers ask first.
    """
    try:
        done = subprocess.run(
            run_argv(host),
            input=f"tmux kill-session -t {shlex.quote(session)}".encode(),
            capture_output=True, timeout=20)
    except subprocess.TimeoutExpired:
        raise HostError("no answer after 20s")
    except subprocess.SubprocessError as exc:
        raise HostError(str(exc))
    if done.returncode != 0:
        message = (done.stderr or b"").decode(errors="replace").strip()
        raise HostError(message.splitlines()[-1] if message else "kill failed")


def rename_session(host: str, session: str, wanted: str) -> str:
    """Rename a tmux session, here or on a server. Returns the name it got.

    Nothing is interrupted: tmux renames the session out from under whatever
    is running in it, and a terminal already attached stays attached. The
    label in tmux's own status bar is rewritten to match, because it was
    stamped with the old name at attach time and would otherwise go on
    claiming to be a session that no longer exists.

    The name is filtered the way connect_argv filters one, for the same
    reason: it is interpolated into a shell script, and a session name that
    needs quoting is a mistake rather than a thing to support.
    """
    name = SESSION_NAME.sub("_", wanted.strip())
    if not name:
        raise HostError("a session needs a name")
    if name == session:
        return name

    label = f"[{host}/{name}] "
    script = (f"tmux rename-session -t {shlex.quote(session)} {shlex.quote(name)}"
              f" && tmux set-option -t {shlex.quote(name)} status-left "
              f"{shlex.quote(label)}")
    try:
        done = subprocess.run(run_argv(host), input=script.encode(),
                              capture_output=True, timeout=20)
    except subprocess.TimeoutExpired:
        raise HostError("no answer after 20s")
    except subprocess.SubprocessError as exc:
        raise HostError(str(exc))
    if done.returncode != 0:
        message = (done.stderr or b"").decode(errors="replace").strip()
        raise HostError(message.splitlines()[-1] if message
                        else "rename failed")
    return name


def _tmux_arg(value: str) -> str:
    """`value` as tmux will store it when it arrives as an argument.

    tmux reads a command-line argument that ends in ';' as the end of one
    command and the start of the next -- `set-option @conn_title "x;"` stores
    "x", and a bare ";" is an error -- unless that last ';' is escaped as
    "\\;", which tmux turns back into ';'. Only the final character is
    examined, so escaping it is the whole fix. Nothing else in the argument
    is interpreted: no formats without -F, no '~', no inner ';'.
    """
    return value[:-1] + "\\;" if value.endswith(";") else value


def set_chat_option(host: str, session: str, name: str,
                    value: str | None) -> None:
    """Set @conn_<name> on one session, or with None unset it. Blocking.

    These are the SPEC's chat options -- a star, a title, the recency stamp --
    living on the session itself so every client reads the same thing and
    they survive a restart of the panel. Only that set is accepted: the name
    lands in the tmux command, and this must never become a way to set an
    arbitrary option on somebody's server.

    The session is targeted as "=<name>:", tmux's exact-match form: a bare
    name is a prefix match, and "deploy" would quietly star "deploy-old" if
    "deploy" had just gone. Session and value are shell-quoted for the remote
    sh; the value then also gets _tmux_arg, because tmux's own argument
    parser has one rule of its own that quoting cannot reach.
    """
    if name not in CHAT_OPTIONS:
        raise HostError(f"not a chat option: {name!r}")
    option = f"@conn_{name}"
    where = shlex.quote(f"={session}:")
    if value is None:
        script = f"tmux set-option -u -t {where} {option}"
    else:
        script = (f"tmux set-option -t {where} -- {option} "
                  f"{shlex.quote(_tmux_arg(str(value)))}")
    try:
        done = subprocess.run(run_argv(host), input=script.encode(),
                              capture_output=True, timeout=20)
    except subprocess.TimeoutExpired:
        raise HostError("no answer after 20s")
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostError(str(exc))
    if done.returncode != 0:
        message = (done.stderr or b"").decode(errors="replace").strip()
        raise HostError(message.splitlines()[-1] if message
                        else f"could not set {option}")


def copy_key(host: str) -> None:
    """ssh-copy-id needs a password typed, so it gets a real terminal window
    rather than being run headless behind the panel."""
    if is_local(host):
        raise HostError("local is this machine -- no key needed.")
    # The alias travels as an argument, never inside the -c string: it is
    # data from the config, and bash would run whatever it contained.
    launch(["bash", "-lc",
            'ssh-copy-id -- "$1"; echo; '
            "read -rsn1 -p 'Press any key to close...'",
            "ssh-copy-id", host])


# First contact, by hand. The probe will not accept a key for you (it runs
# StrictHostKeyChecking=yes, see probe()), so the fingerprint prompt has to be
# answered in a terminal by the person who knows which server they meant.
# StrictHostKeyChecking=ask overrides an accept-new in the config: the point
# is to be asked. ControlPath=none so a shared connection cannot answer for it.
TRUST_SCRIPT = r'''
host="$1"
echo "First contact with $host."
echo "ssh shows the key the server offers and asks whether to trust it."
echo "Answer yes only if this is the server you expect."
echo
if ssh -o StrictHostKeyChecking=ask -o ControlPath=none -- "$host" true; then
    echo
    echo "Trusted. Conn picks it up on its next check."
fi
echo
read -rsn1 -p 'Press any key to close...'
'''

# A *changed* key: ssh will not prompt for that, it refuses. This shows the
# key on file beside the key offered now, says what the difference can mean,
# and asks -- and only a "y" typed here removes the old entry (ssh-keygen -R)
# and connects once with accept-new to pin the new one. Conn itself never
# edits known_hosts; the person at the keyboard does, through this.
#
# The offered key is read from ssh's own -v output ("Server host key:") rather
# than ssh-keyscan: keyscan ignores ProxyJump, and a server behind a jump host
# would be scanned at an address that does not reach it -- or reaches
# something else. The look is BatchMode against an empty known_hosts, so it
# stops at the key exchange: nothing is logged in to and nothing is written.
# The last such line is the server's; a jump host's own comes first.
CHECK_KEY_SCRIPT = r'''
host="$1"
pause() { echo; read -rsn1 -p 'Press any key to close...'; exit 0; }
cfg=$(ssh -G -- "$host" 2>/dev/null)
get() { printf '%s\n' "$cfg" | awk -v k="$1" '$1 == k { print $2; exit }'; }
name=$(get hostname); port=$(get port); alias=$(get hostkeyalias)
known=$(get userknownhostsfile)
known="${known/#\~/$HOME}"
[ -n "$known" ] || known="$HOME/.ssh/known_hosts"
[ -n "$name" ] || name="$host"
[ -n "$port" ] || port=22
if [ -n "$alias" ] && [ "$alias" != none ]; then
    entries=("$alias")
elif [ "$port" = 22 ]; then
    entries=("$name")
else
    # Only the bracketed form: ssh never files a non-22 server under its
    # bare name, so a bare entry here belongs to whatever answers on 22 at
    # the same address -- another box behind the same NAT -- and removing
    # it would forget a key that is still right.
    entries=("[$name]:$port")
fi
echo "The key $host ($name, port $port) offers has CHANGED."
echo
echo "On file in $known:"
found=
for entry in "${entries[@]}"; do
    while read -r line; do
        case "$line" in ''|'#'*) ;; *) echo "    $line"; found=1 ;; esac
    done < <(ssh-keygen -l -F "$entry" -f "$known" 2>/dev/null)
done
[ -n "$found" ] || echo "    (nothing -- it may be in another known_hosts file)"
echo
echo "Offered now:"
offered=$(ssh -v -o BatchMode=yes -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile=/dev/null -o GlobalKnownHostsFile=/dev/null \
    -o ControlPath=none -o ConnectTimeout=10 -- "$host" true 2>&1 </dev/null \
    | sed -n 's/.*Server host key: //p' | tail -n 1)
if [ -z "$offered" ]; then
    echo "    (could not reach $host to ask -- try again later)"
    pause
fi
echo "    $offered"
echo
echo "A server that was rebuilt or reinstalled gets a new key, and this is"
echo "expected. If nothing like that happened, someone may be sitting between"
echo "you and it: say no, and find out before you log in."
echo
read -r -p "Forget the old key and trust the one offered now? [y/N] " answer
case "$answer" in
    y|Y|yes|Yes|YES) ;;
    *) echo "Left as it was."; pause ;;
esac
for entry in "${entries[@]}"; do
    ssh-keygen -R "$entry" -f "$known"
done
echo
echo "Connecting once to pin the new key (log in if asked)..."
if ssh -o StrictHostKeyChecking=accept-new -o ControlPath=none -- "$host" true; then
    echo
    echo "Trusted. Conn picks it up on its next check."
fi
pause
'''


def trust_key(host: str) -> None:
    """A terminal where a new server's fingerprint prompt is answered."""
    if is_local(host):
        raise HostError("local is this machine -- there is no key to trust.")
    launch(["bash", "-lc", TRUST_SCRIPT, "conn-trust", host])


def check_new_key(host: str) -> None:
    """A terminal comparing a changed host key with the one on file, which
    replaces it only if the person there types y."""
    if is_local(host):
        raise HostError("local is this machine -- there is no key to check.")
    launch(["bash", "-lc", CHECK_KEY_SCRIPT, "conn-check-key", host])
