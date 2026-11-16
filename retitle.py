"""Chat titles that follow the conversation.

Claude Code names a conversation once, from its first topic, and never again:
a chat that opened on "Server crash status" keeps that name a week later
while it is building calendar views. So when a chat finishes a turn, Conn
asks Haiku what the conversation on screen is about now, and keeps the
answer as @conn_autotitle. A name you gave it (@conn_name, F2) still wins.

The call goes through the local `claude` CLI and its own login -- no API key,
no tools, no MCP servers, no memory, nothing saved as a session, run in an
empty folder -- with the last lines of the screen as the only input. The
title is kept with the Claude title it replaced (@conn_autotitle_for), and
stops applying when that changes: a new conversation in the same session. At most one call runs at a time, and a chat
is looked at again only after RETRY_SECONDS.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time

import agent_state

RETRY_SECONDS = 15 * 60
SCREEN_LINES = 80
MAX_TITLE = 48
TIMEOUT = 60

SYSTEM = ("You name chats between a person and a coding assistant. Reply with "
          "only a title of 2 to 5 words in sentence case for what the "
          "conversation on screen is about right now -- the current task, "
          "not earlier ones. No quotes, no trailing punctuation, nothing else.")

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)")


def screen_text(screen: str, lines: int = SCREEN_LINES) -> str:
    text = _ANSI.sub("", screen or "")
    kept = [line.rstrip() for line in text.splitlines()]
    kept = [line for line in kept if line.strip()]
    return "\n".join(kept[-lines:])


def clean(reply: str) -> str:
    """One short line, or "" when the reply is not a title."""
    line = next((l.strip() for l in (reply or "").splitlines() if l.strip()), "")
    line = re.sub(r"^\W*(title|chat title)\s*:\s*", "", line, flags=re.I)
    for _ in range(2):
        line = line.strip("\"'`*#").strip().rstrip(".!?:;,").strip()
    if not line or "<" in line or len(line.split()) > 8:
        return ""
    if len(line) > MAX_TITLE:
        line = line[:MAX_TITLE].rsplit(" ", 1)[0] + "…"
    return line


def ask(screen: str, run=subprocess.run) -> str:
    """Haiku's title for a screen, or "" (no claude, a timeout, nonsense)."""
    text = screen_text(screen)
    if len(text) < 40:
        return ""
    exe = shutil.which("claude")
    if not exe:
        return ""
    try:
        # An empty folder, so no CLAUDE.md or project memory rides along
        # with the screen; auto-memory off for the same reason.
        env = dict(os.environ, CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
        with tempfile.TemporaryDirectory(prefix="conn-title-") as cwd:
            done = run([exe, "-p", "--model", "haiku", "--tools", "",
                        "--no-session-persistence", "--strict-mcp-config",
                        "--system-prompt", SYSTEM, "Title this conversation:"],
                       input=text, capture_output=True, text=True,
                       timeout=TIMEOUT, cwd=cwd, env=env)
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0:
        return ""
    return clean(done.stdout)


class Retitler:
    """Which chats are due a fresh title. The asking is the caller's (on a
    worker thread); this only keeps the books."""

    def __init__(self):
        self.last: dict[str, float] = {}
        self.busy = False

    def due(self, chat, finished: bool, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if chat.name:                       # the user's name wins
            return False
        # Only a chat at rest after a turn: not working, not booting
        # (UNKNOWN), not on a prompt -- a startup or trust screen titles
        # itself "Claude Code startup".
        if chat.state not in (agent_state.READY, agent_state.DRAFT):
            return False
        last = self.last.get(chat.id)
        if last is not None and now - last < RETRY_SECONDS:
            return False
        if finished:
            return True
        # First sight since Conn started: only a chat that has no title of
        # ours yet, so a restart does not re-ask about every chat.
        return last is None and not getattr(chat, "autotitle", "")

    def took(self, chat_id: str, now: float | None = None) -> None:
        self.last[chat_id] = time.time() if now is None else now
