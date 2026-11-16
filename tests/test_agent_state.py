#!/usr/bin/env python3
"""Checks the screen reader against real captures.

Run it with plain python -- there is no test dependency on purpose:

    python3 tests/test_agent_state.py

The opencode fixtures are genuine `tmux capture-pane` output from opencode
1.18.19, one file per state, with only the working directory in the footer
rewritten to /home/you/demo. The Claude Code screens are built here from its
chrome rather than captured, because a real capture carries whatever the
session was actually working on.

The point of this file is the last rule in agent_state's docstring: an
unreadable screen must come back UNKNOWN, never READY. A wrong READY is what
makes you walk away from a session that is sitting blocked.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_state as A  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RULE = "─" * 100

CLAUDE = {
    "busy": f"✶ Coalescing…\n\n{RULE}\n❯ \n{RULE}\n  ⏵⏵ auto mode on · esc to interrupt",
    "permission": f"Do you want to make this edit?\n 1. Yes\n{RULE}\n❯ \n{RULE}",
    "draft": f"{RULE}\n❯ finish the migration\n{RULE}\n  ⏵⏵ auto mode on",
    "idle": f"✻ Cooked for 12s\n{RULE}\n❯ Try \"fix the bug\"\n{RULE}\n  ⏵⏵ auto mode on",
    "background": f"{RULE}\n❯ \n{RULE}\n  3/8 agents done · 1m 2s ·",
    # Two finished turns still on screen. The one underneath is the one that
    # just happened; reading forwards reported the older one for as long as it
    # stayed visible.
    "two turns": (f"✻ Cogitated for 26s\n\n  all 6 rendered successfully.\n\n"
                  f"✻ Cooked for 1m 5s\n{RULE}\n❯ Try \"fix the bug\"\n{RULE}\n"
                  f"  ⏵⏵ auto mode on"),
    # A real dialog, chrome and all -- the question is drawn at the start of
    # its line, behind the box bar.
    "boxed prompt": ("╭──────────────╮\n│ Bash command\n│\n│ rm -rf build\n│\n"
                     "│ Do you want to proceed?\n│ ❯ 1. Yes\n│   2. No\n╰──────────────╯"),
    # Someone *talking* about a prompt. This is a session working on conn,
    # with the words on screen inside a sentence -- which is exactly how the
    # local session reported itself blocked while it was busy writing this.
    "prose about prompts": (
        '  For the blocked prompt I kept the pattern order -- the question\n'
        '  "Do you want to run rm -rf build?" tells you more than the "1. Yes"\n'
        '  underneath it.\n\n✻ Churned for 2m 55s\n' + RULE +
        '\n❯ Try "fix the bug"\n' + RULE + '\n  ⏵⏵ auto mode on'),
    # Same for a prompt you have already answered, sitting above a live one.
    "two prompts": (f"Do you want to make this edit?\n 1. Yes\n"
                    f"  ⎿ Updated main.py\n\n"
                    f"Do you want to run rm -rf build?\n 1. Yes\n{RULE}\n❯ \n{RULE}"),
    # An answered prompt with nothing live below it: the turn moved on, and
    # the session is sitting at an empty box. The prompt stays on screen for
    # as long as 200 lines keep it, and it must not keep the session red.
    "answered prompt": (f"Do you want to make this edit?\n 1. Yes\n"
                        f"  ⎿ Updated main.py\n\n✻ Cooked for 12s\n"
                        f"{RULE}\n❯ \n{RULE}\n  ⏵⏵ auto mode on"),
    # The answered prompt matches a higher-priority pattern than the live
    # one underneath. The quoted prompt must still be the live one.
    "prompt after prompt": (f"Do you want to make this edit?\n 1. Yes\n"
                            f"  ⎿ Updated main.py\n\n"
                            f"Would you like to create teardown.sh?\n"
                            f" 1. Yes\n{RULE}\n❯ \n{RULE}"),
    # Claude answering a question with a numbered list -- prose, not a
    # dialog. No selection caret, so it must not read as one.
    "list in prose": (f"Should we keep the design?\n"
                      f"  1. Yes, keep the current design\n"
                      f"  2. No, rewrite it\n{RULE}\n❯ \n{RULE}\n"
                      f"  ⏵⏵ auto mode on"),
    # Typographic single quotes wrap onto a line start the same way the
    # straight and double ones do.
    "curly quoted prose": ("  I saw the prompt\n"
                           "‘Do you want to run rm -rf build?’ earlier today.\n"
                           f"{RULE}\n❯ Try \"fix the bug\"\n{RULE}\n"
                           f"  ⏵⏵ auto mode on"),
    # The box grows with what you type, so its rules drift apart. Reading
    # the footer strip instead reported every tall draft as idle.
    "tall draft": (f"{RULE}\n❯ please refactor the loader\nand the tests\n"
                   f"and the docs\nand the readme\nand the changelog\n{RULE}\n"
                   f"  ⏵⏵ auto mode on"),
    # The capture is a tail, so a trim cuts the top of the screen: only the
    # box's closing rule survives, and the box itself is gone.
    "trimmed box": f"❯ half a draft\n{RULE}\n  ⏵⏵ auto mode on",
    # A finished fleet in the transcript above one still running.
    "finished fleet above": (f"8/8 agents done · 5m 0s ·\n  fleet summary\n"
                             f"{RULE}\n❯ \n{RULE}\n  1/8 agents done · 12s ·"),
    "workflow": f"{RULE}\n❯ \n{RULE}\n  Waiting for 2 workflows",
    "draft with fleet": (f"{RULE}\n❯ finish the migration\n{RULE}\n"
                         f"  3/8 agents done · 1m 2s ·"),
}

CASES = [
    # (name, screen, commands, expected state)
    ("opencode idle",        "opencode_idle.txt",             ["opencode"], A.READY),
    ("opencode draft",       "opencode_draft.txt",            ["opencode"], A.DRAFT),
    ("opencode working",     "opencode_working.txt",          ["opencode"], A.WORKING),
    ("opencode permission",  "opencode_needs_you.txt",        ["opencode"], A.NEEDS_YOU),
    ("opencode after turn",  "opencode_ready_after_turn.txt", ["opencode"], A.READY),

    ("claude busy",          CLAUDE["busy"],       ["claude"], A.WORKING),
    ("claude permission",    CLAUDE["permission"], ["claude"], A.NEEDS_YOU),
    ("claude draft",         CLAUDE["draft"],      ["claude"], A.DRAFT),
    ("claude idle",          CLAUDE["idle"],       ["claude"], A.READY),
    ("claude background",    CLAUDE["background"], ["claude"], A.WORKING),
    ("claude after two turns", CLAUDE["two turns"], ["claude"], A.READY),
    # A finished chat whose transcript *quotes* the marker -- one debugging
    # this classifier -- is not working: only the footer and the spinner
    # line count.
    ("claude quotes the marker",
     "● The screen does say \"Cogitating… (14s)\" and \"esc to interrupt\".\n"
     "  It reads \"esc to interrupt\" in the footer while busy.\n\n"
     "✻ Sautéed for 2m 31s · done 11:39 AM\n\n"
     f"{RULE}\n❯ \n{RULE}\n  ⏵⏵ auto mode on (shift+tab to cycle)",
     ["claude"], A.READY),
    # Older versions put the marker on the spinner line only.
    ("claude marker on spinner only",
     "● Reading.\n\n✻ Pondering… (12s · esc to interrupt)\n\n"
     f"{RULE}\n❯ \n{RULE}\n  ⏵⏵ auto mode on",
     ["claude"], A.WORKING),
    # No box drawn at all: the last lines are still chrome.
    ("claude marker, no box",
     "● Reading.\n\n✶ Noodling… (3s)\n  esc to interrupt",
     ["claude"], A.WORKING),
    ("claude second prompt",  CLAUDE["two prompts"], ["claude"], A.NEEDS_YOU),
    ("claude boxed prompt",   CLAUDE["boxed prompt"], ["claude"], A.NEEDS_YOU),
    ("claude talking about one", CLAUDE["prose about prompts"], ["claude"], A.READY),
    ("claude answered prompt", CLAUDE["answered prompt"], ["claude"], A.READY),
    ("claude prompt after prompt", CLAUDE["prompt after prompt"],
     ["claude"], A.NEEDS_YOU),
    ("claude list in prose",  CLAUDE["list in prose"], ["claude"], A.READY),
    ("claude curly quotes",   CLAUDE["curly quoted prose"], ["claude"], A.READY),
    ("claude tall draft",     CLAUDE["tall draft"],  ["claude"], A.DRAFT),
    ("claude trimmed box",    CLAUDE["trimmed box"], ["claude"], A.UNKNOWN),
    ("claude fleet still going", CLAUDE["finished fleet above"],
     ["claude"], A.WORKING),
    ("claude workflow",       CLAUDE["workflow"],   ["claude"], A.WORKING),
    ("claude draft over a fleet", CLAUDE["draft with fleet"],
     ["claude"], A.DRAFT),

    # No pane list at all: the agent is guessed from its own chrome.
    ("claude guessed from chrome",   CLAUDE["busy"],      [], A.WORKING),
    ("opencode guessed from chrome", "opencode_idle.txt", [], A.READY),
    ("no panes, no chrome",          "just some text",    [], A.UNKNOWN),

    # Not an agent at all.
    ("plain shell",          "opencode_idle.txt",  ["bash"],   A.SHELL),
    # Unreadable input must never come back as READY.
    ("empty screen",         "",                   ["claude"], A.UNKNOWN),
    ("no chrome",            "just some text",     ["claude"], A.UNKNOWN),
    ("truncated opencode",   "┃  half a box",      ["opencode"], A.UNKNOWN),
]


# The second line of a chat list. Built from today's chrome (Claude Code,
# November 2026): a reply is a bullet at column 0, a tool call is the same
# bullet with "Name(args)" and a result hanging off it on a ⎿ line.
PREVIEW = {
    # The spinner as Claude Code now draws it, stats appended after the
    # ellipsis. SPINNER cannot read this shape; the preview must still.
    "busy with stats": (f"● Bash(npm test)\n  ⎿  Running…\n\n"
                        f"✶ Running the tests… (1m 2s · ↓ 3.4k tokens)\n\n"
                        f"{RULE}\n❯ \n{RULE}\n  ⏵⏵ auto mode on · esc to interrupt"),
    "busy, no step": f"{RULE}\n❯ \n{RULE}\n  ⏵⏵ auto mode on · esc to interrupt",
    # Claude's random spinner verb, with the turn's real work around it. The
    # text is made up; the shapes are today's Claude Code.
    "verb over bash": ("❯ fix the tests\n\n"
                       "● Bash(cd /srv/app && npm test -- --grep auth)\n"
                       "  ⎿  Running…\n\n"
                       "✶ Transmogrifying… (1m 2s · ↓ 2.1k tokens · esc to interrupt)\n"
                       f"{RULE}\n❯ \n{RULE}\n  ⏵⏵ auto mode on · esc to interrupt"),
    "verb over todo": ("❯ fix the redirect\n\n● Update Todos\n"
                       "  ⎿  ☒ Read the handler\n"
                       "     ◼ Fixing the auth redirect\n     ☐ Ship it\n\n"
                       "✻ Pondering… (12s · esc to interrupt)\n"
                       f"{RULE}\n❯ \n{RULE}\n  esc to interrupt"),
    "verb over reply": ("❯ go on\n\n● Now let me check the tests.\n\n"
                        "✻ Schlepping… (5s · thinking)\n"
                        f"{RULE}\n❯ \n{RULE}\n  esc to interrupt"),
    "verb, old turn above": ("● Bash(make)\n  ⎿  done\n\n❯ and now?\n\n"
                             "✢ Moseying… (2s · esc to interrupt)\n"
                             f"{RULE}\n❯ \n{RULE}\n  esc to interrupt"),
    "verb over read": ("❯ look\n\n● Read(src/app.py)\n  ⎿  Read 120 lines\n\n"
                       "✶ Noodling… (3s · esc to interrupt)\n"
                       f"{RULE}\n❯ \n{RULE}\n  esc to interrupt"),
    "verb over collapsed": ("❯ look\n\n● Read 3 files (ctrl+o to expand)\n\n"
                            "✶ Noodling… (3s · esc to interrupt)\n"
                            f"{RULE}\n❯ \n{RULE}\n  esc to interrupt"),
    "verb over mcp": ("❯ open it\n\n"
                      "● claude-in-chrome - navigate (MCP)(url: \"https://example.com\")\n"
                      "  ⎿  Navigated\n\n✶ Noodling… (3s · esc to interrupt)\n"
                      f"{RULE}\n❯ \n{RULE}\n  esc to interrupt"),
    # AskUserQuestion, as Claude Code draws it in place of the input box.
    "picker": ("● I'll check the hosting options.\n\n☐ Hosting\n\n"
               "Which host should the site move to?\n\n"
               "❯ 1. IONOS\n     Cheap, slow support\n  2. Vultr\n"
               "  3. Type something.\n\n"
               "Enter to select · ↑/↓ to navigate · Esc to cancel"),
    "picker in box": (f"● I'll check.\n{RULE}\n☐ Hosting\n\n"
                      "Which host should the site move to?\n\n"
                      f"❯ 1. IONOS\n  2. Vultr\n{RULE}\n  Chat about this\n\n"
                      "Enter to select · Tab/Arrow keys to navigate · "
                      "Esc to cancel"),
    "caret list in prose": ("● Options:\n  ❯ 1. IONOS\n    2. Vultr\n\n"
                            f"✻ Cooked for 3s\n{RULE}\n❯ \n{RULE}\n"
                            "  ⏵⏵ auto mode on"),
    "answered picker": ("Which host should the site move to?\n❯ 1. IONOS\n"
                        "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
                        "  ⎿  User answered: IONOS\n\n● Moving to IONOS.\n\n"
                        f"✻ Cooked for 3s\n{RULE}\n❯ \n{RULE}\n"
                        "  ⏵⏵ auto mode on"),
    "one workflow": ("✻ Waiting for 1 dynamic workflow to finish\n"
                     f"{RULE}\n❯ \n{RULE}\n"
                     "  ◯ conn-phase1-visual-review ▰▰▰▱▱▱ 2/6 · 8m29s · "
                     "↓ 550.2k tokens"),
    "recap with hint": ("● Done.\n\n※ recap: Widgets is done. Next,\n"
                        "  open kb in Conn, or run tmux attach -t kb. "
                        "(disable recaps in /config)\n\n"
                        f"{RULE}\n❯ \n{RULE}\n  ⏵⏵ auto mode on"),
    "accented stamp": ("✻ Crunched for 1m 27s\n\n● Both are fixed.\n\n"
                       "✻ Sautéed for 1m 55s · done Tuesday 10:09 AM\n"
                       f"{RULE}\n❯ \n{RULE}\n  ⏵⏵ auto mode on"),
    "accented settles": ("Do you want to make this edit?\n 1. Yes\n\n"
                         "✻ Sautéed for 1m 55s\n"
                         f"{RULE}\n❯ \n{RULE}\n  ⏵⏵ auto mode on"),
    # A reply, then tool calls underneath it: the reply is still the newest
    # thing Claude *said*.
    "reply then tools": ("● All 41 tests pass now.\n\n  Details below.\n\n"
                         "● Read 3 files (ctrl+o to expand)\n\n"
                         "● Update(src/app.py)\n  ⎿  Updated src/app.py\n\n"
                         f"✻ Cooked for 12s\n{RULE}\n❯ \n{RULE}\n"
                         "  ⏵⏵ auto mode on"),
    # A recap from an earlier turn, then you replied and Claude answered:
    # the recap is history, the reply is current.
    "reply after recap": ("※ recap: Homepage draft is waiting for you.\n\n"
                          "❯ ship it\n\n● Shipped, with a backup of the old page.\n\n"
                          f"✻ Cooked for 3s\n{RULE}\n❯ \n{RULE}\n"
                          "  ⏵⏵ auto mode on"),
    # Claude Code's own notices wear the reply bullet too.
    "notice below reply": ("● The report is saved.\n\n"
                           "● Compacted while idle, before the prompt cache "
                           "expired\n"
                           f"{RULE}\n❯ \n{RULE}\n  ⏵⏵ auto mode on"),
    "draft keeps the reply": ("● Want me to run the migration?\n\n"
                              f"{RULE}\n❯ yes but\n{RULE}\n  ⏵⏵ auto mode on"),
    "no reply at all": f"✻ Cooked for 12s\n{RULE}\n❯ \n{RULE}\n  ⏵⏵ auto mode on",
    # Claude draws inline code without its backticks, so a reply naming a
    # function opens "Verb name(" -- which is not a tool call.
    "reply naming a call": ("● Earlier reply about the database.\n\n"
                            "● Bash(ls)\n  ⎿  a b\n\n"
                            "● Updated parse_frame() so metadata survives "
                            "newlines.\n\n"
                            f"✻ Cooked for 4s\n{RULE}\n❯ \n{RULE}\n"
                            "  ⏵⏵ auto mode on"),
    "reply naming a call, alone": (
        "● Fixed set_chat_option(host, ...) quoting.\n\n"
        f"{RULE}\n❯ \n{RULE}\n  ⏵⏵ auto mode on"),
    # The calls that must still be skipped while they have no ⎿ yet.
    "calls without results": ("● The plan is ready.\n\n"
                              "● Web Search(\"tmux user options\")\n\n"
                              "● mcp__docs__read(id: 3)\n\n"
                              "● context7 - resolve (MCP)(name: \"x\")\n\n"
                              "● Bash(npm test)\n"
                              f"{RULE}\n❯ \n{RULE}\n  ⏵⏵ auto mode on"),
}


# Captured off real screens. The words are interchangeable; only the dim SGR
# says which one you are looking at, so these carry their escapes and go in
# raw as well as stripped.
RAW_CASES = [
    ("claude suggestion is not a draft", "claude_suggestion.txt", A.READY),
    ("claude typed text is a draft",     "claude_typed_draft.txt", A.DRAFT),
]


def screen_for(value: str) -> str:
    """A case carries either a fixture filename or a screen written inline."""
    if value.endswith(".txt"):
        return (FIXTURES / value).read_text()
    return value


def main() -> int:
    failures = 0
    for name, value, commands, expected in CASES:
        result = A.classify(screen_for(value), commands)
        ok = result["state"] == expected
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name:22} {result['state']:9} "
              f"{result['detail'][:40]!r}")
        if not ok:
            print(f"     expected {expected}")

    for name, fixture, expected in RAW_CASES:
        raw = (FIXTURES / fixture).read_text()
        result = A.classify(A.strip_ansi(raw), ["claude"], raw)
        ok = result["state"] == expected
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name:34} {result['state']:9} "
              f"{result['detail'][:34]!r}")
        if not ok:
            print(f"     expected {expected}")

    # The details that make the panel useful, not just correct.
    checks = [
        ("opencode reports context",
         A.classify(screen_for("opencode_ready_after_turn.txt"),
                    ["opencode"])["tokens"] == "7.4K"),
        ("opencode names the tool it is running",
         A.classify(screen_for("opencode_working.txt"),
                    ["opencode"])["detail"] != "working"),
        ("opencode draft is the typed text",
         A.classify(screen_for("opencode_draft.txt"),
                    ["opencode"])["detail"] == "this draft is never sent"),
        ("claude still reports context",
         A.classify("300.2k tokens\n" + CLAUDE["idle"],
                    ["claude"])["tokens"] == "300.2k"),
        ("and the count it reports is the current one",
         A.classify("120.0k tokens\nlater\n559.6k tokens\n" + CLAUDE["idle"],
                    ["claude"])["tokens"] == "559.6k"),
        ("the agent is named",
         A.classify(screen_for("opencode_idle.txt"),
                    ["opencode"])["agent"] == "opencode"),
        ("the timing is the turn that just finished",
         A.classify(CLAUDE["two turns"], ["claude"])["detail"]
         == "Cooked for 1m 5s"),
        ("a boxed prompt is quoted past its chrome",
         A.classify(CLAUDE["boxed prompt"], ["claude"])["detail"]
         == "Do you want to proceed?"),
        ("and the prompt is the one still waiting",
         "rm -rf build" in A.classify(CLAUDE["two prompts"], ["claude"])["detail"]),
        ("even when the answered one outranks it",
         "teardown.sh" in A.classify(CLAUDE["prompt after prompt"],
                                     ["claude"])["detail"]),
        ("the fleet line that counts is the one still running",
         A.classify(CLAUDE["finished fleet above"], ["claude"])["detail"]
         == "1/8 agents - 12s"),
        ("a workflow is background work",
         A.classify(CLAUDE["workflow"], ["claude"])["detail"]
         == "2 workflows running"),
        ("the draft clock gets the box without the ticking fleet",
         A.classify(CLAUDE["draft with fleet"], ["claude"])["draft"]
         == "finish the migration"),
        ("draft and blocked both call for a human",
         A.needs_attention(A.DRAFT) and A.needs_attention(A.NEEDS_YOU)),
        ("working and idle do not",
         not A.needs_attention(A.WORKING) and not A.needs_attention(A.READY)),
    ]
    def preview(screen, commands=("claude",)):
        return A.classify(screen, list(commands))["preview"]

    recap_raw = (FIXTURES / "claude_recap.txt").read_text()
    recap_result = A.classify(A.strip_ansi(recap_raw), ["claude"], recap_raw)
    checks += [
        ("classify still hands out every old key",
         {"state", "label", "detail", "draft", "tokens", "agent"}
         <= set(A.classify("", ["claude"]))),
        ("and the new ones even on an empty screen",
         A.classify("", ["claude"])["preview"] == ""
         and A.classify("", ["claude"])["recap"] == ""),
        ("a shell has no preview",
         A.classify("$ ls", ["bash"])["preview"] == ""),
        ("working previews the current step, ellipsis and stats dropped",
         preview(PREVIEW["busy with stats"]) == "Running the tests"),
        ("working with no readable step says so plainly",
         preview(PREVIEW["busy, no step"]) == "Working"),
        ("a spinner verb alone is not a step",
         preview(CLAUDE["busy"]) == "Working"),
        ("a real step in the old spinner shape still reads",
         preview(f"✶ Running the tests…\n\n{RULE}\n❯ \n{RULE}\n"
                 "  esc to interrupt") == "Running the tests"),
        ("the random verb gives way to the tool call it is running",
         preview(PREVIEW["verb over bash"])
         == "Running cd /srv/app && npm test -- --grep auth"),
        ("or to the todo in progress",
         preview(PREVIEW["verb over todo"]) == "Fixing the auth redirect"),
        ("or to what Claude said in this turn",
         preview(PREVIEW["verb over reply"]) == "Now let me check the tests."),
        ("but never to a turn before the last prompt",
         preview(PREVIEW["verb, old turn above"]) == "Working"),
        ("a read previews the file",
         preview(PREVIEW["verb over read"]) == "Reading app.py"),
        ("a collapsed read previews the count",
         preview(PREVIEW["verb over collapsed"]) == "Reading 3 files"),
        ("an mcp call names its tool",
         preview(PREVIEW["verb over mcp"]) == "Using navigate"),
        ("background work previews as itself",
         preview(CLAUDE["workflow"]) == "2 workflows running"),
        ("a permission prompt previews what it wants to do",
         preview(CLAUDE["boxed prompt"]) == "Wants to run rm -rf build"),
        ("and its detail is still the question",
         A.classify(CLAUDE["boxed prompt"], ["claude"])["detail"]
         == "Do you want to proceed?"),
        ("a bare prompt with no header previews its question",
         preview(CLAUDE["permission"]) == "Do you want to make this edit?"),
        ("Claude's question picker needs you",
         A.classify(PREVIEW["picker"], ["claude"])["state"] == A.NEEDS_YOU),
        ("and previews the question, not the first option",
         preview(PREVIEW["picker"]) == "Which host should the site move to?"),
        ("a picker drawn inside the input box's rules needs you too",
         A.classify(PREVIEW["picker in box"], ["claude"])["state"]
         == A.NEEDS_YOU),
        ("a numbered list with a caret but no picker footer is prose",
         A.classify(PREVIEW["caret list in prose"], ["claude"])["state"]
         == A.READY),
        ("an answered picker is history",
         A.classify(PREVIEW["answered picker"], ["claude"])["state"]
         == A.READY),
        ("one workflow previews its name, progress and time",
         preview(PREVIEW["one workflow"])
         == "conn-phase1-visual-review · 2/6 · 8m29s"),
        ("the recap loses Claude Code's own config hint",
         A.classify(PREVIEW["recap with hint"], ["claude"])["recap"]
         == "Widgets is done. Next, open kb in Conn, or run tmux "
            "attach -t kb."),
        ("an accented past tense is the newest finished turn",
         A.classify(PREVIEW["accented stamp"], ["claude"])["detail"]
         == "Sautéed for 1m 55s"),
        ("and it settles the prompt above it",
         A.classify(PREVIEW["accented settles"], ["claude"])["state"]
         == A.READY),
        ("the recap is the preview when it is current",
         recap_result["preview"].startswith("We're refreshing the example.com")
         and recap_result["preview"].endswith("I'll put it live.")),
        ("a wrapped recap is one line, footer hint left out",
         "new task" not in recap_result["recap"]
         and "Once you've looked" in recap_result["recap"]),
        ("hyperlink escapes never reach the text",
         "]8;" not in A.strip_ansi(recap_raw)
         and "https://example.com/preview/" in A.strip_ansi(recap_raw)),
        ("and stripping keeps the line count ghost_lines relies on",
         len(A.strip_ansi(recap_raw).splitlines())
         == len(recap_raw.splitlines())),
        ("idle without a recap previews the newest reply, not a tool call",
         preview(PREVIEW["reply then tools"]) == "All 41 tests pass now."),
        ("a reply after the recap wins",
         preview(PREVIEW["reply after recap"])
         == "Shipped, with a backup of the old page."),
        ("but the recap is still reported",
         A.classify(PREVIEW["reply after recap"], ["claude"])["recap"]
         == "Homepage draft is waiting for you."),
        ("Claude Code's own notices are not replies",
         preview(PREVIEW["notice below reply"]) == "The report is saved."),
        ("a draft previews what Claude said, not what you typed",
         preview(PREVIEW["draft keeps the reply"])
         == "Want me to run the migration?"),
        ("nothing said, nothing previewed",
         preview(PREVIEW["no reply at all"]) == ""),
        ("a reply opening 'Verb name(' is a reply, not a tool call",
         preview(PREVIEW["reply naming a call"])
         == "Updated parse_frame() so metadata survives newlines."),
        ("and on its own it is still previewed",
         preview(PREVIEW["reply naming a call, alone"])
         == "Fixed set_chat_option(host, ...) quoting."),
        ("calls with no result yet -- two-word, mcp__, (MCP) -- are skipped",
         preview(PREVIEW["calls without results"]) == "The plan is ready."),
        ("opencode has no reply reader yet, so no guess",
         A.classify(screen_for("opencode_ready_after_turn.txt"),
                    ["opencode"])["preview"] == ""),
        ("opencode working previews its tool",
         preview(screen_for("opencode_working.txt"), ["opencode"])
         == A.classify(screen_for("opencode_working.txt"),
                       ["opencode"])["detail"]),
        ("a Mac's native Claude Code reports its version as the command",
         A.detect(["2.1.322"]) is A.AGENTS[0]
         and A.classify("", ["2.1.322"])["state"] != A.SHELL),
        ("Claude over ssh, working, is a chat",
         A.classify(CLAUDE["working"] if "working" in CLAUDE else
                    "✻ Thinking… (esc to interrupt)\n", ["ssh"])["state"]
         == A.WORKING),
        ("Claude over ssh, idle, is a chat too: its footer says so",
         A.classify(f"● Done.\n\n{RULE}\n❯ \n{RULE}\n"
                    "  ⏵⏵ auto mode on (shift+tab to cycle)\n",
                    ["ssh"])["state"] == A.READY),
        ("a plain ssh session is still a shell",
         A.classify("$ ls\nnotes.txt\n$ ", ["ssh"])["state"] == A.SHELL),
        ("and a quoted footer up the scrollback is not Claude",
         A.classify("(shift+tab to cycle)\n$ echo hi\nhi\n$\n$\n$\n$ ",
                    ["ssh"])["state"] == A.SHELL),
        ("but a shell or a dotted name is still a shell",
         A.detect(["zsh"]) is None and A.detect(["python3.12"]) is None
         and A.detect(["2.1"]) is None
         and A.classify("$ ", ["zsh"])["state"] == A.SHELL),
    ]

    for label, passed in checks:
        failures += not passed
        print(f"{'ok  ' if passed else 'FAIL'} {label}")

    total = len(CASES) + len(RAW_CASES) + len(checks)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
