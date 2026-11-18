#!/usr/bin/env python3
"""The dev channel: one switch, CONN_CHANNEL=dev, and what it changes.

    python3 tests/test_paths.py

paths.py is read straight, with environments passed in. What it changes in
the modules that build paths at import (chats, newchat, gui, connlog) is
checked in child processes with a HOME of their own, so nothing here reads
or writes the real ~/.local/state/conn. The gui checks run on broadway only
(as test_gui.py does) and are skipped elsewhere; every notification in them
goes to a recorder, never the desktop.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import paths  # noqa: E402

FAILURES = 0


def check(name: str, passed: bool, detail: str = "") -> None:
    global FAILURES
    FAILURES += not passed
    print(f"{'ok  ' if passed else 'FAIL'} {name}"
          + (f"   {detail}" if detail and not passed else ""))


def child(code: str, env: dict, home: Path) -> dict:
    """Run code in a fresh python with this env and HOME; it prints JSON."""
    full = {k: v for k, v in os.environ.items()
            if k not in ("CONN_CHANNEL", "CONN_DEV_NOTIFY", "CONN_LOG",
                         "XDG_STATE_HOME")}
    full.update(env, HOME=str(home), PYTHONPATH=str(ROOT))
    done = subprocess.run([sys.executable, "-c", code], env=full,
                          capture_output=True, text=True, timeout=60)
    try:
        return json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": done.stderr[-2000:]}


def switch() -> None:
    home = Path("/h")
    check("stable when CONN_CHANNEL is unset",
          not paths.is_dev({}) and paths.app_id({}) == "org.omarchy.conn"
          and paths.title({}) == "conn" and paths.name({}) == "conn")
    check("dev when CONN_CHANNEL=dev (any case, spaces)",
          paths.is_dev({"CONN_CHANNEL": "dev"})
          and paths.is_dev({"CONN_CHANNEL": " Dev "})
          and paths.app_id({"CONN_CHANNEL": "dev"}) == "org.omarchy.conn.dev"
          and paths.title({"CONN_CHANNEL": "dev"}) == "conn dev")
    check("anything else is stable, not some third channel",
          not paths.is_dev({"CONN_CHANNEL": "beta"})
          and not paths.is_dev({"CONN_CHANNEL": ""}))
    check("the state folder follows the channel",
          paths.state_dir({}, home) == home / ".local/state/conn"
          and paths.state_dir({"CONN_CHANNEL": "dev"}, home)
          == home / ".local/state/conn-dev")
    check("notifications: on in stable, off in dev, CONN_DEV_NOTIFY=1 opts in",
          paths.notifications({})
          and not paths.notifications({"CONN_CHANNEL": "dev"})
          and not paths.notifications({"CONN_CHANNEL": "dev",
                                       "CONN_DEV_NOTIFY": "0"})
          and paths.notifications({"CONN_CHANNEL": "dev",
                                   "CONN_DEV_NOTIFY": "1"})
          and paths.notifications({"CONN_DEV_NOTIFY": "0"}))
    env = {"CONN_CHANNEL": "dev", "CONN_DEV_NOTIFY": "1", "PATH": "/bin"}
    taken = paths.keep_to_self(env)
    check("keep_to_self takes the switch out and hands it back",
          taken == {"CONN_CHANNEL": "dev", "CONN_DEV_NOTIFY": "1"}
          and env == {"PATH": "/bin"}, f"{taken} {env}")


def seeding(tmp: Path) -> None:
    source = tmp / "stable"
    source.mkdir()
    for item in ("starred", "order", "filters.json", "seen.json",
                 "conn.log", "local-sessions.json"):
        (source / item).write_text(item)
    dest = tmp / "dev"
    copied = paths.seed(source, dest)
    check("first run copies the stable state that exists",
          sorted(copied) == ["filters.json", "order", "seen.json", "starred"]
          and (dest / "starred").read_text() == "starred",
          f"copied={copied}")
    check("but not the log or restore's snapshot",
          not (dest / "conn.log").exists()
          and not (dest / "local-sessions.json").exists())
    check("the dev folder is owner-only",
          stat.S_IMODE(dest.stat().st_mode) == 0o700,
          oct(dest.stat().st_mode))
    check("and no temp folder is left beside it",
          sorted(p.name for p in tmp.iterdir()) == ["dev", "stable"])
    (dest / "starred").write_text("mine now")
    (source / "starred").write_text("stable moved on")
    check("a dev folder that exists is never seeded again",
          paths.seed(source, dest) == []
          and (dest / "starred").read_text() == "mine now")
    empty = tmp / "nothing-here"
    check("no stable state at all is an empty dev folder, not an error",
          paths.seed(tmp / "missing", empty) == [] and empty.is_dir())


MODULES = """
import json, os, chats, connlog, paths
# As the window does at start: what it starts must not inherit the switch,
# and what was read from it must not change.
paths.keep_to_self()
print(json.dumps({"left": os.environ.get("CONN_CHANNEL"),"filters": str(chats.FILTERS_STATE),
                  "seen": str(chats.SEEN_STATE),
                  "known": str(chats.KNOWN_STATE),
                  "log": str(connlog.path())}))
"""


def modules(tmp: Path) -> None:
    home = tmp / "home"
    state = home / ".local" / "state"
    stable = child(MODULES, {}, home)
    dev = child(MODULES, {"CONN_CHANNEL": "dev"}, home)
    check("chats keeps its state in conn/ when stable",
          stable.get("filters") == str(state / "conn" / "filters.json")
          and stable.get("known") == str(state / "conn" / "known-chats.json"),
          str(stable))
    check("and in conn-dev/ when dev",
          dev.get("filters") == str(state / "conn-dev" / "filters.json")
          and dev.get("seen") == str(state / "conn-dev" / "seen.json"),
          str(dev))
    check("taken out of the environment, the channel is still dev",
          dev.get("left") is None, str(dev.get("left")))
    check("the log follows the channel",
          stable.get("log") == str(state / "conn" / "conn.log")
          and dev.get("log") == str(state / "conn-dev" / "conn.log"),
          f"{stable.get('log')} / {dev.get('log')}")
    xdg = child(MODULES, {"CONN_CHANNEL": "dev",
                          "XDG_STATE_HOME": str(tmp / "xdg")}, home)
    check("under $XDG_STATE_HOME too",
          xdg.get("log") == str(tmp / "xdg" / "conn-dev" / "conn.log"),
          str(xdg.get("log")))
    pinned = child(MODULES, {"CONN_CHANNEL": "dev",
                             "CONN_LOG": str(tmp / "x.log")}, home)
    check("CONN_LOG still wins over the channel",
          pinned.get("log") == str(tmp / "x.log"), str(pinned.get("log")))


# The window's module in each channel. send_notification is replaced on the
# GTK class before anything can send, so ConnApp's gate is what is measured
# and nothing reaches the desktop.
GUI = """
import json
import gui
from gi.repository import Gio, GLib, Gtk
sent = []
Gtk.Application.send_notification = lambda self, ident, note: sent.append(ident)
app = gui.ConnApp()
gui.paths.keep_to_self()        # what run() does before anything is sent
app.send_notification("conn-x", Gio.Notification.new("t"))
print(json.dumps({"app_id": gui.APP_ID, "icon": gui.ICON,
                  "prgname": GLib.get_prgname(),
                  "name": GLib.get_application_name(),
                  "own_id": app.get_application_id(),
                  "zoom": str(gui.ZOOM_STATE), "stars": str(gui.STARS_STATE),
                  "launcher": str(gui.paths.LAUNCHER),
                  "installed": str(gui.paths.INSTALLED),
                  "dev": gui.paths.DEV, "sent": sent}))
"""


def window(tmp: Path) -> None:
    # Broadway only: nothing is shown, but this is the window's module, and
    # the rule for every gui run is that it never touches the real desktop.
    if os.environ.get("GDK_BACKEND") != "broadway":
        print("skipped: the gui checks run on broadway (GDK_BACKEND=broadway)")
        return
    home = tmp / "home"
    local = home / ".local"
    stable = child(GUI, {}, home)
    dev = child(GUI, {"CONN_CHANNEL": "dev"}, home)
    opted = child(GUI, {"CONN_CHANNEL": "dev", "CONN_DEV_NOTIFY": "1"}, home)
    check("stable is org.omarchy.conn, named conn",
          stable.get("app_id") == stable.get("own_id") == "org.omarchy.conn"
          and stable.get("name") == "conn", str(stable))
    check("dev is its own application, org.omarchy.conn.dev",
          dev.get("app_id") == dev.get("own_id") == dev.get("prgname")
          == "org.omarchy.conn.dev", str(dev))
    check("named conn dev, with the stable icon (the only one installed)",
          dev.get("name") == "conn dev"
          and dev.get("icon") == "org.omarchy.conn", str(dev))
    check("the window's own state goes to conn-dev/",
          dev.get("zoom") == str(local / "state" / "conn-dev" / "zoom")
          and dev.get("stars") == str(local / "state" / "conn-dev" / "starred"),
          str(dev))
    check("restart goes to the dev install and launcher",
          dev.get("launcher") == str(local / "bin" / "conn-dev")
          and dev.get("installed") == str(local / "share" / "conn-dev" / "app")
          and stable.get("launcher") == str(local / "bin" / "conn"),
          str(dev))
    check("stable sends notifications",
          stable.get("sent") == ["conn-x"], str(stable.get("sent")))
    check("dev sends none by default",
          dev.get("sent") == [], str(dev.get("sent")))
    check("and does with CONN_DEV_NOTIFY=1",
          opted.get("sent") == ["conn-x"], str(opted.get("sent")))


def launcher() -> None:
    text = (ROOT / "bin" / "conn-dev").read_text()
    check("bin/conn-dev sets the channel and runs the dev install",
          "export CONN_CHANNEL=dev" in text
          and '"$HOME/.local/share/conn-dev"' in text
          and os.access(ROOT / "bin" / "conn-dev", os.X_OK))
    stable = (ROOT / "bin" / "conn").read_text()
    check("bin/conn is always stable, whatever the shell inherited",
          "unset CONN_CHANNEL CONN_DEV_NOTIFY" in stable
          and stable.index("unset CONN_CHANNEL") < stable.index("exec "))
    desktop = (ROOT / "org.omarchy.conn.dev.desktop").read_text()
    check("the dev copy has a launcher entry named for its window",
          "Exec=conn-dev" in desktop and "Name=Conn dev" in desktop
          and "StartupWMClass=org.omarchy.conn.dev\n" in desktop
          and "DBusActivatable" not in desktop
          and "org.omarchy.conn.dev.desktop" in text)
    import agent_setup
    check("paths.py goes to the mini with the agent (connlog imports it)",
          "paths.py" in agent_setup.FILES)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="conn-paths-test-") as name:
        tmp = Path(name)
        switch()
        (tmp / "seed").mkdir()
        seeding(tmp / "seed")
        modules(tmp)
        window(tmp)
        launcher()
    print(f"\n{'all good' if not FAILURES else str(FAILURES) + ' failed'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
