"""Entry point for the server panel."""

from __future__ import annotations

import os
import re
import sys

USAGE = """conn -- your servers, and what is running on them

  conn                   open the window
  conn --list            print host names (for scripts)
  conn --check           probe every host once and print the result
  conn --notify          send one notification, to see what one looks like
  conn --log [N]         the last N (200) lines of the connection-event log:
                         wakes, network changes, reconnects, host states
  conn --quick-add       the one-line launcher: start a chat or jot a note
                         from anywhere (bind it to Super+N)
  conn --snapshot-sessions
                         write down the local tmux sessions and the Claude
                         conversation in each (a user timer, every minute)
  conn --restore-sessions
                         after a reboot, bring them back and resume Claude
                         (Hyprland autostart; --force to redo it this boot)
  conn --help            this message

Inside the window:
  F1 or ?        the key guide -- every key and mouse action
  ctrl-k         jump to anything (ctrl-shift-p from inside a terminal)
  ctrl-n         new chat (ctrl-shift-n from inside a terminal)
  ctrl-1 ctrl-2  the chat list / the notes
  alt-1..alt-9   open the session with that number
  ctrl-tab       next open session (ctrl-shift-tab for the last)
  F12            back to the list      F11  fullscreen
  ctrl-shift-w   close the view        ctrl-shift-k  kill the session
  ctrl-shift-r   rename the session
  ctrl-shift-c/v copy / paste          ctrl-f        filter
  ctrl-shift-y   copy Claude's last reply, from its transcript
  ctrl-+ - 0     text bigger, smaller, back to the terminal's own size
  ctrl-q         quit
  right-click    a host for new session, files, keys; a session to open or kill

Chat states:  working | needs you | unsent draft | idle | shell | unknown

The list is a live view of every session's screen, updated about once a second.
CONN_NO_WATCH=1 disables streaming and falls back to 45s polling.
CONN_NO_LOCAL=1 drops the 'local' row for this machine.

Sessions use the font this machine's terminal is configured with, so they
match the terminals beside them. CONN_FONT="JetBrainsMono Nerd Font 11"
overrides it, and ctrl-+ / ctrl-- / ctrl-0 zoom, remembered between runs.
CONN_THEME=light|dark forces the colours.

The window logs connection events (never chat text) to
~/.local/state/conn/conn.log; CONN_LOG=<path> puts the log elsewhere.

CONN_CHANNEL=dev (what conn-dev sets) runs a dev copy beside this one: its
own window and app id, state in ~/.local/state/conn-dev (first started from
a copy of conn's), its own log, "dev" beside the name. It sends no desktop
notifications -- the stable conn already does, for the same chats -- unless
CONN_DEV_NOTIFY=1, and leaves the turn stamps and chat titles to the stable
conn while that is running.
"""

# --check prints strings a remote can influence: ssh's auth banner lands in
# row['error'], and a session is named whatever the far side called it. This
# goes to a terminal, which obeys control characters -- an OSC can retitle
# the window, or write the clipboard where OSC 52 is on -- so none survive.
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def leave_tmux() -> None:
    """Forget any tmux the window was started from.

    The window always opens one of its own, so it is never "inside" a
    session -- but a conn restarted from a tmux pane (by a Claude chat, say)
    inherits that pane's TMUX, and own_session() would then hide that
    session from the list as the one place an attach cannot go. The CLI
    modes keep the variable: there the terminal really is in that session.
    """
    import hosts
    os.environ.pop("TMUX", None)
    os.environ.pop("TMUX_PANE", None)
    hosts._OWN_SESSION = ""


def code_stamp() -> str:
    """Which code this is, for the log: the commit when run from a checkout,
    and the newest source file's time either way (the installed copy is no
    repository)."""
    import subprocess
    import time
    from pathlib import Path
    here = Path(__file__).resolve().parent
    try:
        newest = max(p.stat().st_mtime for p in here.glob("*.py"))
        stamp = time.strftime("code of %Y-%m-%dT%H:%M:%S", time.localtime(newest))
    except (OSError, ValueError):
        stamp = "code of unknown date"
    if (here / ".git").exists():
        try:
            head = subprocess.run(["git", "-C", str(here), "rev-parse",
                                   "--short", "HEAD"], capture_output=True,
                                  text=True, timeout=3).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            head = ""
        if head:
            stamp += f", commit {head}"
    return f"{stamp} in {here}"


def run_window(service: bool = False) -> int:
    """The window (or a D-Bus activation start), with the connection-event
    log around it. Only these modes log, and only in the primary instance:
    the CLI modes are one-shot and do nothing the log is for, a second
    `conn` just hands over to the running one, and tests never come through
    here."""
    import paths
    if paths.DEV:
        # Before gui reads anything: the dev copy's first run starts from
        # the stable one's stars, order and filters (paths.seed).
        paths.seed()
    try:
        from gui import run
    except (ImportError, ValueError) as exc:
        if service:
            print(f"needs GTK4: {exc}", file=sys.stderr)
        else:
            print(f"the window needs GTK4 and VTE: {exc}\n"
                  "  sudo pacman -S gtk4 vte4 python-gobject",
                  file=sys.stderr)
        return 1
    import connlog

    def started() -> None:
        # From GApplication's startup, which only the primary instance
        # runs. `conn` typed while conn is open registers as a remote,
        # hands over 'activate' and exits: logged here, that would be a
        # start/exit pair for a restart that never happened -- in the one
        # record meant to say whether conn was restarted -- and a second
        # process rotating the file under the first.
        if connlog.setup() is not None:
            connlog.event(f"conn start: pid {os.getpid()}, "
                          f"{'D-Bus activation' if service else 'window'}, "
                          f"{code_stamp()}")

    status = 1
    try:
        status = run(service=service, on_startup=started)
    finally:
        if connlog.active():    # only if this was the primary
            connlog.event(f"conn exit: status {status}")
            connlog.shutdown()
    return status


def show_log(argv: list[str]) -> int:
    import connlog
    try:
        count = int(argv[0]) if argv else 200
    except ValueError:
        print(f"conn --log takes a number of lines, not {argv[0]!r}",
              file=sys.stderr)
        return 2
    lines = connlog.tail(count)
    if lines is None:
        print(f"no log yet at {connlog.path()}", file=sys.stderr)
        return 1
    # It is cleaned as it is written, but a terminal obeys whatever is in
    # the file, so once more on the way out.
    for line in lines:
        print(CONTROL.sub("", line))
    return 0


def main(argv: list[str]) -> int:
    import hosts

    # Before anything spawns an ssh: a conn started from a launcher inherits
    # no SSH_AUTH_SOCK, and would look like it had no agent at all.
    hosts.ensure_agent()

    if len(argv) > 1:
        arg = argv[1]

        if arg in ("-h", "--help"):
            print(USAGE)
            return 0

        if arg == "--log":
            return show_log(argv[2:])

        if arg == "--list":
            for host in hosts.list_hosts():
                print(host)
            return 0

        if arg == "--check":
            names = hosts.list_hosts()
            if not names:
                print("No hosts in ~/.ssh/config.")
                return 1
            for host in names:
                row = hosts.probe(host)
                sessions = ", ".join(s["name"] for s in row["sessions"]) or "-"
                print(CONTROL.sub("", f"{host:<20} {row['state']:<8} "
                                      f"{sessions:<24} {row['target']}"
                                  + (f"  ({row['error']})"
                                     if row["error"] else "")))
            return 0

        if arg in ("--snapshot-sessions", "--restore-sessions"):
            # Local sessions across a reboot (restore.py): a timer takes the
            # list each minute, and Hyprland's autostart rebuilds it.
            leave_tmux()
            import restore
            return restore.main(argv[1:])

        if arg == "--notify":
            try:
                from gui import send_test_notification
            except (ImportError, ValueError) as exc:
                print(f"needs GTK4: {exc}", file=sys.stderr)
                return 1
            return send_test_notification()

        if arg == "--quick-add":
            try:
                from gui import ask_running
            except (ImportError, ValueError) as exc:
                print(f"needs GTK4: {exc}", file=sys.stderr)
                return 1
            return ask_running("quick-add")

        if arg == "--gapplication-service":
            leave_tmux()
            # Not a user flag: the bus daemon passes it when it starts conn
            # to answer an activation (org.omarchy.conn.service). It has to
            # get past this parser to GApplication, which owns it -- register
            # on the bus, run the action that caused the start, and only open
            # a window if that action asks for one.
            return run_window(service=True)

        print(f"unknown option: {arg}\n\n{USAGE}", file=sys.stderr)
        return 2

    leave_tmux()
    return run_window()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
