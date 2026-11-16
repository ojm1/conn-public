"""Starting a chat on a server: SPEC §4, as one ssh round trip.

The sequence is the spec's -- a slug, the task written to a file over
stdin, tmux new-session running claude on that file, the @conn_* options --
done by one POSIX script fed to the host's `sh` (hosts.run_argv), so it is
one connection whether the host is this laptop or a server two hops away.

No GTK in here: the agent on the mini starts chats the same way, and the script is testable as text.

Why every piece is shaped the way it is:

- The task never appears in a command line. It rides inside a quoted
  heredoc whose end marker is random, so nothing in it -- quotes, `$(...)`,
  a line that says EOF -- is read by any shell.
- claude is started by the user's *login* shell. On most of these servers
  it lives in ~/.local/bin, which only a login shell puts on PATH; plain
  `sh -c` cannot find it (acme-app, widgets-new). A small runner script
  per chat holds that one line, so the command tmux runs is just
  `sh <file>` -- nothing in it needs quoting.
- When claude exits the runner drops to a login shell, so the session and
  what claude last printed are still there to read.
- The slug is made unique on the server itself, inside the same script, so
  two quick adds a second apart cannot both take `csv-export`.
"""

from __future__ import annotations

import re
import secrets
import shlex
import subprocess
from dataclasses import dataclass

import hosts

# Words that say nothing about the job. The slug is "the first 3 or 4
# meaningful words" (SPEC §4); "can we add a csv export" is csv-export.
_FILLER = set("""
a an the and or but to of for in on at by with from into onto about as is are
was be been it its this that these those there here we i you me my our us
can could would should will shall please pls just also so then now let lets
let's do does did make sure some any all up out need want like get go see
hey hi ok okay yeah yep add dont don't new
""".split())

MAX_SLUG = 32
TITLE_LIMIT = 40

# Permission modes the New chat screen offers, in the order it offers them.
# "" passes no flag at all, which is the one choice every installed version
# of claude understands. The rest are `claude --permission-mode` values.
MODES = (
    ("", "Ask first"),
    ("auto", "Auto mode"),
    ("acceptEdits", "Accept edits"),
    ("plan", "Plan mode"),
    ("bypassPermissions", "Skip permissions"),
)
MODE_LABEL = dict(MODES)

AGENTS = (("claude", "Claude Code"),)
AGENT_LABEL = dict(AGENTS)


def task_slug(text: str) -> str:
    """csv-export from "Can we add a CSV export to the switches table?".

    Lowercase ASCII, hyphens, at most four words and MAX_SLUG characters --
    it becomes a tmux session name and a file name on the server, so nothing
    else survives. A task of nothing but filler still gets a name.
    """
    words = re.findall(r"[a-z0-9]+", text.lower().replace("'", ""))
    meaningful = [w for w in words if w not in _FILLER and len(w) > 1]
    # Three words, or four when they are short: a session name is read in
    # a narrow sidebar column, and "csv-export-switches" already says it.
    picked = (meaningful or words)[:4]
    if len(picked) == 4 and len("-".join(picked)) > 20:
        picked = picked[:3]
    out = "-".join(picked)[:MAX_SLUG].strip("-")
    return out or "chat"


def task_title(text: str) -> str:
    """@conn_title: the first line of the task, trimmed to 40 characters."""
    line = next((l.strip() for l in text.splitlines() if l.strip()), "")
    line = re.sub(r"\s+", " ", line)
    if len(line) <= TITLE_LIMIT:
        return line or "New chat"
    cut = line[:TITLE_LIMIT - 1].rstrip()
    space = cut.rfind(" ")
    if space > TITLE_LIMIT // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:-") + "…"


def remote_dir(folder: str) -> str:
    """A projects.toml folder as a POSIX shell word: "~/apps/x" becomes
    "$HOME"/'apps/x', so the home expands remotely and the rest cannot."""
    folder = (folder or "~").strip() or "~"
    if folder == "~":
        return '"$HOME"'
    if folder.startswith("~/"):
        rest = folder[2:].rstrip("/")
        return '"$HOME"' + ("/" + shlex.quote(rest) if rest else "")
    return shlex.quote(folder)


def claude_command(mode: str = "", resume: bool = False) -> str:
    """What the runner runs, task file aside. Shown in the New chat screen's
    "Command Conn will run" too, so it must read as what really happens."""
    parts = ["claude"]
    if resume:
        parts.append("--continue")
    if mode:
        if mode not in MODE_LABEL:
            raise ValueError(f"not a permission mode: {mode!r}")
        parts += ["--permission-mode", mode]
    return " ".join(parts)


def preview_command(slug: str, folder: str, mode: str = "",
                    resume: bool = False) -> str:
    """The two lines the New chat screen shows under "Command Conn will
    run" -- the spec's form, with the slug the server will probably give."""
    return (f"tmux new -d -s {slug} -c {folder or '~'} \\\n"
            f"  '{claude_command(mode, resume)} -- \"$(cat ~/.conn/tasks/{slug}.md)\"'")


def _marker(text: str) -> str:
    while True:
        mark = "CONN_" + secrets.token_hex(8).upper()
        if mark not in text:
            return mark


@dataclass
class Started:
    host: str
    session: str
    title: str


class StartError(RuntimeError):
    pass


def start_script(text: str, folder: str, *, project: str, source: str,
                 now: int, mode: str = "", resume: bool = False,
                 notes_block: str = "", base: str | None = None) -> str:
    """The POSIX script that does SPEC §4 steps 1-4 on the host.

    Prints "CONN_STARTED <session>" last when it worked, or
    "CONN_FAILED <reason>" and exits non-zero.
    """
    body = text.strip() + "\n"
    if notes_block:
        body += "\n" + notes_block.strip() + "\n"
    base = base or task_slug(text)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", base):
        raise ValueError(f"unusable slug {base!r}")
    # The project id is quoted like every other value; it is checked only
    # for being one line, since a newline would end the tmux command. An
    # empty one is not written: a server in no project should go on
    # following projects.toml, not be pinned to its own name for good.
    if "\n" in project or "\r" in project:
        raise ValueError(f"unusable project id {project!r}")
    title = task_title(text)
    run = claude_command(mode, resume)
    task_mark = _marker(body)
    options = [
        ("project", project),
        ("title", title),
        ("source", source),
        ("last_event", str(int(now))),
    ]
    sets = "\n".join(
        f"tmux set-option -t \"=$s:\" -- @conn_{name} "
        f"{shlex.quote(hosts._tmux_arg(value))} >/dev/null"
        for name, value in options if value)
    # The runner is written with a quoted heredoc, so $SHELL and $HOME in it
    # expand when it runs, on the server, not now. Its one line is fixed
    # text plus the slug, which the regex above confines to [a-z0-9-].
    return f"""\
fail() {{ echo "CONN_FAILED $*"; exit 3; }}
command -v tmux >/dev/null 2>&1 || fail "tmux is not installed"
"${{SHELL:-/bin/sh}}" -lc 'command -v claude' </dev/null >/dev/null 2>&1 \
  || fail "Claude Code is not installed"
dir={remote_dir(folder)}
[ -d "$dir" ] || fail "no folder $dir"
mkdir -p "$HOME/.conn/tasks" || fail "cannot write ~/.conn/tasks"
# The slug is claimed by creating its task file with noclobber, which is
# atomic: two starts a moment apart cannot both get csv-export.
s={base}; n=1
while tmux has-session -t "=$s" 2>/dev/null \
    || ! (set -C; : > "$HOME/.conn/tasks/$s.md") 2>/dev/null; do
  n=$((n+1)); s={base}-$n
  [ $n -gt 200 ] && fail "no free session name"
done
cat > "$HOME/.conn/tasks/$s.md" <<'{task_mark}'
{body}{task_mark}
cat > "$HOME/.conn/tasks/$s.sh" <<'CONN_RUNNER'
exec "${{SHELL:-/bin/sh}}" -lc '{run} -- "$(cat "$HOME/.conn/tasks/'"$(basename "$0" .sh)"'.md")"; exec "${{SHELL:-/bin/sh}}" -l'
CONN_RUNNER
tmux new-session -d -s "$s" -c "$dir" "sh $HOME/.conn/tasks/$s.sh" \
  || fail "tmux would not start the session"
tmux set-option -t "=$s:" -- @conn_task "~/.conn/tasks/$s.md" >/dev/null
{sets}
tmux set-option -g @conn_typed "$(date +%s)" 2>/dev/null
echo "CONN_STARTED $s"
"""


def start_chat(host: str, text: str, folder: str, *, project: str,
               source: str, now: int, mode: str = "", resume: bool = False,
               notes_block: str = "", timeout: float = 30) -> Started:
    """Start the chat. Blocking: call it on a worker. Raises StartError
    with a sentence a person can act on."""
    if not text.strip():
        raise StartError("the task is empty")
    try:
        script = start_script(text, folder, project=project, source=source,
                              now=now, mode=mode, resume=resume,
                              notes_block=notes_block)
    except ValueError as exc:
        raise StartError(str(exc))
    try:
        done = subprocess.run(hosts.run_argv(host), input=script.encode(),
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise StartError(f"{host} did not answer in {int(timeout)}s")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise StartError(str(exc))
    out = done.stdout.decode(errors="replace")
    for line in reversed(out.splitlines()):
        if line.startswith("CONN_STARTED "):
            return Started(host, line.split(" ", 1)[1].strip(),
                           task_title(text))
        if line.startswith("CONN_FAILED "):
            raise StartError(f"{line.split(' ', 1)[1].strip()} on {host}")
    err = done.stderr.decode(errors="replace").strip()
    raise StartError(err.splitlines()[-1] if err
                     else f"{host} did not say whether it started")


def send_text(host: str, session: str, text: str, timeout: float = 20) -> None:
    """SPEC §4's reply: load-buffer from stdin, bracketed paste, Enter.

    The text is the heredoc body of a script read from stdin, marker random,
    so it never meets a command line either."""
    mark = _marker(text)
    where = shlex.quote(f"={session}:")
    # $(...) drops the newline the heredoc adds, so the paste is exactly
    # the text and Enter is the only thing that submits it.
    script = (f"buf=$(cat <<'{mark}'\n{text}\n{mark}\n)\n"
              f"printf '%s' \"$buf\" | tmux load-buffer -b conn-send - && "
              f"tmux paste-buffer -d -p -b conn-send -t {where} && "
              f"tmux send-keys -t {where} Enter && "
              # Typing that went through no terminal: conn-agent reads this
              # beside tmux's client_activity to tell you are at the desk.
              f"{{ tmux set-option -g @conn_typed \"$(date +%s)\" "
              f"2>/dev/null; true; }}\n")
    try:
        done = subprocess.run(hosts.run_argv(host), input=script.encode(),
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise StartError(f"{host} did not answer in {int(timeout)}s")
    except (OSError, subprocess.SubprocessError) as exc:
        raise StartError(str(exc))
    if done.returncode != 0:
        err = done.stderr.decode(errors="replace").strip()
        raise StartError(err.splitlines()[-1] if err else "send failed")


def interrupt(host: str, session: str, timeout: float = 20) -> None:
    """SPEC §4: Escape into the session -- Claude stops its turn."""
    where = shlex.quote(f"={session}:")
    try:
        done = subprocess.run(hosts.run_argv(host),
                              input=f"tmux send-keys -t {where} Escape\n".encode(),
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise StartError(f"{host} did not answer in {int(timeout)}s")
    except (OSError, subprocess.SubprocessError) as exc:
        raise StartError(str(exc))
    if done.returncode != 0:
        err = done.stderr.decode(errors="replace").strip()
        raise StartError(err.splitlines()[-1] if err else "could not send Esc")
