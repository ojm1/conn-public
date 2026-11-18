#!/usr/bin/env python3
"""Rebuilding local sessions after a reboot, on a throwaway tmux server.

    python3 tests/test_restore.py

TMUX is unset and TMUX_TMPDIR points into a temporary directory before
anything runs, so every tmux call here talks to a server of its own and
never to the one you are using. claude is a fake that writes down its
arguments; $SHELL is a fake login shell that puts it on PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

failures = 0
total = 0


def check(label: str, passed: bool, detail: str = "") -> None:
    global failures, total
    total += 1
    if not passed:
        failures += 1
    print(("ok   " if passed else "FAIL ") + label
          + ("" if passed or not detail else f"\n     {detail}"))


def main() -> int:
    for key in ("TMUX", "TMUX_PANE"):
        os.environ.pop(key, None)
    tmp = Path(tempfile.mkdtemp(prefix="conn-restore-"))
    sock = tmp / "tmux"
    sock.mkdir()
    os.environ["TMUX_TMPDIR"] = str(sock)
    import restore as R

    try:
        # Flags carried across.
        check("flags: permission mode kept, prompt dropped",
              R.kept_flags(["claude", "--permission-mode", "auto", "do x"])
              == ["--permission-mode", "auto"])
        check("flags: --model=x form kept",
              R.kept_flags(["claude", "--model=opus", "--resume", "abc"])
              == ["--model=opus"])
        check("flags: skip-permissions kept",
              R.kept_flags(["claude", "--dangerously-skip-permissions"])
              == ["--dangerously-skip-permissions"])
        check("command: plain shell when no conversation",
              R.pane_command({"path": "/x"}) is None)
        check("command: shell when the conversation file is gone",
              R.pane_command({"claude": "abc"}, exists=lambda c: False) is None)
        cmd = R.pane_command({"claude": "abc-1", "flags": ["--model", "a b"]},
                             exists=lambda c: True)
        check("command: resume by id, login shell, shell after",
              "claude --resume abc-1 --model" in cmd and "a b" in cmd
              and cmd.startswith("exec ") and " -lc " in cmd,
              cmd)

        if not shutil.which("tmux"):
            print("skip end to end: no tmux")
            return 1 if failures else 0

        bindir = tmp / "bin"
        bindir.mkdir()
        log = tmp / "claude-args"
        fake = bindir / "claude"
        fake.write_text(f"#!/bin/bash\necho \"$@\" >> {log}\npwd >> {log}\n"
                        "echo ' \u276f No, exit'\n"
                        "echo '   Yes, I trust this folder'\n"
                        f"read -r -s line; echo \"answered:$line\" | cat -v >> {log}\n"
                        "sleep 30\n")
        fake.chmod(0o755)
        shell = bindir / "loginsh"
        # Stands in for the login shell without reading the real profile, which
        # would put the real claude back first on PATH. "-lc CMD" (restore's) and
        # "-c CMD" (tmux's own, as $SHELL is its default shell) run CMD.
        shell.write_text(f"#!/bin/bash\nexport PATH={bindir}:$PATH\n"
                         "case $1 in -lc|-c) exec bash --noprofile --norc -c \"$2\";; esac\n"
                         "exec bash --noprofile --norc\n")
        shell.chmod(0o755)
        os.environ["SHELL"] = str(shell)
        work = tmp / "work"
        work.mkdir()

        state = tmp / "state.json"
        snap = {
            "boot": "an-earlier-boot", "taken": 1, "restored_in": "",
            "sessions": [
                {"name": "chatty", "options": {"title": "CSV export",
                                               "project": "acme",
                                               "bogus": "x"},
                 "windows": [{"index": 1, "name": "main", "active": True,
                              "panes": [{"path": str(work), "active": True,
                                         "claude": "conv-1",
                                         "flags": ["--permission-mode", "auto"]}]},
                             {"index": 2, "name": "logs", "active": False,
                              "panes": [{"path": "/nowhere/at/all"},
                                        {"path": str(work)}]}]},
                {"name": "plain", "options": {},
                 "windows": [{"index": 1, "name": "", "active": True,
                              "panes": [{"path": str(work)}]}]},
            ]}
        state.write_text(json.dumps(snap))
        R.conversation_exists = lambda conv, projects=None: conv == "conv-1"

        made = R.restore(state, now=1793900000, trust_wait=10)
        check("restore: both sessions made", sorted(made) == ["chatty", "plain"],
              repr(made))

        def tm(*args):
            return subprocess.run(["tmux", "-u", *args], capture_output=True,
                                  text=True).stdout.strip()

        check("restore: windows and names",
              tm("list-windows", "-t", "=chatty", "-F", "#{window_name}")
              .split() == ["main", "logs"])
        check("restore: split pane kept",
              tm("list-panes", "-t", "=chatty:logs", "-F", "x").count("x") == 2)
        check("restore: options carried, unknown ones not",
              tm("show-options", "-v", "-t", "=chatty:", "@conn_title") == "CSV export"
              and tm("show-options", "-v", "-t", "=chatty:", "@conn_project") == "acme"
              and "@conn_bogus" not in tm("show-options", "-t", "=chatty:"))
        check("restore: marked restored",
              tm("show-options", "-v", "-t", "=chatty:", "@conn_restored")
              == "1793900000")
        check("restore: missing folder falls back to home",
              tm("display", "-p", "-t", "=chatty:logs.0",
                 "#{pane_current_path}") == str(Path.home()))
        for _ in range(50):
            if log.exists() and "answered:" in log.read_text():
                break
            time.sleep(0.1)
        got = log.read_text() if log.exists() else ""
        check("restore: claude resumed by id with its flags, in its folder",
              "--resume conv-1 --permission-mode auto" in got and str(work) in got,
              repr(got))

        check("restore: the folder-trust question answered yes (Down, Enter)",
              "answered:^[[B" in got or "answered:^[OB" in got, repr(got))

        again = R.restore(state)
        check("restore: only once per boot", again == [], repr(again))
        check("restore: marks the list", json.loads(state.read_text())
              ["restored_in"] == R.boot_id())

        # A snapshot of the live server: the restored sessions, in order.
        taken = R.take(claude={})
        names = [s["name"] for s in taken or []]
        check("take: lists the sessions", names == ["chatty", "plain"], repr(names))
        check("take: carries the options",
              taken and taken[0]["options"].get("title") == "CSV export")
        R.snapshot(state)
        after = json.loads(state.read_text())
        check("snapshot: this boot's id", after["boot"] == R.boot_id())
        check("snapshot: same-boot list is not restored", R.restore(state) == [])

        subprocess.run(["tmux", "kill-server"], capture_output=True)
        time.sleep(0.2)
        before = state.read_text()
        R.snapshot(state)
        check("snapshot: no server leaves the last list alone",
              state.read_text() == before)
        check("take: None with no server", R.take(claude={}) is None)
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
