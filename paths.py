"""Which conn this is -- the stable one, or the dev copy beside it.

One switch, CONN_CHANNEL=dev (bin/conn-dev sets it), and everything that
differs between the two is decided here rather than read from the
environment wherever a path is built:

    stable                          dev
    org.omarchy.conn                org.omarchy.conn.dev   (application id)
    ~/.local/state/conn             ~/.local/state/conn-dev
    ~/.local/share/conn/app         ~/.local/share/conn-dev/app
    ~/.local/bin/conn               ~/.local/bin/conn-dev

The dev copy is a separate GApplication, not a second window of the stable
one: with the same id, `conn-dev` would register as a remote, hand
'activate' to the running stable conn and exit -- you would be testing the
old code without knowing it.

What stays shared, on purpose: ~/.config/conn (the projects, the notes
outbox) is configuration, not state; and restore.py's local-sessions.json is
the record of what to bring back after a reboot -- two of those would bring
every session back twice.

Read at import, like every path in conn: the channel cannot change under a
running process. The window then takes the switch out of its environment
(keep_to_self), so nothing it starts inherits it, and restart() puts it
back for the exec.
Stdlib only -- this goes to the Mac mini with the agent (agent_setup.FILES).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

STABLE_ID = "org.omarchy.conn"
STABLE_NAME = "conn"
DEV_NAME = "conn-dev"

# What the dev state folder starts from: the files that make the window look
# like yours -- stars, order, filters, what you have seen. Not conn.log (dev
# keeps a log of its own, which is the point of it) and not
# local-sessions.json (restore's, and shared -- see above).
SEEDED = ("starred", "order", "expanded", "zoom", "filters.json",
          "seen.json", "known-chats.json", "newchat.json")


def is_dev(env=None) -> bool:
    env = os.environ if env is None else env
    return env.get("CONN_CHANNEL", "").strip().lower() == "dev"


def name(env=None) -> str:
    """The folder name: conn, or conn-dev. Also the launcher's name."""
    return DEV_NAME if is_dev(env) else STABLE_NAME


def app_id(env=None) -> str:
    return STABLE_ID + ".dev" if is_dev(env) else STABLE_ID


def title(env=None) -> str:
    return "conn dev" if is_dev(env) else "conn"


def state_dir(env=None, home: Path | None = None) -> Path:
    """Where the window keeps what it remembers between runs. Under the
    home folder, as it always has been, not $XDG_STATE_HOME: changing that
    for the stable copy would strand everyone's stars where it used to look."""
    return (home or Path.home()) / ".local" / "state" / name(env)


def notifications(env=None) -> bool:
    """Whether this copy sends desktop notifications. The dev copy watches
    the same chats as the stable one, so with both on every toast would come
    twice; CONN_DEV_NOTIFY=1 turns them on in dev, for working on them."""
    env = os.environ if env is None else env
    return not is_dev(env) or env.get("CONN_DEV_NOTIFY", "").strip() == "1"


DEV = is_dev()
NAME = name()
APP_ID = app_id()
TITLE = title()
NOTIFY = notifications()
STATE = state_dir()
STABLE_STATE = Path.home() / ".local" / "state" / STABLE_NAME
LAUNCHER = Path.home() / ".local" / "bin" / NAME
INSTALLED = Path.home() / ".local" / "share" / NAME / "app"


# The switch and its opt-in, as the environment carries them.
SWITCHES = ("CONN_CHANNEL", "CONN_DEV_NOTIFY")


def keep_to_self(env=None) -> dict[str, str]:
    """Take the switch out of the environment the window's children inherit,
    and return what it was (for restart(), which puts it back for the exec).

    Everything the window starts -- the terminal in each view, ssh, the local
    `tmux new-session -A` -- inherits os.environ. A dev copy that happens to
    start the local tmux server would plant CONN_CHANNEL=dev in tmux's global
    environment, every shell would have it, and a plain `conn` typed in one
    would come up as the dev channel: the dev app id (handing over to the dev
    window), the dev state, no notifications. Read once, at import, so
    nothing here needs it in the environment afterwards."""
    env = os.environ if env is None else env
    taken = {}
    for key in SWITCHES:
        value = env.pop(key, None)
        if value is not None:
            taken[key] = value
    return taken


def seed(source: Path = STABLE_STATE, dest: Path = STATE) -> list[str]:
    """First run of the dev copy: start it from the stable one's state, so it
    opens with your stars, order and filters rather than as a stranger.

    Only when dest does not exist at all -- after that the two drift apart
    as you use them, which is what a test copy should do. Copied into a
    temp folder and renamed into place, so two conn-devs started at once (or
    one killed halfway) cannot leave a half-seeded folder that looks done.
    Returns the names copied; never raises -- a dev copy that starts empty
    is still a dev copy."""
    if dest.exists() or source == dest:
        return []
    temp = dest.with_name(f".{dest.name}.seeding-{os.getpid()}")
    copied = []
    try:
        temp.mkdir(parents=True, exist_ok=True)
        os.chmod(temp, 0o700)
        for item in SEEDED:
            try:
                shutil.copy2(source / item, temp / item)
                copied.append(item)
            except OSError:
                pass        # not every file exists (no zoom until you zoom)
        os.rename(temp, dest)
    except OSError:
        # Lost the race to another conn-dev, or no disk: either way what is
        # there is what there is.
        shutil.rmtree(temp, ignore_errors=True)
        return []
    return copied
