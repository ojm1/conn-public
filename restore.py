"""Local tmux sessions back after a reboot, with Claude resumed in each.

tmux keeps sessions in a server process, so a reboot (or a logout) takes
every local session with it. This keeps a list of them and rebuilds it:

  conn --snapshot-sessions   every minute, from a user timer: each local
                             session's windows, their folders, and the
                             Claude conversation running in each one
  conn --restore-sessions    at login, from Hyprland's autostart: recreate
                             whatever the last boot's list had, Claude
                             panes as `claude --resume <id>`, the rest as a
                             shell in the same folder

Which conversation is in which pane comes from Claude Code itself: every
running instance keeps ~/.claude/sessions/<pid>.json with its sessionId,
cwd and tmux pane ("name:@window.%pane"). `claude --continue` would not do
-- it resumes the newest conversation in a folder, and most of these run
in ~ -- so it is the exact id or nothing.

When to restore: only from a list taken in an earlier boot, and only once
per boot. A snapshot is never written while no tmux server is running, so
the one taken just before shutdown is the one that survives. A session
killed by hand drops out of the next snapshot, a minute later, and is not
brought back.

A resumed Claude in a folder it has to ask about -- always ~, whose "trust
this folder" answer lasts one session only -- would sit at that question
in every restored session until someone opened each one. These are
conversations that were already running there before the reboot, so the
restore answers it: it watches each resumed pane for a while and says yes.

Anything Claude was in the middle of is lost: it comes back idle with its
history, and the session carries @conn_restored (the time) so Conn can say
"restored after reboot" until there is something newer to show.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

STATE = Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser() \
    / "conn" / "local-sessions.json"
CLAUDE_SESSIONS = Path("~/.claude/sessions").expanduser()
CLAUDE_PROJECTS = Path("~/.claude/projects").expanduser()

# Flags worth carrying into the resumed instance. Everything else on the old
# command line (a prompt, --resume's own argument, --print...) is dropped.
KEEP_WITH_VALUE = ("--permission-mode", "--model", "--add-dir", "--agent")
KEEP_ALONE = ("--dangerously-skip-permissions", "--verbose")

# The @conn_* options carried across, as the SPEC names them.
CARRY = ("project", "title", "task", "source", "starred", "name",
         "last_event")

SEP = "\x1f"


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", "-u", *args], capture_output=True,
                          text=True, timeout=10)


def kept_flags(argv: list[str]) -> list[str]:
    """The flags of a claude command line worth giving the resumed one."""
    kept, i = [], 1
    while i < len(argv):
        arg = argv[i]
        name, eq, value = arg.partition("=")
        if name in KEEP_WITH_VALUE:
            if eq:
                kept.append(arg)
            elif i + 1 < len(argv):
                kept += [arg, argv[i + 1]]
                i += 1
        elif arg in KEEP_ALONE:
            kept.append(arg)
        i += 1
    return kept


def _cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]


def claude_by_pane(sessions_dir: Path = CLAUDE_SESSIONS) -> dict[str, dict]:
    """{"%17": {"id": ..., "cwd": ..., "flags": [...]}} for every live
    Claude instance that knows its tmux pane."""
    found: dict[str, dict] = {}
    for path in sessions_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text())
            pid = int(data["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not Path(f"/proc/{pid}").exists():
            continue
        pane = str(data.get("tmux") or "").rpartition(".")[2]
        if not pane.startswith("%") or not data.get("sessionId"):
            continue
        found[pane] = {"id": data["sessionId"], "cwd": data.get("cwd", ""),
                       "flags": kept_flags(_cmdline(pid)),
                       "at": data.get("updatedAt", 0)}
    return found


def take(claude: dict[str, dict] | None = None) -> list[dict] | None:
    """Every local session as it stands, or None when no server runs."""
    fmt = SEP.join(["#{session_name}", "#{window_index}", "#{window_name}",
                    "#{pane_index}", "#{pane_id}", "#{pane_current_path}",
                    "#{window_active}", "#{pane_active}"])
    try:
        out = _tmux("list-panes", "-a", "-F", fmt)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    claude = claude_by_pane() if claude is None else claude
    sessions: dict[str, dict] = {}
    for line in out.stdout.splitlines():
        parts = line.split(SEP)
        if len(parts) != 8:
            continue
        name, widx, wname, _pidx, pane, path, wact, pact = parts
        sess = sessions.setdefault(name, {"name": name, "windows": {}})
        win = sess["windows"].setdefault(widx, {
            "index": int(widx), "name": wname, "active": wact == "1",
            "panes": []})
        entry = {"path": path, "active": pact == "1"}
        if pane in claude:
            c = claude[pane]
            entry["claude"] = c["id"]
            entry["flags"] = c["flags"]
            # Claude resumes against the folder it was started in, which a
            # `cd` inside it does not change.
            entry["path"] = c["cwd"] or path
        win["panes"].append(entry)
    if not sessions:
        return None
    result = []
    for sess in sessions.values():
        sess["windows"] = sorted(sess["windows"].values(),
                                 key=lambda w: w["index"])
        sess["options"] = _options(sess["name"])
        result.append(sess)
    return result


def _options(session: str) -> dict[str, str]:
    try:
        out = _tmux("show-options", "-t", f"={session}:")
    except (OSError, subprocess.SubprocessError):
        return {}
    found = {}
    for line in out.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key.startswith("@conn_") and key[6:] in CARRY:
            try:
                value = shlex.split(value)[0] if value else ""
            except ValueError:
                pass
            found[key[6:]] = value
    return found


def _write(data: dict, path: Path = STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def load(path: Path = STATE) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def snapshot(path: Path = STATE) -> int:
    sessions = take()
    if sessions is None:
        return 0        # no server: keep the last good list
    old = load(path)
    _write({"boot": boot_id(), "taken": int(time.time()),
            "restored_in": old.get("restored_in", ""),
            "sessions": sessions}, path)
    return len(sessions)


def conversation_exists(conv: str, projects: Path = CLAUDE_PROJECTS) -> bool:
    return any(projects.glob(f"*/{conv}.jsonl"))


def pane_command(pane: dict, exists=None) -> str | None:
    """What a restored pane runs: None for a plain shell."""
    conv = pane.get("claude")
    if not conv or not (exists or conversation_exists)(conv):
        return None
    # The shell named outright: inside a pane tmux sets $SHELL to its own
    # default-shell, which is not always the login shell that has claude.
    sh = shlex.quote(os.environ.get("SHELL") or "/bin/bash")
    flags = " ".join(shlex.quote(f) for f in pane.get("flags", []))
    # set -m: job control, so Claude runs in its own process group and is
    # the pane's foreground command. Without it tmux reports the pane as
    # the shell around it, and Conn files the chat as a plain shell.
    inner = "set -m; " \
        + f"claude --resume {shlex.quote(conv)} {flags}".rstrip() \
        + f"; exec {sh} -l"
    return f"exec {sh} -lc {shlex.quote(inner)}"


def _dir(path: str) -> str:
    return path if path and os.path.isdir(path) else str(Path.home())


def restore(path: Path = STATE, force: bool = False,
            now: float | None = None, trust_wait: float = 90) -> list[str]:
    """Recreate the last boot's sessions. Returns the names made."""
    data = load(path)
    here = boot_id()
    if not data.get("sessions"):
        return []
    if not force and (data.get("boot") == here
                      or data.get("restored_in") == here):
        return []
    try:
        live = _tmux("list-sessions", "-F", "#{session_name}")
        existing = set(live.stdout.split()) if live.returncode == 0 else set()
    except (OSError, subprocess.SubprocessError):
        return []
    stamp = str(int(now if now is not None else time.time()))
    made, resumed = [], []
    for sess in data["sessions"]:
        name = sess["name"]
        if name in existing or not sess.get("windows"):
            continue
        panes = _make(sess, stamp)
        if panes is None:
            continue
        made.append(name)
        resumed += panes
    data["restored_in"] = here
    _write(data, path)
    if resumed and trust_wait > 0:
        accept_trust(resumed, trust_wait)
    return made


# Claude's folder-trust question, as it draws it: the second option is yes.
TRUST_MARK = "Yes, I trust this folder"


def accept_trust(panes: list[str], wait: float, poll: float = 0.5) -> list[str]:
    """Answer yes to the folder-trust question in each pane that shows it
    within `wait` seconds. Returns the panes answered."""
    answered: list[str] = []
    pending = list(panes)
    deadline = time.monotonic() + wait
    while pending and time.monotonic() < deadline:
        for pane in list(pending):
            try:
                out = _tmux("capture-pane", "-p", "-t", pane)
            except (OSError, subprocess.SubprocessError):
                out = None
            if out is None or out.returncode != 0:
                pending.remove(pane)        # the pane is gone
                continue
            if TRUST_MARK not in out.stdout:
                continue
            _tmux("send-keys", "-t", pane, "Down")
            time.sleep(0.3)
            _tmux("send-keys", "-t", pane, "Enter")
            answered.append(pane)
            pending.remove(pane)
        if pending:
            time.sleep(poll)
    return answered


def _new_pane(args: list[str], pane: dict) -> tuple[list[str], bool]:
    """The tmux command for a pane, and whether it resumes Claude."""
    cmd = pane_command(pane)
    return (args + ["-c", _dir(pane.get("path", ""))] + ([cmd] if cmd else []),
            cmd is not None)


def _make(sess: dict, stamp: str) -> list[str] | None:
    """Make one session. None if it could not be; else the ids of the
    panes resuming Claude."""
    name = sess["name"]
    first = True
    active = None
    resumed: list[str] = []
    for win in sess["windows"]:
        panes = win.get("panes") or [{"path": ""}]
        if first:
            args = ["new-session", "-d", "-s", name]
        else:
            args = ["new-window", "-d", "-t", f"={name}:"]
        args += ["-P", "-F", "#{window_id} #{pane_id}"]
        if win.get("name"):
            args += ["-n", win["name"]]
        cmd, is_claude = _new_pane(args, panes[0])
        out = _tmux(*cmd)
        if out.returncode != 0:
            if first:
                return None
            continue
        first = False
        target, _, pane_id = out.stdout.strip().partition(" ")
        if is_claude and pane_id:
            resumed.append(pane_id)
        for pane in panes[1:]:
            cmd, is_claude = _new_pane(["split-window", "-d", "-P", "-F",
                                        "#{pane_id}", "-t", target], pane)
            out = _tmux(*cmd)
            if is_claude and out.returncode == 0 and out.stdout.strip():
                resumed.append(out.stdout.strip())
        if len(panes) > 1:
            _tmux("select-layout", "-t", target, "tiled")
        if win.get("active"):
            active = target
    for key, value in (sess.get("options") or {}).items():
        if key in CARRY and value:
            _tmux("set-option", "-t", f"={name}:", f"@conn_{key}", value)
    _tmux("set-option", "-t", f"={name}:", "@conn_restored", stamp)
    if active:
        _tmux("select-window", "-t", active)
    return resumed


def main(argv: list[str]) -> int:
    if "--snapshot-sessions" in argv:
        snapshot()
        return 0
    made = restore(force="--force" in argv)
    if made:
        print("restored: " + ", ".join(made))
    return 0
