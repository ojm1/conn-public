#!/usr/bin/env python3
"""Starting a chat (SPEC §4), checked end to end on a throwaway tmux server.

    python3 tests/test_starting.py

The start script runs for real under bash, but with HOME, the tmux socket
directory and $SHELL all pointing into a temporary directory, and a fake
`claude` that only writes down what it was given. So tmux really makes the
session and really stores the options, and nothing touches the tmux server
you are using, nor any server anywhere.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import starting as S  # noqa: E402

failures = 0
total = 0


def check(label: str, passed: bool, detail: str = "") -> None:
    global failures, total
    total += 1
    if not passed:
        failures += 1
    print(("ok   " if passed else "FAIL ") + label
          + ("" if passed or not detail else f"\n     {detail}"))


def test_slugs() -> None:
    cases = {
        "Can we add a CSV export to the switches table? Same filters": "csv-export-switches",
        "fix the login bug": "fix-login-bug",
        "Homepage refresh": "homepage-refresh",
        "???": "chat",
        "please do it": "please-do-it",
        "Rebuild the Élan café page": "rebuild-lan-caf-page",
        "a" * 80: "a" * S.MAX_SLUG,
    }
    for text, want in cases.items():
        got = S.task_slug(text)
        check(f"slug {text[:30]!r} -> {want}", got == want, got)
    check("slug is shell- and tmux-safe",
          all(c.isalnum() or c == "-" for c in S.task_slug("x; rm -rf ~ $(id)")))


def test_titles() -> None:
    check("title: first line", S.task_title("CSV export\nmore") == "CSV export")
    long = "Can we add a CSV export to the switches table? Same filters as the page"
    t = S.task_title(long)
    check("title: trimmed to 40 at a word", len(t) <= 40 and t.endswith("…"), t)
    check("title: empty", S.task_title("  \n ") == "New chat")


def test_dirs_and_commands() -> None:
    check("dir ~", S.remote_dir("~") == '"$HOME"')
    check("dir ~/apps/x", S.remote_dir("~/apps/x") == '"$HOME"/apps/x')
    check("dir with a space is quoted",
          S.remote_dir("~/my app") == '"$HOME"/\'my app\'')
    check("dir absolute", S.remote_dir("/srv/x") == "/srv/x")
    check("command plain", S.claude_command() == "claude")
    check("command auto continue",
          S.claude_command("auto", True)
          == "claude --continue --permission-mode auto")
    try:
        S.claude_command("rm -rf")
        check("unknown mode refused", False)
    except ValueError:
        check("unknown mode refused", True)
    try:
        S.start_script("x", "~", project="bad\nid", source="desktop", now=1)
        check("bad project id refused", False)
    except ValueError:
        check("bad project id refused", True)


def _run(script: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash"], input=script.encode(), env=env,
                          capture_output=True, timeout=30)


def test_end_to_end(tmp: Path) -> None:
    if not shutil.which("tmux"):
        print("skip end to end: no tmux")
        return
    home = tmp / "home"
    (home / "proj").mkdir(parents=True)
    bindir = tmp / "bin"
    bindir.mkdir()
    log = tmp / "claude-args"
    fake = bindir / "claude"
    fake.write_text("#!/bin/sh\nfor a in \"$@\"; do printf '%s\\n--ARG--\\n' \"$a\"; done"
                    f" > {log}\npwd >> {log}\nsleep 30\n")
    fake.chmod(0o755)
    # A login shell that knows where claude is, the way ~/.profile does.
    shell = bindir / "loginsh"
    shell.write_text(f"#!/bin/bash\nexport PATH={bindir}:$PATH\nexec bash \"$@\"\n")
    shell.chmod(0o755)
    sock = tmp / "tmux"
    sock.mkdir()
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin",
           "SHELL": str(shell), "TMUX_TMPDIR": str(sock), "TERM": "xterm"}

    # A task that starts like an option must still be the task: -- ends
    # claude's options (the review's case: a pasted markdown list).
    task = ("CSV export, please. Don't break $(id) or `uname`,\n"
            "and EOF\nCONN_RUNNER\n'quotes' \"too\"")
    try:
        script = S.start_script(task, "~/proj", project="atlas",
                                source="quick-add", now=1793847000,
                                mode="auto", base="csv-export",
                                notes_block="---\nNotes on this project")
        done = _run(script, env)
        out = done.stdout.decode()
        check("start: says it started", "CONN_STARTED csv-export" in out,
              out + done.stderr.decode())

        # Second start with the same words gets -2.
        done2 = _run(S.start_script(task, "~/proj", project="atlas",
                                    source="desktop", now=1793847001, mode="auto",
                                    base="csv-export"), env)
        check("start: second gets a unique slug",
              "CONN_STARTED csv-export-2" in done2.stdout.decode(),
              done2.stdout.decode() + done2.stderr.decode())

        saved = (home / ".conn/tasks/csv-export.md").read_text()
        check("task file: verbatim, notes appended",
              saved.startswith(task + "\n") and "Notes on this project" in saved,
              repr(saved))

        def opt(name: str) -> str:
            r = subprocess.run(["tmux", "-u", "show-options", "-v", "-t", "=csv-export:",
                                f"@conn_{name}"], env=env, capture_output=True)
            return r.stdout.decode().strip()

        check("options: project", opt("project") == "atlas")
        check("options: title", opt("title") == S.task_title(task), opt("title"))
        check("options: source", opt("source") == "quick-add")
        check("options: last_event", opt("last_event") == "1793847000")
        check("options: task", opt("task") == "~/.conn/tasks/csv-export.md")
        check("empty project is not written",
              "@conn_project" not in S.start_script(
                  "x", "~", project="", source="desktop", now=1))

        for _ in range(50):
            if log.exists() and log.read_text().count("--ARG--") >= 3:
                break
            time.sleep(0.1)
        args = log.read_text().split("\n--ARG--\n") if log.exists() else []
        check("claude got the flag, --, and the task as one argument",
              len(args) >= 4 and args[0] == "--permission-mode"
              and args[1] == "auto" and args[2] == "--"
              and args[3].startswith(task), repr(args[:4]))
        check("claude ran in the project folder",
              log.exists() and log.read_text().strip().endswith(str(home / "proj")))

        # Failures are sentences.
        bad = _run(S.start_script("x", "~/missing", project="p",
                                  source="desktop", now=1), env)
        check("missing folder fails clearly",
              "CONN_FAILED no folder" in bad.stdout.decode() and bad.returncode != 0,
              bad.stdout.decode())
        env_nc = dict(env, SHELL="/bin/bash")
        nc = _run(S.start_script("x", "~", project="p", source="desktop", now=1),
                  env_nc)
        check("no claude fails clearly",
              "Claude Code is not installed" in nc.stdout.decode(),
              nc.stdout.decode())

        # Reply: bracketed paste of text with a newline, then Enter.
        S_send = S.send_text  # noqa: F841 -- exercised through its script
        mark_text = "line one\nline $(two)"
        script = (f"tmux load-buffer -b conn-send - <<'M'\n{mark_text}\nM\n"
                  "tmux show-buffer -b conn-send\n")
        shown = _run(script, env).stdout.decode()
        check("load-buffer keeps the text literally", mark_text in shown, shown)
        # A real send_text, through run_argv("local") -- with this process's
        # environment pointed at the throwaway server, TMUX dropped so tmux
        # cannot find the server this test is being run from.
        out = tmp / "typed"
        subprocess.run(["tmux", "new-session", "-d", "-s", "typing", "-x", "200",
                        f"stty -echo; cat > {out}"], env=env, check=True)
        time.sleep(0.5)
        saved = {k: os.environ.get(k) for k in ("TMUX", "TMUX_TMPDIR", "HOME")}
        os.environ.pop("TMUX", None)
        os.environ["TMUX_TMPDIR"] = str(sock)
        try:
            S.send_text("local", "typing", "first line\nsecond $(id) `x`")
            S.interrupt("local", "typing")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        for _ in range(30):
            if out.exists() and out.read_text().count("\n") >= 2:
                break
            time.sleep(0.1)
        got = out.read_text() if out.exists() else ""
        check("send_text: the text, literally, then Enter",
              got.startswith("first line\nsecond $(id) `x`\n"), repr(got))
    finally:
        subprocess.run(["tmux", "kill-server"], env=env, capture_output=True)


def main() -> int:
    with tempfile.TemporaryDirectory() as name:
        test_slugs()
        test_titles()
        test_dirs_and_commands()
        test_end_to_end(Path(name))
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
