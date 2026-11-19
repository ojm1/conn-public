#!/usr/bin/env python3
"""Titles that follow the conversation (retitle.py). Never calls claude:
ask() is given a fake runner."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_state  # noqa: E402
import chats  # noqa: E402
import retitle as R  # noqa: E402

failures = total = 0


def check(label, passed, detail=""):
    global failures, total
    total += 1
    failures += not passed
    print(("ok   " if passed else "FAIL ") + label
          + ("" if passed or not detail else f"\n     {detail}"))


check("clean: plain title", R.clean("Calendar view for purchase orders\n")
      == "Calendar view for purchase orders")
check("clean: quotes, prefix and full stop go",
      R.clean('Title: "PO deliveries on calendar."') == "PO deliveries on calendar")
check("clean: tool-call nonsense refused", R.clean("</invoke>") == "")
check("clean: a paragraph is not a title",
      R.clean("This conversation is about many different things at once and more") == "")
long = R.clean("Supercalifragilisticexpialidocious " * 2)
check("clean: long cut at a word", len(long) <= R.MAX_TITLE + 1, long)
check("screen: ansi and blank lines stripped, tail kept",
      R.screen_text("\x1b[1mhello\x1b[0m\n\n" + "\n".join(f"l{i}" for i in range(200)))
      .splitlines()[-1] == "l199"
      and len(R.screen_text("\n".join(f"l{i}" for i in range(200))).splitlines())
      == R.SCREEN_LINES)

seen = {}


def fake(argv, **kw):
    seen["argv"], seen["input"] = argv, kw.get("input")
    seen["cwd"], seen["env"] = kw.get("cwd") or "", kw.get("env") or {}
    return subprocess.CompletedProcess(argv, 0, "Calendar view for POs\n", "")


R.shutil.which = lambda name: "/usr/bin/claude"
screen = "user: show me what that would look like on the calendar\n" * 3
check("ask: returns the cleaned reply", R.ask(screen, run=fake) == "Calendar view for POs")
argv = seen.get("argv", [])
check("ask: haiku, no tools, no MCP, no saved session",
      "haiku" in argv and argv[argv.index("--tools") + 1] == ""
      and "--strict-mcp-config" in argv and "--no-session-persistence" in argv)
check("ask: the screen goes on stdin, not the command line",
      "calendar" in (seen.get("input") or "") and not any("calendar" in a for a in argv))
check("ask: run in an empty folder with auto-memory off",
      seen.get("cwd", "").startswith("/") and "conn-title-" in seen.get("cwd", "")
      and seen.get("env", {}).get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") == "1")
check("ask: a near-empty screen is not sent", R.ask("$ ", run=fake) == "")
check("ask: a failed call is no title", R.ask(screen, run=lambda *a, **k:
      subprocess.CompletedProcess(a, 1, "", "err")) == "")
R.shutil.which = lambda name: None
check("ask: no claude installed is no title", R.ask(screen, run=fake) == "")

t = R.Retitler()
chat = SimpleNamespace(id="h/s", name="", state=agent_state.READY)
check("due: first sight of an idle chat", t.due(chat, False, now=1000))
t.took("h/s", now=1000)
check("due: not again within the retry window", not t.due(chat, True, now=1000 + 60))
check("due: a finished turn after the window", t.due(chat, True, now=1000 + R.RETRY_SECONDS + 1))
check("due: an idle chat that did not finish is left alone",
      not t.due(chat, False, now=1000 + R.RETRY_SECONDS + 1))
check("due: a restart does not re-ask about a chat that has our title",
      not R.Retitler().due(SimpleNamespace(id="z", name="", autotitle="T",
                                           state=agent_state.READY), False))
check("due: but a finished turn does",
      R.Retitler().due(SimpleNamespace(id="z", name="", autotitle="T",
                                       state=agent_state.READY), True))
check("due: not while booting or on a prompt",
      not R.Retitler().due(SimpleNamespace(id="b", name="", state=agent_state.UNKNOWN), False)
      and not R.Retitler().due(SimpleNamespace(id="b", name="", state=agent_state.NEEDS_YOU), True))
check("due: never while working",
      not t.due(SimpleNamespace(id="x", name="", state=agent_state.WORKING), True))
check("due: never over the user's own name",
      not t.due(SimpleNamespace(id="y", name="Mine", state=agent_state.READY), True))

check("title: the user's name beats the autotitle",
      chats.title_for({"name": "Mine", "autotitle": "Auto"}, "✳ Claude's", "s") == "Mine")
check("title: Claude's own title holds; the autotitle is the second line",
      chats.title_for({"autotitle": "PO calendar"}, "✳ Server crash status", "s")
      == "Server crash status"
      and chats.doing(SimpleNamespace(title="Server crash status",
                                      autotitle="PO calendar")) == "PO calendar")
check("title: no second line when the autotitle is the name already",
      chats.doing(SimpleNamespace(title="PO calendar", autotitle="PO calendar")) == ""
      and chats.doing(SimpleNamespace(title="X", autotitle="")) == "")
check("title: the autotitle names a chat Claude has not named",
      chats.title_for({"autotitle": "PO calendar"}, "✳ Claude Code", "s") == "PO calendar")
check("title: a new conversation's Claude title beats an old autotitle",
      chats.title_for({"autotitle": "PO calendar", "autotitle_for": "✳ Server crash status"},
                      "✳ Telegram retries", "s") == "Telegram retries")
check("title: Claude's title stays the name while the autotitle moves on",
      chats.title_for({"autotitle": "PO calendar", "autotitle_for": "Server crash status"},
                      "✳ Server crash status", "s") == "Server crash status")
check("title: Claude's title when there is no autotitle",
      chats.title_for({}, "✳ Server crash status", "s") == "Server crash status")

print(f"\n{total - failures}/{total} passed")
sys.exit(1 if failures else 0)
