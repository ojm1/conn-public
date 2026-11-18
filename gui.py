"""conn as a window: the session list beside the session itself.

The panel used to hand its terminal to tmux, or throw a new window at the
compositor and hope it landed somewhere sensible. Neither is a layout. Here the
list is a sidebar and the session opens in a terminal widget next to it, so
opening a chat costs you nothing you were already looking at.

Only the front is new. hosts.py and agent_state.py are untouched: the probe,
the stream, the states and connect_argv() never knew what was drawing them.

GTK4 rather than Qt because VTE -- the widget GNOME Terminal, Tilix and Ptyxis
are built on -- is a real terminal emulator with first-class Python bindings.
Qt's equivalent is Konsole's, which has none.
"""

from __future__ import annotations

import os
import queue
import random
import re
import select
import sys
import textwrap
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Vte", "3.91")

from gi.repository import Gdk, Gio, GLib, Gtk, Pango, Vte  # noqa: E402

import agent_state  # noqa: E402
import chats  # noqa: E402
import connlog  # noqa: E402
import hosts  # noqa: E402
import newchat  # noqa: E402
import notes as notes_store  # noqa: E402
import notes_client  # noqa: E402
import notesview  # noqa: E402
import palette as cmdk  # noqa: E402
import paths  # noqa: E402
import retitle  # noqa: E402
import sessionview  # noqa: E402
import starting  # noqa: E402
import transcript  # noqa: E402
from theming import legible, load_palette, mix, terminal_font  # noqa: E402

# org.omarchy.conn, or org.omarchy.conn.dev for the copy run beside it
# (CONN_CHANNEL=dev, see paths.py): its own GApplication, so starting it
# never hands over to the stable window, and its own Wayland app_id, so
# Hyprland's launch-or-focus for conn never grabs it.
APP_ID = paths.APP_ID
# The icon is the stable one's in both: only org.omarchy.conn has one
# installed, and a dev toast with a blank circle helps nobody.
ICON = paths.STABLE_ID

# What the desktop calls this process. Without it every notification is
# announced as "python3", with a letter in a circle where the icon should be:
# the name comes from here, not from the application id. Set on import
# because it belongs to the process rather than to one class -- anything that
# imports this module is conn, including the tests. "conn dev" for the dev
# copy, so a toast it was asked to send says which one sent it.
GLib.set_application_name(paths.TITLE)
GLib.set_prgname(APP_ID)
HOME_PAGE = "https://github.com/ojm1/conn-public"
SOURCE = "https://github.com/ojm1/conn-public"

# The wordmark is box-drawing rather than an image so it takes the theme's
# accent colour like everything else. There is no glyph beside it any more:
# U+2388 is named HELM SYMBOL and reads as Kubernetes to exactly the people
# who would run this, which is half the reason for the new name. What sits
# there instead is the fleet's condition -- see _wordmark().
# PCRE2 compile flags, which VTE takes as-is. NO_UTF_CHECK because the
# terminal's own contents are the input and it is already UTF-8.
PCRE2_MULTILINE = 0x00000400
PCRE2_UTF = 0x00080000
PCRE2_NO_UTF_CHECK = 0x40000000

# Bare URLs, which no terminal marks up for you. Trailing punctuation is left
# out deliberately: a link at the end of a sentence should not swallow the
# full stop, and brackets around it are not part of it either.
URL_PATTERN = (r"(?:https?://|ftp://|file://|mailto:)"
               r"[^\s<>\"\'`{}|\\^\[\]]*[^\s<>\"\'`{}|\\^\[\].,;:!?)]")

# The wordmark is the name in the mono face, as the chat-list design draws
# it. Beside it, a thin bar split by how much of the fleet answers -- green up,
# red dark, grey not heard from yet -- see _wordmark() and _paint_condition().
WORDMARK = "conn"
CONDITION_HEIGHT = 20

# The mono face for server names, times and paths. The design asks for
# JetBrains Mono, which this machine has as the Nerd Font build; "monospace"
# behind it is what the rest of the window has always used, so a machine
# without it still gets a mono face rather than a proportional one.
MONO = '"JetBrainsMono Nerd Font", "JetBrains Mono", monospace'

# The chat metadata hosts.py puts on every session, probe and frame alike.
META_KEYS = ("options", "pane_title", "path", "created")

# How often the times on the chat rows ("14m", "3h") are brought up to date.
# A repaint, not a rebuild: the rows stay put, only their labels change.
TICK_SECONDS = 30

# How wide the list is, and stays. Wide enough for the longest host name you
# have beside a session's state, and narrow enough to leave the terminal the
# room it actually needs.
SIDEBAR_WIDTH = 320
# The fewest characters a host or session name keeps before it will ellipsize.
# A floor under the label's minimum width, so a squeezed sidebar shortens the
# longest names rather than collapsing every name to "lo...".
NAME_FLOOR = 12
# How much of a server name a chat row keeps before it ellipsizes, in the
# middle: "acme-data" and "convert-status" whole.
SERVER_CHARS = 20

REFRESH_SECONDS = 45     # the full sweep: uptime, disk, the session list
WATCH_INTERVAL = 1.0     # how often the far side re-dumps a screen
WATCH_RETRY_MIN = 3.0
WATCH_RETRY_MAX = 300.0
WATCH_HEALTHY = 30.0     # a stream that lasted this long was not a refusal
# The far side heartbeats every tick -- a second, plus however long the dump
# takes on a slow remote -- so this much silence is a dead channel, not a
# quiet or a slow one. It is the liveness ServerAliveInterval cannot give: a
# mux client riding the user's ControlMaster ignores it, and a master whose
# TCP died without an EOF would otherwise block the reader forever.
WATCH_STALL = 45.0
# How often to look for a wake or a new network, and how big a jump in time
# spent suspended counts as having slept (see wake_tick).
WAKE_CHECK_SECONDS = 5
WAKE_GAP = 10.0
# How long the dev copy waits, after the stable conn's bus name goes, before
# taking over the stamps and titles: a restart-to-update drops the name for a
# moment and takes it back (see _stable_seen).
STABLE_GRACE = 15
# A probe slower than this goes in the connection-event log even when the
# host's state did not change (see connlog).
SLOW_PROBE = 10.0
# How long an unsent draft has to sit untouched before it is worth saying out
# loud. Typing is a draft too -- see draft_settled().
DRAFT_DWELL = 120.0

# Zoom, the way every terminal does it: a multiplier on the font the config
# asked for, kept between runs because a size you had to set twice is not a
# setting.
ZOOM_STATE = paths.STATE / "zoom"

# Which servers are watched live (shown as "Watch live" in the Site Manager;
# the file and the in-code name are from when it was a star on the host).
# Kept beside the config rather than in it: it has no representation in ssh
# config, and the cheap way of faking one -- commenting the block out -- would
# break ssh, scp, rsync and every git remote using that alias. Not to be
# confused with a starred *chat*, which is @conn_starred on the tmux session
# itself so every client sees it (SPEC §2).
STARS_STATE = paths.STATE / "starred"

# The order you last arranged the servers in, by hand. Beside the config for
# the same reason the stars are: order has no representation in ssh config
# either, and rewriting Host blocks to fake one is how a comment or a shared
# alias gets eaten. Advisory -- a host it does not name still shows, after the
# ones it does, and a name for a host that is gone is ignored.
ORDER_STATE = paths.STATE / "order"

# Which servers are opened out in the Servers tab, their sessions showing.
# None by default: the tab is a list of servers first, and a dozen servers
# each followed by every tmux session on it read as fifty servers. Kept
# between runs, as the order is, because a server you keep open is one you
# go to.
EXPANDED_STATE = paths.STATE / "expanded"

# A watched host is live: a watch stream and the 45s sweep. The rest are
# probed on a long timer and never streamed, which is what stops a list of
# forty servers being eighty ssh connections. Their chats are still listed and
# still report waiting on you -- not watched live means slower, not unwatched.
SLOW_REFRESH = 300.0
# The last-known chats are written when the set of chats on a server changes;
# a newer "last seen" alone is written at most this often.
KNOWN_FLUSH = 300.0
# A terminal whose attach died with its server's connection is attached again
# when the server is back -- once per return: a respawn is spent until the
# server is seen dark again, so one that cannot stay up is let go rather than
# retried in a loop. A respawn that stayed up this many seconds worked, and a
# later drop is a new one with a respawn of its own. ssh-connect's hold on a
# launch that failed outright cannot pass for staying up: that exit is told
# apart by the note it leaves (hosts.launch_error), not by the clock.
RESPAWN_STABLE = 60.0

# Where this copy of conn is running from, so it can notice when a newer one
# has been installed underneath it. Sessions live in tmux, so restarting costs
# nothing -- but only if you know there is a reason to.
APP_DIR = Path(__file__).resolve().parent
ZOOM_STEP = 1.1
ZOOM_MIN, ZOOM_MAX = 0.4, 5.0

# The mark against each session, in the order the eye should find them: the
# ones waiting on you first. Same vocabulary as the TUI, so a state means the
# same thing in either front end.
MARKS = {
    agent_state.NEEDS_YOU: "!",
    agent_state.DRAFT: "!",
    agent_state.WORKING: "*",
    agent_state.READY: "o",
    agent_state.SHELL: ".",
    agent_state.UNKNOWN: "?",
}


# What may reach the notification daemon. The title carries a remote-chosen
# session name and the body a line read off a remote screen, and whatever
# renders them is not a terminal conn controls: control characters -- C0 and
# C1 both, which also beheads any escape sequence -- have no business there,
# and neither does unbounded length. Nor does markup: a daemon that renders
# body markup would follow <b> or <a href> where a name should be, so it is
# escaped last -- after the clamp, which could otherwise cut an entity in
# half and leave the text ending in a bare "&am".
NOTIFY_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def notify_text(text: str, limit: int = 120) -> str:
    return GLib.markup_escape_text(NOTIFY_UNSAFE.sub("", text)[:limit])


def session_ref(host: str, name: str) -> str:
    """One string naming one session, host first, joined with a tab.

    Tab, because it is the one separator a host alias cannot contain.
    'conn-{host}-{session}' let web/1-deploy collide with web-1/deploy, and
    a notification id two sessions share is a click that can be redirected.
    Used for both the notification id and its action target; split_ref is
    the way back.
    """
    return f"{host}\t{name}"


def split_ref(ref: str) -> tuple[str, str]:
    host, _, name = ref.partition("\t")
    return host, name


def notification(host: str, session: dict, chat=None,
                 finished: bool = False) -> "Gio.Notification":
    """The notification for one session, wherever it is being sent from.

    Built out here rather than on the window so `conn --notify` can send
    exactly what the panel sends -- same icon, same priority, same action --
    instead of an imitation that proves nothing.

    With the chat it is about, it reads as the list does (Toast.dc.html):
    the chat's title and what happened, then what it last said, with the
    session underneath. Without one -- a plain shell, `conn --notify` --
    it is the session by name, as it always was.
    """
    if chat is not None:
        state = session["agent"]["state"]
        what = ("has finished" if finished
                else "has an unsent draft" if state == agent_state.DRAFT
                else "is waiting on you")
        note = Gio.Notification.new(
            f"{notify_text(chat.title, 60)} {what}")
        words = notify_text(chat.preview or session["agent"]["detail"])
        where = f"{host}/{notify_text(session['name'], 60)}"
        note.set_body(f"{words}\n{where}" if words else where)
    else:
        note = Gio.Notification.new(f"{host}/{notify_text(session['name'], 60)}")
        detail = notify_text(session["agent"]["detail"])
        note.set_body(f"{session['agent']['label']}"
                      + (f" -- {detail}" if detail else ""))
    # Our own icon rather than whatever the shell picks for an unknown sender.
    # Themed, so it follows the icon installed under the app id.
    note.set_icon(Gio.ThemedIcon.new(ICON))
    # A blocked session is the one thing here worth interrupting for;
    # everything else this window has to say, it says in the sidebar.
    note.set_priority(Gio.NotificationPriority.HIGH
                      if session["agent"]["state"] == agent_state.NEEDS_YOU
                      else Gio.NotificationPriority.NORMAL)
    # Clicking it opens the session it is about, which is the only thing
    # anyone wants from a notification like this.
    note.set_default_action_and_target(
        "app.open-session",
        GLib.Variant("s", session_ref(host, session["name"])))
    return note


def mark_tone(state: str) -> str:
    """The tone a state is drawn in -- and, in the sidebar, the CSS class that
    draws it.

    The chat list's vocabulary (SPEC §3), mapped onto the theme rather than
    onto its hex values: waiting on you is the theme's amber, working its
    blue. Red is left to hosts that are dark, so it only ever means broken.
    """
    if state in (agent_state.NEEDS_YOU, agent_state.DRAFT):
        return "amber"
    if state == agent_state.WORKING:
        return "blue"
    if state == agent_state.READY:
        return "green"
    return "muted"


MARK_TONES = ("amber", "blue", "green", "muted")

# Which palette role each tone is. Amber is the theme's yellow: most themes
# have no amber of their own, and their yellow is the warm one.
TONE_ROLES = {"amber": "yellow", "blue": "blue", "green": "green",
              "muted": "muted"}


def mark_colour(palette, state: str) -> str:
    return getattr(palette, TONE_ROLES[mark_tone(state)])


# What a chat row's badge shows for each state (the Gtk.Stack page names).
# Idle has no badge at all: the dimmed row says it.
BADGES = {
    agent_state.NEEDS_YOU: "needs",
    agent_state.WORKING: "working",
    agent_state.DRAFT: "draft",
    agent_state.UNKNOWN: "unknown",
}


def tile_colours(palette, project, index: int,
                 idle: bool = False) -> tuple[str, str]:
    """(background, letter) for a project's tile, readable on this theme.

    The SPEC's tiles are dark-theme hex. Taking only the letter colour from
    them (or from projects.toml's `color`) and mixing the background toward
    the panel from it gives the same muted tile on a dark theme and a pale one
    on a light theme; the letter is then put through legible() against that
    background, so it is never a pale letter on a pale tile. A project with no
    colour at all gets one of the theme's own hues, picked by its place in the
    list so neighbours differ and the choice is stable between runs.
    """
    hint = project.color if project is not None else ""
    if not hint or not hint.startswith("#"):
        hues = ("blue", "magenta", "cyan", "green", "orange", "yellow", "red")
        hint = getattr(palette, hues[index % len(hues)])
    # An idle chat's tile is quieter -- the design draws it at 0.75 opacity
    # -- but by colour, not opacity: opacity fades the letter with the tile
    # and took it from the 4.5:1 legible() just set down to about 3.4. A
    # tile mixed further toward the panel, with its letter re-solved against
    # it, is as quiet and still readable.
    # A tint, not a block: mixed most of the way to the panel, the tile
    # reads as the project's colour without competing with the title, and
    # the letter -- the hue itself, lifted only as far as legible() needs --
    # carries it. At 0.68 the tile was a mid-tone the hue could not clear
    # 4.5:1 on, so every letter washed out to near white.
    background = mix(hint, palette.panel, 0.9 if idle else 0.84)
    return background, legible(hint, (background,))


def selected_card(palette) -> str:
    """The selected row's card: a step off the panel toward the text, the
    design's "#1E222A with a 1px ring" (SPEC §8) worked out from the theme.
    Toward the foreground, so it lifts on a dark theme and darkens a little
    on a light one; never the accent."""
    return mix(palette.panel, palette.foreground, 0.08)


# The Needs you chip's tints: the count's pill, the checked chip, and the
# count sitting on the checked chip -- yellow at that strength over the panel.
CHIP_TINT = 0.14
COUNT_TINT = 0.18


def amber_on_tints(palette) -> str:
    """The amber for words drawn on the Needs you tints, held to 4.5:1.

    The theme's amber made legible against the plain surfaces is not enough:
    the count and the checked chip paint it on a yellow wash, which pulls the
    background toward the text, and on Nord it fell to 3.9:1 -- 3.1 for the
    count on a checked chip. So the washes themselves are what it is solved
    against.
    """
    chip = mix(palette.panel, palette.yellow, CHIP_TINT)
    tints = (chip, mix(palette.panel, palette.yellow, COUNT_TINT),
             mix(chip, palette.yellow, COUNT_TINT))
    return legible(palette.yellow, tints + tuple(palette.surfaces))


class OptionWriter:
    """Carries @conn_* writes to their servers, one at a time, off the GTK
    main thread.

    One worker for all of them, as the recency stamps want: a burst of state
    changes is a handful of ssh calls in a row, not a thread per chat. Writes
    are coalesced per (host, session, option) -- a chat that changes state
    three times while an earlier write is still in flight is written once,
    with the latest value -- so a slow host can delay its stamps but never
    queue them up without bound.

    `failed(host, session, name, value, error)` is called on the main loop
    when a write does not land. The caller decides whether that is worth
    saying: a star you clicked is, a recency stamp to a host that just went
    dark is not.
    """

    def __init__(self, failed):
        self.failed = failed
        self.lock = threading.Lock()
        self.pending: dict[tuple[str, str, str], str | None] = {}
        self.wake = threading.Event()
        self.stopping = False
        self.thread: threading.Thread | None = None

    def put(self, host: str, session: str, name: str,
            value: str | None) -> None:
        with self.lock:
            self.pending[(host, session, name)] = value
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run,
                                               name="chat-options",
                                               daemon=True)
                self.thread.start()
        self.wake.set()

    # What is still owed at quit and worth a short wait for: a star is
    # something you clicked and saw land, and it would come back unstarred
    # on the next launch with no word. A recency stamp is not -- the next
    # state change writes a newer one anyway.
    FLUSHED = ("starred", "name", "project")

    def stop(self, timeout: float = 3.0) -> list[tuple[str, str, str]]:
        """Stop the worker, sending any owed stars first, for up to `timeout`.

        The stars go out on a thread of their own rather than behind the
        worker: it may be stuck for 20s on a stamp to a host that just went
        dark, and a star queued behind that would be dropped by any wait
        short enough to quit on. Returns the (host, session, name) writes
        that were not confirmed in time, for the caller to own up to.
        """
        with self.lock:
            self.stopping = True
            owed = {key: value for key, value in self.pending.items()
                    if key[2] in self.FLUSHED}
            self.pending.clear()
        self.wake.set()
        if not owed:
            return []
        done: set[tuple[str, str, str]] = set()

        def flush() -> None:
            for key, value in owed.items():
                try:
                    hosts.set_chat_option(*key, value)
                except (hosts.HostError, OSError):
                    continue
                done.add(key)

        thread = threading.Thread(target=flush, name="chat-options-flush",
                                  daemon=True)
        thread.start()
        thread.join(timeout)
        return [key for key in owed if key not in done]

    def _next(self) -> tuple[str, str, str]:
        """Stars ahead of stamps: a click is waiting to be seen to land."""
        for key in self.pending:
            if key[2] in self.FLUSHED:
                return key
        return next(iter(self.pending))

    def _run(self) -> None:
        while not self.stopping:
            self.wake.wait()
            with self.lock:
                if self.stopping:
                    return      # stop() took what was owed, under this lock
                if not self.pending:
                    # Cleared under the lock that put() inserts under, so a
                    # write arriving now either is seen above or sets the
                    # event again after this.
                    self.wake.clear()
                    continue
                key = self._next()
                value = self.pending.pop(key)
            try:
                hosts.set_chat_option(*key, value)
            except (hosts.HostError, OSError) as exc:
                GLib.idle_add(self.failed, *key, value, str(exc))


class WorkingBadge(Gtk.DrawingArea):
    """The working badge, drawn: a disc in the panel colour holding a faint
    blue track and a turning quarter arc, as the design has it.

    Gtk.Spinner draws whatever the icon theme's spinner is, which was a
    faint partial ring on one theme and a solid dark blob on the selected
    row on another. The colours here are the palette's, so the blue stays
    held to 3:1 on the panel it is drawn on, on every theme. It only ticks
    while spinning -- a fleet of idle chats costs no frames."""

    SIZE = 16

    def __init__(self, palette):
        super().__init__()
        self.set_size_request(self.SIZE, self.SIZE)
        self.set_content_width(self.SIZE)
        self.set_content_height(self.SIZE)
        self.disc = rgba(palette.panel)
        self.blue = rgba(palette.blue)
        self.angle = 0.0
        self.tick = 0
        self.spinning = False
        # Unbound, so the draw func the widget owns does not own the widget
        # back: a bound method made a cycle through C that kept every badge
        # of every row a rebuild threw away (the widget is passed in anyway).
        self.set_draw_func(WorkingBadge._draw)

    def get_spinning(self) -> bool:
        return self.spinning

    def set_spinning(self, spinning: bool) -> None:
        if spinning == self.spinning:
            return
        self.spinning = spinning
        if spinning:
            self.tick = self.add_tick_callback(WorkingBadge._advance)
        elif self.tick:
            self.remove_tick_callback(self.tick)
            self.tick = 0
        self.queue_draw()

    def _advance(self, clock) -> bool:     # self is the widget GTK passes
        self.angle = (clock.get_frame_time() / 1_000_000 * 2 * 3.14159
                      / 0.9) % (2 * 3.14159)
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _draw(self, cr, width, height) -> None:   # self is the area
        import math
        cx, cy = width / 2, height / 2
        cr.set_source_rgba(self.disc.red, self.disc.green, self.disc.blue, 1)
        cr.arc(cx, cy, min(width, height) / 2, 0, 2 * math.pi)
        cr.fill()
        cr.set_line_width(2.4)
        cr.set_source_rgba(self.blue.red, self.blue.green, self.blue.blue, 0.3)
        cr.arc(cx, cy, 5.0, 0, 2 * math.pi)
        cr.stroke()
        cr.set_line_cap(1)          # cairo.LINE_CAP_ROUND
        cr.set_source_rgba(self.blue.red, self.blue.green, self.blue.blue, 1)
        cr.arc(cx, cy, 5.0, self.angle, self.angle + math.pi / 2)
        cr.stroke()


def rgba(colour: str) -> Gdk.RGBA:
    value = Gdk.RGBA()
    value.parse(colour)
    return value


def source_stamp() -> dict[str, tuple[int, float]]:
    """{path: (size, mtime)} for every file this app is made of.

    A map rather than the newest mtime: `cp -p` and `rsync -a` install with
    the source's timestamps, so a real update can arrive with nothing newer
    than what was already here -- but it cannot leave this map alone. rglob,
    so files under a future package subdirectory count too.
    """
    stamp = {}
    for path in APP_DIR.rglob("*.py"):
        try:
            info = path.stat()
        except OSError:
            continue        # deleted mid-scan: absent, which is a difference
        stamp[str(path)] = (info.st_size, info.st_mtime)
    return stamp


def read_stars() -> set[str] | None:
    """The starred hosts, or None when the file has never been written.

    None is not the empty set: a first run with nothing starred would draw an
    empty sidebar under a full one, which looks like the app has lost your
    servers. The window treats None as "star what is already there".
    """
    try:
        names = STARS_STATE.read_text().split()
    except OSError:
        return None
    return set(names)


def save_stars(names) -> None:
    try:
        STARS_STATE.parent.mkdir(parents=True, exist_ok=True)
        STARS_STATE.write_text("\n".join(sorted(names)) + "\n")
    except OSError:
        pass        # a star that does not persist is not worth an error


def read_order() -> list[str]:
    """The hosts in the order you last arranged them, or [] if never set."""
    try:
        return ORDER_STATE.read_text().split()
    except OSError:
        return []


def save_order(names) -> None:
    # Not sorted, unlike the stars: here the order is the whole point.
    try:
        ORDER_STATE.parent.mkdir(parents=True, exist_ok=True)
        ORDER_STATE.write_text("\n".join(names) + "\n")
    except OSError:
        pass        # an order that does not persist is not worth an error


def read_expanded() -> set[str]:
    """The servers left opened out in the Servers tab; none if never set."""
    try:
        return set(EXPANDED_STATE.read_text().split())
    except OSError:
        return set()


def save_expanded(names) -> None:
    try:
        EXPANDED_STATE.parent.mkdir(parents=True, exist_ok=True)
        EXPANDED_STATE.write_text("\n".join(sorted(names)) + "\n")
    except OSError:
        pass        # a fold that does not persist is not worth an error


def arrange(config_order: list[str], saved: list[str]) -> list[str]:
    """The config's hosts, reordered by a saved hand-arrangement.

    Hosts the arrangement names lead, in its order; the rest keep their config
    order behind them. A saved name for a host that is gone is dropped, and a
    host the arrangement never saw is not lost -- it simply arrives at the end,
    which is where a newly-added server belongs until you place it.
    """
    known = set(config_order)
    front: list[str] = []
    for h in saved:                         # a hand-edited file may repeat a
        if h in known and h not in front:   # name; the config order never does
            front.append(h)
    placed = set(front)
    return front + [h for h in config_order if h not in placed]


def move_within_tier(order: list[str], starred, host: str,
                     delta: int) -> list[str] | None:
    """order with host swapped one place, among hosts of its own tier.

    Up and down move within the starred group or within the rest, never across
    the fold between them: a move that reordered a row folded out of sight, or
    bumped a starred host down into hiding, is a move whose result you could
    not read. None means there is no neighbour that way -- it is already at the
    top or bottom of its group.
    """
    if host not in order:
        return None         # nothing in the list to move
    tier = host in starred
    peers = [h for h in order if (h in starred) == tier]
    here = peers.index(host)
    there = here + delta
    if not 0 <= there < len(peers):
        return None
    a, b = order.index(peers[here]), order.index(peers[there])
    moved = order[:]
    moved[a], moved[b] = moved[b], moved[a]
    return moved


def read_zoom() -> float:
    """The zoom left over from last time, or 1.0 -- which is also what an
    unreadable or nonsense file means. A font size is not worth an error."""
    try:
        zoom = float(ZOOM_STATE.read_text().strip())
    except (OSError, ValueError):
        return 1.0
    return min(max(zoom, ZOOM_MIN), ZOOM_MAX)


def save_zoom(zoom: float) -> None:
    try:
        ZOOM_STATE.parent.mkdir(parents=True, exist_ok=True)
        ZOOM_STATE.write_text(f"{zoom:.3f}\n")
    except OSError:
        pass            # a size that does not survive a restart still works


class Session(Gtk.Box):
    """One open session: a VTE terminal with the real thing running in it.

    The argv comes from hosts.connect_argv(), which is the same call the TUI
    makes -- so a local session and a remote one differ only in what that
    returns, and neither knows it is being drawn into a widget instead of
    taking over a terminal.
    """

    # Counts every write conn makes to the clipboard, so the timed wipe after
    # copying a secret can tell "still the password" from "something copied
    # since", which is not its to blank.
    clipboard_serial = 0

    def __init__(self, host: str, name: str, palette, on_exit, screen=None,
                 font: str = "", zoom: float = 1.0, notice=None,
                 last_reply=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.host = host
        self.name = name
        self.on_exit = on_exit
        # What the far side thinks is on this screen. The panel is already
        # capturing it every second for the state; the menu reads links out of
        # the same text, which is the only copy where a wrapped URL can be put
        # back together.
        self.screen = screen or (lambda: "")
        # Where an action reports what it did -- the window's footer,
        # normally. A copy with no report is indistinguishable from a copy
        # that did not happen.
        self.notice = notice or (lambda text: None)
        # Copies Claude's last reply, from its transcript (Conn.copy_reply).
        # The window's, because the lookup is an ssh and the window is what
        # runs those off the main loop; None leaves it off the menu.
        self.last_reply = last_reply
        # Whether the window has seen this session's server go dark and not
        # yet come back (Conn.paint_banners), and whether the attach died
        # with its connection: a dead view keeps its scrollback and waits to
        # be respawned rather than closing. respawned is when that last
        # happened and respawn_spent that it has, since the server was last
        # seen dark -- one respawn per return. died is when the attach last
        # died, so an answer from a probe sent before that is not taken for
        # news about after it. spawned is when this attach started, for
        # telling ssh-connect's "did not start" note from an older one.
        self.offline = False
        self.dead = False
        self.respawned = 0.0
        self.respawn_spent = False
        self.died = 0.0
        self.spawned = 0.0

        # Over the terminal, never instead of it: a server going dark leaves
        # what you were reading where it was, with the reason on top.
        self.banner = Gtk.Revealer()
        self.banner.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_DOWN)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.add_css_class("session-banner")
        dot = Gtk.Box()
        dot.add_css_class("dot")
        dot.add_css_class("red")
        dot.set_valign(Gtk.Align.CENTER)
        bar.append(dot)
        self.banner_head = Gtk.Label(xalign=0)
        self.banner_head.add_css_class("banner-head")
        bar.append(self.banner_head)
        self.banner_text = Gtk.Label(xalign=0)
        self.banner_text.add_css_class("banner-text")
        self.banner_text.set_ellipsize(Pango.EllipsizeMode.END)
        self.banner_text.set_hexpand(True)
        bar.append(self.banner_text)
        self.banner.set_child(bar)
        self.append(self.banner)

        self.term = Vte.Terminal()
        self.term.set_hexpand(True)
        self.term.set_vexpand(True)
        self.term.set_scrollback_lines(10000)
        self.term.set_font(Pango.FontDescription.from_string(
            font or terminal_font()))
        self.term.set_font_scale(zoom)
        # The theme's sixteen, not VTE's: without them every ANSI colour in a
        # session -- tmux's own status line included -- is VTE's stock one.
        self.term.set_colors(rgba(palette.foreground),
                             rgba(palette.background),
                             [rgba(colour) for colour in palette.terminal])
        self.term.set_mouse_autohide(True)
        self.term.connect("child-exited", self._exited)

        # OSC 8 hyperlinks, the ones a program marks up itself.
        self.term.set_allow_hyperlink(True)
        # And the ones nobody marked up, which is most of them -- the login
        # URL an agent prints is just text until something matches it.
        self.link_tag = -1
        try:
            regex = Vte.Regex.new_for_match(
                URL_PATTERN, -1,
                PCRE2_UTF | PCRE2_NO_UTF_CHECK | PCRE2_MULTILINE)
            self.link_tag = self.term.match_add_regex(regex, 0)
            self.term.match_set_cursor_name(self.link_tag, "pointer")
        except Exception:
            pass    # no link detection is a worse terminal, not a broken one

        clicks = Gtk.GestureClick()
        clicks.set_button(0)          # any button; the handler sorts them out
        clicks.connect("pressed", self._clicked)
        self.term.add_controller(clicks)

        scroller = Gtk.ScrolledWindow()
        scroller.set_child(self.term)
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        self.append(scroller)
        self._spawn()

    def _spawn(self) -> None:
        self.spawned = time.time()
        self.term.spawn_async(
            Vte.PtyFlags.DEFAULT,
            os.path.expanduser("~"),
            hosts.connect_argv(self.host, self.name),
            None,
            # SEARCH_PATH because the argv names ssh-connect and bash rather
            # than spelling out where they live.
            GLib.SpawnFlags.SEARCH_PATH,
            # The introspected signature omits child_setup_data, but the
            # marshaller wants it: the positional list here is the C one.
            None,      # child_setup
            None,      # child_setup_data
            -1,        # no timeout: a slow host is not a failed spawn
            None,      # cancellable
            self._spawned,
        )

    def set_banner(self, head: str, text: str = "") -> None:
        """Show the banner with these words, or hide it for an empty head."""
        if head:
            if self.banner_head.get_text() != head:
                self.banner_head.set_text(head)
            if self.banner_text.get_text() != text:
                self.banner_text.set_text(text)
        if self.banner.get_reveal_child() != bool(head):
            self.banner.set_reveal_child(bool(head))

    def respawn(self) -> None:
        """Attach again in the same terminal, under what the dead one left.

        A connection that died mid-frame leaves tmux's screen modes behind
        -- the alternate screen, mouse reporting, a hidden cursor -- so they
        are put back first, then a line marks where the old attach ended:
        the new one draws a fresh screen, and the old one is still there in
        the scrollback once you detach."""
        self.dead = False
        self.respawned = time.monotonic()
        self.respawn_spent = True
        stamp = time.strftime("%H:%M")
        self.term.feed(("\x1b[?1049l\x1b[0m\x1b[?25h\x1b[?1000l\x1b[?1002l"
                        "\x1b[?1003l\x1b[?1006l\x1b[?2004l\r\n"
                        f"\u2500\u2500 reconnecting to {self.host}/{self.name}"
                        f" \u00b7 {stamp} \u2500\u2500\r\n").encode())
        self._spawn()

    def link_at(self, x: float, y: float) -> str:
        """The URL under the pointer, marked up or not."""
        try:
            hyperlink = self.term.check_hyperlink_at(x, y)
        except Exception:
            hyperlink = None
        if hyperlink:
            return hyperlink
        try:
            match, _tag = self.term.check_match_at(x, y)
        except Exception:
            return ""
        return match or ""

    def _clicked(self, gesture, _presses, x, y):
        """ctrl-click opens a link; the plain click stays the terminal's.

        A modifier rather than a bare click because the program inside may be
        tracking the mouse itself, and taking its clicks away would be worse
        than not having links.
        """
        button = gesture.get_current_button()
        state = gesture.get_current_event_state()
        control = bool(state & Gdk.ModifierType.CONTROL_MASK)

        if button == 2:                       # middle: the X primary paste
            self.term.paste_primary()
            return
        if button == 3:
            self.menu(x, y)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            return
        if button == 1 and control:
            uri = self.link_at(x, y)
            if uri:
                Gtk.UriLauncher(uri=uri).launch(None, None, None, None)
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def menu(self, x: float, y: float) -> None:
        """Copy, paste, and whatever link is under the pointer."""
        uri = self.link_at(x, y)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        popover = Gtk.Popover()
        popover.add_css_class("conn-menu")
        popover.set_parent(self.term)
        point = Gdk.Rectangle()
        point.x, point.y = int(x), int(y)
        point.width = point.height = 1
        popover.set_pointing_to(point)
        popover.connect("closed", lambda pop: pop.unparent())

        def item(label, handler, enabled=True):
            button = Gtk.Button(label=label)
            button.set_has_frame(False)
            button.get_child().set_xalign(0)
            button.set_sensitive(enabled)
            button.connect("clicked",
                           lambda _b: (popover.popdown(), handler()))
            box.append(button)

        if uri:
            item(f"Open {uri[:44]}",
                 lambda: Gtk.UriLauncher(uri=uri).launch(None, None, None, None))
            item("Copy link", lambda: self.to_clipboard(uri))

        # Links the pointer is not on, and long ones the terminal cannot see
        # as links at all because tmux delivered them in pieces.
        for link in [u for u in hosts.screen_links(self.screen())
                     if u != uri][:5]:
            shown = link if len(link) <= 44 else link[:41] + "..."
            item(f"Open {shown}",
                 lambda u=link: Gtk.UriLauncher(uri=u).launch(None, None, None, None))
            item(f"Copy {shown}", lambda u=link: self.to_clipboard(u))
        # An agent with mouse reporting on takes the drag before the terminal
        # sees it, so there is often nothing selected and no way to tell why.
        # VTE's own answer is shift, and it is hardcoded -- so the menu says
        # so, and offers the whole screen for when you just want the text.
        if self.term.get_has_selection():
            item("Copy", self.copy)
        else:
            item("Copy -- hold shift to select", self.copy, False)
        item("Copy the whole screen", self.copy_screen)
        if self.last_reply is not None:
            item("Copy Claude's last reply", self.last_reply)
        item("Paste", self.paste)
        item("Select all", self.term.select_all)
        popover.set_child(box)
        popover.popup()

    @staticmethod
    def to_clipboard(text: str) -> None:
        Session.clipboard_serial += 1
        Gdk.Display.get_default().get_clipboard().set(text)

    def copy_screen(self) -> None:
        """The whole visible screen, no selection needed -- which is the
        point of it, since an agent with mouse reporting on takes the drag.

        An empty screen leaves the clipboard alone: replacing what you had
        with "" while reporting that nothing was copied is a paste that
        comes up blank an hour later, with nothing to reproduce.
        """
        text = self.screen()
        if text:
            self.to_clipboard(text)
        self.notice(f"copied the screen -- {len(text)} characters"
                    if text else "no screen to copy yet")

    def copy(self) -> int:
        """Put the selection on the clipboard. Returns how much that was.

        The text is asked for directly rather than trusting
        get_has_selection() and handing the job to VTE: this way there is
        something to report -- a count, or the fact that there was nothing --
        and a key that quietly did nothing stops being indistinguishable from
        a key that is not bound.
        """
        try:
            text = self.term.get_text_selected(Vte.Format.TEXT) or ""
        except (AttributeError, TypeError):      # older VTE
            text = ""
        if text:
            self.to_clipboard(text)
            return len(text)
        if self.term.get_has_selection():
            # Still a write conn makes to the clipboard: without the bump,
            # the timed wipe after a secret would blank this newer copy.
            Session.clipboard_serial += 1
            self.term.copy_clipboard_format(Vte.Format.TEXT)
            return -1                            # copied, size unknown
        return 0

    def paste(self) -> None:
        self.term.paste_clipboard()

    def _spawned(self, _term, pid, error, _data=None):
        """A spawn that fails leaves an empty black rectangle and no clue.

        Saying so in the terminal itself puts the reason where the session
        would have been, which is where you are already looking.
        """
        if error is not None:
            self.term.feed(f"\r\n  could not start {self.host}/{self.name}:"
                           f"\r\n  {error.message}\r\n".encode())

    def _exited(self, _term, status):
        self.on_exit(self, status)


class OfflinePage(Gtk.Box):
    """What the right-hand side shows for a chat whose server is not
    answering: which chat, that it cannot be reached and since when, and a
    way to ask again -- instead of the terminal you were in before, which
    said nothing about the chat you had just clicked.

    One per window, reused: show() points it at a chat and paint() redraws
    the words from the window's state on every render, so "last seen" ages
    and "Trying again..." comes and goes with the probe. It is not a
    Session -- ctrl-tab and alt-N never land on it.
    """

    def __init__(self, on_retry, on_login=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.key: tuple[str, str] | None = None
        # Set once the server answered without the chat: the page says so
        # and stops offering to try again.
        self.ended = False
        self.chat_title = ""
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)
        self.add_css_class("offline-page")

        self.title = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.title.set_max_width_chars(48)
        self.title.add_css_class("offline-title")
        self.append(self.title)

        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        line.set_halign(Gtk.Align.CENTER)
        self.dot = Gtk.Box()
        self.dot.add_css_class("dot")
        self.dot.set_valign(Gtk.Align.CENTER)
        line.append(self.dot)
        self.status = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.status.add_css_class("offline-status")
        line.append(self.status)
        self.append(line)

        self.seen = Gtk.Label()
        self.seen.add_css_class("placeholder")
        self.append(self.seen)
        self.trying = Gtk.Label(label="Trying again\u2026")
        self.trying.add_css_class("placeholder")
        self.trying.add_css_class("offline-trying")
        self.append(self.trying)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.CENTER)
        buttons.set_margin_top(8)
        # A server ssh does not trust the key of yet answers every probe the
        # same way -- probes run BatchMode and cannot accept a key -- so
        # asking again is not the fix there: a session you can answer
        # ssh's prompt in is. Shown only for that state.
        self.login = Gtk.Button(label="Log in\u2026")
        self.login.add_css_class("start")
        self.login.set_visible(False)
        self.login.connect(
            "clicked", lambda _b: self.key and on_login and on_login(*self.key))
        buttons.append(self.login)
        self.retry = Gtk.Button(label="Check again")
        self.retry.add_css_class("start")
        self.retry.connect("clicked", lambda _b: self.key and on_retry(*self.key))
        buttons.append(self.retry)
        self.append(buttons)

    def show_chat(self, key: tuple[str, str]) -> None:
        if key != self.key:
            self.chat_title = ""
        self.key = key
        self.ended = False

    def paint(self, title: str, status: str, seen: str, trying: bool,
              dark: bool, login: bool = False) -> None:
        """Only what changed, so a render per frame costs nothing."""
        for label, text in ((self.title, title), (self.status, status),
                            (self.seen, seen)):
            if label.get_text() != text:
                label.set_text(text)
        self.seen.set_visible(bool(seen))
        self.trying.set_visible(trying and not self.ended)
        self.retry.set_visible(not self.ended)
        self.login.set_visible(login and not self.ended)
        for name, on in (("red", dark and not self.ended),
                         ("muted", not dark or self.ended)):
            if on:
                self.dot.add_css_class(name)
            else:
                self.dot.remove_css_class(name)


class Conn(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=paths.TITLE)
        self.set_default_size(1400, 860)

        self.palette = load_palette()
        # The font the terminals beside this window are using, and how far off
        # it you have zoomed. Palette and font are read once: a theme changed
        # mid-run lands at the next start, not before. ctrl-+ moves the zoom.
        self.font = terminal_font()
        self.zoom = read_zoom()
        # What was on disk when this process started. Any difference from it
        # is a version nobody is running yet.
        self.stamp = source_stamp()
        # A footer message and when it stops being worth showing.
        self.said: tuple[str, float] = ("", 0.0)
        self.rows: dict[str, dict] = {}
        self.order: list[str] = []
        self.open: dict[tuple[str, str], Session] = {}
        # What alt-1..9 reaches, rebuilt with the list. Numbering what is on
        # screen beats numbering what happens to be open: the number is there
        # before you open it, which is when you need it.
        self.slots: list[tuple[str, str]] = []
        # What the sidebar is currently made of, and the labels inside each
        # session row. Kept so a screen changing can repaint the words without
        # rebuilding the widgets under the pointer -- see render().
        # What each session was last seen doing. A notification is worth
        # sending when this changes to one of the states that means you, and
        # never for a state that was already true when conn started.
        self.was: dict[tuple[str, str], str] = {}
        # Per session, the draft last seen in its box, when it stopped
        # changing, and whether that one has been announced already.
        self.drafts: dict[tuple[str, str], tuple[str, float, bool]] = {}
        self.shape: list = []
        self.widgets: dict[tuple[str, str], dict] = {}
        # The condition of the fleet as a whole -- all clear, yellow alert,
        # red alert -- which is what the bar beside the name is coloured by.
        self.alert: str = agent_state.READY
        # The servers watched live ("Watch live" in the Site Manager). The
        # Servers tab lists the rest just the same -- a server you add today
        # is one click away, not something you must remember to un-hide --
        # and only the polling differs.
        self.starred: set[str] = set()
        # Watched live without being asked: a server with a chat on it. Its
        # chats' states, needs-you and dates are only worth anything if they
        # are current, and a 5-minute poll misses whole turns. A server with
        # nothing but plain shells stays on the slow poll. See follow_chats().
        self.auto_live: set[str] = set()
        # The chat list (desktop phase 1). Projects come from projects.toml,
        # read with the hosts; the filters are the chips, remembered between
        # runs. self.chats is rebuilt from self.rows on every render -- it is
        # a view of them, never a second copy anyone writes to.
        self.projects: list[chats.Project] = []
        self.filters = chats.load_filters(chats.FILTERS_STATE)
        self.chats: list[chats.Chat] = []
        self.chat_index: dict[str, chats.Chat] = {}
        self.visible_chats: list[chats.Chat] = []     # what the list shows
        # Which chats are on a host that answered, so forgetting the phase of
        # a chat that has gone (RecencyTracker.retain) does not also forget
        # every chat on a host that is merely dark for a minute.
        self.chat_ids: set[str] = set()
        self.shell_ids: set[str] = set()   # plain shells, for RecencyTracker
        self.recency = chats.RecencyTracker()
        # Stamps this window wrote and the servers have not echoed back yet.
        # A probe or frame taken before the write landed still reads the old
        # value, and without this the row would jump back down for a frame
        # and then up again. Dropped once the server's value catches up.
        self.stamps: dict[str, int] = {}
        # Stars clicked and not yet confirmed by the server, for the same
        # reason: the row shows what you asked for at once (optimistic), and
        # a write that fails takes the wish away again, with a notice.
        self.star_wish: dict[str, bool] = {}
        # A rename (@conn_name) and a move (@conn_project), the same way:
        # {chat id: value}, None meaning unset, laid over the options in
        # build_chats until the server echoes them back.
        self.name_wish: dict[str, str | None] = {}
        self.project_wish: dict[str, str | None] = {}
        # Finished while you were elsewhere (decision D, SPEC §3): which
        # chats this window watched go working -> idle, the seen book kept
        # per device, and the ones that finished this frame, for announce().
        self.finish = chats.FinishTracker()
        # Titles that follow the conversation (retitle.py): one Haiku call
        # at a time, queued by chat id.
        self.retitler = retitle.Retitler()
        self.retitle_queue: list[str] = []
        self.autotitle_wish: dict[str, str] = {}
        self.autotitle_for_wish: dict[str, str] = {}
        self.seen: dict[str, int] = chats.load_seen(chats.SEEN_STATE)
        # The chats each server last showed while it answered, so one going
        # dark -- or dark at launch -- leaves them listed, greyed, instead of
        # hidden away under a red host you forget to look at. Only ever fed
        # to chats.build_chats(): self.rows stays the live truth, which is
        # what attach, announce and every write go by.
        self.known: dict[str, dict] = chats.load_known(chats.KNOWN_STATE)
        self.known_written = time.time()
        self.just_finished: set[str] = set()
        self.projects_window: Gtk.Window | None = None
        self.writer = OptionWriter(self._option_failed)
        # The dev copy beside the stable one: both watch the same sessions,
        # and both would write the @conn_* options the window writes on its
        # own -- a last_event stamp per turn, a Haiku title per finish. Those
        # are left to the stable conn while it is running (defer_to_stable);
        # what you do in a window (star, rename, project, a reply sent) is
        # written by the window you did it in, as ever. Watched, not polled:
        # the bus says when the stable name comes and goes. Until it has
        # said, assume it is there -- a stamp missed for a second is
        # nothing, a title asked for twice is a Haiku call.
        self.stable_up = paths.DEV
        self.stable_grace = 0       # the wait before believing it gone
        if paths.DEV:
            self.stable_watch = Gio.bus_watch_name(
                Gio.BusType.SESSION, paths.STABLE_ID,
                Gio.BusNameWatcherFlags.NONE,
                lambda *_: self._stable_seen(True),
                lambda *_: self._stable_seen(False))
        # The fold in the chat list: the chats past this week. Servers used
        # to be a second fold under the chats; it is its own tab now (ctrl-2,
        # see listed()), which is where everything the old host-grouped list
        # did still is.
        self.showing_older = False
        # The servers opened out in the Servers tab (see servers_listed()).
        self.expanded: set[str] = read_expanded()
        # Set while a server row's click opens its "shell": reveal() then
        # leaves the server folded. Opening it out would undo the fold on
        # every click, and the server's own row is the one to highlight.
        self.shell_from_server = False
        # The servers folded by hand during a search that had opened them
        # (server_open()), with the search they belong to: never saved,
        # and forgotten as soon as the search text changes.
        self.search_folded: tuple[str, set[str]] = ("", set())
        # The labels inside each chat row, kept for repaint() like
        # self.widgets is for the session rows in the Servers tab, and
        # self.host_widgets for the server rows there.
        self.chat_widgets: dict[str, dict] = {}
        self.host_widgets: dict[str, dict] = {}
        self.tile_class: dict[str, str] = {}
        self.tile_css = Gtk.CssProvider()
        # Set while a chip is being brought into line with self.filters, so
        # its own toggled signal is not taken for a click.
        self.syncing = False
        self.alert_text = ""
        # When each host was last probed, so the unstarred can be swept on a
        # long timer without a second GLib source to keep in step.
        self.probed: dict[str, float] = {}
        # When a stream frame last landed for each host. A probe's captures
        # are taken at connect and arrive seconds later; this is how a probe
        # that was overtaken by the stream knows not to shout it down.
        self.streamed: dict[str, float] = {}
        # The same moment on the wall clock: a remembered chat's "last seen"
        # is when its server last really spoke, by probe (row["checked"]) or
        # by stream -- never when the window last happened to redraw.
        self.heard: dict[str, float] = {}
        # The offline chat the offline page is showing (OfflinePage): not
        # attached to a server that is not answering, but opened when an
        # answer brings it back live -- at launch, or a notification that
        # started the app, as much as a click on a dark server's chat.
        # Anything else shown or opened since cancels it (_owed_open).
        self.open_when_up: tuple[str, str] | None = None
        # A session opened before the sidebar knows it exists -- a brand new
        # one -- has no row to highlight yet. The key waits here until the
        # probe brings the row in. See select_key().
        self.pending: tuple[str, str] | None = None
        # True while the window is moving the highlight itself, so
        # row_selected() can tell the user's own move -- the only kind that
        # cancels the promise in self.pending -- from the window's.
        self.selecting = False
        # One watch thread per host, with the two events the main thread
        # steers it by: halt ends the loop for good, poke cuts a backoff
        # short. The thread itself is the record of whether a host is being
        # streamed -- see start_stream().
        self.streams: dict[str, tuple[threading.Thread,
                                      threading.Event, threading.Event]] = {}
        self.watchers: dict[str, object] = {}
        # Every watchers registration and pop happens under this lock:
        # registering is a halt-check plus a write, and unless that pair is
        # atomic against stop_stream's pop, a halting thread can bury its
        # successor's entry and leave a live ssh no stop can reach.
        self.watch_lock = threading.Lock()
        self.inflight: set[str] = set()
        self.frames: queue.Queue = queue.Queue(maxsize=256)
        self.stopping = False
        # The Site Manager, when one is open. Not modal, so it lives beside
        # the window rather than in front of it; these are how a rename or a
        # forget reaches back into it to redraw its list and report. None when
        # closed. manager_gen drops a resolve() answer a host switch outran.
        self.manager: Gtk.Window | None = None
        self.manager_reload = None
        self.manager_note = None
        self.manager_refill = None
        self.manager_host = ""
        self.manager_fields: dict[str, Gtk.Entry] = {}
        self.manager_show: Gtk.CheckButton | None = None
        # The "Connect through" choice: the dropdown, and the ProxyJump value
        # each of its rows stands for ("" is direct).
        self.manager_jump: Gtk.DropDown | None = None
        self.manager_jumps: list[str] = []
        # The "All servers connect" choice, and the jump each row stands for.
        self.manager_route: Gtk.DropDown | None = None
        self.manager_routes: list[str] = []
        self.manager_route_quiet = False
        self.manager_gen = 0
        # Notes (phase 5): kept on the notes host -- the mini -- and
        # fetched through notes_client. note_list is what the Notes view
        # draws: the last list fetched, plus anything still in the outbox.
        # notes_ok is None until the first fetch answers either way.
        self.notes = notes_client.NotesClient(notes_client.notes_host())
        self.note_list: list[dict] = (self.notes.queued()
                                      + self.notes.cached())
        self.notes_ok: bool | None = None
        self.notes_problem = ""
        self.notes_fetching = False
        self.sidebar_mode = "chats"
        self.quick: Gtk.Window | None = None
        # What was showing before New chat or the palette took over, so
        # Esc can put it back.
        self.before_new: Gtk.Widget | None = None
        self.before_palette: Gtk.Widget | None = None

        self._build()
        self._style()
        self._shortcuts()

        # The ssh is held back until check_agent() says there is a key to
        # ride, so a fresh boot does not put up one passphrase prompt per
        # connection. See connect_hosts().
        # Once, at startup, and off the main loop -- the window should come
        # up whatever state the mounts are in. A mountpoint with nothing on
        # it is left by a failed mount, and it looks like a mount that worked.
        threading.Thread(target=hosts.tidy_mounts,
                         name="tidy-mounts", daemon=True).start()

        self.holding = True
        self.load_hosts(connect=False)
        self.check_agent()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self.sweep_tick)
        # The times on the rows. render() rather than a label pass of its own:
        # an unchanged shape is only a repaint, and a day rolling over moves
        # chats between sections, which only render() knows how to do.
        GLib.timeout_add_seconds(TICK_SECONDS, self.tick)
        # The stream can speak several times a second on a busy host. Draining
        # it on a timer rather than on arrival keeps the list from rebuilding
        # under its own feet while you are trying to click something.
        GLib.timeout_add(400, self.drain_frames)
        # Notes: once a few seconds in (after the ssh agent check), then
        # every minute while the Notes view is up or the host has not
        # answered yet.
        GLib.timeout_add_seconds(4, lambda: (self.refresh_notes(),
                                             GLib.SOURCE_REMOVE)[1])
        GLib.timeout_add_seconds(60, self.notes_tick)
        # Waking from sleep, or landing on another network: see wake_tick.
        self.slept = hosts.slept_seconds()
        self.addresses = hosts.local_addresses()
        self.reviving = False
        self.revive_again = False
        # The state each host's probes last reported, and the cleaned
        # reason with it, for the log: kept apart from rows, which
        # recheck() sets to "unknown" by hand.
        self.probe_state: dict[str, tuple[str, str]] = {}
        # Hosts whose next probe result is logged whatever it is: the ones
        # a forced sweep (after a revive) asked. See _log_probe.
        self.probe_report: set[str] = set()
        GLib.timeout_add_seconds(WAKE_CHECK_SECONDS, self.wake_tick)

        self.connect("close-request", self.shut_down)

    # -- layout ------------------------------------------------------------

    def _build(self) -> None:
        # No title bar at all. GTK hides it when the window is fullscreened,
        # so nothing that matters can live there -- and once the actions moved
        # into the sidebar it was an empty strip taking a row of pixels for
        # the name of a window whose name is drawn six lines below it.
        # Closing is the x in the footer, ctrl-q, or the compositor.
        self.set_decorated(False)

        # One list for the Chats and the Servers tabs: listed() decides which
        # rows it holds. One list means one selection, one parent for every
        # right-click menu, one set of keys -- and a session open in either
        # tab keeps its highlight when you switch, because the same
        # select_key() finds its row in whichever is showing.
        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list.connect("row-activated", self.row_activated)
        self.list.connect("row-selected", self.row_selected)
        self.list.add_css_class("sidebar")
        self.list.set_filter_func(self.matches)

        self.filter = Gtk.SearchEntry()
        self.filter.set_placeholder_text("Search chats")
        self.filter.set_tooltip_text(
            "Search chats by title, server, project or what they last said "
            "-- in the Servers tab, servers and their sessions by name. "
            "ctrl-f, esc clears. ctrl-k searches everything, notes and "
            "servers included.")
        self.filter.add_css_class("filter")
        # Rebuild rather than just re-filter: a search has to be able to turn
        # up a server that is not watched live, and invalidate_filter() can
        # only hide rows that already exist.
        self.filter.connect("search-changed", lambda _e: self.refilter())
        self.filter.connect("stop-search", lambda _e: self.clear_filter())
        # Enter in the box opens what the search found, like a launcher.
        self.filter.connect("activate", lambda _e: self.open_first())

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.list)
        scroller.set_vexpand(True)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        inner.add_css_class("side")      # its border inside the fixed width
        # Name, then the tabs, then the tab's own New / search / chips: the
        # tabs first so they never move when the rows under them change.
        inner.append(self._wordmark())
        inner.append(self._mode_tabs())
        inner.append(self.side_actions)
        # Chats, Servers or Notes (ctrl-1 / ctrl-2 / ctrl-3): the same
        # column, a different list under it. Chats and Servers are both
        # self.list -- the page named "chats" -- refilled by render().
        self.side_stack = Gtk.Stack()
        self.side_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.side_stack.set_vexpand(True)
        self.side_stack.add_named(scroller, "chats")
        # Notes mode puts the projects here and the notes themselves where
        # the terminal goes (notesview.NotesBoard, added to the stack below).
        self.notes_pane = notesview.NotesBoard(self)
        self.side_stack.add_named(self.notes_pane.side, "notes")
        inner.append(self.side_stack)
        inner.append(self._footer())

        # Exactly SIDEBAR_WIDTH, whatever is in it. A Box beside the stack
        # takes its children's *natural* width, and set_size_request is only
        # a floor -- so the sidebar was 377px with "10 chats · 6 projects" in
        # the footer and 321px with "3 of 10 chats", and the terminal
        # reflowed every time a filter changed. A scrolled window that never
        # scrolls and does not pass its child's natural width up hands the
        # children exactly this much, and they ellipsize to fit. Everything
        # inside keeps a minimum at or under it (the tests measure that), so
        # nothing is ever clipped.
        side = Gtk.ScrolledWindow()
        side.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.NEVER)
        side.set_min_content_width(SIDEBAR_WIDTH)
        side.set_max_content_width(SIDEBAR_WIDTH)
        side.set_propagate_natural_width(False)
        side.set_propagate_natural_height(False)
        side.set_hexpand(False)
        side.set_child(inner)
        self.side = side

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)

        self.placeholder = Gtk.Label(
            label="Pick a session on the left.\n"
                  "It opens here -- this window keeps the list.\n\n"
                  "alt-1..9  the numbered session on the left\n"
                  "ctrl-tab  next open session\n"
                  "F12       back to the list")
        self.placeholder.add_css_class("placeholder")
        self.placeholder.set_justify(Gtk.Justification.CENTER)
        self.stack.add_named(self.placeholder, "placeholder")
        # The New chat screen (phase 2), one per window.
        self.newchat = newchat.NewChatPage(self)
        self.stack.add_named(self.newchat, "new-chat")
        self.stack.add_named(self.notes_pane, "notes")
        self.before_notes = None
        # An offline chat, opened: see OfflinePage.
        self.offline_page = OfflinePage(self.check_again, self.log_in)
        self.stack.add_named(self.offline_page, "offline")

        # Not a Paned. The divider was never worth dragging -- the list holds
        # host names and a short detail, and neither wants more room -- but it
        # sat under the pointer all day and got caught on the way to a row.
        # A fixed width is one less thing that can be knocked out of shape,
        # and it is the shape the footer's minimum is measured against.
        split = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.stack.set_hexpand(True)
        split.append(side)
        split.append(self.stack)
        # The ctrl-k palette is laid over everything, dim layer and all.
        overlay = Gtk.Overlay()
        overlay.set_child(split)
        self.cmdk = cmdk.Palette(self)
        overlay.add_overlay(self.cmdk)
        self.set_child(overlay)

    def _guide(self) -> Gtk.Popover:
        """What a mark means, spelled out.

        An amber dot on a tile is only obvious once someone has told you;
        until then it is decoration. The chat rows wear these as badges on
        the project tile, the session rows in the Servers tab as the marks
        shown here, in the same colours.
        """
        marks = [
            ("!", agent_state.NEEDS_YOU, "needs you",
             "amber dot -- blocked on a prompt only you can answer"),
            ("!", agent_state.DRAFT, "unsent draft",
             "pencil -- text left in the box, never submitted"),
            ("*", agent_state.WORKING, "working",
             "blue spinner -- busy, or running agents. Leave it"),
            ("o", agent_state.READY, "idle",
             "no badge, dimmed -- waiting at an empty prompt"),
            (".", agent_state.SHELL, "shell",
             "not an agent, just a shell -- listed in the Servers tab"),
            ("?", agent_state.UNKNOWN, "unknown",
             "grey ? -- not recognised, never assume it is idle"),
        ]
        grid = Gtk.Grid(row_spacing=4, column_spacing=10, margin_top=12,
                        margin_bottom=12, margin_start=12, margin_end=12)
        for row, (mark, state, name, means) in enumerate(marks):
            glyph = Gtk.Label(label=mark, xalign=0)
            glyph.add_css_class("mark")
            glyph.set_attributes(self._colour_attrs(
                mark_colour(self.palette, state)))
            grid.attach(glyph, 0, row, 1, 1)
            grid.attach(Gtk.Label(label=name, xalign=0), 1, row, 1, 1)
            hint = Gtk.Label(label=means, xalign=0)
            hint.add_css_class("detail")
            grid.attach(hint, 2, row, 1, 1)

        # Everything the window answers to. A guide that lists some of the
        # keys is worse than none: it is read as the whole set, so the ones
        # left out are the ones nobody ever finds.
        rows = [
            ("keys", ""),
            ("alt-1..9", "open the chat or session with that number -- "
             "hold alt to see the numbers"),
            ("F2", "rename the selected chat (empty: Claude's title again)"),
            ("ctrl-k", "jump to anything: chats, shells, notes, servers, "
             "actions. Tab peeks, ctrl-enter opens in a window"),
            ("ctrl-shift-p", "the same, from inside a terminal"),
            ("ctrl-n", "new chat; ctrl-enter starts it (ctrl-shift-n in a "
             "terminal)"),
            ("ctrl-f", "search the chat list, or the servers in that tab"),
            ("ctrl-1 / ctrl-2 / ctrl-3", "the chats / the servers / the notes"),
            ("Super+N", "quick add from any app: Tab project, ctrl-Tab note"),
            ("s", "star or unstar the selected chat, in the list"),
            ("ctrl-tab", "next open session -- add shift to go back"),
            ("F12", "back to the list; arrows move, enter opens"),
            ("ctrl-shift-c / v", "copy / paste -- shift-drag to select first"),
            ("ctrl-shift-s", "copy the whole screen, no selecting needed"),
            ("ctrl-shift-y", "copy Claude's last reply, word for word"),
            ("ctrl-shift-a", "select everything on the screen"),
            ("ctrl-shift-w", "close the view, leave the session running"),
            ("ctrl-shift-k", "kill the selected session, for good"),
            ("ctrl-shift-r", "rename its tmux session -- nothing in it is "
             "interrupted"),
            ("ctrl-shift-up / down", "move the selected server up or down"),
            ("right / left", "show / hide a server's sessions, in the Servers "
             "tab (+ and - too)"),
            ("ctrl-+ - 0", "text bigger, smaller, back to your terminal's size"),
            ("F11 / ctrl-q", "fullscreen / quit"),
            ("F1 or ?", "this guide"),
            ("mouse", ""),
            ("ctrl-click", "open a link in a session"),
            ("right-click a session", "open, star, rename, move to a project, "
             "save its recap to notes, rename the session, or kill it"),
            ("right-click a note", "start a chat, edit, mark done, copy, "
             "delete"),
            ("click a server", "a terminal on it -- its \"shell\" session, "
             "made if it is not there yet"),
            ("right-click a server",
             "watch it live or move it, its projects, check again, new "
             "session, files, passwords, edit, forget"),
            ("right-click the screen", "copy, paste, Claude's last reply, and "
             "every link on it"),
            ("hover a session", "a bin over the time kills it"),
            ("star on a chat", "pins it under Starred, on every client"),
            ("chips", "Project, Server, Needs you, group by project -- "
             "remembered between runs"),
            ("amber, no prompt", "Claude finished while you were elsewhere -- "
             "until you open it"),
            ("Servers tab", "every server; the arrow beside one shows its "
             "sessions, plain shells too; a broken one has its fix on a "
             "button -- install my key, trust, check a changed key, check "
             "again"),
            ("server icon", "Hosts -- edit, rename, add or forget a server, "
             "its passwords, and Watch live"),
            ("menu icon", "add a server, edit projects, refresh, this guide, "
             "close"),
        ]
        for offset, (key, means) in enumerate(rows):
            row = len(marks) + offset + 1
            if not means:               # a heading, not a key
                heading = Gtk.Label(label=key, xalign=0, margin_top=10)
                heading.add_css_class("host")
                grid.attach(heading, 0, row, 3, 1)
                continue
            label = Gtk.Label(label=key, xalign=0)
            label.add_css_class("tab")
            grid.attach(label, 0, row, 2, 1)
            hint = Gtk.Label(label=means, xalign=0)
            hint.add_css_class("detail")
            grid.attach(hint, 2, row, 1, 1)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        # Scrolled, because a popover is clipped to what fits between the
        # button and the edge of the screen -- and it is anchored to the foot
        # of the list, where there is least room. Silently losing the last few
        # rows is exactly how a key goes missing from a key guide.
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(True)
        scroller.set_propagate_natural_width(True)
        # Measured, not guessed: the full list is ~570px of rows and this
        # laptop's screen is 750 logical pixels tall, with the button at the
        # foot of the list. It did not fit, and GTK clips a popover rather
        # than scrolling one -- which is how ctrl-+ was in the guide, in the
        # build, and still nowhere anybody could see it.
        scroller.set_max_content_height(400)
        scroller.set_child(grid)
        box.append(scroller)

        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.set_margin_start(12)
        rule.set_margin_end(12)
        box.append(rule)

        credits = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                          margin_bottom=12, margin_start=12, margin_end=12)
        blurb = Gtk.Label(
            label="You have the conn -- the servers you keep coding agents "
                  "on, and whether they are working or waiting on you.",
            xalign=0)
        blurb.add_css_class("detail")
        blurb.set_wrap(True)
        blurb.set_max_width_chars(46)
        credits.append(blurb)

        links = Gtk.Box(spacing=12, margin_top=4)
        for label, uri in (("source", SOURCE),):
            link = Gtk.LinkButton(uri=uri, label=label)
            link.set_has_frame(False)
            link.add_css_class("link")
            links.append(link)
        credits.append(links)

        holder = Gtk.Label(label="(c) 2026 ojm1  --  MIT", xalign=0)
        holder.add_css_class("detail")
        credits.append(holder)
        box.append(credits)

        popover = Gtk.Popover()
        popover.add_css_class("conn-menu")
        popover.set_child(box)
        return popover

    # -- acting on a session -----------------------------------------------

    def selected_key(self) -> tuple[str, str] | None:
        row = self.list.get_selected_row()
        return getattr(row, "key", None) if row is not None else None

    def selected_host(self) -> str:
        """The host of whatever is selected, or the first one listed.

        A session row implies its host, so "new session" does not need a
        separate host selection to mean something.
        """
        key = self.selected_key()
        if key:
            return key[0]
        row = self.list.get_selected_row()
        server = getattr(row, "server", None) if row is not None else None
        if server:
            return server
        return self.order[0] if self.order else hosts.LOCAL

    def row_menu(self, row, x: float, y: float) -> None:
        key = getattr(row, "key", None)
        if key is None:
            return
        host, name = key
        items = [("Open", lambda: self.open_session(host, name), False)]
        chat = self.chat_index.get(chats.chat_id(host, name))
        if chat is not None and chat.offline:
            # Everything else here writes to the session; shown, not offered.
            items += [(f"Check {host} again", lambda: self.recheck(host), False),
                      (None, None, False),
                      ("Unstar" if chat.starred else "Star", None, False),
                      ("Rename…", None, False),
                      ("Move to project ›", None, False),
                      ("Rename session…", None, False),
                      (f"Kill {host}/{name}…", None, True)]
            self.popup(row, x, y, items)
            return
        if chat is not None:
            items.append(("Unstar" if chat.starred else "Star",
                          lambda: self.toggle_chat_star(chat.id), False))
            items.append(("Rename…", lambda: self.rename_chat(chat.id), False))
            items.append(("Move to project ›", self._move_entries(chat), False))
            items.append(("Save recap to notes",
                          lambda: self.save_recap(chat.id), False))
        items += [
            ("Rename session…", lambda: self.prompt_rename(host, name), False),
            (None, None, False),
            (f"Kill {host}/{name}…",
             lambda: self.confirm_kill(host, name), True),
        ]
        self.popup(row, x, y, items)

    def _move_entries(self, chat) -> list:
        """The "Move to project ›" page: each project from projects.toml,
        then Automatic -- the folder decides, which is @conn_project unset."""
        entries = []
        explicit = [p for p in self.projects if not p.implicit]
        for project in explicit:
            meta = ("" if chat.host in project.servers else "other servers")
            entries.append((self._tile(project, small=True), project.name, meta,
                            chat.project_option == project.id,
                            lambda pid=project.id: self.move_chat(chat.id, pid)))
        automatic = chats.project_for("", chat.host, chat.path,
                                      self.rows.get(chat.host, {}).get("home", ""),
                                      self.projects)
        name = next((p.name for p in self.projects if p.id == automatic),
                    automatic)
        entries.append((self._tile(None, small=True), "Automatic",
                        f"by folder: {name}", not chat.project_option,
                        lambda: self.move_chat(chat.id, None)))
        if not explicit:
            entries.insert(0, (self._tile(None, small=True), "Edit projects…",
                               "none yet", False, lambda: self.open_projects()))
        return entries

    def host_menu(self, row, host: str, x: float, y: float) -> None:
        items = [("Stop watching live" if host in self.starred
                  else "Watch live", lambda: self.toggle_star(host), False)]
        if self.can_move(host, -1):
            items.append(("Move up", lambda: self.move_host(host, -1), False))
        if self.can_move(host, 1):
            items.append(("Move down", lambda: self.move_host(host, 1), False))
        items.append(("Projects…", lambda: self.open_projects(host), False))
        items += [("Check again", lambda: self.recheck(host), False),
                  ("New session...", lambda: self.prompt_new_session(host), False),
                  ("Passwords and keys...", lambda: self.show_secrets(host), False)]
        if hosts.is_local(host):
            items.append((f"Open {hosts.files_label(host)}",
                          lambda: hosts.open_files(str(hosts.files_root(host))),
                          False))
        elif hosts.is_mounted(host):
            items.append((f"Open {hosts.files_label(host)}",
                          lambda: hosts.open_files(str(hosts.files_root(host))),
                          False))
            items.append(("Unmount", lambda: self.do_mount(host, False), False))
        else:
            items.append(("Mount files over sshfs",
                          lambda: self.do_mount(host, True), False))
            items.append(("Install my key (ssh-copy-id)",
                          lambda: self.install_key(host), False))
        if host in hosts.config_hosts():
            items.append(("Edit...", lambda: self.open_manager(host), False))
            items.append((f"Forget {host}...",
                          lambda: self.confirm_forget(host), True))
        self.popup(row, x, y, items)

    def recheck(self, host: str) -> None:
        """Probe one host now.

        The sweep already comes back to a down host every 45 seconds, but
        silently, so a box you have just fixed looks stuck until it happens.
        This is the same probe on demand, and the row says "checking" while
        it runs so the answer is visibly being fetched. The host's stream is
        poked too: one sitting out a long backoff reconnects now rather than
        in minutes, or the row would say up while the screens stay frozen.
        """
        entry = self.streams.get(host)
        if entry is not None:
            entry[2].set()
        if host in self.inflight:
            return
        row = self.rows.get(host)
        if row is not None:
            row["state"] = "unknown"
            row["error"] = ""
        self.notice(f"checking {host}...")
        self.render()
        self.inflight.add(host)
        threading.Thread(target=self._probe, args=(host,),
                         name=f"probe-{host}", daemon=True).start()

    def do_mount(self, host: str, mount: bool) -> None:
        """sshfs on a worker: fusermount blocks for seconds on a busy mount,
        and doing that on the main thread freezes the window."""
        def work():
            try:
                if mount:
                    path = hosts.mount(host)
                    GLib.idle_add(hosts.open_files, path)
                else:
                    hosts.unmount(host)
            except (hosts.HostError, OSError) as exc:
                GLib.idle_add(self.complain, host, str(exc))
                return
            GLib.idle_add(self.render)
        threading.Thread(target=work, daemon=True).start()

    def install_key(self, host: str) -> None:
        try:
            hosts.copy_key(host)
        except hosts.HostError as exc:
            self.complain(host, str(exc))

    def popup(self, row, x: float, y: float, items,
              parent: Gtk.Widget | None = None) -> Gtk.Popover:
        """One popover for both menus, parented to the list rather than to a
        row -- rows are replaced when the session list changes, and a popover
        whose parent goes with them cannot be clicked.

        An item whose handler is a list is a submenu ("Move to project ›"):
        the popover's child is a stack, and the item turns to a second page
        of (lead widget, name, meta, picked, handler) rows with a Back at
        the top. Escape there goes back before it closes anything."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        # Homogeneous, so turning to a submenu never resizes the popover
        # under the pointer: the menu stays where it opened.
        stack = Gtk.Stack()
        stack.set_vhomogeneous(True)
        stack.set_hhomogeneous(True)
        stack.set_transition_type(Gtk.StackTransitionType.NONE)
        stack.add_named(box, "main")
        popover = Gtk.Popover()
        popover.add_css_class("conn-menu")
        popover.set_parent(parent or self.list)
        point = Gdk.Rectangle()
        if row is not None:
            allocation = row.get_allocation()
            point.x = allocation.x + int(x)
            point.y = allocation.y + int(y)
        else:
            point.x, point.y = int(x), int(y)
        point.width = point.height = 1
        popover.set_pointing_to(point)
        popover.connect("closed", lambda pop: pop.unparent())

        for label, handler, destructive in items:
            if label is None:
                box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
                continue
            if isinstance(handler, list):
                button = self._submenu(popover, stack, label, handler)
                box.append(button)
                continue
            button = Gtk.Button(label=label)
            button.set_has_frame(False)
            button.get_child().set_xalign(0)
            if destructive:
                button.add_css_class("destructive")
            if handler is None:
                button.set_sensitive(False)     # shown, but not now
            else:
                button.connect("clicked",
                               lambda _b, h=handler: (popover.popdown(), h()))
            box.append(button)

        def on_key(_controller, keyval, _code, _state):
            if keyval == Gdk.KEY_Escape and stack.get_visible_child_name() != "main":
                stack.set_visible_child_name("main")
                return True
            return False
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", on_key)
        popover.add_controller(keys)
        popover.set_child(stack)
        popover.popup()
        return popover

    def _submenu(self, popover, stack, label: str, entries) -> Gtk.Button:
        """The button that opens a submenu page, and the page itself."""
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        page.set_size_request(SIDEBAR_WIDTH - 64, -1)
        back = Gtk.Button()
        back.set_has_frame(False)
        back_inner = Gtk.Box(spacing=6)
        back_inner.append(Gtk.Image.new_from_icon_name("go-previous-symbolic"))
        title = Gtk.Label(label=label.rstrip(" ›"), xalign=0)
        title.add_css_class("section")
        back_inner.append(title)
        back.set_child(back_inner)
        back.set_tooltip_text("Back")
        back.connect("clicked", lambda _b: stack.set_visible_child_name("main"))
        page.append(back)
        for lead, name, meta, picked, handler in entries:
            page.append(self._menu_row(popover, lead, name, meta, picked,
                                       handler))
        name = f"sub{len(list(stack))}"
        stack.add_named(page, name)

        button = Gtk.Button()
        button.set_has_frame(False)
        # The padding a label-only button gets, so it lines up with the rest.
        button.add_css_class("text-button")
        inner = Gtk.Box(spacing=8)
        text = Gtk.Label(label=label.rstrip(" ›"), xalign=0)
        text.set_hexpand(True)
        inner.append(text)
        inner.append(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        button.set_child(inner)
        button.label = label

        def turn(_b):
            stack.set_visible_child_name(name)
            back.grab_focus()
        button.connect("clicked", turn)
        return button

    # -- what is filed against a host --------------------------------------

    def show_secrets(self, host: str) -> None:
        """Whatever you keep for this host, read out of the desktop keyring.

        Nothing is stored by conn and nothing is held in memory once the
        window closes: a value is fetched when you ask to see it and the
        field is emptied again on Hide. The keyring is already unlocked by
        your login password, which is the single password this all hangs on.
        """
        window = Gtk.Window(title=f"{host} -- passwords and keys",
                            transient_for=self, modal=True)
        window.set_default_size(460, 320)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                        margin_top=14, margin_bottom=14,
                        margin_start=14, margin_end=14)

        # The host in the body, not just the title -- nothing in conn trains
        # you to read a title, and a password filed against the wrong server
        # is the mistake this window exists to prevent. Two labels, like
        # ask(): a host named with an ampersand is a name, not broken Pango.
        top = Gtk.Box(spacing=6)
        lead = Gtk.Label(label="Kept for", xalign=0)
        lead.add_css_class("detail")
        top.append(lead)
        subject = Gtk.Label(label=host, xalign=0)
        subject.add_css_class("heading")
        top.append(subject)
        outer.append(top)

        listing = Gtk.ListBox()
        listing.set_selection_mode(Gtk.SelectionMode.NONE)
        scroller = Gtk.ScrolledWindow()
        scroller.set_child(listing)
        scroller.set_vexpand(True)
        outer.append(scroller)

        add = self._secret_rows(host, listing)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        new = Gtk.Button(label="Add...")
        new.connect("clicked", add)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda _b: window.close())
        buttons.append(new)
        buttons.append(close)
        outer.append(buttons)

        note = Gtk.Label(
            label="Kept in the desktop keyring, which your login password "
                  "unlocks. conn stores nothing itself.", xalign=0)
        note.add_css_class("detail")
        note.set_wrap(True)
        outer.append(note)

        window.set_child(outer)
        window.present()
        return window

    def _secret_rows(self, host: str, listing: Gtk.ListBox):
        """Fill `listing` with a row per secret filed against `host` -- Show /
        Hide, Copy with a 30-second wipe, and Forget -- and return the Add...
        handler that stores a new one and refills.

        The machinery show_secrets and the Site Manager share, so a secret
        reads and behaves the same wherever it is shown. Nothing is stored by
        conn and nothing is held in memory: a value is fetched when you ask to
        see it and the field is emptied again on Hide.
        """
        def refill():
            while (child := listing.get_first_child()) is not None:
                listing.remove(child)
            names = hosts.secret_names(host)
            if not names:
                empty = Gtk.Label(
                    label="Nothing kept for this host yet.", xalign=0)
                empty.add_css_class("detail")
                listing.append(empty)
            for name in names:
                listing.append(entry_row(name))

        def fetch(name: str, landed) -> None:
            # On a worker: secret-tool waits on the keyring, which can put up
            # an unlock prompt of its own, and the main loop must not wait
            # with it. The value comes back to the main loop when it comes.
            def work():
                try:
                    value = hosts.secret_value(host, name)
                except (hosts.HostError, OSError) as exc:
                    GLib.idle_add(self.complain, f"{host}/{name}", str(exc))
                    return
                def deliver():
                    landed(value)
                    return GLib.SOURCE_REMOVE
                GLib.idle_add(deliver)
            threading.Thread(target=work, daemon=True).start()

        def entry_row(name: str) -> Gtk.Widget:
            line = Gtk.Box(spacing=8, margin_top=4, margin_bottom=4)
            title = Gtk.Label(label=name, xalign=0)
            title.set_size_request(110, -1)
            line.append(title)

            shown = Gtk.Label(label="\u2022" * 8, xalign=0, selectable=True)
            shown.add_css_class("secret")
            shown.set_hexpand(True)
            shown.set_ellipsize(Pango.EllipsizeMode.END)
            line.append(shown)

            reveal = Gtk.Button(label="Show")

            def toggle(_button):
                if reveal.get_label() == "Show":
                    def landed(value):
                        shown.set_label(value)
                        reveal.set_label("Hide")
                    fetch(name, landed)
                else:
                    shown.set_label("\u2022" * 8)
                    reveal.set_label("Show")

            reveal.connect("clicked", toggle)
            line.append(reveal)

            copy = Gtk.Button(label="Copy")

            def to_clipboard(_button):
                def landed(value):
                    Session.to_clipboard(value)
                    stamped = Session.clipboard_serial
                    copy.set_label("Copied")
                    # Cleared again shortly: a password left on the clipboard
                    # is readable by anything that asks for it. Only while it
                    # is still the password, though -- a URL or a screen
                    # copied from a session since is not this timer's to
                    # blank, and neither is another application's clipboard.
                    def wipe():
                        clipboard = Gdk.Display.get_default().get_clipboard()
                        if (Session.clipboard_serial == stamped
                                and clipboard.get_content() is not None):
                            Session.to_clipboard("")
                        copy.set_label("Copy")
                        return False
                    GLib.timeout_add_seconds(30, wipe)
                fetch(name, landed)

            copy.connect("clicked", to_clipboard)
            line.append(copy)

            remove = Gtk.Button(label="Forget")
            remove.add_css_class("destructive")

            def forget(_button):
                try:
                    hosts.secret_clear(host, name)
                except hosts.HostError as exc:
                    self.complain(f"{host}/{name}", str(exc))
                    return
                refill()

            remove.connect("clicked", forget)
            line.append(remove)
            return line

        def add(_button):
            def store(values):
                try:
                    hosts.secret_store(host, values["Name"], values["Value"])
                except hosts.HostError as exc:
                    self.complain("Could not store", str(exc))
                    return
                refill()
            self.ask(f"Keep for {host}", [("Name", ""), ("Value", "")], store,
                     secret="Value", lead="Keep for", heading=host)

        refill()
        return add

    # -- the Site Manager --------------------------------------------------

    def open_manager(self, host: str | None = None) -> Gtk.Window:
        """The servers ~/.ssh/config names down one side, everything you can
        change about the selected one down the other.

        The right pane edits the block's own settings, shows the aliases it
        shares its Host line with -- an edit reaches all of them -- and carries
        the same passwords-and-keys machinery show_secrets does. Rename and
        Forget are the whole-server actions; Save writes the fields back.

        Not modal: it edits the very config the sidebar is drawn from, and the
        point is to watch that list change behind it. transient_for so the
        compositor keeps the two together. Called again while open, it
        surfaces the one window rather than a second.
        """
        if self.manager is not None:
            self.manager.present()
            if host:
                self._manager_reload(host)
            return self.manager

        window = Gtk.Window(title="Site Manager", transient_for=self)
        window.set_default_size(780, 560)
        split = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        # The identity in the body -- conn's own window has no title bar, and
        # nothing here trains you to read one -- then the list and the two
        # whole-list actions under it.
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                       margin_top=14, margin_bottom=14,
                       margin_start=14, margin_end=10)
        left.set_size_request(220, -1)
        title = Gtk.Label(label="Site Manager", xalign=0)
        title.add_css_class("heading")
        left.append(title)

        # The default route for every server at once -- the "Host * !via"
        # block -- so switching every server to a different jump host is one pick,
        # not one per server. A server's own Connect still overrides it.
        route_caption = Gtk.Label(label="All servers connect", xalign=0)
        route_caption.add_css_class("detail")
        left.append(route_caption)
        route = Gtk.DropDown.new_from_strings(["Direct"])
        route.set_tooltip_text(
            "The default for every server: straight out, or through one of "
            "them -- the machine whose IP the servers whitelist. A server's "
            "own Connect setting overrides this.")
        left.append(route)
        self.manager_route = route
        self.manager_routes = [""]

        picker = Gtk.ListBox()
        picker.set_selection_mode(Gtk.SelectionMode.SINGLE)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(picker)
        scroller.set_vexpand(True)
        left.append(scroller)

        # Reorder the servers here too -- the manager is where you keep them.
        moves = Gtk.Box(spacing=6)
        up_btn = Gtk.Button(label="Move up")
        up_btn.set_hexpand(True)
        up_btn.set_sensitive(False)
        down_btn = Gtk.Button(label="Move down")
        down_btn.set_hexpand(True)
        down_btn.set_sensitive(False)
        moves.append(up_btn)
        moves.append(down_btn)
        left.append(moves)

        add_btn = Gtk.Button(label="Add server...")
        left.append(add_btn)
        forget_btn = Gtk.Button(label="Forget...")
        forget_btn.add_css_class("destructive")
        forget_btn.set_visible(False)      # only with a host selected to forget
        left.append(forget_btn)

        # A way out that is not the compositor's -- conn's own windows have no
        # title bar to close from, and Escape does the same.
        close_btn = Gtk.Button(label="Close")
        close_btn.set_margin_top(6)
        left.append(close_btn)

        # A body the selection rebuilds, over an inline note that outlives the
        # rebuild so a save or a rename can report into it.
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                        margin_top=14, margin_bottom=14,
                        margin_start=14, margin_end=14)
        right.set_hexpand(True)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.set_vexpand(True)
        status = Gtk.Label(label="", xalign=0)
        status.add_css_class("detail")
        status.set_wrap(True)
        right.append(body)
        right.append(status)

        split.append(left)
        split.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        split.append(right)
        window.set_child(split)

        def set_status(text, ok=True):
            status.set_text(text)
            status.remove_css_class("destructive")
            if not ok:
                status.add_css_class("destructive")

        def fill_route():
            # Rebuilt with the list, so an added or renamed server is offered.
            # The guard keeps the rebuild's own set_selected from reading as a
            # pick and rewriting the config.
            current = hosts.default_jump()
            routes = [""] + hosts.config_hosts()
            if current and current not in routes:
                routes.append(current)       # hand-written: shown, not lost
            self.manager_routes = routes
            self.manager_route_quiet = True
            route.set_model(Gtk.StringList.new(
                ["Direct"] + [f"through {h}" for h in routes[1:]]))
            route.set_selected(routes.index(current))
            self.manager_route_quiet = False

        def load_list(select=None):
            fill_route()
            while (child := picker.get_first_child()) is not None:
                picker.remove(child)
            chosen = None
            for name in self._manager_hosts():
                row = Gtk.ListBoxRow()
                row.alias = name
                label = Gtk.Label(label=name, xalign=0)
                label.set_ellipsize(Pango.EllipsizeMode.END)
                label.set_width_chars(NAME_FLOOR)
                row.set_child(label)
                picker.append(row)
                if name == select:
                    chosen = row
            if chosen is not None:
                picker.select_row(chosen)    # fires row-selected -> show_host
            else:
                picker.unselect_all()
                show_host(None)

        def fill_placeholders(gen, entries, effective):
            # A late resolve() answer whose host has since been switched away
            # from is dropped -- the entries it would fill are gone.
            if gen != self.manager_gen or self.manager is None:
                return GLib.SOURCE_REMOVE
            for key, entry in entries.items():
                if not entry.get_text():
                    entry.set_placeholder_text(effective.get(key, ""))
            # Spell out what Default means for this server -- the defaults'
            # jump, or none. Only while the block sets no jump of its own: then
            # ssh -G's answer is the default's. The splice can move the
            # selection, so it is put back.
            jump = self.manager_jump
            if jump is not None and self.manager_jumps[jump.get_selected()] == "":
                via = effective.get("proxyjump", "")
                label = (f"Default (through {via})" if via and via != "none"
                         else "Default (direct)")
                jump.get_model().splice(0, 1, [label])
                jump.set_selected(0)
            return GLib.SOURCE_REMOVE

        def show_host(name):
            set_status("")
            self.manager_jump = None
            while (child := body.get_first_child()) is not None:
                body.remove(child)
            forget_btn.set_visible(bool(name))
            order = self._manager_hosts()
            at = order.index(name) if name in order else -1
            up_btn.set_sensitive(at > 0)
            down_btn.set_sensitive(0 <= at < len(order) - 1)
            if not name:
                hint = Gtk.Label(
                    label="Pick a server on the left, or add one.", xalign=0)
                hint.add_css_class("detail")
                body.append(hint)
                return
            try:
                cfg = hosts.host_config(name)
            except hosts.HostError as exc:
                set_status(str(exc), ok=False)
                return
            try:
                shared = hosts.host_line_aliases(name)
            except hosts.HostError:
                shared = []          # gone between the two reads: treat as alone

            self.manager_host = name

            top = Gtk.Box(spacing=6)
            lead = Gtk.Label(label="Editing", xalign=0)
            lead.add_css_class("detail")
            top.append(lead)
            subject = Gtk.Label(label=name, xalign=0)   # two labels: an
            subject.add_css_class("heading")            # ampersand alias is a
            top.append(subject)                         # name, not broken Pango
            body.append(top)

            if shared:
                note = Gtk.Label(
                    label="shares settings with: " + ", ".join(shared)
                          + " -- changes apply to all", xalign=0)
                note.add_css_class("detail")
                note.set_wrap(True)
                body.append(note)

            # Watch live, as a tickbox (it was the host star, and still is in
            # the state file): ticked keeps a live stream open to the server,
            # unticked checks it on the slow timer. It only acts on a real
            # change, so setting it to match the current state below does not
            # toggle anything.
            show = Gtk.CheckButton(label="Watch live")
            show.set_active(name in self.starred)
            show.set_tooltip_text(
                "Ticked: a live stream to this server, so its chats update "
                "within a second or two. Unticked: checked every "
                f"{int(SLOW_REFRESH // 60)} minutes instead -- its chats are "
                "still listed and still notify you, just later. Either way "
                "it stays in the Servers tab.")

            def toggled(button, host=name):
                if button.get_active() != (host in self.starred):
                    self.toggle_star(host)
            show.connect("toggled", toggled)
            self.manager_show = show
            body.append(show)

            entries: dict[str, Gtk.Entry] = {}
            for label, key in (("Hostname", "hostname"), ("User", "user"),
                               ("Port", "port"),
                               ("IdentityFile", "identityfile")):
                line = Gtk.Box(spacing=8)
                caption = Gtk.Label(label=label, xalign=0)
                caption.set_size_request(96, -1)
                entry = Gtk.Entry()
                entry.set_hexpand(True)
                entry.set_text(cfg[key])
                entry.connect("activate", self._manager_save)
                entries[key] = entry
                line.append(caption)
                line.append(entry)
                body.append(line)
            self.manager_fields = entries

            # Connect through another server -- ProxyJump -- for one that only
            # lets in a whitelisted IP: a machine that sits on that IP and
            # answers from anywhere can be the hop. "Default" writes nothing, so the block
            # takes whatever a wildcard block further down says (a "Host *
            # !mini" with ProxyJump mini sends everything via the mini);
            # "Direct" writes ProxyJump none, which overrides that default.
            # A list of this file's servers rather than a text field, since a
            # hop is almost always one of them; a value written by hand that is
            # not (a user@host:port chain) is kept as its own row, so opening
            # and saving never loses it.
            jumps = ["", "none"] + [h for h in hosts.config_hosts()
                                    if h != name]
            if cfg["proxyjump"] and cfg["proxyjump"] not in jumps:
                jumps.append(cfg["proxyjump"])
            jump = Gtk.DropDown.new_from_strings(
                ["Default", "Direct"] + [f"through {h}" for h in jumps[2:]])
            jump.set_hexpand(True)
            jump.set_selected(jumps.index(cfg["proxyjump"]))
            jump.set_tooltip_text(
                "For a server that only lets in a whitelisted IP: connect to "
                "the server picked here first, and on from there. Default is "
                "whatever the Host * defaults say; Direct overrides them.")
            line = Gtk.Box(spacing=8)
            caption = Gtk.Label(label="Connect", xalign=0)
            caption.set_size_request(96, -1)
            line.append(caption)
            line.append(jump)
            body.append(line)
            self.manager_jump = jump
            self.manager_jumps = jumps

            # The effective config as the placeholder of an empty field, so a
            # blank Hostname still shows what ssh falls back to. On a worker:
            # resolve() shells out to ssh -G. A generation stamp drops an
            # answer a host switch has already moved past.
            self.manager_gen += 1
            gen = self.manager_gen

            def placeholders():
                effective = hosts.resolve(name)
                GLib.idle_add(fill_placeholders, gen, entries, effective)
            threading.Thread(target=placeholders,
                             name=f"resolve-{name}", daemon=True).start()

            def refill_fields():
                try:
                    fresh = hosts.host_config(self.manager_host)
                except hosts.HostError:
                    return
                for field, entry in self.manager_fields.items():
                    entry.set_text(fresh[field])
                if (self.manager_jump is not None
                        and fresh["proxyjump"] in self.manager_jumps):
                    self.manager_jump.set_selected(
                        self.manager_jumps.index(fresh["proxyjump"]))
            self.manager_refill = refill_fields

            actions = Gtk.Box(spacing=8, margin_top=4)
            save = Gtk.Button(label="Save")
            save.add_css_class("suggested-action")
            save.connect("clicked", self._manager_save)
            rename = Gtk.Button(label="Rename...")
            rename.connect("clicked", lambda _b: self._manager_rename(name))
            actions.append(save)
            actions.append(rename)
            body.append(actions)

            rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            rule.set_margin_top(6)
            rule.set_margin_bottom(6)
            body.append(rule)

            heading = Gtk.Label(label="Passwords and keys", xalign=0)
            heading.add_css_class("host")
            body.append(heading)

            secret_list = Gtk.ListBox()
            secret_list.set_selection_mode(Gtk.SelectionMode.NONE)
            secret_scroll = Gtk.ScrolledWindow()
            secret_scroll.set_child(secret_list)
            secret_scroll.set_vexpand(True)
            secret_scroll.set_min_content_height(80)
            body.append(secret_scroll)
            add_secret = self._secret_rows(name, secret_list)
            new_secret = Gtk.Button(label="Add...", halign=Gtk.Align.START)
            new_secret.connect("clicked", add_secret)
            body.append(new_secret)

        def add_server(_btn):
            def create(values):
                try:
                    backup = hosts.add_host(
                        values["Name"], values["Hostname"],
                        values["User"], values["Port"] or "22")
                except (hosts.HostError, OSError) as exc:
                    set_status(str(exc), ok=False)
                    return
                self.load_hosts()
                load_list(select=values["Name"])
                set_status(f"added {values['Name']} -- "
                           f"old config kept at {backup.name}")
            self.ask("Add a server", [("Name", ""), ("Hostname", ""),
                                      ("User", ""), ("Port", "22")], create)

        route.connect("notify::selected",
                      lambda *_a: None if self.manager_route_quiet
                      else self._manager_route(
                          self.manager_routes[route.get_selected()]))
        picker.connect(
            "row-selected",
            lambda _l, r: show_host(getattr(r, "alias", None) if r else None))
        add_btn.connect("clicked", add_server)
        forget_btn.connect("clicked",
                           lambda _b: self.confirm_forget(self.manager_host))
        up_btn.connect("clicked", lambda _b: self.manager_move(-1))
        down_btn.connect("clicked", lambda _b: self.manager_move(1))
        close_btn.connect("clicked", lambda _b: window.close())

        # Escape closes it, the way a dialog does -- and there is no title bar
        # with an x on it, which is what left Super+W as the only way out.
        def on_key(_controller, keyval, _code, _state):
            if keyval == Gdk.KEY_Escape:
                window.close()
                return True
            return False
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", on_key)
        window.add_controller(keys)

        def closed(_w):
            self.manager = None
            self.manager_reload = None
            self.manager_note = None
            self.manager_refill = None
            self.manager_show = None
            self.manager_jump = None
            self.manager_route = None
            return False
        window.connect("close-request", closed)

        self.manager = window
        self.manager_reload = load_list
        self.manager_note = set_status
        load_list(select=host if host and host in hosts.config_hosts()
                  else None)
        window.present()
        return window

    def open_projects(self, host: str | None = None) -> Gtk.Window:
        """Projects, and which servers and folders belong to each (decision
        C) -- the editor projects.toml had only a text editor for.

        A solid window in the Site Manager's shape: the projects down the
        left, the selected one's name, letter, colour and servers down the
        right. It edits a copy of the file's own tables, so whatever it does
        not show -- agent, mode, keys a later version adds -- is written back
        as it was. Save writes the file whole and atomically, keeps the old
        one as projects.toml.bak, and reloads the sidebar's projects. Ids
        are fixed once saved: chats' @conn_project point at them.

        From the host menu it opens on the first project with that host, or
        on a new one with the host already ticked.
        """
        if self.projects_window is not None:
            self.projects_window.present()
            if host:
                self.projects_window.focus_host(host)
            return self.projects_window

        window = Gtk.Window(title="Projects", transient_for=self)
        window.set_default_size(760, 540)
        window.add_css_class("projects")
        state = {"data": {}, "saved": {}, "current": None, "new": set(),
                 "error": "", "quiet": False}
        tile_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), tile_css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)

        split = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                       margin_top=14, margin_bottom=14,
                       margin_start=14, margin_end=10)
        left.set_size_request(230, -1)
        title = Gtk.Label(label="Projects", xalign=0)
        title.add_css_class("heading")
        left.append(title)
        picker = Gtk.ListBox()
        picker.set_selection_mode(Gtk.SelectionMode.SINGLE)
        picker.add_css_class("sidebar")
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(picker)
        scroller.set_vexpand(True)
        left.append(scroller)
        add_btn = Gtk.Button(label="Add project…")
        left.append(add_btn)
        delete_btn = Gtk.Button(label="Delete…")
        delete_btn.add_css_class("destructive")
        left.append(delete_btn)
        close_btn = Gtk.Button(label="Close")
        close_btn.set_margin_top(6)
        left.append(close_btn)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                        margin_top=14, margin_bottom=14,
                        margin_start=14, margin_end=14)
        right.set_hexpand(True)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.set_vexpand(True)
        status = Gtk.Label(label="", xalign=0)
        status.add_css_class("detail")
        status.set_wrap(True)
        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        open_file = Gtk.Button(label="Open projects.toml")
        open_file.set_visible(False)
        open_file.connect("clicked", lambda _b: self.edit_projects())
        revert_btn = Gtk.Button(label="Revert")
        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        actions.append(open_file)
        actions.append(revert_btn)
        actions.append(save_btn)
        right.append(body)
        right.append(status)
        right.append(actions)
        split.append(left)
        split.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        split.append(right)
        window.set_child(split)

        def set_status(text, ok=True):
            status.set_text(text)
            status.remove_css_class("destructive")
            if not ok:
                status.add_css_class("destructive")

        def tables():
            return [(pid, t) for pid, t in state["data"].items()
                    if isinstance(t, dict)]

        def as_project(pid, table):
            name = str(table.get("name") or pid)
            color = str(table.get("color") or "")
            bg, fg = chats._tile(pid, name)
            if color and not chats.COLOUR.match(color):
                color = ""
            return chats.Project(
                id=pid, name=name,
                letter=(str(table.get("letter") or name[:1] or "?"))[:1].upper(),
                color=color or fg, bg="" if color else bg,
                servers=dict(table.get("servers") or {}))

        def paint_tiles():
            rules = []
            for index, (pid, table) in enumerate(tables()):
                background, letter = tile_colours(self.palette,
                                                  as_project(pid, table), index)
                rules.append(f".tile.ptile-{index} {{ background: {background}; "
                             f"color: {letter}; }}")
            tile_css.load_from_data("\n".join(rules).encode())

        def tile_for(pid, table, small=True):
            index = [p for p, _t in tables()].index(pid)
            tile = Gtk.Label(label=as_project(pid, table).letter)
            tile.add_css_class("tile")
            tile.add_css_class("tile-small" if small else "tile-big")
            tile.add_css_class(f"ptile-{index}")
            size = 22 if small else 32
            tile.set_size_request(size, size)
            tile.set_valign(Gtk.Align.CENTER)
            tile.set_halign(Gtk.Align.START)
            return tile

        def dirty():
            return state["data"] != state["saved"] or bool(state["new"])

        def load_list(select=None):
            state["quiet"] = True
            paint_tiles()
            while (child := picker.get_first_child()) is not None:
                picker.remove(child)
            chosen = None
            for pid, table in tables():
                row = Gtk.ListBoxRow()
                row.pid = pid
                row.add_css_class("session")    # the sidebar's card, not accent
                line = Gtk.Box(spacing=10, margin_top=4, margin_bottom=4,
                               margin_start=4, margin_end=4)
                line.append(tile_for(pid, table))
                name = Gtk.Label(label=str(table.get("name") or pid), xalign=0)
                name.set_hexpand(True)
                name.set_ellipsize(Pango.EllipsizeMode.END)
                line.append(name)
                count = len(table.get("servers") or {})
                meta = Gtk.Label(label=f"{count} server{'s' if count != 1 else ''}")
                meta.add_css_class("meta")
                line.append(meta)
                row.set_child(line)
                picker.append(row)
                if pid == select:
                    chosen = row
            listed = {h for _p, t in tables() for h in (t.get("servers") or {})}
            loose = [h for h in self.order if h not in listed]
            if loose:
                head = Gtk.ListBoxRow()
                head.set_selectable(False)
                head.set_activatable(False)
                caption = Gtk.Label(label="NOT IN A PROJECT", xalign=0,
                                    margin_top=12, margin_start=4)
                caption.add_css_class("section")
                head.set_child(caption)
                picker.append(head)
                for loose_host in loose:
                    row = Gtk.ListBoxRow()
                    row.loose = loose_host
                    label = Gtk.Label(label=loose_host, xalign=0,
                                      margin_start=4, margin_top=2,
                                      margin_bottom=2)
                    label.add_css_class("mono")
                    label.add_css_class("detail")
                    label.set_ellipsize(Pango.EllipsizeMode.END)
                    row.set_child(label)
                    row.set_tooltip_text(f"Start a project with {loose_host}")
                    picker.append(row)
            state["quiet"] = False
            if chosen is not None:
                picker.select_row(chosen)
            elif select is None:
                show_project(None)

        def field(label_text, widget, grid, row):
            label = Gtk.Label(label=label_text, xalign=0)
            label.add_css_class("detail")
            grid.attach(label, 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)

        def show_project(pid):
            state["current"] = pid
            delete_btn.set_visible(pid is not None)
            while (child := body.get_first_child()) is not None:
                body.remove(child)
            if pid is None:
                empty = Gtk.Label(
                    label="A project is a name and the servers it lives on, "
                          "each with the folder its chats start in. Add one, "
                          "or pick a server on the left that is in none.",
                    xalign=0)
                empty.set_wrap(True)
                empty.add_css_class("detail")
                body.append(empty)
                return
            table = state["data"][pid]
            head = Gtk.Box(spacing=12)
            preview = tile_for(pid, table, small=False)
            head.append(preview)
            heading = Gtk.Label(label=str(table.get("name") or pid), xalign=0)
            heading.add_css_class("heading")
            head.append(heading)
            body.append(head)

            grid = Gtk.Grid(row_spacing=8, column_spacing=12)
            name = Gtk.Entry(text=str(table.get("name", "")))
            name.set_hexpand(True)
            field("Name", name, grid, 0)
            letter = Gtk.Entry(text=str(table.get("letter", "")))
            letter.set_max_length(1)
            letter.set_width_chars(2)
            letter.set_max_width_chars(2)
            letter.set_halign(Gtk.Align.START)
            letter.set_placeholder_text((str(table.get("name") or pid)[:1]
                                         or "?").upper())
            field("Letter", letter, grid, 1)
            colour = Gtk.Entry(text=str(table.get("color", "")))
            colour.set_placeholder_text("automatic")
            colour.set_width_chars(10)
            colour.set_max_width_chars(10)
            colour.set_halign(Gtk.Align.START)
            colour.set_tooltip_text("#RRGGBB for the letter; the tile is "
                                    "mixed from it. Empty picks one.")
            field("Colour", colour, grid, 2)
            if pid in state["new"]:
                ident = Gtk.Entry(text=pid)
                ident.add_css_class("mono")
                ident.set_tooltip_text("Lowercase letters, digits and hyphens. "
                                       "Fixed once saved: chats point at it.")
                ident.edited = False
            else:
                ident = Gtk.Label(label=pid, xalign=0)
                ident.add_css_class("mono")
                ident.add_css_class("detail")
                ident.set_tooltip_text("The id chats' @conn_project points at, "
                                       "so it stays as it is")
            field("Id", ident, grid, 3)
            body.append(grid)

            def rename_id(new_id):
                if new_id == state["current"] or not new_id:
                    return
                if new_id in state["data"]:
                    set_status(f"there is already a project “{new_id}”", False)
                    return
                old = state["current"]
                # Re-keyed in place, order kept, and the list row relabelled
                # rather than rebuilt -- a rebuild would take the entry out
                # from under the typing.
                state["data"] = {(new_id if k == old else k): v
                                 for k, v in state["data"].items()}
                state["new"].discard(old)
                state["new"].add(new_id)
                state["current"] = new_id
                for row in rows_of(picker):
                    if getattr(row, "pid", None) == old:
                        row.pid = new_id
                if status.has_css_class("destructive"):
                    set_status("")

            def name_changed(entry):
                text = entry.get_text()
                state["data"][state["current"]]["name"] = text
                heading.set_text(text or state["current"])
                letter.set_placeholder_text((text[:1] or "?").upper())
                if isinstance(ident, Gtk.Entry) and not ident.edited:
                    ident.quiet = True
                    ident.set_text(chats.slug(text))
                    ident.quiet = False
                refresh_tiles()

            def letter_changed(entry):
                text = entry.get_text().strip()
                table = state["data"][state["current"]]
                if text:
                    table["letter"] = text
                else:
                    table.pop("letter", None)
                refresh_tiles()

            def colour_changed(entry):
                text = entry.get_text().strip()
                table = state["data"][state["current"]]
                if text:
                    table["color"] = text
                else:
                    table.pop("color", None)
                refresh_tiles()

            def refresh_tiles():
                paint_tiles()
                preview.set_label(as_project(state["current"],
                                             state["data"][state["current"]])
                                  .letter)
                for row in rows_of(picker):
                    if getattr(row, "pid", None) == state["current"]:
                        line = row.get_child()
                        line.get_first_child().set_label(preview.get_label())
                        line.get_first_child().get_next_sibling().set_text(
                            str(state["data"][state["current"]].get("name")
                                or state["current"]))

            name.connect("changed", name_changed)
            letter.connect("changed", letter_changed)
            colour.connect("changed", colour_changed)
            if isinstance(ident, Gtk.Entry):
                ident.quiet = False

                def ident_changed(entry):
                    if entry.quiet:
                        rename_id(entry.get_text().strip())
                        return
                    entry.edited = True
                    rename_id(entry.get_text().strip())
                ident.connect("changed", ident_changed)

            servers_head = Gtk.Label(label="SERVERS", xalign=0, margin_top=6)
            servers_head.add_css_class("section")
            body.append(servers_head)
            listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            hosts_scroll = Gtk.ScrolledWindow()
            hosts_scroll.set_policy(Gtk.PolicyType.NEVER,
                                    Gtk.PolicyType.AUTOMATIC)
            hosts_scroll.set_vexpand(True)
            hosts_scroll.set_child(listing)
            body.append(hosts_scroll)
            servers = table.setdefault("servers", {})
            known = list(self.order) + [h for h in servers if h not in self.order]
            window.server_rows = {}
            for each in known:
                line = Gtk.Box(spacing=8)
                tick = Gtk.CheckButton(label=each)
                tick.add_css_class("mono")
                tick.set_active(each in servers)
                tick.set_size_request(190, -1)
                line.append(tick)
                folder = Gtk.Entry(text=str(servers.get(each, "~")))
                folder.set_hexpand(True)
                folder.add_css_class("mono")
                folder.set_sensitive(each in servers)
                folder.set_tooltip_text("The folder chats for this project "
                                        "start in on this server")
                line.append(folder)
                ideas = chats.folder_suggestions(
                    self.rows.get(each, {}).get("sessions", []),
                    self.rows.get(each, {}).get("home", ""))
                pick = Gtk.MenuButton(icon_name="pan-down-symbolic")
                pick.set_has_frame(False)
                pick.set_tooltip_text("Folders chats on this server run in")
                pick.set_sensitive(each in servers)
                pop = Gtk.Popover()
                pop.add_css_class("conn-menu")
                options = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                for idea in ideas:
                    choice = Gtk.Button(label=idea)
                    choice.set_has_frame(False)
                    choice.get_child().set_xalign(0)
                    choice.add_css_class("mono")
                    choice.connect("clicked", lambda _b, f=folder, i=idea, p=pop:
                                   (f.set_text(i), p.popdown()))
                    options.append(choice)
                pop.set_child(options)
                pick.set_popover(pop)
                line.append(pick)
                listing.append(line)
                window.server_rows[each] = (tick, folder)

                def toggled(button, h=each, f=folder, m=pick, i=ideas):
                    table = state["data"][state["current"]]
                    servers = table.setdefault("servers", {})
                    on = button.get_active()
                    f.set_sensitive(on)
                    m.set_sensitive(on)
                    if on:
                        if f.get_text().strip() in ("", "~") and i:
                            f.set_text(i[0])
                        servers[h] = f.get_text().strip() or "~"
                    else:
                        servers.pop(h, None)
                    refresh_count()

                def folder_changed(entry, h=each, t=tick):
                    if t.get_active():
                        state["data"][state["current"]].setdefault(
                            "servers", {})[h] = entry.get_text().strip()
                tick.connect("toggled", toggled)
                folder.connect("changed", folder_changed)
            window.fields = {"name": name, "letter": letter, "colour": colour,
                             "id": ident}

            def refresh_count():
                for row in rows_of(picker):
                    if getattr(row, "pid", None) == state["current"]:
                        meta = row.get_child().get_last_child()
                        n = len(state["data"][state["current"]]
                                .get("servers") or {})
                        meta.set_text(f"{n} server{'s' if n != 1 else ''}")

        def rows_of(box):
            out, index = [], 0
            while (row := box.get_row_at_index(index)) is not None:
                out.append(row)
                index += 1
            return out

        def selected(_list, row):
            if state["quiet"] or row is None:
                return
            if getattr(row, "loose", None):
                new_project(row.loose)
                return
            show_project(getattr(row, "pid", None))

        def new_project(with_host=None):
            base = chats.slug(with_host) if with_host else "new-project"
            pid, n = base, 2
            while pid in state["data"]:
                pid, n = f"{base}-{n}", n + 1
            table = {"name": with_host or "", "servers": {}}
            if with_host:
                ideas = chats.folder_suggestions(
                    self.rows.get(with_host, {}).get("sessions", []),
                    self.rows.get(with_host, {}).get("home", ""))
                table["servers"][with_host] = ideas[0] if ideas else "~"
            state["data"][pid] = table
            state["new"].add(pid)
            load_list(select=pid)
            if "name" in getattr(window, "fields", {}):
                window.fields["name"].grab_focus()
            set_status("new project -- name it, tick its servers, then Save")
            return pid

        def delete_project(_b=None):
            pid = state["current"]
            if pid is None:
                return
            dialog = Gtk.AlertDialog()
            name = str(state["data"][pid].get("name") or pid)
            dialog.set_message(f"Delete {name}?")
            dialog.set_detail("Chats filed under it by @conn_project go back "
                              "to matching by folder. Nothing on any server "
                              "changes. It is gone from the file when you "
                              "Save.")
            dialog.set_buttons(["Cancel", "Delete project"])
            dialog.set_cancel_button(0)
            dialog.set_default_button(0)

            def answered(source, result):
                try:
                    choice = source.choose_finish(result)
                except GLib.Error:
                    return
                if choice == 1:
                    remove_project(pid)
            dialog.choose(window, None, answered)

        def remove_project(pid):
            del state["data"][pid]
            state["new"].discard(pid)
            load_list(select=None)
            set_status(f"{pid} deleted -- Save to write it")

        def revert(_b=None):
            reload()
            set_status("reverted to the file")

        def save(_b=None):
            if state["error"]:
                set_status(f"projects.toml does not parse, so it is not saved "
                           f"over -- {state['error']}", False)
                return False
            errors = chats.project_errors(state["data"])
            if errors:
                set_status(errors[0] + (f" (and {len(errors) - 1} more)"
                                        if len(errors) > 1 else ""), False)
                return False
            try:
                backup = chats.write_projects(chats.PROJECTS_FILE,
                                              state["data"])
            except (OSError, ValueError) as exc:
                set_status(f"not saved -- {exc}", False)
                return False
            keep = state["current"]
            reload(select=keep)
            self.load_hosts(connect=False)
            set_status("saved" + (f" -- old file kept as {backup.name} "
                                  "(comments are not kept)" if backup else ""))
            return True

        def reload(select=None):
            try:
                data = chats.read_project_tables(chats.PROJECTS_FILE)
                state["error"] = ""
            except (OSError, ValueError) as exc:
                data = {}
                state["error"] = str(exc)
            import copy
            state["data"] = copy.deepcopy(data)
            state["saved"] = copy.deepcopy(data)
            state["new"] = set()
            save_btn.set_sensitive(not state["error"])
            open_file.set_visible(bool(state["error"]))
            if state["error"]:
                set_status(f"projects.toml does not parse: {state['error']}",
                           False)
            first = select if select in state["data"] else next(
                (p for p, _t in tables()), None)
            load_list(select=first)

        def focus_host(h):
            owner = next((p for p, t in tables()
                          if h in (t.get("servers") or {})), None)
            if owner is not None:
                load_list(select=owner)
            else:
                new_project(h)

        picker.connect("row-selected", selected)
        add_btn.connect("clicked", lambda _b: new_project())
        delete_btn.connect("clicked", delete_project)
        revert_btn.connect("clicked", revert)
        save_btn.connect("clicked", save)
        close_btn.connect("clicked", lambda _b: window.close())

        def on_key(_controller, keyval, _code, modifiers):
            if keyval == Gdk.KEY_Escape:
                window.close()
                return True
            if (modifiers & Gdk.ModifierType.CONTROL_MASK
                    and Gdk.keyval_to_lower(keyval) == Gdk.KEY_s):
                save()
                return True
            return False
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", on_key)
        window.add_controller(keys)

        def closed(_w):
            self.projects_window = None
            Gtk.StyleContext.remove_provider_for_display(
                Gdk.Display.get_default(), tile_css)
            return False
        window.connect("close-request", closed)

        window.state = state
        window.status = status
        window.save = save
        window.revert = revert
        window.new_project = new_project
        window.remove_project = remove_project
        window.select_project = lambda pid: load_list(select=pid)
        window.focus_host = focus_host
        window.dirty = dirty
        window.fields = {}
        self.projects_window = window
        reload()
        if host:
            focus_host(host)
        window.present()
        return window

    def _manager_hosts(self) -> list[str]:
        """The servers the manager lists: every one the config names, in the
        arranged order the sidebar uses, with the invented "local" left out --
        it has no Host block to edit."""
        named = hosts.config_hosts()
        known = set(named)
        return ([h for h in self.order if h in known]
                + [h for h in named if h not in self.order])

    def manager_move(self, delta: int) -> None:
        """Move the selected server up (-1) or down (+1) the manager's list.

        A flat swap across the whole arrangement -- the manager is one list of
        every server, not the sidebar's starred-then-folded split -- written to
        the same order the sidebar reads, so a move here shows up there too.
        """
        host = self.manager_host
        order = self._manager_hosts()
        if self.manager is None or host not in order:
            return
        here = order.index(host)
        there = here + delta
        if not 0 <= there < len(order):
            if self.manager_note is not None:
                self.manager_note(f"{host} is already at the "
                                  f"{'top' if delta < 0 else 'bottom'}", False)
            return
        other = order[there]
        if host not in self.order or other not in self.order:
            return       # order not yet synced with the config -- nothing to swap
        a, b = self.order.index(host), self.order.index(other)
        self.order[a], self.order[b] = self.order[b], self.order[a]
        save_order(self.order)
        self.shape = []
        self.render()
        self._manager_reload(host)

    def _manager_reload(self, select: str | None = None) -> None:
        if self.manager_reload is not None:
            self.manager_reload(select)

    def _manager_note(self, text: str, ok: bool = True) -> bool:
        if self.manager_note is not None:
            self.manager_note(text, ok)
        return GLib.SOURCE_REMOVE

    def _manager_save(self, _widget=None) -> None:
        """Write the fields and the jump back to the selected host's block.

        On a worker for consistency with everything else that edits the config
        -- these are quick file writes, but one path serves them all.
        update_host refuses an unsafe value before it writes, so a HostError
        reports inline and nothing changed. The fields are re-read from the
        file afterwards, so a normalised value -- a dropped Port 22, say --
        shows as it was actually written.
        """
        host = self.manager_host
        fields = {key: entry.get_text().strip()
                  for key, entry in self.manager_fields.items()}
        if self.manager_jump is not None:
            fields["proxyjump"] = self.manager_jumps[
                self.manager_jump.get_selected()]

        def work():
            try:
                before = hosts.host_config(host)["proxyjump"]
                backup = hosts.update_host(host, fields)
            except (hosts.HostError, OSError) as exc:
                GLib.idle_add(self._manager_note, str(exc), False)
                return
            if fields.get("proxyjump", before) != before:
                hosts.drop_master(host)   # the next ssh takes the new route
            GLib.idle_add(self._manager_saved, backup)
        threading.Thread(target=work, name=f"save-{host}", daemon=True).start()

    def _manager_route(self, via: str) -> None:
        """Point every server's default route at `via` ("" is direct).

        Every shared connection is closed too -- each was opened on the old
        route and would carry the next ssh along it for up to ControlPersist --
        and then every host is probed again, so the sidebar shows the new
        route working (or not) now rather than at the next sweep.
        """
        def work():
            try:
                backup = hosts.set_default_jump(via)
            except (hosts.HostError, OSError) as exc:
                GLib.idle_add(self._manager_note, str(exc), False)
                return
            for host in hosts.config_hosts():
                hosts.drop_master(host)
            GLib.idle_add(self._manager_routed, via, backup)
        threading.Thread(target=work, name="route", daemon=True).start()

    def _manager_routed(self, via: str, backup) -> bool:
        if self.manager_reload is not None:
            self.manager_reload(self.manager_host or None)
        self.load_hosts(force=True)
        where = f"through {via}" if via else "direct"
        self._manager_note(f"every server now connects {where} unless it "
                           f"says otherwise -- old config kept at "
                           f"{backup.name}")
        return GLib.SOURCE_REMOVE

    def _manager_saved(self, backup) -> bool:
        if self.manager_refill is not None:
            self.manager_refill()
        self._manager_note(f"saved -- old config kept at {backup.name}")
        return GLib.SOURCE_REMOVE

    def _manager_rename(self, old: str) -> None:
        self.ask(f"Rename {old}", [("New name", old)],
                 lambda values: self.migrate_host(old, values["New name"]),
                 lead="Rename", heading=old)

    def migrate_host(self, old: str, new: str) -> None:
        """The whole of a rename, in an order an interruption survives.

        rename_host moves only the Host line; the keyring secrets, the stars
        and the hand-arranged order are all filed under the old alias and are
        this method's to carry over. The config edit lands first -- everything
        after keys off the new name being in the config -- so a HostError there
        aborts with nothing else touched. The keyring goes last and on a
        worker: secret-tool can block on an unlock prompt, and every other
        keyring path here is off the main thread for exactly that reason.
        """
        old = old.strip()
        new = new.strip()
        try:
            backup = hosts.rename_host(old, new)             # 1. the config
        except (hosts.HostError, OSError) as exc:
            self._manager_note(str(exc), False)
            return

        self.stop_stream(old)                                # 2. live views
        for key in [k for k in self.open if k[0] == old]:
            self.close_session(self.open[key])
        self.streams.pop(old, None)
        self.rows.pop(old, None)
        self.inflight.discard(old)
        self.probed.pop(old, None)
        self.streamed.pop(old, None)

        self.auto_live.discard(old)     # the next render follows its chats
        if old in self.starred:                              # 3. the state files
            self.starred.discard(old)
            self.starred.add(new)
            save_stars(self.starred)
        if old in self.expanded:
            self.expanded.discard(old)
            self.expanded.add(new)
            save_expanded(self.expanded)
        if old in self.order:
            self.order[self.order.index(old)] = new
            save_order(self.order)
        if old in self.known:
            self.known[new] = self.known.pop(old)
            self.save_known()

        self.load_hosts(connect=True)                        # 4. rebuild + probe
        self.recheck(new)

        self._manager_reload(new)                            # 5. the manager
        self._manager_note(f"renamed {old} to {new}")
        self.notice(f"renamed {old} to {new} -- old config kept at {backup.name}")

        # 6. the keyring, off the main thread. It stores each secret under the
        # new name before clearing the old, so a failure part-way leaves some
        # already moved -- which is why the note says "may not", not "not".
        def carry():
            try:
                hosts.migrate_secrets(old, new)
            except (hosts.HostError, OSError) as exc:
                GLib.idle_add(self._manager_note,
                              f"some passwords may not have moved: {exc}", False)
        threading.Thread(target=carry, name=f"secrets-{old}",
                         daemon=True).start()

    def confirm_kill(self, host: str, name: str) -> None:
        """Ask first. A killed session takes whatever it was doing with it,
        and there is no scrollback afterwards to find out what that was."""
        if self.offline_refused(host, name):
            return
        dialog = Gtk.AlertDialog()
        dialog.set_message(f"Kill {host}/{name}?")
        dialog.set_detail("Everything running in the session ends. This cannot "
                          "be undone, and the screen is not kept.")
        dialog.set_buttons(["Cancel", "Kill session"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)

        def answered(source, result):
            try:
                choice = source.choose_finish(result)
            except GLib.Error:
                return
            if choice == 1:
                threading.Thread(target=self._kill, args=(host, name),
                                 daemon=True).start()

        dialog.choose(self, None, answered)

    def _kill(self, host: str, name: str) -> None:
        try:
            hosts.kill_session(host, name)
        except (hosts.HostError, OSError) as exc:
            GLib.idle_add(self.complain, f"{host}/{name}", str(exc))
            return
        # The view is of a session that no longer exists; the terminal will
        # notice on its own when the client exits, but closing it now is what
        # you asked for.
        session = self.open.get((host, name))
        if session is not None:
            GLib.idle_add(self.close_session, session)
        # A probe of this host now, not the sweep: on the slow timer the dead
        # session's row would linger for minutes, and activating it would
        # quietly re-create the session -- attach is new-session -A.
        GLib.idle_add(self.recheck, host)

    def complain(self, what: str, message: str) -> bool:
        dialog = Gtk.AlertDialog()
        dialog.set_message(what)
        dialog.set_detail(message)
        dialog.show(self)
        return GLib.SOURCE_REMOVE

    # -- new session, new host ---------------------------------------------

    def prompt_new_session(self, host: str | None = None) -> None:
        """Ask for a name, having first said where the session is going.

        The host was only ever in the window title, and conn's own window has
        no title bar, so nothing here trains you to read one. That matters
        most from the + button, which takes the host off whatever is selected
        and falls back to the first in the list when nothing is -- so the
        answer can be a server you were not looking at. It is in the body now,
        where the question is.
        """
        host = host or self.selected_host()
        # A name already taken would attach to that session instead -- which
        # looks like the action did nothing -- so offer the next free one.
        taken = {s.get("name", "") for s in
                 (self.rows.get(host) or {}).get("sessions") or []}
        taken |= {name for h, name in self.open if h == host}
        name = next(n for n in ("shell", *(f"shell-{i}" for i in range(2, 100)),
                                "shell-new") if n not in taken)
        self.ask(f"New session on {host}", [("Name", name)],
                 lambda values: self.open_session(host, values["Name"]),
                 lead="New session on", heading=host)

    def new_chat_host(self) -> str:
        """Where New chat goes: the server the filter picks; else the first
        server of the picked project that answers (the first at all if none
        does); else the selected row's host, as the + button always did."""
        if self.filters.server:
            return self.filters.server
        project = next((p for p in self.projects
                        if p.id == self.filters.project), None)
        if project is not None and project.servers:
            servers = list(project.servers)
            return next((h for h in servers
                         if self.rows.get(h, {}).get("state") == "up"),
                        servers[0])
        return self.selected_host()

    def new_for_tab(self) -> bool:
        """The sidebar's New button and ctrl-n: whatever is new on the
        tab showing -- a chat, a server, a note."""
        if self.sidebar_mode == "servers":
            self.prompt_add_host()
            return True
        if self.sidebar_mode == "notes":
            self.notes_pane.new_note()
            return True
        return self.new_chat()

    def new_chat(self, project: str | None = None,
                 host: str | None = None) -> bool:
        """ctrl-n and the New chat button: the New chat screen (phase 2),
        pointed at the project and server the filter means."""
        current = self.stack.get_visible_child()
        if current is not self.newchat:
            self.before_new = current
        if self.newchat.open(project, host):
            self.stack.set_visible_child(self.newchat)
            self.newchat.text.grab_focus()
        return True

    def leave_new_chat(self) -> None:
        """Esc on the New chat screen: back to what was showing."""
        back = self.before_new
        self.before_new = None
        if isinstance(back, Session) and back in self.open.values():
            self.show(back)
        elif back is self.offline_page and back.key is not None:
            self._back_to_offline_page()
        elif back is self.notes_pane and self.sidebar_mode == "notes":
            self.stack.set_visible_child(self.notes_pane)
            self.notes_pane.focus()
        else:
            self.stack.set_visible_child(self.placeholder)
            self.focus_list()

    def _back_to_offline_page(self) -> None:
        """Back to the offline page from New chat or Notes: through
        open_session, so a chat that came back meanwhile opens instead."""
        page = self.offline_page
        host, name = page.key
        row = self.rows.get(host, {})
        if not page.ended and row.get("state") == "up" and not any(
                s.get("name") == name for s in row.get("sessions", [])):
            # Answered without it meanwhile: opening it now would have tmux
            # make an empty session of the old name.
            page.ended = True
            self.open_when_up = None
            self.notice(f"{host}/{name} has ended")
        if page.ended:
            self.stack.set_visible_child(page)
            self.select_key(page.key, promise=False)
            self.paint_offline_page()
        else:
            self.open_session(host, name)

    # -- starting a chat (SPEC §4) ---------------------------------------

    def start_chat(self, target, text: str, source: str, open_after: bool,
                   done=None, announce: bool = False) -> None:
        """Start a chat on a worker. done(started, error) runs on the main
        loop either way. open_after attaches the view to it; announce sends
        quick add's "Started X on Y" notification instead."""
        pid = "" if target.project.implicit else target.project.id
        block = ""
        if target.include_notes and self.notes_ok:
            block = notes_store.notes_block(
                [n for n in self.note_list
                 if n.get("project") == target.project.id
                 and not n.get("queued")])
        now = int(time.time())

        def work():
            try:
                started = starting.start_chat(
                    target.host, text, target.folder, project=pid,
                    source=source, now=now, mode=target.mode,
                    resume=target.resume, notes_block=block)
                GLib.idle_add(landed, started, None)
            except Exception as exc:  # noqa: BLE001 -- landed must run
                GLib.idle_add(landed, None, str(exc))

        def landed(started, error):
            if started is not None:
                cid = chats.chat_id(started.host, started.session)
                self.stamps[cid] = now
                self.recheck(started.host)
                if open_after:
                    self.present()
                    self.open_session(started.host, started.session)
                elif announce:
                    self.announce_started(started)
            if done is not None:
                done(started, error)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=work, name="start-chat", daemon=True).start()

    def announce_started(self, started) -> None:
        note = Gio.Notification.new(notify_text(
            f"Started {started.title} on {started.host}"))
        note.set_body("Claude is on it. You'll get a ping if it needs you.")
        ref = session_ref(started.host, started.session)
        note.set_default_action_and_target("app.open-session",
                                           GLib.Variant("s", ref))
        note.add_button_with_target("Open", "app.open-session",
                                    GLib.Variant("s", ref))
        note.add_button_with_target("Stop", "app.interrupt-chat",
                                    GLib.Variant("s", ref))
        self.get_application().send_notification(f"started-{ref}", note)

    def interrupt(self, host: str, session: str) -> None:
        """Esc into the chat: what Stop on the started notification does.
        Claude stops what it is doing; the session stays."""
        def work():
            try:
                starting.interrupt(host, session)
            except Exception as exc:  # noqa: BLE001 -- reported, not raised
                GLib.idle_add(self.notice, f"Could not stop {session}: {exc}")
        threading.Thread(target=work, name="interrupt", daemon=True).start()

    def quick_add(self) -> None:
        if self.quick is not None:
            self.quick.present()
            return
        self.quick = newchat.QuickAdd(self)
        self.quick.connect("close-request", self._quick_closed)
        self.quick.present()
        if self.notes_ok is None:
            self.refresh_notes()

    def _quick_closed(self, _w) -> bool:
        self.quick = None
        return False

    # -- notes (SPEC §5) -------------------------------------------------

    def notes_ready(self, optimistic: bool = False) -> bool:
        """Whether notes can be used: the host answered -- or, optimistic,
        has not refused yet, since a note can always wait in the outbox."""
        return bool(self.notes_ok) or (optimistic and self.notes_ok is not False) \
            or (optimistic and bool(self.note_list))

    def open_note_count(self, project: str) -> int:
        return sum(1 for n in self.note_list
                   if n.get("project") == project
                   and n.get("kind", "note") == "note"
                   and n.get("status", "open") == "open"
                   and not n.get("queued"))

    def refresh_notes(self) -> None:
        if self.notes_fetching:
            return
        self.notes_fetching = True

        def work():
            try:
                found = self.notes.list()
                GLib.idle_add(landed, found, "")
            except Exception as exc:  # noqa: BLE001 -- landed must run
                GLib.idle_add(landed, None, str(exc))

        def landed(found, problem):
            self.notes_fetching = False
            if found is not None:
                self.notes_ok = True
                self.notes_problem = ""
                self.note_list = self.notes.queued() + found
            else:
                self.notes_ok = False
                self.notes_problem = (
                    f"Can't reach {self.notes.host} for notes ({problem}). "
                    "Showing the last ones fetched; new notes wait here and "
                    "go when it answers.")
                self.note_list = self.notes.queued() + self.notes.cached()
            self.notes_pane.render()
            self._paint_mode_tabs()
            if self.stack.get_visible_child() is self.newchat:
                self.newchat.refresh()
            return GLib.SOURCE_REMOVE

        threading.Thread(target=work, name="notes", daemon=True).start()

    def notes_tick(self) -> bool:
        if self.sidebar_mode == "notes" or self.notes_ok is not True:
            self.refresh_notes()
        return GLib.SOURCE_CONTINUE

    def _notes_call(self, label: str, call, then=None) -> None:
        """Run a store change on a worker, then refetch. then(result, error)
        on the main loop."""
        def work():
            try:
                result = call()
                GLib.idle_add(landed, result, "")
            except Exception as exc:  # noqa: BLE001 -- landed must run
                GLib.idle_add(landed, None, str(exc))

        def landed(result, error):
            if error:
                self.notice(f"{label}: {error}")
            if then is not None:
                then(result, error)
            self.refresh_notes()
            return GLib.SOURCE_REMOVE

        threading.Thread(target=work, name="notes-change", daemon=True).start()

    def add_note(self, project: str, text: str, source: str = "desktop",
                 done=None, announce: bool = False, kind: str = "note",
                 chat: str = "") -> None:
        def then(note, error):
            if note is not None:
                if note.get("queued"):
                    self.notice("Saved here -- it goes to "
                                f"{self.notes.host} when that answers.")
                if announce:
                    self.announce_note(project, note)
            if done is not None:
                done(note, error)

        self._notes_call("Note not saved", lambda: self.notes.create(
            project, text, source=source, kind=kind, chat=chat), then)

    def announce_note(self, project: str, note: dict) -> None:
        name = next((p.name for p in self.projects if p.id == project),
                    project)
        message = Gio.Notification.new(notify_text(f"Saved to {name} notes"))
        message.set_body(notify_text(
            "Waiting to send -- " + self.notes.host + " did not answer"
            if note.get("queued") else note.get("text", "")))
        self.get_application().send_notification("note-saved", message)

    def update_note(self, note: dict, **changes) -> None:
        self._notes_call("Note not changed", lambda: self.notes.update(
            note["id"], **changes))

    def discard_queued(self, note: dict) -> None:
        self.notes.unqueue(note.get("queued", ""))
        self.note_list = [n for n in self.note_list
                          if n.get("queued") != note.get("queued")]
        self.notes_pane.render()

    def confirm_delete_note(self, note: dict) -> None:
        dialog = Gtk.AlertDialog()
        dialog.set_message("Delete this note?")
        dialog.set_detail(textwrap.shorten(note.get("text", ""), 160)
                          + "\n\nIt stays in the git history in "
                          f"~/conn-notes on {self.notes.host}.")
        dialog.set_buttons(["Cancel", "Delete note"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)

        def answered(source, result):
            try:
                choice = source.choose_finish(result)
            except GLib.Error:
                return
            if choice == 1:
                self._notes_call("Note not deleted",
                                 lambda: self.notes.delete(note["id"]))

        dialog.choose(self, None, answered)

    def start_from_note(self, note: dict) -> None:
        """SPEC §5.2: the note's text as the task, @conn_source note:<id>,
        then the note marked started and linked to the chat."""
        pid = note.get("project", "")
        target = newchat.default_target(self, pid)
        if target is None or target.project.id != pid:
            self.notice(f"No server is set for {pid} -- Edit projects first.")
            return
        target.include_notes = False

        def done(started, error):
            if error:
                self.notice(f"Could not start: {error}")
                return
            self.update_note(note, status="started",
                             chat=chats.chat_id(started.host, started.session))

        self.notice(f"Starting on {target.host}…")
        self.start_chat(target, note.get("text", ""),
                        source=f"note:{note.get('id', '')}", open_after=True,
                        done=done)

    def save_recap(self, cid: str) -> None:
        """"Save recap to notes": the chat's recap, else what it last said."""
        chat = self.chat_index.get(cid)
        if chat is None or self.offline_refused(chat.host, chat.session):
            return
        session = next((s for s in self.rows.get(chat.host, {})
                        .get("sessions", []) if s["name"] == chat.session), {})
        agent = session.get("agent") or {}
        text = agent.get("recap") or chat.preview
        if not text:
            self.notice("Nothing to save -- no recap on screen yet.")
            return
        pid = chat.project
        if not any(p.id == pid and not p.implicit for p in self.projects):
            pid = pid if notes_store.PROJECT_ID.match(pid or "") else ""
        if not pid:
            self.notice("That chat has no project to file a note under.")
            return
        self.add_note(pid, text, source="recap", kind="recap", chat=cid,
                      done=lambda note, err: note is not None and
                      self.notice("Recap saved to notes."))

    # -- the sidebar's two modes -----------------------------------------

    def _mode_tabs(self) -> Gtk.Widget:
        box = Gtk.Box(spacing=0, margin_start=12, margin_end=12,
                      margin_bottom=4)
        box.add_css_class("mode-tabs")
        self.mode_buttons = {}
        for mode, label, key in (("chats", "Chats", "ctrl-1"),
                                 ("servers", "Servers", "ctrl-2"),
                                 ("notes", "Notes", "ctrl-3")):
            button = Gtk.ToggleButton()
            button.add_css_class("mode-tab")
            inner = Gtk.Box(spacing=6)
            words = Gtk.Label(label=label)
            inner.append(words)
            count = Gtk.Label(label="")
            count.add_css_class("mode-count")
            inner.append(count)
            button.set_child(inner)
            button.count = count
            button.set_tooltip_text(f"{label} ({key})")
            box.append(button)
            self.mode_buttons[mode] = button
        self.mode_buttons["notes"].set_group(self.mode_buttons["chats"])
        self.mode_buttons["servers"].set_group(self.mode_buttons["chats"])
        self.mode_buttons["chats"].set_active(True)
        # Connected only now, so the initial set_active above is not taken
        # for a click before the stack it switches exists.
        for mode, button in self.mode_buttons.items():
            button.connect("toggled", lambda b, m=mode:
                           b.get_active() and self.set_sidebar_mode(m))
        return box

    def _paint_mode_tabs(self) -> None:
        # No number on Notes: "2" next to the word told you nothing.
        self.mode_buttons["notes"].count.set_text("")
        # On Servers, the ones that need you -- dark or refusing -- and
        # nothing when they all answer: a "9" there would be the same
        # nothing-to-read as on Notes.
        dark = sum(1 for host in getattr(self, "order", [])
                   if self.host_dark(host))
        self.mode_buttons["servers"].count.set_text(str(dark) if dark else "")

    def set_sidebar_mode(self, mode: str) -> bool:
        # Chats and Servers share self.list (the "chats" page); Notes has
        # its own.
        page = "notes" if mode == "notes" else "chats"
        if mode == self.sidebar_mode and \
                self.side_stack.get_visible_child_name() == page:
            return True
        before = self.sidebar_mode
        self.sidebar_mode = mode
        self.side_stack.set_visible_child_name(page)
        button = self.mode_buttons[mode]
        if not button.get_active():
            button.set_active(True)
        # The chips are about chats; in Notes the sidebar is the projects.
        # Servers keeps the search box -- it finds a server or a session by
        # name there -- but not the chips, which filter chats.
        self.project_chip.set_visible(mode == "chats")
        self.server_chip.set_visible(mode == "chats")
        self.needs_chip.set_visible(mode == "chats")
        self.group_chip.set_visible(mode == "chats")
        self.chips_row.set_visible(mode == "chats")
        self.filter.set_placeholder_text(
            {"servers": "Search servers", "notes": "Search notes"}.get(
                mode, "Search chats"))
        self.new_label.set_text(
            {"servers": "New server", "notes": "New note"}.get(
                mode, "New chat"))
        # A chat search is not a note search: entering or leaving Notes
        # starts the box empty rather than filtering by the other's words.
        if (mode == "notes") != (before == "notes") and self.filter.get_text():
            self.filter.set_text("")
        if mode != "notes" and before != mode:
            # The same list, other rows: rebuilt now rather than on the next
            # tick, so the tab and what is under it change together.
            had_focus = self.within(self.list)
            self.shape = []
            self.render()
            self.list.invalidate_filter()
            if had_focus:
                self.focus_list()
        current = self.stack.get_visible_child()
        if mode == "notes":
            # The notes take the main area; what was there comes back with
            # ctrl-1 (or opening any chat).
            if current is not self.notes_pane:
                self.before_notes = current
            self.stack.set_visible_child(self.notes_pane)
            self.notes_pane.render()
            self.refresh_notes()
            self.notes_pane.focus()
        elif current is self.notes_pane:
            self.notes_pane.flush()
            back, self.before_notes = self.before_notes, None
            if isinstance(back, Session) and back in self.open.values():
                self.show(back)
            elif back is self.offline_page and back.key is not None:
                self._back_to_offline_page()
            else:
                self.stack.set_visible_child(self.placeholder)
                self.focus_list()
        return True

    def popup_at(self, widget: Gtk.Widget, x: float, y: float, items,
                 parent: Gtk.Widget | None = None):
        """popup() for a note: parented to the Notes pane (or the notes
        tree), which outlives the rows a refetch replaces, at the click's
        point."""
        parent = parent or self.notes_pane
        # PyGObject gives (ok, x, y) on older GTK and (x, y) or None on newer
        # ones; the three-way unpack threw, so the menu never opened.
        got = widget.translate_coordinates(parent, x, y)
        if got and len(got) == 3:
            got = got[1:] if got[0] else None
        px, py = got or (0, 0)
        return self.popup(None, px, py, items, parent=parent)

    # -- the ctrl-k palette ----------------------------------------------

    def open_palette(self, query: str = "") -> bool:
        if not self.cmdk.get_visible():
            self.before_palette = self.get_focus()
        return self.cmdk.open(query)

    def palette_closed(self) -> None:
        back = self.before_palette
        self.before_palette = None
        if back is not None and back.get_root() is self and back.get_mapped():
            back.grab_focus()

    def palette_items(self, query: str) -> list:
        return cmdk.items_for(
            query, chat_list=self.chats, rows=self.rows,
            chat_ids=set(self.chat_index), notes=self.note_list,
            projects=self.projects, order=self.order, tone=self.host_tone,
            actions=self._palette_actions(query))

    def _palette_actions(self, query: str) -> list:
        acts = []
        q = query.strip()
        I = cmdk.Item
        for project in self.projects:
            if project.servers and q and cmdk.rank(q, project.name, project.id):
                acts.append(I("Actions", f"New chat in {project.name}",
                              kind="action", icon="document-edit-symbolic",
                              run=lambda p=project.id: self.new_chat(p),
                              words=project.name))
        for host in self.order:
            if q and cmdk.rank(q, host):
                acts.append(I("Actions", f"New chat on {host}", kind="action",
                              icon="document-edit-symbolic",
                              run=lambda h=host: self.new_chat(host=h),
                              words=host))
                acts.append(I("Actions", f"Plain shell on {host}",
                              kind="action", icon="utilities-terminal-symbolic",
                              run=lambda h=host: self.prompt_new_session(h),
                              words=host))
        target = newchat.default_target(self)
        if q and target is not None:
            acts.append(I("Actions", f"Save as a note for {target.project.name}",
                          sub=q, kind="action", icon="text-x-generic-symbolic",
                          run=lambda t=q, p=target.project.id:
                          self.add_note(p, t), words=q))
        fixed = [
            ("New chat", "ctrl-n", "document-edit-symbolic",
             lambda: self.new_chat(), "new chat start"),
            ("Notes", "ctrl-3", "text-x-generic-symbolic",
             lambda: self.set_sidebar_mode("notes"), "notes jot"),
            ("Chats", "ctrl-1", "view-list-symbolic",
             lambda: self.set_sidebar_mode("chats"), "chats list"),
            ("Servers", "ctrl-2", "network-server-symbolic",
             lambda: self.set_sidebar_mode("servers"), "servers hosts"),
            ("Edit projects…", "", "folder-symbolic", self.open_projects,
             "projects folders servers"),
            ("Hosts and passwords…", "", "network-server-symbolic",
             self.open_manager, "site manager hosts servers passwords keys"),
            ("Add server…", "", "list-add-symbolic", self.prompt_add_host,
             "add server host"),
            ("Refresh now", "", "view-refresh-symbolic",
             lambda: self.load_hosts(force=True), "refresh reload"),
            ("Keys and marks", "F1", "input-keyboard-symbolic",
             self.show_guide, "help keys shortcuts guide"),
        ]
        for title, hint, icon, run, words in fixed:
            acts.append(I("Actions", title, kind="action", icon=icon,
                          run=run, hint=hint, words=words))
        return acts

    def palette_run(self, item, new_window: bool = False) -> None:
        if item.kind in ("chat", "shell"):
            host, session = item.key
            if new_window:
                if self.offline_refused(host, session, attach=True):
                    return
                hosts.launch(hosts.connect_argv(host, session))
            else:
                self.open_session(host, session)
        elif item.kind == "note":
            self.set_sidebar_mode("notes")
        elif item.kind == "host":
            # To the server, in the Servers tab: its row has every fix and
            # its whole menu, where a New session prompt had one thing.
            self.set_sidebar_mode("servers")
            if not self.select_server(item.key[0]) and self.query():
                self.filter.set_text("")    # a search was hiding it
                self.refilter()
                self.select_server(item.key[0])
            self.focus_list()
        elif item.run is not None:
            item.run()

    def defer_to_stable(self) -> bool:
        """Whether this is the dev copy with the stable conn running, which
        then does the writing nobody asked for (see stable_up)."""
        return paths.DEV and self.stable_up

    def _stable_seen(self, up: bool) -> None:
        """The bus says the stable conn's name came or went. A going is
        believed only after STABLE_GRACE: the stable conn's restart (to
        update) is an exec, which drops its bus connection and takes it up
        again a second later, and is not the stable conn gone."""
        if self.stable_grace:
            GLib.source_remove(self.stable_grace)
            self.stable_grace = 0
        if up:
            self._stable_set(True)
        elif self.stable_up:
            def gone():
                self.stable_grace = 0
                self._stable_set(False)
                return GLib.SOURCE_REMOVE
            self.stable_grace = GLib.timeout_add_seconds(STABLE_GRACE, gone)

    def _stable_set(self, up: bool) -> None:
        if up != self.stable_up:
            connlog.event("dev: stable conn "
                          + ("running, leaving stamps and titles to it" if up
                             else "not running, stamping and titling here"))
        self.stable_up = up
        if up:
            # Anything queued before it came is its to title now.
            self.retitle_queue.clear()

    def _retitle_next(self) -> None:
        """Title the next queued chat on a worker; one call at a time."""
        if self.retitler.busy or os.environ.get("CONN_NO_RETITLE") \
                or self.defer_to_stable():
            return
        while self.retitle_queue:
            cid = self.retitle_queue.pop(0)
            chat = self.chat_index.get(cid)
            if chat is None or chat.name or chat.offline:
                continue        # an offline chat has no screen to read
            session = next((s for s in self.rows.get(chat.host, {})
                            .get("sessions", []) if s.get("name") == chat.session),
                           None)
            screen = (session or {}).get("raw") or (session or {}).get("screen", "")
            self.retitler.took(cid)
            self.retitler.busy = True
            host, name, before = chat.host, chat.session, chat.title
            pane = chat.pane_title

            def work():
                try:
                    title = retitle.ask(screen)
                except Exception:  # noqa: BLE001 -- landed must run
                    title = ""
                GLib.idle_add(landed, title)

            def landed(title):
                self.retitler.busy = False
                current = self.chat_index.get(cid)
                # Dark since it was asked: nothing to write it to.
                if title and current is not None and not current.name \
                        and not current.offline \
                        and self.rows.get(host, {}).get("state") == "up" \
                        and title != before:
                    # With the Claude title it replaced, so a new
                    # conversation in the same session (a new pane title)
                    # gets its own name back at once.
                    self.autotitle_wish[cid] = title
                    self.autotitle_for_wish[cid] = pane
                    self.writer.put(host, name, "autotitle_for", pane)
                    self.writer.put(host, name, "autotitle", title)
                    self.render()
                self._retitle_next()
                return GLib.SOURCE_REMOVE

            threading.Thread(target=work, name="retitle", daemon=True).start()
            return

    def toggle_chat_star(self, cid: str) -> None:
        """Star or unstar a chat: @conn_starred on its tmux session, so it
        survives a restart and shows on every client (SPEC §2).

        Shown at once and written behind, on the option worker. A write
        that fails takes the star back and says why -- see _option_failed.
        """
        chat = self.chat_index.get(cid)
        if chat is None or self.offline_refused(chat.host, chat.session):
            return
        want = not chat.starred
        self.star_wish[cid] = want
        self.writer.put(chat.host, chat.session, "starred",
                        "1" if want else None)
        self.render()

    def rename_chat(self, cid: str) -> Gtk.Window | None:
        """Give a chat your own name (@conn_name, decision A). It outranks
        Claude's title and @conn_title (SPEC §2); an empty name unsets it and
        the chat falls back to them. Shown at once, written behind, and taken
        back with a notice if the write fails -- as a star is. The tmux
        session keeps its name: that is "Rename session…"."""
        chat = self.chat_index.get(cid)
        if chat is None or self.offline_refused(chat.host, chat.session):
            return None

        def done(values):
            value = values["Name"].strip() or None
            if value is not None and not chat.name and value == chat.title:
                return          # Enter on the title it already had
            self.name_wish[cid] = value
            self.writer.put(chat.host, chat.session, "name", value)
            self.render()
        return self.ask("Rename chat", [("Name", chat.name or chat.title)],
                        done, lead="Rename", heading=chat.title,
                        allow_empty=True,
                        hint="Your name for it, shown everywhere the chat is. "
                             "Empty goes back to Claude's own title. The tmux "
                             f"session stays {chat.host}/{chat.session}.")

    def rename_selected_chat(self) -> bool:
        """F2 in the list: rename the selected chat."""
        row = self.list.get_selected_row()
        cid = getattr(row, "chat", None) if row is not None else None
        if cid is None and row is not None:
            key = getattr(row, "key", None)
            if key is not None and chats.chat_id(*key) in self.chat_index:
                cid = chats.chat_id(*key)
        if cid is None:
            if row is not None:
                self.notice("only chats have names -- Rename session… is in "
                            "the right-click menu")
            return True
        self.rename_chat(cid)
        return True

    def move_chat(self, cid: str, project: str | None) -> None:
        """File a chat under a project (@conn_project, decision B), or with
        None back to Automatic -- the folder decides. Optimistic, like a
        rename."""
        chat = self.chat_index.get(cid)
        if chat is None or self.offline_refused(chat.host, chat.session):
            return
        self.project_wish[cid] = project
        self.writer.put(chat.host, chat.session, "project", project)
        self.render()

    def star_selected(self) -> bool:
        row = self.list.get_selected_row()
        cid = getattr(row, "chat", None) if row is not None else None
        if cid is None:
            key = self.selected_key()
            if key is not None and chats.chat_id(*key) in self.chat_index:
                cid = chats.chat_id(*key)
        if cid is None:
            if row is not None:
                self.notice("only chats have stars -- a plain shell is not one")
            return True
        self.toggle_chat_star(cid)
        return True

    def _option_failed(self, host: str, session: str, name: str,
                       value, error: str) -> bool:
        """A chat-option write did not land.

        A star you clicked goes back to what the server has, and the footer
        says so. A recency stamp says nothing: the host has most likely just
        gone dark, and the row keeps its place in memory for this run --
        which is what it would have had if the write had landed.
        """
        cid = chats.chat_id(host, session)
        if name == "starred":
            wanted = value is not None
            if self.star_wish.get(cid) == wanted:
                del self.star_wish[cid]
            self.notice(f"could not {'star' if wanted else 'unstar'} "
                        f"{host}/{session} -- {error}")
            self.render()
        elif name in ("name", "project"):
            book = self.name_wish if name == "name" else self.project_wish
            if cid in book and book[cid] == value:
                del book[cid]
            self.notice(f"could not {'rename' if name == 'name' else 'move'} "
                        f"{host}/{session} -- {error}")
            self.render()
        return GLib.SOURCE_REMOVE

    def focus_first_chat(self) -> bool:
        """Down from the search box: the first row that can be opened."""
        index = 0
        while (row := self.list.get_row_at_index(index)) is not None:
            index += 1
            if ((getattr(row, "key", None) is not None
                 or getattr(row, "server", None) is not None)
                    and row.get_selectable() and self.matches(row)):
                self.list.select_row(row)
                row.grab_focus()
                return True
        return False

    def first_shown_chat(self) -> "chats.Chat":
        """The chat the list draws first, in section order."""
        for _label, members in self.chat_sections(self.visible_chats,
                                                  time.time()):
            if members:
                return members[0]
        return self.visible_chats[0]

    def open_first(self) -> None:
        """Enter in the search box: open the first chat it found -- in the
        Servers tab, the first server or session it found; in Notes, the
        first note."""
        if self.sidebar_mode == "notes":
            self.notes_pane.side.open_first()
            return
        if not self.query():
            row = self.list.get_selected_row()
            if row is not None and (getattr(row, "key", None)
                                    or getattr(row, "server", None)):
                self.row_activated(self.list, row)
            return
        if self.sidebar_mode == "servers":
            index = 0
            while (row := self.list.get_row_at_index(index)) is not None:
                index += 1
                if ((getattr(row, "key", None) or getattr(row, "server", None))
                        and self.matches(row)):
                    self.row_activated(self.list, row)
                    return
            return
        if self.visible_chats:
            # The first row on screen, not the newest: the list draws Starred
            # ahead of the day sections, and Down-then-Enter walks rows in
            # display order -- plain Enter has to land on the same chat.
            chat = self.first_shown_chat()
            self.open_session(chat.host, chat.session)

    def edit_projects(self) -> None:
        """projects.toml in whatever opens text here. Phase 4's Hosts screen
        edits it properly; until then the file is the interface."""
        path = chats.PROJECTS_FILE
        if not path.exists():
            self.notice(f"no {path} yet -- the format is in SPEC §1")
            return
        Gtk.FileLauncher.new(Gio.File.new_for_path(str(path))).launch(
            self, None, None, None)

    def prompt_rename(self, host: str, name: str) -> None:
        """Sessions get named once, when they are made, and "shell" is what
        three of them end up called. Renaming one costs a tmux command.

        Which one is in the body, like prompt_new_session: the target was
        only ever in the window title, and nothing in conn trains you to
        read one -- with three sessions called shell, that made this dialog
        a coin toss."""
        if self.offline_refused(host, name):
            return
        self.ask(f"Rename {host}/{name}", [("Name", name)],
                 lambda values: threading.Thread(
                     target=self._rename, args=(host, name, values["Name"]),
                     daemon=True).start(),
                 lead="Rename", heading=f"{host}/{name}")

    def _rename(self, host: str, name: str, wanted: str) -> None:
        try:
            now = hosts.rename_session(host, name, wanted)
        except (hosts.HostError, OSError) as exc:
            GLib.idle_add(self.complain, f"{host}/{name}", str(exc))
            return
        GLib.idle_add(self.renamed, host, name, now)

    def renamed(self, host: str, was: str, now: str) -> bool:
        """Follow the session that has just been renamed.

        A session open in the window is still the same terminal on the same
        pty -- nothing about it is stale except the name conn files it under,
        and losing track of that would leave the view open with no row and no
        key to close it by.
        """
        session = self.open.pop((host, was), None)
        if session is not None:
            session.name = now
            self.open[(host, now)] = session
        # The old key would otherwise sit in the notification memory as a
        # session that has gone quiet, and the new one arrives as first seen,
        # which is right: a rename is not a chat that started needing you.
        self.was.pop((host, was), None)
        self.drafts.pop((host, was), None)
        # The chat-id books follow the session too: a pending star, name or
        # move, the stamp not echoed yet, what was seen, and what the
        # trackers know -- or the renamed chat would arrive as a stranger.
        old, new = chats.chat_id(host, was), chats.chat_id(host, now)
        for book in (self.stamps, self.star_wish, self.name_wish,
                     self.project_wish, self.seen, self.autotitle_wish,
                     self.autotitle_for_wish, self.retitler.last):
            if old in book:
                book[new] = book.pop(old)
        self.retitle_queue = [new if cid == old else cid
                              for cid in self.retitle_queue]
        self.recency.rename(old, new)
        self.finish.rename(old, new)
        self.chat_ids = {new if cid == old else cid for cid in self.chat_ids}
        chats.save_seen(self.seen, chats.SEEN_STATE)
        # A probe of this host now, not the sweep: an unstarred host on the
        # slow timer would keep the old name on show for up to five minutes,
        # and its stale row silently re-creates the session if activated --
        # attach is new-session -A.
        self.recheck(host)
        return GLib.SOURCE_REMOVE

    def toggle_star(self, host: str) -> None:
        """Watch a server live, or stop (the Site Manager's "Watch live").

        This was the host star, and it is still what decides whether a host
        is streamed; the name stayed, the label did not. Stopping drops the
        stream then and there rather than at the next sweep, because the
        point of it is the connection, not the row. Its chats stay in the
        chat list either way -- only how quickly they update changes.
        """
        if host in self.starred:
            self.starred.discard(host)
            if host not in self.auto_live:
                self.stop_stream(host)
            self.probed[host] = time.monotonic()
        else:
            self.starred.add(host)
            if not self.holding:
                self.start_stream(host)
            self.recheck(host)      # it is live now; do not wait for the sweep
        save_stars(self.starred)
        self.shape = []
        self.render()

    def can_move(self, host: str, delta: int) -> bool:
        return move_within_tier(self.order, self.starred, host, delta) is not None

    def move_host(self, host: str, delta: int) -> None:
        """Nudge a server one place up (-1) or down (+1) the list.

        Within its own group: a starred host moves among the starred, an
        unstarred one among the rest, so the row you moved is always one you
        can see move. The arrangement is written down, so it outlives both a
        restart and the next time the config is re-read.
        """
        moved = move_within_tier(self.order, self.starred, host, delta)
        if moved is None:
            self.notice(f"{host} is already at the "
                        f"{'top' if delta < 0 else 'bottom'}")
            return
        self.order = moved
        save_order(self.order)
        self.shape = []
        self.render()

    def move_selected(self, delta: int) -> bool:
        """ctrl-shift-up / down: move the host the selected session belongs to.

        A session row implies its host, the same way "new session" reads it off
        the selection. With nothing selected there is no host to mean, so the
        key is left to the terminal rather than claimed to do nothing.
        """
        row = self.list.get_selected_row()
        host = getattr(row, "server", None) if row is not None else None
        key = self.selected_key()
        if host is None and key is None:
            return False
        self.move_host(host or key[0], delta)
        if host is not None:
            self.select_server(host)    # the rebuild moved the row, not it
        return True

    def confirm_forget(self, host: str) -> None:
        """Ask before editing ~/.ssh/config, and offer to take the passwords
        with it.

        Leaving a keyring entry behind for a server that no longer exists is
        how you end up with secrets you cannot place a year later, so the
        offer is made here rather than left as a second errand. It is a
        separate button rather than a checkbox because GTK's alert dialog has
        no room for one, and three plain choices read better than a form.

        The keyring is asked on a worker -- secret-tool can sit waiting on
        it, and a right-click must not freeze the window -- so the dialog
        appears when the count is in.
        """
        def count():
            try:
                secrets = len(hosts.secret_names(host))
            except (hosts.HostError, OSError):
                secrets = 0
            GLib.idle_add(self._ask_forget, host, secrets)
        threading.Thread(target=count, daemon=True).start()

    def _ask_forget(self, host: str, secrets: int) -> bool:
        buttons = ["Cancel", "Forget server"]
        if secrets:
            buttons.append(f"Forget server and {secrets} password"
                           + ("s" if secrets > 1 else ""))

        dialog = Gtk.AlertDialog()
        dialog.set_message(f"Forget {host}?")
        dialog.set_detail(
            "The Host block comes out of ~/.ssh/config and the server leaves "
            "the list. Nothing on the server itself is touched, and the old "
            "config is kept as a backup beside it.")
        dialog.set_buttons(buttons)
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)

        def answered(source, result):
            try:
                choice = source.choose_finish(result)
            except GLib.Error:
                return
            if choice >= 1:
                self.forget_host(host, passwords=choice == 2)

        dialog.choose(self, None, answered)
        return GLib.SOURCE_REMOVE

    def forget_host(self, host: str, passwords: bool = False) -> None:
        """Remove a server, and everything this window was holding for it."""
        try:
            backup = hosts.remove_host(host)
        except (hosts.HostError, OSError) as exc:
            self.complain(f"Could not forget {host}", str(exc))
            return

        # The views for it go too. The tmux sessions on the far side keep
        # running -- forgetting a server is not killing anything on it -- but
        # a terminal pointed at a host the config no longer knows cannot
        # reconnect, and a view with no row is how the list and the stack get
        # out of step.
        for key in [k for k in self.open if k[0] == host]:
            self.close_session(self.open[key])

        self.stop_stream(host)
        self.streams.pop(host, None)
        self.rows.pop(host, None)
        self.inflight.discard(host)
        self.probed.pop(host, None)
        self.streamed.pop(host, None)
        self.auto_live.discard(host)
        if host in self.starred:
            self.starred.discard(host)
            save_stars(self.starred)
        if self.known.pop(host, None) is not None:
            self.save_known()

        self.load_hosts()
        self.notice(f"forgot {host} -- old config kept at {backup.name}")
        # The Site Manager, if open, is drawn from the same config: drop the
        # forgotten host from its list, keeping the selection on whatever it
        # was showing unless that is the host that just went.
        keep = self.manager_host
        self._manager_reload(keep if self.manager is not None
                             and keep in hosts.config_hosts() else None)

        # A server you have forgotten should not keep a mountpoint or its
        # keyring entries. On a worker, like do_mount(): fusermount blocks
        # for seconds on a busy mount and secret-tool waits on the keyring,
        # and the window freezing is not part of forgetting a server. The
        # footer says how it went when it is done.
        def tidy():
            unmount_error = ""
            if not hosts.is_local(host) and hosts.is_mounted(host):
                try:
                    hosts.unmount(host)
                except (hosts.HostError, OSError) as exc:
                    unmount_error = str(exc)
            hosts.tidy_mounts()

            cleared = 0
            if passwords:
                for name in hosts.secret_names(host):
                    try:
                        hosts.secret_clear(host, name)
                        cleared += 1
                    except (hosts.HostError, OSError):
                        pass    # one stubborn entry is not worth losing the rest

            said = f"forgot {host}"
            if cleared:
                said += f" and {cleared} password" + ("s" if cleared > 1 else "")
            said += f" -- old config kept at {backup.name}"
            if unmount_error:
                said += f"; still mounted: {unmount_error}"
            GLib.idle_add(self.notice, said)

        threading.Thread(target=tidy, name=f"forget-{host}",
                         daemon=True).start()

    def prompt_add_host(self) -> None:
        def add(values):
            try:
                backup = hosts.add_host(values["Name"], values["Hostname"],
                                        values["User"], values["Port"] or "22")
            except (hosts.HostError, OSError) as exc:
                self.complain("Could not add host", str(exc))
                return
            self.load_hosts()
            self.complain("Added to ~/.ssh/config",
                          f"The old file is kept at {backup.name}.")

        self.ask("Add a server", [("Name", ""), ("Hostname", ""),
                                  ("User", ""), ("Port", "22")], add)

    def ask(self, title: str, fields: list[tuple[str, str]], done,
            secret: str = "", lead: str = "", heading: str = "",
            allow_empty: bool = False, hint: str = "") -> Gtk.Window:
        """A small modal form. GTK has no one-line prompt, and four of these
        are cheaper than four hand-built dialogs.

        `heading` is the thing being acted on -- a host, usually -- set above
        the fields where it cannot be missed. Two labels rather than one
        marked-up string, so a host named with an ampersand is a host name and
        not broken Pango.
        """
        window = Gtk.Window(title=title, transient_for=self, modal=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_top=14, margin_bottom=14,
                      margin_start=14, margin_end=14)
        if heading:
            top = Gtk.Box(spacing=6, margin_bottom=4)
            if lead:
                said = Gtk.Label(label=lead, xalign=0)
                said.add_css_class("detail")
                top.append(said)
            subject = Gtk.Label(label=heading, xalign=0)
            subject.add_css_class("heading")
            top.append(subject)
            box.append(top)

        entries: dict[str, Gtk.Entry] = {}
        for label, initial in fields:
            line = Gtk.Box(spacing=8)
            name = Gtk.Label(label=label, xalign=0)
            name.set_size_request(90, -1)
            entry = Gtk.Entry()
            if label == secret:
                entry.set_visibility(False)
            entry.set_text(initial)
            entry.set_hexpand(True)
            entries[label] = entry
            line.append(name)
            line.append(entry)
            box.append(line)
        if hint:
            note = Gtk.Label(label=hint, xalign=0)
            note.add_css_class("detail")
            note.set_wrap(True)
            note.set_max_width_chars(52)
            box.append(note)

        # An all-empty form is normally a slip, and nothing is done with it.
        # Where empty means something -- a rename cleared back to Claude's
        # title -- the caller says so.
        def submit(*_args):
            values = {k: e.get_text().strip() for k, e in entries.items()}
            window.close()
            if allow_empty or any(values.values()):
                done(values)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: window.close())
        confirm = Gtk.Button(label=title.split()[0])
        confirm.add_css_class("suggested-action")
        confirm.connect("clicked", submit)
        buttons.append(cancel)
        buttons.append(confirm)
        box.append(buttons)

        for entry in entries.values():
            entry.connect("activate", submit)   # enter submits from any field
        window.entries = entries
        window.submit = submit
        window.set_child(box)
        window.present()
        return window

    def _shortcuts(self) -> None:
        """Keys, matched by hand rather than by accelerator.

        GtkShortcutController looked like the right tool and quietly is not:
        an accelerator asking for CTRL+SHIFT+c never fires, because shift is
        consumed producing the C the event actually carries, and what is left
        no longer matches what was asked for. Nothing errors -- the shortcut
        simply does nothing, which is how ctrl-shift-c, -v, -w and -k were all
        dead without anyone noticing.

        A key controller hands over the keyval and the modifiers and lets this
        decide. It is longer, and it is readable, and it can be tested without
        a keyboard.

        CAPTURE phase because a focused VTE swallows nearly everything: alt-1
        it would forward to the shell as ESC-1.
        """
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.key_pressed)
        keys.connect("key-released", self.key_released)
        self.add_controller(keys)
        # An alt-tab away never delivers the release, so leaving the window
        # drops the numbers too; coming back marks the visible chat seen.
        self.connect("notify::is-active", self._active_changed)

    def _active_changed(self, *_args) -> None:
        if not self.is_active():
            self.alt_held(False)
            return
        cid = self.visible_chat()
        if cid is not None:
            self.mark_seen(cid)

    def alt_held(self, held: bool) -> None:
        """The alt-1..9 numbers show while alt is down (decision E)."""
        if held:
            self.list.add_css_class("alt-held")
        else:
            self.list.remove_css_class("alt-held")

    def key_released(self, _controller, keyval, _code, _state) -> None:
        if keyval in (Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Meta_L,
                      Gdk.KEY_Meta_R):
            self.alt_held(False)

    def key_pressed(self, _controller, keyval, _code, state) -> bool:
        """True means conn took the key; False leaves it to the terminal."""
        # The palette has the keyboard while it is up -- its own controller
        # reads the arrows, Tab and Enter.
        if self.cmdk.get_visible():
            return False
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(state & Gdk.ModifierType.ALT_MASK)
        # Lowered, because shift has already turned c into C by the time this
        # sees it -- which is the whole reason accelerators did not work.
        name = (Gdk.keyval_name(Gdk.keyval_to_lower(keyval)) or "").lower()

        # Alt alone: show the numbers, and leave the key to whoever had it --
        # the terminal still gets its alt.
        if keyval in (Gdk.KEY_Alt_L, Gdk.KEY_Alt_R):
            self.alt_held(True)
            return False

        if alt and not ctrl and name.isdigit() and name != "0":
            return self.show_nth(int(name) - 1)

        if ctrl and name in ("tab", "iso_left_tab"):
            return self.cycle(-1 if shift else 1)
        if ctrl and name == "page_down":
            return self.cycle(1)
        if ctrl and name == "page_up":
            return self.cycle(-1)

        if name == "f12":
            return self.focus_list()
        if (name == "escape" and not (ctrl or alt or shift)
                and self.stack.get_visible_child() is self.newchat
                and not self.within(self.newchat.text)):
            self.leave_new_chat()
            return True
        if name == "f11":
            return self.toggle_fullscreen()

        # F1 always; "?" only when it is not a character someone is typing.
        # A guide you have to find with the mouse is one the keyboard user
        # never opens -- and "?" is what the panel this grew out of used.
        if name == "f1" or (name == "question" and not self.typing()):
            return self.show_guide()

        # Down from the search box walks into what it found -- the arrows
        # are the way through the list, and they should not stop at the box.
        if (name == "down" and not (ctrl or alt or shift)
                and self.within(self.filter)):
            return self.focus_first_chat()

        # Right / plus open the highlighted server out in the Servers tab,
        # Left / minus fold it -- a tree's keys, from the list only, where
        # the arrows otherwise mean nothing sideways. Shift is let through
        # because + is shift-= on most layouts.
        if (self.sidebar_mode == "servers" and not (ctrl or alt)
                and self.within(self.list) and not self.typing()):
            if name in ("right", "plus", "kp_add"):
                return self.fold_key(True)
            if name in ("left", "minus", "kp_subtract"):
                return self.fold_key(False)

        # "s" stars the selected chat -- but only while the list has the
        # keyboard. Anywhere else it is a letter someone is typing.
        if (name == "s" and not (ctrl or alt or shift)
                and self.within(self.list) and not self.typing()):
            return self.star_selected()
        # F2 renames the selected chat, from the list only: in a terminal F2
        # belongs to whatever is running there.
        if (name == "f2" and not (ctrl or alt or shift)
                and self.within(self.list) and not self.typing()):
            return self.rename_selected_chat()

        if ctrl and not shift:
            # ctrl-f is the search key this window always had, and stays
            # conn's wherever the focus is. ctrl-k (the design's search key)
            # and ctrl-n are kill-line and next-history in readline, cut and
            # next-line in nano, and kill-to-end in Claude's input box -- so
            # in a terminal they are the terminal's, as they were before the
            # chat list. F12 then ctrl-k/ctrl-n reaches them from there.
            in_terminal = isinstance(self.get_focus(), Vte.Terminal)
            if name == "f":
                # Each tab searches its own rows.
                self.filter.grab_focus()
                self.filter.select_region(0, -1)
                return True
            # ctrl-k is the palette (Palette.dc.html) everywhere but a
            # terminal; ctrl-shift-p reaches it from inside one.
            if name == "k" and not in_terminal:
                return self.open_palette()
            if name == "n" and not in_terminal:
                return self.new_for_tab()
            # ctrl-1 / ctrl-2 / ctrl-3: Chats, Servers and Notes, the tabs'
            # order. Nothing in
            # a shell or in Claude reads ctrl-digit, so these are conn's
            # everywhere.
            if name == "1":
                return self.set_sidebar_mode("chats")
            if name == "2":
                return self.set_sidebar_mode("servers")
            if name == "3":
                return self.set_sidebar_mode("notes")
            if name == "q":
                self.close()
                return True
            # What every terminal binds. "equal" is the unshifted key the +
            # is printed on, so ctrl-= zooms too; the literal ctrl-+ arrives
            # with shift down and is matched in the shifted block below.
            if name in ("plus", "equal", "kp_add"):
                return self.zoom_by(ZOOM_STEP)
            if name in ("minus", "kp_subtract"):
                return self.zoom_by(1 / ZOOM_STEP)
            if name in ("0", "kp_0"):
                return self.set_zoom(1.0)

        if ctrl and shift:
            if name == "p":
                return self.open_palette()
            if name == "n":
                return self.new_chat()
            if name == "c":
                return self.on_terminal("copy")
            if name == "v":
                return self.on_terminal("paste")
            if name == "a":
                return self.on_terminal("select_all")
            if name == "w":
                return self.close_visible()
            if name == "k":
                return self.kill_selected()
            if name == "r":
                return self.rename_selected()
            if name == "s":
                return self.copy_screen()
            if name == "y":
                return self.copy_reply()
            if name == "up":
                return self.move_selected(-1)
            if name == "down":
                return self.move_selected(1)
            # The + on most layouts is shift-=, so the ctrl-+ the guide
            # promises lands here, shift and all.
            if name in ("plus", "equal", "kp_add"):
                return self.zoom_by(ZOOM_STEP)

        return False        # everything else belongs to the terminal

    def within(self, widget: Gtk.Widget) -> bool:
        """Whether the keyboard focus is on `widget` or inside it."""
        focus = self.get_focus()
        return focus is not None and (focus is widget
                                      or focus.is_ancestor(widget))

    def typing(self) -> bool:
        """Whether the keystroke belongs to something taking text: a session's
        terminal, the filter box, or a note being written (a TextView -- not
        an Editable, so it was missed and "?" in a note opened the guide).
        Everything else is conn's to bind."""
        focus = self.get_focus()
        return isinstance(focus, (Vte.Terminal, Gtk.Editable, Gtk.Text,
                                  Gtk.TextView))

    def zoom_by(self, factor: float) -> bool:
        return self.set_zoom(self.zoom * factor)

    def set_zoom(self, zoom: float) -> bool:
        """Resize the text in every session, now and the ones opened later.

        VTE scales the font it was given rather than being handed a new one,
        so the family and the size out of the terminal config stay the thing
        being adjusted -- zooming is not a second opinion about the font.
        """
        self.zoom = round(min(max(zoom, ZOOM_MIN), ZOOM_MAX), 3)
        for session in self.open.values():
            session.term.set_font_scale(self.zoom)
        save_zoom(self.zoom)
        # No message to go with it: the text changing size in front of you is
        # the feedback, and the footer belongs to the host count.
        return True

    def opened(self) -> list[Session]:
        """Open sessions in the order they were opened -- which is the order
        alt-1..9 counts in, and the order the list marks them."""
        return list(self.open.values())

    def show(self, session: Session, focus: bool = True) -> bool:
        """Put a session on the right. focus=False leaves the keyboard and
        the list's highlight where they are -- for an open nobody asked for
        just then (_owed_open)."""
        self.open_when_up = None    # whatever the offline page was owed
        if self.sidebar_mode == "notes":
            # Opening a chat from a note: back to the chat list beside it.
            self.before_notes = session
            self.set_sidebar_mode("chats")
        self.stack.set_visible_child(session)
        if focus:
            session.term.grab_focus()
            self.select_key((session.host, session.name))
        # Opening a chat is seeing it (decision D): every way in -- a click,
        # Enter, alt-N, ctrl-tab, a notification -- ends here.
        self.mark_seen(chats.chat_id(session.host, session.name))
        return True

    def visible_chat(self) -> str | None:
        """The chat id of the session showing, or None."""
        current = self.stack.get_visible_child()
        if isinstance(current, Session):
            return chats.chat_id(current.host, current.name)
        return None

    def mark_seen(self, cid: str) -> None:
        """Book a chat's last_event as seen, so a finish up to it is not
        unseen any more. Redrawn when that changes anything on screen."""
        chat = self.chat_index.get(cid)
        if chat is None or self.seen.get(cid) == chat.last_event:
            return
        self.seen[cid] = chat.last_event
        chats.save_seen(self.seen, chats.SEEN_STATE)
        if chat.finished:
            chat.finished = False
            self.render()

    def show_nth(self, index: int) -> bool:
        if index < len(self.slots):
            self.open_session(*self.slots[index])
        return True     # claimed either way, or alt-4 types a 4 in the shell

    def cycle(self, step: int) -> bool:
        sessions = self.opened()
        if not sessions:
            return True
        current = self.stack.get_visible_child()
        try:
            index = sessions.index(current)
        except ValueError:
            # From the offline page (or nothing): forward is the first open
            # session, back the last.
            index = -1 if step > 0 else 0
        return self.show(sessions[(index + step) % len(sessions)])

    def on_terminal(self, action: str) -> bool:
        """Run one of the terminal's own actions on whichever is showing."""
        current = self.stack.get_visible_child()
        if not isinstance(current, Session):
            return True
        if action == "select_all":
            current.term.select_all()
            return True
        if action == "copy":
            size = current.copy()
            self.notice(f"copied {size} characters" if size > 0
                        else "copied" if size < 0
                        else "nothing selected -- hold shift while you drag")
            return True
        getattr(current, action)()
        return True

    def copy_screen(self) -> bool:
        """ctrl-shift-s: the same copy the right-click menu offers, with the
        same guard and the same report -- see Session.copy_screen()."""
        current = self.stack.get_visible_child()
        if isinstance(current, Session):
            current.copy_screen()
        return True

    def copy_reply(self, host: str | None = None,
                   name: str | None = None) -> bool:
        """ctrl-shift-y, the terminal menu and the header's ⋯: Claude's last
        reply on the clipboard, word for word.

        From the transcript Claude Code writes (transcript.py), because the
        screen has it wrapped at the terminal's width and cut where it
        scrolled away; the screen's reading of it only when there is no
        transcript to read -- an older Claude, a server that will not say --
        and the notice says which, since that copy may be clipped.

        The lookup is an ssh round trip, so it runs on a thread and lands
        back here; a second press while one is out is the same copy, not
        another ssh. The log gets where it came from, never the text."""
        if host is None or name is None:
            current = self.stack.get_visible_child()
            if not isinstance(current, Session):
                return False        # no chat open: the key is not ours
            host, name = current.host, current.name
        key = (host, name)
        pending = self.__dict__.setdefault("replies_pending", set())
        if key in pending:
            return True
        pending.add(key)
        screen = self.screen_of(host, name)

        def work():
            try:
                text, source = transcript.last_reply(host, name, screen)
            except Exception:  # noqa: BLE001 -- landed must run
                text, source = "", ""
            GLib.idle_add(landed, text, source)

        def landed(text, source):
            pending.discard(key)
            if text:
                Session.to_clipboard(text)
            connlog.event(f"copy reply {host}/{name}: "
                          + (f"from the {source}" if text else "none found"))
            # Mid-turn, the transcript's last finished answer is the turn
            # before's: copied, but not passed off as the one in progress.
            self.notice("Copied Claude's last reply" if source == "transcript"
                        else "Copied Claude's previous reply -- it is still "
                        "working" if source == "transcript-busy"
                        else "Copied Claude's last reply, from the screen -- "
                        "it may be cut short" if text
                        else "No reply from Claude to copy yet")
            return GLib.SOURCE_REMOVE

        threading.Thread(target=work, name="copy-reply", daemon=True).start()
        return True

    def notice(self, text: str) -> None:
        """Say something in the footer for a few seconds.

        The footer is rewritten on every frame, so a message has to be told to
        outlast that; render() reads this. It exists because copy is silent
        either way, and "did that work?" is not a question the window should
        leave you holding.
        """
        self.said = (text, time.monotonic() + 4.0)
        self.footnote.set_text(text)
        # Its own timer, because the next render can be minutes out: the
        # streams speak only when a screen changes, and with nothing starred
        # the next probe is on the slow timer. Stamped with the tuple above,
        # so expiring a notice that has since been replaced is a no-op.
        GLib.timeout_add_seconds(4, self.expire_notice, self.said)

    def expire_notice(self, said: tuple[str, float]) -> bool:
        if self.said == said:
            self.said = ("", 0.0)
            self.render()
        return GLib.SOURCE_REMOVE

    def kill_selected(self) -> bool:
        """Kill whatever the list is pointing at. Not bound to Delete: this
        runs in the capture phase, so it would be taken from a terminal you
        were typing in."""
        key = self.selected_key()
        if key:
            self.confirm_kill(*key)
        return True

    def rename_selected(self) -> bool:
        key = self.selected_key()
        if key:
            self.prompt_rename(*key)
        return True

    def toggle_fullscreen(self) -> bool:
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()
        return True

    def focus_list(self) -> bool:
        """Back to the list, without reaching for the mouse. Arrows move,
        enter opens."""
        self.list.grab_focus()
        row = self.list.get_selected_row()
        if row is not None:
            row.grab_focus()
        return True

    def close_visible(self) -> bool:
        """Close the view. The tmux session on the other side keeps running --
        that is what it is for -- so this loses nothing but the tab."""
        current = self.stack.get_visible_child()
        if isinstance(current, Session):
            self.close_session(current)
        return True

    def select_key(self, key: tuple[str, str],
                   promise: bool = True, zone: str | None = None) -> None:
        """Highlight one session, now or as soon as it has a row.

        A session made from the + button is running before anything has asked
        the host what it has: the terminal opens on it, and the row for it
        arrives a second later with the next probe. Selecting it here found
        nothing and gave up quietly, and the render that brought the row in
        then put the highlight back where it had been -- so the list sat
        pointing at the session you had just left, for as long as you left it
        alone. The key is kept instead, and render() finishes the job.

        Only an open or show path may leave that promise: promise=False is
        for restoring a highlight, where a row that has vanished -- killed
        session, folded host -- is a highlight lost, not a session owed. A
        restore that armed self.pending fired as an opened-looking selection
        whenever a row by that name came back, however much later.
        """
        row = self.row_for(key, zone)
        if row is None:
            if promise:
                self.pending = key
            return
        self.selecting = True
        self.list.select_row(row)
        self.selecting = False
        self.pending = None

    def row_for(self, key: tuple[str, str],
                zone: str | None = None) -> Gtk.ListBoxRow | None:
        """The row for a session -- the first, or the one in `zone`.

        The list holds one tab's rows at a time, so a session has one row
        in it -- but the zone still says which tab a highlight was saved in,
        and a restore asks for that one, falling back to any row.
        """
        first = None
        index = 0
        while (row := self.list.get_row_at_index(index)) is not None:
            if getattr(row, "key", None) == key:
                if zone is None or getattr(row, "zone", None) == zone:
                    return row
                first = first or row
            index += 1
        return first

    def row_selected(self, _list, row) -> None:
        """A highlight moved by hand cancels the promise in self.pending.

        By hand means a click or an arrow key -- the arrows move the
        highlight without changing which session is showing, so the old test
        of "is the promised session still on screen" could not see the move
        and stole the highlight back. The window's own moves run under
        self.selecting, and a row being removed reports None: neither is a
        choice.
        """
        if self.selecting or row is None:
            return
        self.pending = None

    def _wordmark(self) -> Gtk.Widget:
        """The top of the sidebar, as the chat-list design has it: the name
        and the fleet in one line, the New chat button, search, and the
        three filter chips.

        The bar beside the name is the fleet's condition -- how much of it
        answers, green up and red dark, grey for hosts not heard from yet,
        because a fleet out of sight drawn green is reassuring exactly when
        nothing is known. What is waiting on you is the Needs you chip's
        count, and the same words are on the bar's tooltip.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      margin_top=14, margin_bottom=10,
                      margin_start=12, margin_end=12)

        top = Gtk.Box(spacing=10)
        self.condition = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.condition.add_css_class("condition")
        self.condition.set_valign(Gtk.Align.CENTER)
        self.condition.set_size_request(4, CONDITION_HEIGHT)
        self.condition_parts = {}
        for tone in ("green", "red", "muted"):
            part = Gtk.Box()
            part.add_css_class(f"fleet-{tone}")
            part.set_size_request(4, 0)
            self.condition.append(part)
            self.condition_parts[tone] = part
        top.append(self.condition)

        mark = Gtk.Label(label=WORDMARK, xalign=0)
        mark.add_css_class("wordmark")
        top.append(mark)
        # The dev copy says so, in the one place the eye lands first: two
        # windows that look the same are two windows you type into by
        # mistake.
        if paths.DEV:
            channel = Gtk.Label(label="dev", valign=Gtk.Align.CENTER)
            channel.add_css_class("channel")
            channel.set_tooltip_text(
                "The dev copy (CONN_CHANNEL=dev), running beside the stable "
                "conn with state of its own")
            top.append(channel)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        top.append(spacer)

        # Two labels so the dark half can be red by class, not by markup --
        # in a box of their own with no spacing, so "10 up · 3 dark" reads as
        # one phrase rather than two words 10px apart.
        fleet = Gtk.Box(spacing=0)
        fleet.set_valign(Gtk.Align.CENTER)
        self.fleet_up = Gtk.Label(label="", xalign=1)
        self.fleet_up.add_css_class("fleet")
        fleet.append(self.fleet_up)
        self.fleet_dark = Gtk.Label(label="", xalign=1)
        self.fleet_dark.add_css_class("fleet")
        self.fleet_dark.add_css_class("dark")
        fleet.append(self.fleet_dark)
        top.append(fleet)
        box.append(top)

        # Everything under the tabs: the tab's own New button, its search
        # box and (Chats only) the chips. Built here, placed by _build()
        # below the tabs, so the tabs sit in the same place on every tab.
        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                          margin_top=10, margin_bottom=8,
                          margin_start=12, margin_end=12)
        self.side_actions = actions

        # The one primary button. Its label follows the filter -- "New chat
        # in Acme", "New chat on acme-app" -- so it says where the chat will
        # go before you press it. In phase 1 it opens the new-session prompt
        # on that server; the full New chat screen is phase 2.
        self.new_button = Gtk.Button()
        self.new_button.add_css_class("primary")
        inner = Gtk.Box(spacing=10)
        inner.append(Gtk.Image.new_from_icon_name("document-edit-symbolic"))
        self.new_label = Gtk.Label(label="New chat", xalign=0)
        self.new_label.set_hexpand(True)
        self.new_label.set_ellipsize(Pango.EllipsizeMode.END)
        inner.append(self.new_label)
        hint = Gtk.Label(label="ctrl-n")
        hint.add_css_class("kbd")
        inner.append(hint)
        self.new_button.set_child(inner)
        self.new_button.set_tooltip_text(
            "Start a session on the server the filter picks -- or the "
            "selected one (ctrl-n)")
        self.new_button.connect("clicked", lambda _b: self.new_for_tab())
        actions.append(self.new_button)

        # ctrl-k on the field's right edge, as the design keys it. Hidden
        # while there is text: the entry's clear icon takes that spot.
        search = Gtk.Overlay()
        search.set_child(self.filter)
        self.search_hint = Gtk.Label(label="ctrl-f")
        self.search_hint.add_css_class("kbd-chip")
        self.search_hint.set_halign(Gtk.Align.END)
        self.search_hint.set_valign(Gtk.Align.CENTER)
        self.search_hint.set_margin_end(8)
        self.search_hint.set_can_target(False)
        search.add_overlay(self.search_hint)
        self.filter.connect("changed", lambda e: self.search_hint.set_visible(
            not e.get_text()))
        actions.append(search)
        self.search_box = search

        chips = Gtk.Box(spacing=6)
        self.project_chip = self._chip_menu(self._fill_project_menu)
        self.server_chip = self._chip_menu(self._fill_server_menu)
        chips.append(self.project_chip)
        chips.append(self.server_chip)

        self.needs_chip = Gtk.ToggleButton()
        self.needs_chip.add_css_class("chip")
        self.needs_chip.add_css_class("chip-needs")
        needs = Gtk.Box(spacing=6)
        needs.append(Gtk.Label(label="Needs you"))
        self.needs_count = Gtk.Label(label="0")
        self.needs_count.add_css_class("count")
        # A 16px pill in the middle of the chip, not a block its full height.
        self.needs_count.set_valign(Gtk.Align.CENTER)
        needs.append(self.needs_count)
        self.needs_chip.set_child(needs)
        self.needs_chip.set_tooltip_text(
            "Only chats blocked on a question, holding an unsent draft, or "
            "finished since you last opened them")
        self.needs_chip.connect("toggled", self._needs_toggled)
        chips.append(self.needs_chip)

        # Group by project: a view, not a filter -- every chat, one section
        # per project, rows in a fixed order rather than newest first.
        self.group_chip = Gtk.ToggleButton()
        self.group_chip.add_css_class("chip")
        self.group_chip.set_child(Gtk.Image.new_from_icon_name(
            "view-list-symbolic"))
        self.group_chip.set_tooltip_text(
            "Group by project: every chat under its project, in a fixed "
            "order, instead of newest first")
        self.group_chip.connect("toggled", self._group_toggled)
        chips.append(self.group_chip)
        actions.append(chips)
        self.chips_row = chips
        return box

    def _chip_menu(self, fill) -> Gtk.MenuButton:
        """A filter chip: a label and a chevron that opens a menu. The menu
        is built when it opens, so it always counts what is there now."""
        chip = Gtk.MenuButton()
        chip.add_css_class("chip")
        chip.set_halign(Gtk.Align.START)
        inner = Gtk.Box(spacing=6)
        label = Gtk.Label(label="")
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_max_width_chars(14)
        inner.append(label)
        chevron = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        chevron.set_pixel_size(12)
        inner.append(chevron)
        chip.set_child(inner)
        chip.label = label
        popover = Gtk.Popover()
        popover.add_css_class("chip-menu")
        popover.add_css_class("conn-menu")
        popover.set_has_arrow(False)
        # A popover is placed by its own halign: START puts the menu's left
        # edge under the chip's, instead of centring a menu wider than the
        # chip out past the side of the window.
        popover.set_halign(Gtk.Align.START)
        # The chip whose menu is open looks it, as the design draws it.
        popover.connect("show", lambda _p: chip.add_css_class("open"))
        popover.connect("closed", lambda _p: chip.remove_css_class("open"))
        chip.set_popover(popover)
        chip.set_create_popup_func(lambda button: fill(button.get_popover()))
        return chip

    def _menu_box(self, popover: Gtk.Popover, heading: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        # As wide as the sidebar less its gutters, as the design's menu is.
        box.set_size_request(SIDEBAR_WIDTH - 36, -1)
        title = Gtk.Label(label=heading.upper(), xalign=0, margin_start=8,
                          margin_top=4, margin_bottom=4)
        title.add_css_class("section")
        box.append(title)
        popover.set_child(box)
        return box

    def _menu_row(self, popover, lead: Gtk.Widget, name: str, meta: str,
                  picked: bool, pick, mono: bool = False,
                  dark: bool = False) -> Gtk.Button:
        button = Gtk.Button()
        button.set_has_frame(False)
        button.add_css_class("menu-row")
        if picked:
            button.add_css_class("picked")
        inner = Gtk.Box(spacing=10)
        inner.append(lead)
        label = Gtk.Label(label=name, xalign=0)
        label.set_hexpand(True)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_max_width_chars(24)
        if mono:
            label.add_css_class("mono")
        inner.append(label)
        tail = Gtk.Label(label=meta, xalign=1)
        tail.add_css_class("meta")
        if dark:
            tail.add_css_class("dark")
        inner.append(tail)
        button.set_child(inner)
        button.connect("clicked", lambda _b: (popover.popdown(), pick()))
        return button

    def _tile(self, project, small: bool = False) -> Gtk.Label:
        tile = Gtk.Label(label=project.letter if project else "*")
        tile.add_css_class("tile")
        tile.add_css_class("tile-small" if small else "tile-big")
        tile.add_css_class(self.tile_class.get(project.id, "tile-none")
                           if project else "tile-none")
        size = 22 if small else 32
        tile.set_size_request(size, size)
        tile.set_halign(Gtk.Align.START)
        tile.set_valign(Gtk.Align.START)
        return tile

    def _fill_project_menu(self, popover: Gtk.Popover) -> None:
        box = self._menu_box(popover, "Projects")
        for item in chats.project_menu(self.filters, self.projects):
            box.append(self._menu_row(
                popover, self._tile(item.project, small=True), item.name,
                item.meta, item.selected,
                lambda pid=item.id: self.set_filters(
                    chats.pick_project(self.filters, pid))))
        rule = Gtk.Box()
        rule.add_css_class("menu-sep")
        box.append(rule)
        edit = Gtk.Button()
        edit.set_has_frame(False)
        edit.add_css_class("menu-link")
        inner = Gtk.Box(spacing=8)
        plus = Gtk.Image.new_from_icon_name("list-add-symbolic")
        plus.set_pixel_size(13)
        inner.append(plus)
        inner.append(Gtk.Label(label="Edit projects…", xalign=0))
        edit.set_child(inner)
        edit.set_tooltip_text("Projects, their letters and colours, and which "
                              "servers and folders belong to each")
        edit.connect("clicked", lambda _b: (popover.popdown(),
                                            self.open_projects()))
        box.append(edit)

    def _fill_server_menu(self, popover: Gtk.Popover) -> None:
        dark = [h for h in self.order if self.host_dark(h)]
        heading, items = chats.server_menu(self.filters, self.projects,
                                           self.chats, dark)
        box = self._menu_box(popover, heading)
        for item in items:
            dot = Gtk.Box()
            dot.set_size_request(8, 8)
            dot.set_valign(Gtk.Align.CENTER)
            if item.id is not None:
                dot.add_css_class("dot")
                dot.add_css_class(self.host_tone(item.id))
            box.append(self._menu_row(
                popover, dot, item.name, item.meta, item.selected,
                lambda host=item.id: self.set_filters(
                    chats.pick_server(self.filters, host)),
                mono=item.id is not None, dark=item.dark))

    def _needs_toggled(self, button) -> None:
        if self.syncing or button.get_active() == self.filters.needs_you:
            return
        self.set_filters(chats.toggle_needs(self.filters))

    def _group_toggled(self, button) -> None:
        if self.syncing or button.get_active() == self.filters.by_project:
            return
        self.set_filters(chats.toggle_by_project(self.filters))

    def chat_sections(self, shown: list, now: float) -> list[tuple]:
        """The chat list's sections in the layout picked: by project, or
        Starred and by day."""
        if self.filters.by_project:
            return chats.project_sections(shown, self.projects)
        return chats.sections(shown, now)

    def _footer(self) -> Gtk.Widget:
        """Help sits at the bottom of the list, where it is out of the way
        until the moment you want it.

        Two rows rather than one, because the two buttons below are conditional
        and long. Sharing a line with the host count, they pushed the sidebar's
        *minimum* width to 453px against a 300px pane -- and a box that cannot
        have its minimum is drawn clipped, which is why the list sometimes
        appeared with its left edge cut off. It only happened when one of them
        was showing, which is what made it look random.

        Their own row also suits them better: a restart notice is worth the
        full width, not a squeeze between a count and a close button.
        """
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                        margin_top=6, margin_bottom=8,
                        margin_start=10, margin_end=10)
        outer.add_css_class("footer")

        notices = Gtk.Box(spacing=6)
        bar = Gtk.Box(spacing=2)

        self.footnote = Gtk.Label(label="", xalign=0)
        self.footnote.add_css_class("footnote")
        self.footnote.set_hexpand(True)
        self.footnote.set_margin_start(6)
        # A label demands room for every character it holds unless told it may
        # give some back. This one carries the chat count ("10 chats · 7
        # projects") and, for a few seconds at a time, whatever the window
        # last did. No max_width_chars: the sidebar's width is fixed, so the
        # count takes what the two buttons leave and ellipsizes past that.
        self.footnote.set_ellipsize(Pango.EllipsizeMode.END)
        bar.append(self.footnote)

        # Two quiet buttons, as the design has them: the servers, and a menu
        # for the rest. Everything the old row of icons did is in that menu,
        # by name, which is easier to find than a fifth icon.
        servers = Gtk.Button(icon_name="network-server-symbolic")
        servers.add_css_class("action")
        servers.set_has_frame(False)
        servers.set_tooltip_text(
            "Hosts -- edit, rename, add or forget a server, its passwords, "
            "and Watch live")
        servers.connect("clicked", lambda _b: self.open_manager())
        bar.append(servers)

        self.menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        self.menu_button.add_css_class("action")
        self.menu_button.set_has_frame(False)
        self.menu_button.set_tooltip_text("Add a server, projects, refresh, "
                                          "the keys, close")
        menu = Gtk.Popover()
        menu.add_css_class("conn-menu")
        menu.set_has_arrow(False)
        items = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        for label, hint, handler in (
            ("Search everything…", "ctrl-k", self.open_palette),
            ("New chat…", "ctrl-n", self.new_chat),
            ("Servers", "ctrl-2", lambda: self.set_sidebar_mode("servers")),
            ("Notes", "ctrl-3", lambda: self.set_sidebar_mode("notes")),
            (None, None, None),
            ("Add server…", "", self.prompt_add_host),
            ("Edit projects…", "", self.open_projects),
            ("Refresh now", "", lambda: self.load_hosts(force=True)),
            ("Keys and marks…", "F1", self.show_guide),
            (None, None, None),
            ("Close Conn", "ctrl-q", self.close),
        ):
            if label is None:
                items.append(Gtk.Separator(
                    orientation=Gtk.Orientation.HORIZONTAL))
                continue
            items.append(self._menu_item(menu, label, hint, handler))
        menu.set_child(items)
        self.menu_button.set_popover(menu)
        bar.append(self.menu_button)

        # The guide is its own popover on the same button: F1 and ? open it
        # straight away, and the menu's "Keys and marks…" swaps to it.
        self.guide = self._guide()
        self.guide.set_parent(self.menu_button)
        self.help_button = self.guide       # the name the guide went by

        # Shown only once a newer conn has been copied over this one. There
        # is no other sign: the window goes on running the code it started
        # with, and a feature added an hour ago is simply absent.
        self.updated = Gtk.Button(label="restart to update")
        self.updated.add_css_class("locked")
        self.updated.set_tooltip_text(
            "A newer conn is installed. Restarting reloads it -- the sessions "
            "are tmux and survive it; the views close and reopen.")
        self.updated.set_visible(False)
        self.updated.connect("clicked", lambda _b: self.restart())
        self.updated.set_hexpand(True)
        notices.append(self.updated)

        # Shown only when there is nothing to unlock it with. A padlock that
        # is always there stops being read.
        self.unlock = Gtk.Button(label="unlock key")
        self.unlock.add_css_class("locked")
        self.unlock.set_tooltip_text(
            "The ssh agent is holding no keys, so every host will refuse you. "
            "This runs ssh-add in a terminal -- the passphrase goes to it, "
            "never through conn.")
        self.unlock.set_visible(False)
        self.unlock.connect("clicked", lambda _b: self.do_unlock())
        self.unlock.set_hexpand(True)
        notices.append(self.unlock)

        # Closing -- the one thing genuinely lost with the title bar -- is
        # "Close Conn" in the menu above, and ctrl-q. Down here it is still
        # there fullscreen, which is where it went missing before.

        outer.append(notices)
        outer.append(bar)
        return outer

    def _menu_item(self, popover, label: str, hint: str, handler) -> Gtk.Button:
        """One line of a conn menu: the name, and its key on the right."""
        button = Gtk.Button()
        button.set_has_frame(False)
        button.add_css_class("menu-item")
        inner = Gtk.Box(spacing=12)
        name = Gtk.Label(label=label, xalign=0)
        name.set_hexpand(True)
        inner.append(name)
        if hint:
            key = Gtk.Label(label=hint, xalign=1)
            key.add_css_class("kbd-hint")
            inner.append(key)
        button.set_child(inner)
        button.label = label
        button.connect("clicked", lambda _b: (popover.popdown(), handler()))
        return button

    def show_guide(self) -> bool:
        """F1, ?, and the menu's "Keys and marks…": the guide, on the menu
        button, after the menu has had a frame to close."""
        menu = self.menu_button.get_popover()
        if menu is not None and menu.get_visible():
            menu.popdown()
        self.guide.popup()
        return True

    def do_unlock(self) -> None:
        hosts.unlock_agent()
        # The terminal outlives this call, so the state is re-read after it
        # rather than now, when it would still say locked. Polled rather than
        # read once: the hosts stay held until the key lands, and how long
        # you take to type it is not something to guess at.
        self.polls = 20
        GLib.timeout_add_seconds(3, self.poll_unlock)

    def poll_unlock(self) -> bool:
        self.check_agent()
        self.polls -= 1
        if self.polls > 0 and self.holding:
            return GLib.SOURCE_CONTINUE
        return GLib.SOURCE_REMOVE

    def check_agent(self) -> None:
        def look():
            keys = hosts.agent_keys()
            GLib.idle_add(self.show_agent, keys)
        threading.Thread(target=look, daemon=True).start()

    def show_agent(self, keys) -> bool:
        """None is no agent at all, 0 is an agent holding nothing. Both mean
        key authentication is going to fail, which is worth saying before
        seven hosts turn red for no visible reason."""
        locked = not keys
        self.unlock.set_visible(locked)
        if locked:
            self.unlock.set_label(
                "no ssh agent" if keys is None else "unlock key")
            if self.holding:
                self.notice("ssh agent holds no key -- unlock it, or "
                            "refresh to connect anyway")
        elif self.holding:
            # The one moment when fanning out is free: every ssh below rides
            # the agent and none of them stops to ask for anything.
            self.connect_hosts()
        return GLib.SOURCE_REMOVE

    def _style(self) -> None:
        """Take the desktop's colours rather than GTK's.

        The panel sits next to terminals all day; matching Omarchy's theme is
        what stops it reading as a foreign application.
        """
        p = self.palette
        # Text colours derived for the chat rows, each held to the same 4.5:1
        # as the palette's own text colours (theming.legible). The state hues
        # are only held to 3:1 by the palette because they are marks; the
        # times are words, so they get the full floor.
        # The selected chat is a card lifted a step off the panel with a thin
        # ring, as the design draws it -- not the accent fill, which forced
        # every word on the row to one colour and took the amber and blue
        # times with it. Every text colour on a row is held to 4.5:1 against
        # the card as well as the plain surfaces, so selecting a row can
        # never take a word below the floor.
        card = selected_card(p)
        row_surfaces = tuple(p.surfaces) + (card,)
        amber_text = legible(p.yellow, row_surfaces)
        amber_tint_text = amber_on_tints(p)
        blue_text = legible(p.blue, row_surfaces)
        dim_title = legible(mix(p.foreground, p.panel, 0.22), row_surfaces)
        server_text = legible(mix(p.foreground, p.panel, 0.18), row_surfaces)
        row_muted = legible(p.muted, row_surfaces)
        border = mix(p.foreground, p.panel, 0.78)
        kbd = legible(mix(p.background, p.foreground, 0.45), (p.foreground,))
        # The search field sits a little below the panel, the way the design
        # insets it; the placeholder and the key chip are held to it.
        field = mix(p.background, p.panel, 0.5)
        field_muted = legible(p.muted, (field,) + tuple(p.surfaces))
        self.card_colour = card
        css = f"""
        window {{ background: {p.background}; color: {p.foreground}; }}
        headerbar {{ background: {p.panel}; color: {p.foreground};
                     border-bottom: 1px solid {p.surface}; }}
        .subtitle {{ color: {p.muted}; font-size: 0.9em; }}
        .side {{ background: {p.panel};
                 border-right: 1px solid {p.surface}; }}
        .sidebar {{ background: transparent; }}
        .wordmark {{ color: {p.foreground}; font-family: {MONO};
                     font-weight: bold; font-size: 1.35em;
                     letter-spacing: 0.04em; }}
        .channel {{ color: {amber_tint_text};
                    background: alpha({p.yellow}, {CHIP_TINT});
                    border: 1px solid alpha({p.yellow}, 0.5);
                    font-family: {MONO}; font-weight: bold;
                    font-size: 0.75em; border-radius: 6px;
                    padding: 1px 6px; }}
        .condition {{ border-radius: 2px; }}
        .fleet-green {{ background: {p.green}; }}
        .fleet-red {{ background: {p.red}; }}
        .fleet-muted {{ background: {p.muted}; }}
        .fleet {{ color: {p.muted}; font-family: {MONO}; font-size: 0.8em; }}
        .fleet.dark {{ color: {p.red}; }}
        .primary {{ background: {p.foreground}; color: {p.background};
                    border-radius: 10px; min-height: 36px; padding: 0 12px;
                    font-weight: 600; box-shadow: none; border: none; }}
        .primary:hover {{ background: {mix(p.foreground, p.panel, 0.1)}; }}
        .primary .kbd {{ color: {kbd}; font-family: {MONO};
                         font-size: 0.8em; font-weight: normal; }}
        .chip {{ border: 1px solid {border}; border-radius: 8px;
                 background: transparent; color: {p.muted};
                 min-height: 26px; padding: 0 8px; font-size: 0.9em;
                 box-shadow: none; }}
        .chip > button {{ background: transparent; color: inherit;
                          padding: 0 2px; min-height: 26px; box-shadow: none;
                          border: none; }}
        .chip.on {{ background: {p.surface}; color: {p.foreground};
                    border-color: {mix(p.foreground, p.panel, 0.55)}; }}
        .chip-needs:checked {{ background: alpha({p.yellow}, {CHIP_TINT});
                               border-color: alpha({p.yellow}, 0.5);
                               color: {amber_tint_text}; }}
        .chip > button:checked {{ background: {mix(p.panel, p.surface, 0.5)};
                                  color: {p.foreground}; }}
        .chip.open {{ border-color: {mix(p.foreground, p.panel, 0.6)};
                      color: {p.foreground}; }}
        .count {{ background: alpha({p.yellow}, {COUNT_TINT});
                  color: {amber_tint_text}; font-family: {MONO};
                  border-radius: 8px; padding: 0 4px; font-size: 0.75em;
                  min-width: 8px; min-height: 16px; }}
        /* Every popover conn opens is drawn from the theme, not GTK's: a
           dark Omarchy palette under a light GTK theme put the picked row's
           dark slab on GTK's white, unreadable. */
        popover.conn-menu > contents {{ background: {p.panel};
                                        color: {p.foreground};
                                        border: 1px solid {border};
                                        border-radius: 12px; padding: 6px;
                                        box-shadow: 0 18px 40px alpha(black, 0.45); }}
        popover.conn-menu > arrow {{ background: {p.panel};
                                     border: 1px solid {border}; }}
        popover.conn-menu button {{ color: {p.foreground}; }}
        popover.conn-menu button:hover {{ background: alpha({p.surface}, 0.7); }}
        popover.conn-menu .destructive {{ color: {p.red}; }}
        popover.conn-menu separator {{ background: {border}; margin: 4px 0; }}
        .menu-row {{ border-radius: 8px; padding: 4px 8px; min-height: 28px;
                     color: {p.foreground}; }}
        .menu-row:hover {{ background: alpha({p.surface}, 0.6); }}
        .menu-row.picked {{ background: {p.surface}; }}
        .menu-row .mono {{ font-size: 0.92em; }}
        .menu-row .meta, .meta {{ color: {p.muted}; font-family: {MONO};
                                  font-size: 0.8em; }}
        .meta.dark {{ color: {p.red}; }}
        .menu-sep {{ background: {border}; min-height: 1px; margin: 4px 0 0 0; }}
        .menu-link {{ color: {p.muted}; font-size: 0.9em; padding: 9px 8px 5px 8px;
                      border-radius: 8px; min-height: 0; }}
        .menu-link:hover {{ color: {p.foreground}; background: transparent; }}
        .menu-item {{ padding: 4px 10px; min-height: 28px; border-radius: 8px; }}
        .menu-item .kbd-hint {{ color: {p.muted}; font-family: {MONO};
                                font-size: 0.8em; }}
        .mono {{ font-family: {MONO}; }}
        .dot {{ border-radius: 999px; min-width: 8px; min-height: 8px; }}
        .dot.green {{ background: {p.green}; }}
        .dot.red {{ border: 1.5px solid {p.red}; }}
        .dot.muted {{ border: 1.5px solid {p.muted}; }}
        .dot.amber {{ background: {p.yellow}; }}
        .dot.blue {{ background: {p.blue}; }}
        .tile {{ border-radius: 9px; font-family: {MONO}; font-weight: bold; }}
        .tile-big {{ font-size: 1.05em; }}
        .tile-small {{ border-radius: 6px; font-size: 0.8em; }}
        .tile-none {{ background: {p.surface}; color: {p.muted}; }}
        .badge-needs {{ background: {p.yellow}; border-radius: 999px;
                        border: 2px solid {p.panel};
                        min-width: 9px; min-height: 9px; }}
        .badge-working {{ background: transparent; }}
        .badge-draft {{ background: {p.foreground}; color: {p.background};
                        border-radius: 999px; border: 2px solid {p.panel};
                        padding: 1px; }}
        .badge-unknown {{ background: {p.surface}; color: {p.muted};
                          border-radius: 999px; border: 2px solid {p.panel};
                          font-family: {MONO}; font-size: 0.65em;
                          font-weight: bold; min-width: 12px; }}
        /* The selected row is accent-coloured, and the badge's fills are
           the theme's blue, amber and foreground -- on the accent they fell
           to 1.2-1.4:1 (1:1 on a theme whose accent is its blue). So the
           badge keeps its panel disc and ring there too: each fill stays on
           the panel it is held to 3:1 against, and the ring sets it off the
           highlight. Restated for :selected so a theme's selection rules
           cannot take it. */
        .sidebar row:selected .badge-needs,
        .sidebar row:selected .badge-draft,
        .sidebar row:selected .badge-unknown {{ border-color: {p.panel}; }}
        .chat-title {{ color: {p.foreground}; font-weight: 500; }}
        .chat-needs .chat-title,
        .sidebar row.chat:selected .chat-title {{ font-weight: 600; }}
        .chat-idle .chat-title {{ color: {dim_title}; }}
        .sidebar row.chat:selected.chat-idle .chat-title {{ color: {p.foreground}; }}
        .chat-time {{ color: {row_muted}; font-family: {MONO};
                      font-size: 0.8em; }}
        .chat-time.amber {{ color: {amber_text}; }}
        .chat-time.blue {{ color: {blue_text}; }}
        .chat-server {{ color: {server_text}; font-family: {MONO};
                        font-size: 0.8em; }}
        .chat-preview, .chat-dot {{ color: {row_muted}; font-size: 0.85em; }}
        .chat-offline .chat-preview {{ font-style: italic; }}
        .star {{ color: {row_muted}; min-width: 26px; min-height: 26px;
                 padding: 0; }}
        .star.on {{ color: {p.foreground}; }}
        .section {{ color: {p.muted}; font-size: 0.72em; font-weight: bold;
                    letter-spacing: 0.08em; }}
        .summary {{ color: {p.muted}; font-size: 0.9em; }}
        .clear {{ color: {p.foreground}; font-size: 0.9em; padding: 0 6px;
                  min-height: 22px; background: transparent;
                  box-shadow: none; }}
        .clear label {{ text-decoration-line: underline; font-weight: 500; }}
        .clear:hover {{ background: alpha({p.surface}, 0.6); }}
        .empty {{ color: {p.foreground}; font-size: 0.95em; }}
        .start {{ background: {p.surface}; border: 1px solid {border};
                  color: {p.foreground}; box-shadow: none; min-height: 30px;
                  padding: 0 12px; border-radius: 8px; font-size: 0.9em;
                  font-weight: 500; }}
        .start:hover {{ background: {mix(p.surface, p.foreground, 0.08)}; }}
        .footer {{ border-top: 1px solid {p.surface}; }}
        .footnote {{ color: {p.muted}; font-family: {MONO}; font-size: 0.8em; }}
        .link {{ color: {p.accent}; font-size: 0.85em; padding: 0; }}
        .filter {{ font-size: 0.95em; min-height: 34px; background: {field};
                   border: 1px solid {border}; border-radius: 9px;
                   box-shadow: none; outline: none; color: {p.foreground}; }}
        .filter:focus-within {{ border-color: {p.accent}; }}
        .filter > image {{ color: {field_muted}; -gtk-icon-size: 14px; }}
        .filter > text > placeholder {{ color: {field_muted}; }}
        .kbd-chip {{ border: 1px solid {border}; border-radius: 5px;
                     padding: 0 5px; font-family: {MONO}; font-size: 0.75em;
                     color: {field_muted}; }}
        .locked {{ color: {p.red}; font-size: 0.8em; padding: 2px 8px; }}
        .action {{ min-width: 30px; min-height: 30px; padding: 0;
                   border-radius: 8px; color: {p.muted}; background: transparent;
                   box-shadow: none; }}
        .action:hover, .action:checked {{ color: {p.foreground};
                                          background: {p.surface}; }}
        .secret {{ font-family: monospace; }}
        /* The theme's text, not GTK's: CONN_THEME=light under a dark GTK
           theme left session names in GTK's white on a light sidebar. */
        .sidebar row {{ padding: 2px 10px; color: {p.foreground}; }}
        .sidebar row.chat {{ padding: 0 2px 0 0; border-radius: 10px;
                             margin: 0 8px; }}
        /* A session in the Servers tab sits under its server as a branch
           of a tree: indented past the server's chevron to about its dot,
           a thin guide line down the left that runs on through the rows
           below it, and smaller, regular-weight text -- at the server's
           size and indent they read as more servers. A plain shell is
           quieter still than a chat. On "branch", not "session": the
           Projects window's picker borrows "session" for the sidebar's card
           and must keep its plain card, not a guide line and an indent. */
        .sidebar row.session {{ padding: 4px 8px; border-radius: 10px;
                                margin: 0 8px; }}
        .sidebar row.session.branch {{ padding: 3px 8px 3px 10px;
                                       border-radius: 0 10px 10px 0;
                                       margin: 0 8px 0 38px;
                                       border-left: 1px solid {p.surface}; }}
        .sidebar row.branch .name {{ font-size: 0.9em; font-weight: normal; }}
        .sidebar row.branch .dot {{ min-width: 6px; min-height: 6px; }}
        /* A server in the Servers tab: a destination like a session, with
           air above it so each reads as the head of its group. */
        .sidebar row.server {{ padding: 6px 8px 4px 8px; border-radius: 10px;
                               margin: 10px 8px 0 8px; }}
        .sidebar button.chevron {{ min-width: 16px; min-height: 16px;
                                   padding: 0; color: {p.muted};
                                   background: transparent; }}
        .sidebar button.chevron:hover {{ color: {p.foreground}; }}
        .sidebar row.server .host {{ padding-top: 0; }}
        .sidebar row.server:hover {{ background: alpha({card}, 0.55); }}
        .sidebar row.server:selected {{ background: {card};
                                        box-shadow: inset 0 0 0 1px {border};
                                        color: {p.foreground}; }}
        .sidebar row.server:selected .host {{ color: {p.foreground}; }}
        .sidebar button.fix {{ min-height: 20px; padding: 0 8px;
                               font-size: 0.8em; border-radius: 6px; }}
        .sidebar row.fold {{ padding: 8px 16px 4px 16px; }}
        .sidebar row.heading-row {{ padding: 10px 16px 4px 16px; }}
        .sidebar row.chat:hover, .sidebar row.session:hover {{
            background: alpha({card}, 0.55); }}
        .sidebar row:selected {{ background: {p.accent}; color: {p.on_accent}; }}
        .sidebar row.chat:selected,
        .sidebar row.session:selected {{ background: {card};
                                         box-shadow: inset 0 0 0 1px {border};
                                         color: {p.foreground}; }}
        /* Child labels carry their own colour, and one of them is the accent
           itself -- which on the accent-coloured selection is invisible. */
        .sidebar row:selected:not(.chat):not(.session):not(.server) .name,
        .sidebar row:selected:not(.chat):not(.session):not(.server) .detail,
        .sidebar row:selected:not(.chat):not(.session):not(.server) .host,
        .sidebar row:selected:not(.chat):not(.session):not(.server) .attention,
        .sidebar row:selected:not(.chat):not(.session):not(.server) .mark {{
            color: {p.on_accent}; }}
        .sidebar row.session .name {{ color: {p.foreground}; }}
        .sidebar row.session.plain .name {{ color: {row_muted}; }}
        .sidebar row.session .detail {{ color: {row_muted}; }}
        .host {{ color: {p.muted}; font-weight: bold;
                 padding-top: 10px; letter-spacing: 0.06em; }}
        .mark {{ font-family: monospace; font-weight: bold; }}
        .mark.amber {{ color: {p.yellow}; }}
        .mark.blue {{ color: {p.blue}; }}
        .mark.green {{ color: {p.green}; }}
        .mark.muted {{ color: {p.muted}; }}
        .name {{ font-family: monospace; }}
        .detail {{ color: {p.muted}; font-size: 0.85em; }}
        .attention {{ color: {p.accent}; font-size: 0.85em; font-weight: bold; }}
        .heading {{ color: {p.accent}; font-weight: bold;
                    font-family: monospace; }}
        .placeholder {{ color: {p.muted}; }}
        /* An offline chat, opened, and the bar over a terminal whose server
           went dark: the theme's panel and words, the red only on the dot
           -- the same ring the list's dark hosts wear. */
        .offline-title {{ color: {p.foreground}; font-size: 1.3em;
                          font-weight: 600; }}
        .offline-status {{ color: {p.foreground}; }}
        .offline-trying {{ font-style: italic; }}
        .session-banner {{ background: {p.panel}; color: {p.foreground};
                           border-bottom: 1px solid {border};
                           padding: 6px 12px; }}
        .session-banner .banner-head {{ font-weight: 600; }}
        .session-banner .banner-text {{ color: {legible(p.muted, (p.panel,))};
                                        font-size: 0.9em; }}
        .tab {{ color: {p.accent}; font-size: 0.8em;
                font-family: monospace; }}
        /* The alt-1..9 numbers: a small pill on the tile's corner, drawn
           only while alt is held (decision E) so the row is the design's
           the rest of the time and nothing reflows when it appears. */
        .slot-num {{ background: alpha({p.background}, 0.88);
                     color: {legible(p.foreground, (p.background,))};
                     font-family: {MONO}; font-size: 0.72em; font-weight: bold;
                     border-radius: 4px; padding: 0 3px; opacity: 0; }}
        .alt-held .slot-num {{ opacity: 1; }}
        .slot-num.slot-open {{ color: {legible(p.accent, (p.background,))}; }}
        .destructive {{ color: {p.red}; }}
        /* The hover bin sits over the time rather than beside it, so it
           takes no width on rows it is not shown on; muted until it is the
           thing under the pointer, red only then. */
        .kill {{ color: {row_muted}; min-width: 22px; min-height: 22px;
                 padding: 0; margin: 0; background: transparent;
                 box-shadow: none; }}
        .kill:hover {{ color: {p.red}; }}

        /* -- phase 2: the New chat screen ------------------------------ */
        .newchat {{ background: {p.background}; }}
        .nc-crumb {{ color: {row_muted}; font-family: {MONO};
                     font-size: 0.85em; }}
        .nc-heading {{ color: {p.foreground}; font-size: 1.9em;
                       font-weight: 700; }}
        .nc-card {{ background: {p.panel}; border: 1px solid {border};
                    border-radius: 16px; padding: 14px 12px 12px 12px;
                    box-shadow: 0 18px 50px alpha(black, 0.25); }}
        .nc-text, .nc-text text {{ background: transparent;
                                   color: {p.foreground}; font-size: 1.08em; }}
        .nc-placeholder {{ color: {field_muted}; font-size: 1.08em; }}
        .nc-chip {{ border: 1px solid {border}; border-radius: 9px;
                    background: transparent; color: {p.foreground};
                    min-height: 30px; padding: 0 9px; box-shadow: none;
                    font-size: 0.9em; }}
        .nc-chip > button {{ background: transparent; border: none;
                             box-shadow: none; padding: 0 2px;
                             min-height: 30px; color: inherit; }}
        .nc-chip .nc-chip-main.mono {{ font-weight: 600; }}
        .nc-chip .nc-chip-extra {{ color: {row_muted}; font-family: {MONO};
                                   font-size: 0.92em; }}
        .nc-toggle {{ color: {p.muted}; }}
        .nc-toggle:checked {{ background: {p.surface}; color: {p.foreground};
                              border-color: {mix(p.foreground, p.panel, 0.55)}; }}
        .nc-start {{ min-height: 34px; padding: 0 14px; }}
        .nc-seg {{ border: 1px solid {border}; border-radius: 10px;
                   padding: 3px; }}
        .nc-seg-btn {{ background: transparent; border: none;
                       box-shadow: none; color: {p.muted}; min-height: 28px;
                       padding: 0 10px; border-radius: 7px; font-size: 0.9em; }}
        .nc-seg-btn:checked {{ background: {p.surface}; color: {p.foreground};
                               font-weight: 500; }}
        .nc-starts {{ color: {row_muted}; font-size: 0.9em; }}
        .nc-status {{ color: {p.red}; font-size: 0.92em; }}
        .nc-recent {{ border: 1px solid {border}; border-radius: 12px;
                      padding: 10px 14px; background: {p.panel}; }}
        .nc-recent:hover {{ background: {card}; }}
        .nc-command {{ border: 1px solid {border}; border-radius: 12px;
                       padding: 10px 14px; color: {p.muted};
                       font-size: 0.9em; }}
        .nc-command-text {{ color: {p.foreground}; font-size: 0.9em; }}
        .nc-menu-project {{ color: {p.muted}; font-size: 0.85em;
                            font-weight: 600; }}
        .tone-amber {{ color: {amber_text}; }}
        .tone-blue {{ color: {blue_text}; }}

        /* -- quick add ------------------------------------------------- */
        window.quick-window {{ background: transparent; }}
        .quick {{ background: {p.panel}; border: 1px solid {border};
                  border-radius: 14px; }}
        .quick-line {{ padding: 14px 14px 12px 14px;
                       border-bottom: 1px solid {p.surface}; }}
        .quick-token {{ background: {p.surface}; border: 1px solid {border};
                        border-radius: 8px; min-height: 30px; padding: 0 10px;
                        box-shadow: none; color: {p.foreground}; }}
        .quick-token-label {{ font-weight: 700; font-size: 0.92em; }}
        .quick-entry {{ background: transparent; border: none;
                        box-shadow: none; outline: none; font-size: 1.12em;
                        color: {p.foreground}; min-height: 32px; }}
        .quick-entry > text > placeholder {{ color: {field_muted}; }}
        .quick-mode {{ border: 1px solid {border}; border-radius: 6px;
                       padding: 2px 7px; color: {p.muted}; font-size: 0.82em;
                       font-family: {MONO}; }}
        .quick-row {{ padding: 9px 16px; border-bottom: 1px solid {p.surface}; }}
        .quick-meta {{ color: {server_text}; font-size: 0.9em; }}
        .quick-status {{ color: {p.red}; font-size: 0.9em; }}
        .quick-hints {{ color: {row_muted}; font-size: 0.78em;
                        border-bottom: none; }}

        /* -- the ctrl-k palette ---------------------------------------- */
        .palette-dim {{ background: alpha(black, 0.45); }}
        .palette {{ background: {p.panel}; border: 1px solid {border};
                    border-radius: 14px;
                    box-shadow: 0 30px 80px alpha(black, 0.5); }}
        .palette-top {{ padding: 12px 16px;
                        border-bottom: 1px solid {p.surface}; }}
        .palette-icon {{ color: {p.muted}; }}
        .palette-entry {{ font-size: 1.15em; color: {p.foreground};
                          background: transparent; min-height: 30px; }}
        .palette-entry > placeholder {{ color: {field_muted}; }}
        .palette-list {{ background: transparent; padding: 4px 8px 8px 8px; }}
        .palette-list row {{ border-radius: 10px; color: {p.foreground}; }}
        .palette-list row.palette-head {{ padding: 10px 10px 4px 10px; }}
        .palette-list row.palette-head:hover {{ background: transparent; }}
        .palette-list row.palette-row {{ padding: 7px 10px; }}
        .palette-list row.palette-row:hover {{ background: alpha({card}, 0.6); }}
        .palette-list row.palette-row:selected {{ background: {card};
            box-shadow: inset 0 0 0 1px {border}; color: {p.foreground}; }}
        .palette-title {{ color: {p.foreground}; font-weight: 500; }}
        .palette-sub {{ color: {server_text}; font-size: 0.82em; }}
        .palette-peek {{ background: {p.background}; color: {p.foreground};
                         font-size: 0.8em; padding: 10px 14px;
                         border-top: 1px solid {p.surface}; }}
        .palette-empty {{ color: {p.muted}; padding: 18px 18px; }}
        .palette-foot {{ color: {row_muted}; font-size: 0.78em;
                         padding: 9px 16px;
                         border-top: 1px solid {p.surface}; }}

        /* -- phase 3: the chrome around an open chat ------------------- */
        .chat-header {{ background: {p.panel}; padding: 9px 14px;
                        border-bottom: 1px solid {p.surface}; }}
        .chat-header-title {{ color: {p.foreground}; font-weight: 600;
                              font-size: 1.05em; }}
        .chat-header-meta {{ color: {row_muted}; font-family: {MONO};
                             font-size: 0.8em; }}
        .host-chip {{ border: 1px solid {border}; border-radius: 6px;
                      padding: 0 6px; font-size: 0.8em; }}
        .pill {{ border-radius: 6px; padding: 1px 7px; font-size: 0.8em;
                 font-weight: 600; }}
        .pill-amber {{ background: alpha({p.yellow}, {CHIP_TINT});
                       color: {amber_tint_text}; }}
        .pill-blue {{ background: alpha({p.blue}, 0.16);
                      color: {legible(p.blue, (mix(p.panel, p.blue, 0.16),))}; }}
        .pill-muted {{ background: {p.surface}; color: {p.muted}; }}
        .recap-strip {{ background: {mix(p.panel, p.background, 0.5)};
                        color: {server_text}; font-size: 0.88em;
                        padding: 7px 14px;
                        border-bottom: 1px solid {p.surface}; }}
        .reply {{ background: {p.panel}; border-top: 1px solid {p.surface};
                  padding: 8px 14px 8px 14px; }}
        .reply-text, .reply-text text {{ background: transparent;
                                         color: {p.foreground}; }}
        .reply-text {{ min-height: 22px; }}
        .reply-placeholder {{ color: {field_muted}; }}
        .reply-note {{ color: {row_muted}; font-size: 0.78em; }}
        .send {{ background: {p.foreground}; color: {p.background};
                 border-radius: 8px; min-width: 28px; min-height: 28px;
                 padding: 0; box-shadow: none; border: none; }}

        /* -- Chats | Notes, and the Notes view ------------------------- */
        .mode-tabs {{ border-bottom: 1px solid {p.surface}; }}
        .mode-tab {{ background: transparent; border: none; box-shadow: none;
                     color: {p.muted}; border-radius: 0; min-height: 28px;
                     padding: 0 10px; font-size: 0.9em;
                     border-bottom: 2px solid transparent; }}
        .mode-tab:hover {{ color: {p.foreground}; background: transparent; }}
        .mode-tab:checked {{ color: {p.foreground}; background: transparent;
                             font-weight: 600;
                             border-bottom: 2px solid {p.foreground}; }}
        .mode-count {{ color: {row_muted}; font-family: {MONO};
                       font-size: 0.8em; font-weight: normal; }}
        .note-card {{ background: {mix(p.panel, p.surface, 0.35)};
                      border: 1px solid {border}; border-radius: 12px;
                      padding: 11px 12px; }}
        .note-card.note-done .note-text {{ color: {dim_title}; }}
        .note-text {{ color: {p.foreground}; }}
        .note-meta {{ color: {row_muted}; font-size: 0.82em; }}
        .note-tag {{ background: {p.surface}; color: {p.muted};
                     font-family: {MONO}; font-size: 0.7em; font-weight: bold;
                     border-radius: 5px; padding: 1px 6px;
                     letter-spacing: 0.06em; }}
        .note-start {{ background: {p.foreground}; color: {p.background};
                       border-radius: 8px; min-height: 28px; padding: 0 10px;
                       font-size: 0.85em; font-weight: 600; box-shadow: none;
                       border: none; }}
        .note-start:hover {{ background: {mix(p.foreground, p.panel, 0.1)}; }}
        .note-link {{ background: {p.surface}; border: 1px solid {border};
                      color: {p.foreground}; border-radius: 8px;
                      min-height: 28px; padding: 0 10px; font-size: 0.85em;
                      box-shadow: none; }}
        .notes-state {{ color: {p.muted}; font-size: 0.85em; }}
        .notes-board {{ background: {p.background}; }}
        .notes-heading {{ color: {p.foreground}; font-size: 1.6em;
                          font-weight: 700; }}
        .note-composer {{ background: {p.panel}; border: 1px solid {border};
                          border-radius: 14px; padding: 12px 14px 10px 14px; }}
        .note-composer:focus-within {{ border-color: {p.accent}; }}
        .note-write, .note-write text {{ background: transparent;
                                         color: {p.foreground}; }}
        .note-editing {{ border-color: {p.accent}; }}
        .note-placeholder {{ color: {field_muted}; }}
        .note-where > button {{ border: 1px solid {border}; border-radius: 9px;
                                background: transparent; box-shadow: none;
                                min-height: 28px; font-size: 0.88em;
                                color: {p.foreground}; }}
        .notes-project-name {{ color: {p.foreground}; font-weight: 600; }}
        .notes-tree row:selected .mode-count {{ color: inherit; }}
        .notes-tree row {{ border-radius: 6px; margin: 0 6px; }}
        .notes-note .note-row-title {{ color: {p.foreground};
                                       font-size: 0.92em; }}
        .notes-note.note-done .note-row-title {{ color: {dim_title}; }}
        .tree-chevron, .tree-icon {{ color: {row_muted}; }}
        .tree-add {{ min-height: 20px; min-width: 20px; padding: 0;
                     color: {row_muted}; opacity: 0.6; }}
        .tree-add:hover {{ opacity: 1; color: {p.foreground}; }}
        .note-head {{ padding: 12px 18px; background: {p.panel};
                      border-bottom: 1px solid {p.surface}; }}
        .note-crumb {{ color: {p.muted}; }}
        .note-title {{ color: {p.foreground}; font-weight: 700; }}
        .note-editor, .note-editor text {{ background: {p.background};
                                           color: {p.foreground}; }}
        .note-editor {{ font-family: {MONO}; }}
        .note-statusbar {{ background: {p.panel};
                           border-top: 1px solid {p.surface};
                           padding: 4px 14px; color: {row_muted};
                           font-family: {MONO}; font-size: 0.78em; }}
        """
        self.style_css = css    # kept for the tests' contrast checks
        provider = Gtk.CssProvider()
        provider.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        # The project tiles, one rule per project, reloaded whenever
        # projects.toml is read -- see _tile_style().
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self.tile_css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # -- the list ----------------------------------------------------------

    def load_hosts(self, connect: bool = True, force: bool = False) -> None:
        self.order = arrange(hosts.list_hosts(), read_order())
        for host in self.order:
            self.rows.setdefault(host, hosts.blank(host))
        for gone in [h for h in self.rows if h not in self.order]:
            del self.rows[gone]

        # projects.toml is read with the hosts, so the refresh button is also
        # how an edit to it lands. A host in no project is its own project,
        # so a missing file is simply the old one-group-per-server list.
        self.projects = chats.load_projects(chats.PROJECTS_FILE,
                                            hosts=self.order)
        # A filter naming a project or server that has gone would leave the
        # list empty for no visible reason. Dropped here, in memory only: a
        # projects.toml that failed to parse once should not cost the saved
        # filter for good.
        self.filters = self.filters.valid_for(self.projects)
        self._tile_style()
        self.shape = []     # tile classes may have moved; rebuild the rows

        # First run: everything you already had stays where it was, and only
        # servers added from here on arrive quiet. Upgrading should not empty
        # your sidebar and ask you to rebuild it.
        stars = read_stars()
        if stars is None:
            self.starred = set(self.order)
            save_stars(self.starred)
        else:
            self.starred = {h for h in stars if h in self.order}

        self.render()
        if connect:
            self.connect_hosts(force)

    def connect_hosts(self, force: bool = False) -> None:
        """Open the ssh: a watch stream per host, and a probe of each.

        That is two connections a host going off at the same instant, and on
        a fresh boot none of them can ride another's ControlPath socket
        because none exists yet -- so every one authenticates for itself.
        Against a locked key that is a passphrase prompt each, a dozen of
        them stacked up, which is what a reboot looked like. AddKeysToAgent
        does not save you: they are all launched before the first has
        finished asking.

        So this waits for the agent to hold something. The refresh button
        calls it regardless, which is the way out if there is no agent to
        wait for.
        """
        self.holding = False
        for host in self.order:
            if self.watched(host):      # the rest are polled, never streamed
                self.start_stream(host)
        self.sweep(force)

    def listed(self, shown: list, now: float) -> list[tuple]:
        """The sidebar, top to bottom, as (kind, ...) items: the chat list,
        or in the Servers tab the server list (servers_listed()).

        One function so the shape comparison and the widget build cannot
        drift apart -- they walked the same list twice before, and that is
        the sort of thing that silently stops matching.
        """
        if self.sidebar_mode == "servers":
            return self.servers_listed()
        return self.chats_listed(shown, now)

    def chats_listed(self, shown: list, now: float) -> list[tuple]:
        """The Chats tab: the filter summary while a filter is on, then each
        section (Starred, Today, Yesterday, Earlier this week) and Older,
        folded unless opened or searched; the empty state when there is
        nothing left to show.

        A search opens Older for as long as it is typed: hiding the old chat
        you are actively searching for would be the one moment the
        arrangement is wrong.
        """
        out: list[tuple] = []
        query = self.query()
        summary = chats.filter_summary(self.filters, self.projects)
        if summary:
            out.append(("summary", summary))
        for label, members in self.chat_sections(shown, now):
            if label == "Older" and not self.filters.by_project:
                opened = self.showing_older or bool(query)
                out.append(("fold", "older", len(members), opened))
                if not opened:
                    continue
            else:
                out.append(("section", label))
            # Offline is part of the shape: such a row is built without its
            # kill or star (see _chat_row).
            out.extend(("chat", chat.id, chat.project, chat.offline)
                       for chat in members)
        if not shown:
            out.append(("empty", chats.empty_text(self.filters, self.projects,
                                                  query)))
        return out

    def servers_listed(self) -> list[tuple]:
        """The Servers tab: every server in self.order -- watched live or
        not, the difference is only how often it is asked -- each with its
        sessions under it when it is opened out, then the row that adds one.

        Folded by default: a server followed by every tmux session on it
        read as a list of far more servers than there are. A search opens
        the servers with a matching session for as long as it is typed,
        without touching what is remembered (server_open()).

        The server row's state, which fix it offers, whether it is open and
        whether it has anything to open are part of the shape, so a server
        going dark is rebuilt with its button; uptime, disk and the count
        are words, painted by repaint() like a session's detail. A search
        keeps the servers with a name or a session that matches
        (server_matches(), the same test matches() hides rows by).
        """
        out: list[tuple] = []
        query = self.query()
        for host in self.order:
            if query and not self.server_matches(host, query):
                continue
            sessions = self.rows[host]["sessions"]
            opened = bool(sessions) and self.server_open(host, query)
            out.append(("server", host, self.server_state(host),
                        self.server_fix(host), opened, bool(sessions)))
            if opened:
                out.extend(("session", host, session["name"])
                           for session in sessions)
        if query and not out:
            out.append(("empty-servers", f"No server or session matches "
                                         f"\u201c{query}\u201d."))
        out.append(("add",))
        return out

    def server_open(self, host: str, query: str = "") -> bool:
        """Whether a server's sessions are showing: opened out by hand, or
        one of them is what is being searched for. A match on the server's
        own name does not open it -- that search has found the server."""
        if host in self.expanded:
            return True
        needle = query.strip().lower()
        if not needle or host in self.folded_in_search(needle):
            return False
        return any(needle in session["name"].lower()
                   for session in self.rows.get(host, {}).get("sessions", []))

    def folded_in_search(self, needle: str) -> set[str]:
        """The servers folded by hand under this search. A new search
        starts with none: what the last one folded says nothing about
        what this one turns up."""
        searched, hosts = self.search_folded
        if searched != needle:
            hosts = set()
            self.search_folded = (needle, hosts)
        return hosts

    def open_out(self, host: str) -> None:
        """Make a server's sessions show, without drawing. One the search
        has opened stays a search's opening -- remembered only by the
        search, so clearing it folds the server again; anything else is
        opened as by hand, and remembered across restarts."""
        needle = self.query().lower()
        self.folded_in_search(needle).discard(host)
        if not self.server_open(host, needle):
            self.expanded.add(host)
            save_expanded(self.expanded)

    def set_expanded(self, host: str, opened: bool) -> bool:
        """Open a server out in the Servers tab, or fold it, and remember.

        The highlight goes to the server when it folds -- a session row
        that has just gone cannot keep it -- and the list keeps the keyboard
        if it had it, since the rows under it are new widgets."""
        # By what is showing, not by what is remembered: during a search a
        # server can be open without being in self.expanded, and its chevron
        # and Left must fold it all the same.
        needle = self.query().lower()
        if self.server_open(host, needle) == opened:
            return False
        had_focus = self.within(self.list)
        if opened:
            self.open_out(host)
        else:
            if host in self.expanded:
                self.expanded.discard(host)
                save_expanded(self.expanded)
            if self.server_open(host, needle):
                # Still open through the search: folded for this search
                # only, so the remembered state is left as it was.
                self.folded_in_search(needle).add(host)
        selected = self.list.get_selected_row()
        on_it = (getattr(selected, "server", None) == host
                 or (getattr(selected, "key", None) or ("",))[0] == host)
        self.render()
        if not opened and on_it:
            # render() could not find the session row that had the
            # highlight and, for the session showing, promised it the
            # highlight back when its row returns. Folding is a deliberate
            # move to the server: opening it again must not jump down to it.
            self.pending = None
        if on_it and self.list.get_selected_row() is None:
            self.select_server(host)
        if had_focus:
            self.focus_list()
        return True

    def toggle_server(self, host: str) -> None:
        """The chevron on a server row."""
        self.set_expanded(host, not self.server_open(host, self.query()))
        self.select_server(host)

    def fold_key(self, opened: bool) -> bool:
        """Right / plus and Left / minus on the Servers tab's list: open the
        highlighted server out, or fold it. Left on a session folds the
        server it is under and lands on it, so Left twice from anywhere in
        a group gets you back to the list of servers."""
        row = self.list.get_selected_row()
        host = getattr(row, "server", None)
        if host is None:
            key = getattr(row, "key", None)
            if key is None or opened:
                return True     # nothing to open from here; the key is ours
            host = key[0]
            self.set_expanded(host, False)
            self.select_server(host)
            if self.within(self.list):
                self.focus_list()
            return True
        self.set_expanded(host, opened)
        return True

    def server_matches(self, host: str, query: str) -> bool:
        """Whether a search turns up anything under this host: the host's own
        name, or one of its sessions' -- what matches() keeps on screen."""
        needle = query.strip().lower()
        if needle in host.lower():
            return True
        return any(needle in session["name"].lower()
                   for session in self.rows.get(host, {}).get("sessions", []))

    # A server's state in plain words, for the Servers tab. "changed" is not
    # a row state of its own: it is "unverified" with row["keychanged"] (see
    # hosts.key_changed), and "checking" is "unknown" -- not asked yet, or
    # asked again by Check again.
    SERVER_WORDS = {"up": "up", "nokey": "no key",
                    "unverified": "new host key",
                    "changed": "host key changed", "down": "can't reach",
                    "checking": "checking"}
    # The one obvious fix per broken state: (kind, button label, tooltip).
    SERVER_FIXES = {
        "nokey": ("install", "Install my key",
                  "It answers but will not let you in without a password. "
                  "Opens a terminal running ssh-copy-id: type the password "
                  "once, and every connection after that uses your key."),
        "unverified": ("trust", "Trust\u2026",
                       "New server: ssh does not know its key yet. Opens a "
                       "terminal where ssh shows the fingerprint and you "
                       "answer yes or no yourself."),
        "changed": ("checkkey", "Check new key\u2026",
                    "The key it offers is not the one on file. Opens a "
                    "terminal showing both fingerprints; the old one is "
                    "only removed if you type y there."),
        "down": ("recheck", "Check again",
                 "Ask it again now, rather than at the next sweep."),
    }

    def server_state(self, host: str) -> str:
        data = self.rows.get(host, {})
        state = data.get("state", "unknown")
        if state == "unverified" and data.get("keychanged"):
            return "changed"
        if state not in self.SERVER_WORDS:
            return "checking"
        return state

    def server_fix(self, host: str) -> str | None:
        fix = self.SERVER_FIXES.get(self.server_state(host))
        return fix[0] if fix else None

    def server_words(self, host: str) -> tuple[str, str]:
        """The second line of a server row, and its tooltip.

        Up: how long, and how full its disk is -- the two numbers worth a
        glance. Anything else: what ssh said, which is the difference between
        a box that is off and a name that no longer resolves -- shortened on
        the row, whole under the pointer.
        """
        data = self.rows.get(host, {})
        state = self.server_state(host)
        words = self.SERVER_WORDS[state]
        if state == "up" and self.watched(host):
            words = "up, live"
        bits = [words]
        tip = [f"{host}: {words}"]
        if data.get("target"):
            tip.append(data["target"])
        if state == "up":
            if data.get("uptime"):
                bits.append(data["uptime"].split(",")[0])
                tip.append(f"up {data['uptime']}")
            disk = data.get("disk", "")
            if disk:
                pct = disk.rsplit("(", 1)[-1].rstrip(")") if "(" in disk else disk
                bits.append(f"disk {pct}")
                tip.append(f"disk {disk}")
            if data.get("load"):
                tip.append(f"load {data['load']}")
        elif data.get("error") and state != "checking":
            # A key problem's reason opens with the state word again ("host
            # key changed -- ...", "new server -- ..."), so on the row it
            # would only repeat itself; the whole of it is in the tooltip.
            if state not in ("unverified", "changed"):
                bits.append(data["error"].split(" -- ")[0])
            tip.append(data["error"])
        return " \u00b7 ".join(bits), "\n".join(tip)

    def server_summary(self, host: str) -> str:
        """What is on a server, for the right of its row: "4 chats \u00b7 1
        shell", the parts that are not zero -- the count of what its fold
        hides. "no sessions" only for a server that answered with none; one
        not heard from has nothing to report, not nothing on it."""
        n = self.server_chats(host)
        shells = sum(1 for session in self.rows.get(host, {}).get(
                         "sessions", [])
                     if session["agent"]["state"] == agent_state.SHELL)
        bits = []
        if n:
            bits.append(f"{n} chat" + ("s" if n != 1 else ""))
        if shells:
            bits.append(f"{shells} shell" + ("s" if shells != 1 else ""))
        if bits:
            return " \u00b7 ".join(bits)
        return "no sessions" if self.server_state(host) == "up" else ""

    def server_chats(self, host: str) -> int:
        """Chats on a server, the remembered offline ones included -- the
        count the Server chip would filter down to."""
        return sum(1 for chat in self.chats if chat.host == host)

    def query(self) -> str:
        box = getattr(self, "filter", None)
        return box.get_text().strip() if box is not None else ""

    def host_dark(self, host: str) -> bool:
        """Answered with a refusal or not at all -- as opposed to not asked
        yet, which is not the same thing and is not drawn red."""
        return self.rows.get(host, {}).get("state") in ("down", "nokey",
                                                         "unverified")

    def host_tone(self, host: str) -> str:
        if self.rows.get(host, {}).get("state") == "up":
            return "green"
        return "red" if self.host_dark(host) else "muted"

    @staticmethod
    def _machine_name(row: dict) -> str:
        """The far machine's own hostname, as best it can be told.

        tmux's default pane title is the hostname, and a chat whose agent
        has not titled it yet would otherwise be called "data" or whatever
        the box is named. The probe reports the hostname (row["hostname"]);
        this is the fallback for a box renamed since its tmux server started,
        and for a row from before the probe asked: every plain shell on the
        host is showing the name tmux used, so the title most of them share
        is one to ignore too.
        """
        titles = [session.get("pane_title", "")
                  for session in row.get("sessions", [])
                  if session.get("agent", {}).get("state") == agent_state.SHELL
                  and session.get("pane_title")]
        return max(set(titles), key=titles.count) if titles else ""

    def build_chats(self, now: float) -> list:
        """self.rows as chats, with what this window knows that the servers
        have not echoed back yet, and the recency stamps that are now due.

        A stamp is put in memory at once and written behind it, so a chat
        that starts waiting on you moves to the top in this render -- not one
        ssh round trip later. Stamps go only to hosts that answered: a dark
        host's chats are not in the rows anyway, and one that just stopped
        answering would only collect failures.
        """
        # The probe reports the machine's hostname; the title the plain
        # shells share is kept as a second guess, for a box renamed since its
        # tmux server started (panes keep the name they were born with).
        rows = {host: dict(self.rows[host],
                           hostnames=(self._machine_name(self.rows[host]),))
                for host in self.order}
        # A rename or a move the server has not echoed back yet is laid over
        # its options, so the title, the tile and the filters follow now; a
        # wish the server now agrees with is done with.
        raw = {chats.chat_id(host, session.get("name", "")):
               session.get("options") or {}
               for host in self.order for session in self.rows[host]["sessions"]}
        overrides: dict[str, dict] = {}
        for book, key in ((self.name_wish, "name"),
                          (self.project_wish, "project"),
                          (self.autotitle_wish, "autotitle"),
                          (self.autotitle_for_wish, "autotitle_for")):
            for cid, value in list(book.items()):
                if (raw.get(cid, {}).get(key) or "") == (value or ""):
                    del book[cid]
                    continue
                overrides.setdefault(cid, {})[key] = value
        up = {h for h in self.order if self.rows[h]["state"] == "up"}
        self.remember(rows, up, now)
        built = chats.build_chats(rows, self.projects, overrides=overrides,
                                  known=self.known)
        for chat in built:
            if chat.offline:
                chat.preview = chats.offline_text(
                    chat.seen, now, self.rows[chat.host]["state"])
            mine = self.stamps.get(chat.id)
            if mine is not None:
                if chat.stamped and chat.last_event >= mine:
                    del self.stamps[chat.id]       # the server has caught up
                else:
                    chat.last_event, chat.stamped = mine, True
            wish = self.star_wish.get(chat.id)
            if wish is not None:
                if chat.starred == wish:
                    del self.star_wish[chat.id]    # landed, or set elsewhere
                chat.starred = wish

        live = [chat for chat in built if chat.host in up]
        # Plain shells too, though they are not chats: an agent starting in
        # one is a chat starting now, and the tracker can only tell that from
        # a first sighting if it saw the shell.
        shells = {chats.chat_id(host, session.get("name", ""))
                  for host in up for session in self.rows[host]["sessions"]
                  if (session.get("agent") or {}).get("state")
                  == agent_state.SHELL}
        self.follow_chats(up, {chat.host for chat in live})
        self.recency.saw_shells(shells)
        due = self.recency.update_chats(live, now)
        for chat in live:
            stamp = due.get(chat.id)
            if stamp is None:
                continue
            chat.last_event, chat.stamped = stamp, True
            self.stamps[chat.id] = stamp
            # Held in memory either way, so the row moves now; written only
            # by one desktop conn. Two writers could not loop -- each stamps
            # only what it saw change, never the other's stamp -- but every
            # turn would be two ssh writes, the older landing last.
            if not self.defer_to_stable():
                self.writer.put(chat.host, chat.session, "last_event",
                                str(stamp))

        # Finished, not seen (decision D). After the stamps, so a finish has
        # its new last_event to compare with the seen book. The chat you are
        # looking at, in a window you are using, is seen as it finishes --
        # it must never flash amber at you.
        finished = self.finish.update(live)
        watching = self.visible_chat() if self.is_active() else None
        if watching is not None:
            chat = next((c for c in live if c.id == watching), None)
            if chat is not None and self.seen.get(watching) != chat.last_event:
                self.seen[watching] = chat.last_event
                chats.save_seen(self.seen, chats.SEEN_STATE)
        chats.mark_finished(built, self.finish, self.seen)
        self.just_finished |= finished
        # Nothing queued while the stable conn is the one titling: it titles
        # these itself, and a backlog worked through once it quits would
        # overwrite its titles with fresh, different ones -- the flip the
        # deferral is for. Chats with no title of ours stay due (retitle's
        # first-sight rule), so they are still asked for once it is gone.
        for chat in live if not self.defer_to_stable() else ():
            if chat.id not in self.retitle_queue and \
                    self.retitler.due(chat, chat.id in finished, now):
                self.retitle_queue.append(chat.id)
        self._retitle_next()

        # Forget chats that have gone, but not the ones on a host that is
        # only dark for now: when it answers again, a chat that changed while
        # it was away is a change, not a first sighting.
        self.chat_ids = ({chat.id for chat in live}
                         | {cid for cid in self.chat_ids
                            if cid.partition("/")[0] not in up
                            and cid.partition("/")[0] in self.rows})
        self.shell_ids = shells | {cid for cid in self.shell_ids
                                   if cid.partition("/")[0] not in up
                                   and cid.partition("/")[0] in self.rows}
        self.recency.retain(self.chat_ids | self.shell_ids)
        self.finish.retain(self.chat_ids)
        for book in (self.stamps, self.star_wish, self.name_wish,
                     self.project_wish, self.autotitle_wish,
                     self.autotitle_for_wish):
            for gone in [cid for cid in book if cid not in self.chat_ids]:
                del book[gone]
        # The seen book forgets only chats a host that answered no longer
        # lists: a dark host's chats are still there, just out of sight.
        stale = [cid for cid in self.seen
                 if cid.partition("/")[0] in up and cid not in self.chat_ids]
        for cid in stale:
            del self.seen[cid]
        if stale:
            chats.save_seen(self.seen, chats.SEEN_STATE)
        return built

    def remember(self, rows: dict, up: set, now: float) -> None:
        """Book what every answering host shows, for when it stops answering
        (chats.remember). Written when a host's chats change, else at most
        every KNOWN_FLUSH -- render runs on every frame."""
        changed = False
        for host in up:
            heard = max(float(self.rows[host].get("checked") or 0.0),
                        self.heard.get(host, 0.0))
            changed |= chats.remember(self.known, host, rows[host],
                                      heard or now)
        if changed or (up and now - self.known_written >= KNOWN_FLUSH):
            self.save_known(now)

    def save_known(self, now: float | None = None) -> None:
        chats.save_known(self.known, chats.KNOWN_STATE)
        self.known_written = time.time() if now is None else now

    def offline_refused(self, host: str, name: str,
                        attach: bool = False) -> bool:
        """Whether host/name is only a remembered chat, on a server that is
        not answering. If so, says so and asks the server again now -- its
        stream's backoff cut short, a probe -- instead of attaching or
        writing to a session nothing can reach.

        An offline chat is never attached to, whatever the host's row says
        meanwhile: "unknown" is also a recheck in flight, and a host that has
        just failed is asked again that way. Opening one (open_session) shows
        the offline page for it instead, and the open is owed: made when an
        answer brings the chat back live, while the page is still showing it
        (_owed_open). The wording differs for a host not heard from at all
        yet this run (a launch, or a notification that started the app).
        """
        chat = self.chat_index.get(chats.chat_id(host, name))
        if chat is None or not chat.offline:
            return False
        fresh = self.unheard(host)
        self._retry(host)
        state = self.rows.get(host, {}).get("state")
        connlog.limited(("offline", host),
                        f"offline: {host}/{connlog.clean(name, 60)} not "
                        f"attached, host {'unheard' if fresh else state}; "
                        "asking it now")
        self.notice(f"{host} has not answered yet -- opening when it does"
                    if attach and fresh
                    else f"{host} has not answered yet -- trying it now"
                    if fresh
                    else f"{host}'s host key has changed -- Check new key "
                         "in the Servers tab compares them"
                    if state == "unverified"
                    and self.rows.get(host, {}).get("keychanged")
                    else f"{host} needs you to log in -- a session on it "
                         "answers ssh's key prompt"
                    if state == "unverified"
                    else f"{host} refused the key -- trying it again now"
                    if state == "nokey"
                    else f"{host} is unreachable -- trying it again now")
        return True

    def dark_words(self, host: str) -> tuple[str, str]:
        """What is wrong with a server that is not letting you in, and what
        to do about it, from its row's state -- in the host row's terms:
        "no key" and "log in" answer and turn you away, which is not
        "unreachable", and waiting for them to come back fixes nothing.
        The second part is "" where there is nothing to add."""
        state = self.rows.get(host, {}).get("state")
        if state == "nokey":
            return (f"{host} refused the key",
                    "is the ssh agent unlocked?")
        if state == "unverified" and self.rows[host].get("keychanged"):
            return (f"{host}'s host key has changed",
                    "Check new key in the Servers tab compares them")
        if state == "unverified":
            return (f"{host} needs you to log in",
                    "ssh does not trust its key yet")
        if self.unheard(host):
            return f"{host} has not answered yet", ""
        return f"{host} is unreachable", ""

    def unheard(self, host: str) -> bool:
        """Not heard from at all yet this run, as opposed to refused."""
        row = self.rows.get(host, {})
        return (row.get("state") == "unknown" and not row.get("checked")
                and host not in self.heard)

    def _retry(self, host: str) -> None:
        """Ask a host again now: its stream's backoff cut short, a probe.

        Not recheck(): that repaints the row as being checked and says so in
        the footer, and the callers here say it their own way."""
        entry = self.streams.get(host)
        if entry is not None:
            entry[2].set()
        if host not in self.inflight:
            self.inflight.add(host)
            self.probed[host] = time.monotonic()
            threading.Thread(target=self._probe, args=(host,),
                             name=f"probe-{host}", daemon=True).start()

    def check_again(self, host: str, name: str) -> None:
        """The offline page's button: the retry offline_refused makes."""
        self._retry(host)
        self.paint_offline_page()

    def log_in(self, host: str, name: str) -> None:
        """The offline page's Log in, for a server ssh does not trust yet:
        the same New session the host's menu offers -- an interactive ssh
        you can answer the fingerprint prompt in, after which every probe
        rides the key you accepted. Not the chat itself: on a rebuilt
        server it may be gone, and tmux would make an empty one of its
        name."""
        self.prompt_new_session(host)

    def last_seen(self, host: str) -> float:
        """When the host last really answered, as far as anything here
        knows: the book's "seen", or the stream speaking since."""
        return max(float(self.known.get(host, {}).get("seen") or 0.0),
                   self.heard.get(host, 0.0))

    def show_offline(self, key: tuple[str, str]) -> None:
        """Point the right-hand side at an offline chat (OfflinePage), with
        the list's highlight on it, and owe it an open for when its server
        is back with it live."""
        if self.sidebar_mode == "notes":
            self.before_notes = None    # this is where it goes instead
            self.set_sidebar_mode("chats")
        self.offline_page.show_chat(key)
        self.open_when_up = key
        self.reveal(key)
        self.stack.set_visible_child(self.offline_page)
        self.select_key(key, promise=False)
        self.paint_offline_page()
        self.offline_page.retry.grab_focus()

    def offline_showing(self) -> bool:
        return (self.stack.get_visible_child() is self.offline_page
                and self.offline_page.key is not None)

    def paint_offline_page(self, now: float | None = None) -> None:
        page = self.offline_page
        if page.key is None:
            return
        now = time.time() if now is None else now
        host, name = page.key
        if host not in self.rows:
            # Forgotten while shown: nothing left to say anything about.
            page.key = None
            if self.stack.get_visible_child() is page:
                self.stack.set_visible_child(self.placeholder)
            return
        chat = self.chat_index.get(chats.chat_id(host, name))
        if chat is not None and chat.title:
            # Kept: once the server answers without it, the chat is gone
            # from the list, and the page still has to say which one ended.
            page.chat_title = chat.title
        title = page.chat_title or f"{host}/{name}"
        if page.ended:
            status = "This chat has ended"
            seen = f"{host} answered without it"
        else:
            status = " -- ".join(part for part in self.dark_words(host)
                                 if part)
            seen = chats.seen_text(chat.seen if chat is not None and chat.seen
                                   else self.last_seen(host), now)
        page.paint(title, status, seen, host in self.inflight,
                   self.host_dark(host),
                   login=(self.rows[host].get("state") == "unverified"
                          and not self.rows[host].get("keychanged")))

    def _banner_words(self, view: "Session", now: float) -> tuple[str, str]:
        host = view.host
        if view.dead and not view.offline:
            # Died with ssh's own "connection lost", the server not yet
            # known to be dark: the answer is on its way (_session_exited).
            return "Disconnected", f"checking {host}\u2026"
        if not view.offline:
            return "", ""
        # Worded from the state, as the host row is: a server that answers
        # and turns you away is not "unreachable", and only one that is
        # coming back can promise to reconnect by itself.
        state = self.rows.get(host, {}).get("state")
        status, hint = self.dark_words(host)
        parts = [status]
        if hint:
            parts.append(hint)
        seen = chats.seen_text(self.last_seen(host), now)
        if seen:
            parts.append(seen)
        if state == "unverified" and self.rows[host].get("keychanged"):
            head = "Host key changed"
        elif state == "unverified":
            parts.append("a new session on it logs in")
            head = "Needs log in"
        elif state == "nokey":
            parts.append("reconnects once it lets you in")
            head = "Key refused"
        else:
            parts.append("reconnects when it's back")
            head = "Offline"
        return ("Disconnected" if view.dead else head), " \u00b7 ".join(parts)

    def paint_banners(self, now: float | None = None) -> None:
        """Each open terminal's banner, from its server's state: shown once
        the server is dark, kept through a recheck ("unknown"), gone only
        when it answers again."""
        now = time.time() if now is None else now
        for view in self.open.values():
            if not isinstance(view, Session):
                continue
            state = self.rows.get(view.host, {}).get("state")
            if self.host_dark(view.host):
                view.offline = True
                # Seen dark: its coming back is a new return, with a
                # respawn of its own (_host_back).
                view.respawn_spent = False
            elif state == "up":
                view.offline = False
            view.set_banner(*self._banner_words(view, now))

    def render(self) -> None:
        """Bring the sidebar up to date, rebuilding it only if it has to.

        A screen changing is the common case, and it changes words, not
        structure. Rebuilding the rows for that destroys whatever the pointer
        or the keyboard was on -- a right-click menu is parented to a row, so
        it vanished a frame after opening -- so the shape is compared first
        and only a real change to it costs new widgets. A chat moving up a
        section is a change of shape; its spinner turning, its step text and
        its time ticking are not.
        """
        now = time.time()
        self.chats = self.build_chats(now)
        self.chat_index = {chat.id: chat for chat in self.chats}
        shown = chats.visible(self.chats, self.filters, self.query(),
                              self.projects)
        self.visible_chats = shown

        waiting = 0
        busy = 0
        unknown = 0
        # Counted across every host, watched live or not. A server checked
        # slowly is quieter, not unwatched, and a count that ignored half the
        # fleet would make the one number worth reading a lie.
        # A chat finished and not yet opened waits on you too (SPEC §3).
        waiting = sum(1 for chat in self.chats if chat.finished)
        for host in self.order:
            for session in self.rows[host]["sessions"]:
                if session["agent"]["state"] in (agent_state.NEEDS_YOU,
                                                 agent_state.DRAFT):
                    waiting += 1
                elif session["agent"]["state"] == agent_state.WORKING:
                    busy += 1
                elif session["agent"]["state"] == agent_state.UNKNOWN:
                    unknown += 1

        shape = self.listed(shown, now)
        # alt-1..9 counts the chat rows top-down, whichever tab is showing --
        # alt-3 is the same chat from both -- then, in the Servers tab, any
        # session there that is not a chat already. The number is the row's,
        # and a chat listed in both tabs has the one number in both.
        chat_shape = (shape if self.sidebar_mode != "servers"
                      else self.chats_listed(shown, now))
        slots: list[tuple[str, str]] = []
        for item in chat_shape:
            if item[0] == "chat":
                chat = self.chat_index[item[1]]
                slots.append((chat.host, chat.session))
        for item in shape:
            if item[0] == "session" and (item[1], item[2]) not in slots:
                slots.append((item[1], item[2]))
        self.slots = slots

        # And green is a claim, not a default: every host answering, every
        # screen recognised. Before the first probe lands, while a host is
        # dark, or while a session defies classification, the honest colour
        # is grey -- a fleet out of sight was being drawn in the same green
        # as a fleet with nothing to do, reassuring exactly when nothing was
        # known.
        live = sum(1 for host in self.order if self.rows[host]["state"] == "up")
        dark = sum(1 for host in self.order if self.host_dark(host))
        self.alert = (agent_state.NEEDS_YOU if waiting
                      else agent_state.WORKING if busy
                      else agent_state.UNKNOWN if unknown or live < len(self.order)
                      else agent_state.READY)
        gone = len(self.order) - live
        self.alert_text = (
            f"{waiting} waiting on you" if waiting
            else f"{busy} working" if busy
            else f"{unknown} unknown" if unknown
            else (f"{gone} host{'s' if gone != 1 else ''} dark" if gone
                  else "all clear"))
        self._paint_condition(live, dark, len(self.order) - live - dark)
        self.fleet_up.set_text(f"{live} up" + (" · " if dark else ""))
        self.fleet_dark.set_text(f"{dark} dark" if dark else "")
        self.fleet_dark.set_visible(bool(dark))
        self._sync_chips()
        self._paint_mode_tabs()

        said, until = self.said
        if time.monotonic() < until:
            self.footnote.set_text(said)     # a message that has not had its
        else:                                # few seconds yet outranks a count
            self.footnote.set_text(chats.footer_text(
                len(shown), len(self.chats), len(self.projects),
                self.filters.active() or bool(self.query())))

        self.announce()
        self.paint_banners(now)
        if self.offline_showing():
            self.paint_offline_page(now)

        if shape == self.shape:
            self.repaint(now)
            return

        # What the highlight is on, and what it was promised to and could not
        # have yet -- a session opened before its row existed. A highlight
        # moved by hand cancelled the promise the moment it moved -- see
        # row_selected() -- so whatever is still owed here is owed for real.
        chosen = None
        chosen_zone = None
        chosen_server = None
        row = self.list.get_selected_row()
        if row is not None:
            chosen = getattr(row, "key", None)
            chosen_zone = getattr(row, "zone", None)
            chosen_server = getattr(row, "server", None)
        wanted, self.pending = self.pending, None
        if wanted is not None and wanted not in self.open:
            # The view the promise was made for has since been closed: what
            # is left is a lost highlight, not a session owed a selection.
            wanted = None

        # Rows only. A right-click menu is parented to the list too (see
        # popup()), and ListBox.remove() refuses a child that is not a row:
        # "remove the first child until there is none" spun forever on an
        # open menu -- 100% CPU, the window frozen -- whenever the shape
        # changed under it.
        child = self.list.get_first_child()
        while child is not None:
            after = child.get_next_sibling()
            if isinstance(child, Gtk.ListBoxRow):
                self.list.remove(child)
            child = after
        self.widgets.clear()
        self.chat_widgets.clear()
        self.host_widgets.clear()

        numbers = {key: index + 1 for index, key in enumerate(slots)}
        sessions = {(host, s["name"]): s for host in self.order
                    for s in self.rows[host]["sessions"]}
        for item in shape:
            kind = item[0]
            if kind == "summary":
                self.list.append(self._summary_row(item[1]))
            elif kind == "section":
                self.list.append(self._section_row(item[1]))
            elif kind == "fold":
                self.list.append(self._fold_row(item[1], item[2], item[3]))
            elif kind == "empty":
                self.list.append(self._empty_row(item[1]))
            elif kind == "chat":
                chat = self.chat_index[item[1]]
                self.list.append(self._chat_row(
                    chat, numbers.get((chat.host, chat.session), 0), now))
            elif kind == "server":
                self.list.append(self._server_row(*item[1:]))
            elif kind == "empty-servers":
                self.list.append(self._note_row(item[1]))
            elif kind == "add":
                self.list.append(self._add_server_row())
            elif kind == "session":
                key = (item[1], item[2])
                self.list.append(self._session_row(item[1], sessions[key],
                                                   numbers.get(key, 0)))
        self.shape = shape

        if wanted is not None:
            self.select_key(wanted)     # owed again if the row is still missing
        if self.list.get_selected_row() is None and chosen_server is not None:
            # A server row has no session key; it is found by its host.
            self.select_server(chosen_server)
        if self.list.get_selected_row() is None and chosen is not None:
            if self.pending is None:
                # A restore only re-arms the promise for the session still
                # on screen -- its row will be back and is owed the
                # highlight. Any other vanished row -- killed session,
                # folded host -- is a highlight lost, not a session owed:
                # armed here, it fired whenever a row by that name came
                # back, however much later.
                showing = self.stack.get_visible_child()
                self.select_key(chosen,
                                promise=(isinstance(showing, Session)
                                         and (showing.host, showing.name)
                                         == chosen),
                                zone=chosen_zone)
                if (self.list.get_selected_row() is None
                        and self.offline_showing()
                        and self.offline_page.key == chosen):
                    # The page's chat moved rather than went: follow it.
                    self.select_key(chosen, promise=False)
            else:
                # The promise stands. The user's highlight is put back
                # without settling it -- select_key would -- so the pending
                # row still takes over when it finally arrives.
                row = self.row_for(chosen, chosen_zone)
                if row is not None:
                    self.selecting = True
                    self.list.select_row(row)
                    self.selecting = False

    def _paint_condition(self, up: int, dark: int, unheard: int) -> None:
        total = up + dark + unheard
        if not total:
            total, unheard = 1, 1
        heights = {"green": round(CONDITION_HEIGHT * up / total),
                   "red": round(CONDITION_HEIGHT * dark / total)}
        heights["muted"] = max(0, CONDITION_HEIGHT - heights["green"]
                               - heights["red"])
        for tone, part in self.condition_parts.items():
            part.set_size_request(4, heights[tone])
            part.set_visible(heights[tone] > 0)
        self.condition.set_tooltip_text(
            f"{chats.fleet_summary(up, dark)}"
            + (f", {unheard} not heard from yet" if unheard and up + dark
               else "") + f" -- {self.alert_text}")

    def _sync_chips(self) -> None:
        """The chips and the New chat button, brought into line with the
        filters. Under self.syncing, so setting the toggle is not a click."""
        name = next((p.name for p in self.projects
                     if p.id == self.filters.project), "")
        self.syncing = True
        try:
            self.project_chip.label.set_text(name or "Project")
            self.server_chip.label.set_text(self.filters.server or "Server")
            for chip, on in ((self.project_chip, bool(self.filters.project)),
                             (self.server_chip, bool(self.filters.server))):
                if on:
                    chip.add_css_class("on")
                else:
                    chip.remove_css_class("on")
            if self.needs_chip.get_active() != self.filters.needs_you:
                self.needs_chip.set_active(self.filters.needs_you)
            if self.group_chip.get_active() != self.filters.by_project:
                self.group_chip.set_active(self.filters.by_project)
            if self.filters.by_project:
                self.group_chip.add_css_class("on")
            else:
                self.group_chip.remove_css_class("on")
            self.needs_count.set_text(
                str(chats.needs_count(self.chats, self.filters)))
        finally:
            self.syncing = False
        self.new_label.set_text(chats.new_chat_label(self.filters,
                                                     self.projects))

    def set_filters(self, filters) -> None:
        """Apply new filters, and remember them for the next launch."""
        self.filters = filters
        chats.save_filters(filters, chats.FILTERS_STATE)
        self.render()
        self._paint_mode_tabs()
        if self.sidebar_mode == "notes":
            self.notes_pane.render()

    def tick(self) -> bool:
        self.render()
        return GLib.SOURCE_CONTINUE

    def _tile_style(self) -> None:
        """One CSS rule per project tile, from tile_colours(). Classes, not
        inline colours: a tile is drawn in several places (rows, the project
        menu) and the theme's contrast fixes are worked out once."""
        rules = []
        self.tile_class = {}
        for index, project in enumerate(self.projects):
            name = f"tile-p{index}"
            self.tile_class[project.id] = name
            background, letter = tile_colours(self.palette, project, index)
            rules.append(f".tile.{name} {{ background: {background}; "
                         f"color: {letter}; }}")
            background, letter = tile_colours(self.palette, project, index,
                                              idle=True)
            rules.append(f".chat-idle .tile.{name} {{ background: "
                         f"{background}; color: {letter}; }}")
        self.tile_css.load_from_data("\n".join(rules).encode())

    def announce(self) -> None:
        """Say, once, when a session starts waiting on you.

        The panel is honest about who is blocked, but only to someone looking
        at it -- and the thing worth knowing is precisely that a chat has been
        sitting there while you did something else. So it says so out loud on
        the edge into needs-you, and not again until it has been something
        else in between.

        The first sweep only records: everything already waiting when conn
        opens is on screen, and five notifications for it would be noise.

        A blocked chat is worth saying the moment it happens. A draft is not,
        which is what draft_settled() is for.
        """
        first = not self.was
        now = time.monotonic()
        for host in self.order:
            for session in self.rows[host]["sessions"]:
                key = (host, session["name"])
                state = session["agent"]["state"]
                before = self.was.get(key)
                self.was[key] = state
                fresh = first or before is None

                if state == agent_state.DRAFT:
                    # The box text, not the detail: the detail's background
                    # suffix ticks every frame, and a clock keyed on it
                    # never settles while a fleet runs.
                    if self.draft_settled(key, session["agent"]["draft"],
                                          now, silent=fresh):
                        self.notify(host, session)
                    continue

                self.drafts.pop(key, None)
                if fresh or before == state or state != agent_state.NEEDS_YOU:
                    continue
                self.notify(host, session)

        # A turn that finished while you were elsewhere (decision D): said
        # once, on the edge, unless it is the chat you are looking at.
        finished, self.just_finished = self.just_finished, set()
        watching = self.visible_chat() if self.is_active() else None
        for cid in finished:
            chat = self.chat_index.get(cid)
            if chat is None or cid == watching or not chat.finished:
                continue
            session = next((s for s in self.rows.get(chat.host, {})
                            .get("sessions", []) if s["name"] == chat.session),
                           None)
            if session is not None:
                self.notify(chat.host, session, finished=True)

        for gone in [k for k in self.was
                     if k not in {(h, s["name"]) for h in self.order
                                  for s in self.rows[h]["sessions"]}]:
            del self.was[gone]
            self.drafts.pop(gone, None)

    def draft_settled(self, key: tuple[str, str], text: str, now: float,
                      silent: bool) -> bool:
        """Whether an unsent draft has sat untouched long enough to be worth
        interrupting for -- and only the first time it has.

        Typing is a draft too. The box holds text from the first keystroke on,
        so announcing the state itself fired a notification at whoever was at
        the keyboard, about the sentence they were in the middle of. What
        separates the draft worth knowing about is that it stops changing:
        every edit puts the clock back to the start, and the toast is for the
        one that then just sits there.

        Only the first 60 characters of the box reach here, so a draft still
        being typed past that mark can look settled. Two minutes is a long
        time to spend on one sentence, and one toast is the whole cost.
        """
        seen, since, told = self.drafts.get(key, (None, now, silent))
        if text != seen:
            seen, since, told = text, now, silent
        settled = not told and now - since >= DRAFT_DWELL
        self.drafts[key] = (text, since, told or settled)
        return settled

    def notify(self, host: str, session: dict, finished: bool = False) -> None:
        chat = self.chat_index.get(chats.chat_id(host, session["name"]))
        self.get_application().send_notification(
            f"conn-{session_ref(host, session['name'])}",
            notification(host, session, chat, finished=finished))

    def repaint(self, now: float | None = None) -> None:
        """Same rows, new words: marks, states, times and which ones are
        open."""
        now = time.time() if now is None else now
        for cid, parts in self.chat_widgets.items():
            chat = self.chat_index.get(cid)
            if chat is not None:
                self._paint_chat(parts, chat, now)
        self._paint_chrome(now)
        for host, parts in self.host_widgets.items():
            if host in self.rows:
                self._paint_server(host, parts)
        for host in self.order:
            for session in self.rows[host]["sessions"]:
                parts = self.widgets.get((host, session["name"]))
                if parts is None:
                    continue
                state = session["agent"]["state"]
                self._tone(parts["mark"], state)
                parts["mark"].set_tooltip_text(
                    agent_state.LABELS.get(state, state))
                parts["detail"].set_label(self._detail(session))
                parts["detail"].set_tooltip_text(self._detail_tip(session))
                if "row" in parts:      # a shell that became a chat, or back
                    if state == agent_state.SHELL:
                        parts["row"].add_css_class("plain")
                    else:
                        parts["row"].remove_css_class("plain")
                open_now = (host, session["name"]) in self.open
                if open_now:
                    parts["number"].add_css_class("slot-open")
                else:
                    parts["number"].remove_css_class("slot-open")

    def _paint_chrome(self, now: float) -> None:
        """The header, recap and reply box over each open chat (phase 3).
        Laid in the first time a view turns out to be a chat -- a chat
        just started is a session before the probe says what it is."""
        for (host, name), view in self.open.items():
            chat = self.chat_index.get(chats.chat_id(host, name))
            chrome = getattr(view, "chrome", None)
            if chat is None or chat.offline:
                continue
            if chrome is None:
                chrome = view.chrome = sessionview.ChatChrome(view, self)
            row = self.rows.get(host, {})
            found = next((s for s in row.get("sessions", [])
                          if s.get("name") == name), {})
            chrome.paint(chat, found, row.get("home", ""), now)

    def sent_to(self, cid: str) -> None:
        """A reply went in: SPEC §2 says every send is a meaningful event."""
        chat = self.chat_index.get(cid)
        if chat is None or chat.offline:
            return
        stamp = int(time.time())
        chat.last_event = stamp
        self.stamps[cid] = stamp
        self.writer.put(chat.host, chat.session, "last_event", str(stamp))
        self.render()

    @staticmethod
    def _detail(session: dict) -> str:
        """The one status on the right of a session row in the Servers tab.

        It used to be the state word and the detail side by side, which on
        most rows said one thing twice: "working  working", "working
        Crunched for 6s", "shell  bash". The dot beside the name already
        carries the state in its tone, so the words say the most specific
        thing once: a shell's running command; an agent's detail when it
        has one that is not just the state again, else the state word. An
        unsent draft keeps its word -- the detail is the draft itself, which
        is yours, not news."""
        agent = session["agent"]
        label = agent["label"]
        detail = (agent["detail"] or "").strip()
        if agent["state"] == agent_state.SHELL:
            return detail or label
        if agent["state"] == agent_state.DRAFT:
            return label
        if detail and detail.lower() != label.lower():
            return detail
        return label

    @staticmethod
    def _detail_tip(session: dict) -> str:
        """The whole of it, under the pointer: the state and the detail."""
        agent = session["agent"]
        detail = (agent["detail"] or "").strip()
        if detail and detail.lower() != agent["label"].lower():
            return f"{agent['label']}: {detail}"
        return agent["label"]

    def matches(self, row) -> bool:
        """Filter on what is written on the row.

        A host that matches keeps its sessions, or filtering by host name
        would hide everything on it -- which is the opposite of what typing a
        host name means.
        """
        needle = self.filter.get_text().strip().lower()
        if not needle or getattr(row, "zone", "servers") != "servers":
            # The chat rows were filtered by chats.visible() before they were
            # built; this is only for the rows in the Servers tab.
            return True
        server = getattr(row, "server", None)
        if server is not None:
            # By the same test servers_listed() picked it by: its own name or
            # one of its sessions', so a matching session is never orphaned
            # under a hidden server.
            return self.server_matches(server, needle)
        key = getattr(row, "key", None)
        if key is None:
            return True             # furniture: Add server, "no match"
        return needle in key[0].lower() or needle in key[1].lower()

    def refilter(self) -> None:
        if self.sidebar_mode == "notes":
            self.notes_pane.side.render()
            return
        self.shape = []
        self.render()
        self.list.invalidate_filter()

    def clear_filter(self) -> None:
        self.filter.set_text("")
        self.refilter()
        self.list.grab_focus()

    def _server_row(self, host: str, state: str, fix: str | None,
                    opened: bool = False,
                    has_sessions: bool = False) -> Gtk.ListBoxRow:
        """One server in the Servers tab: the chevron that opens it out, its
        dot, its name and what is on it ("4 chats \u00b7 1 shell" -- what
        the fold hides), then its state in plain words and -- when it is
        broken in a way with one obvious fix -- the button that does it.

        The chevron is a button of its own, not the row: the row was already
        a destination (a terminal on the server), and clicking a server to
        see its sessions should not open one. A server with nothing on it
        gets a blank the chevron's width instead, so every name lines up.

        A destination now, not a heading: activating it opens a terminal on
        the server (open_server_shell), so it highlights and takes the arrows
        like a session row. It has no session key -- row.server says which
        host it is -- so nothing that acts on "the selected session" can
        mistake it for one.
        """
        row = Gtk.ListBoxRow()
        row.key = None
        row.server = host
        row.host = host          # so the filter can read it off the row
        row.zone = "servers"
        row.add_css_class("server")

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        top = Gtk.Box(spacing=8)
        label = Gtk.Label(label=host, xalign=0)
        label.add_css_class("host")
        # Ellipsized, or the widest name decides how wide the sidebar is:
        # the list never scrolls sideways, so an unellipsizable label's
        # minimum width propagates up and past the fixed SIDEBAR_WIDTH.
        # But ellipsize alone drops the minimum to a single character, so a
        # squeezed sidebar (a narrow tile, a terminal claiming its columns)
        # cut even "local" to "lo...". width-chars puts a floor back: short
        # names stay whole, only a genuinely long one ellipsizes.
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_width_chars(NAME_FLOOR)
        # And free to take the row, so the count sits at the right.
        label.set_hexpand(True)
        top.append(label)
        count = Gtk.Label(label="", xalign=1)
        count.add_css_class("meta")
        top.append(count)

        bottom = Gtk.Box(spacing=6)
        words = Gtk.Label(label="", xalign=0)
        words.add_css_class("attention" if fix in ("trust", "checkkey")
                            else "detail")
        words.set_ellipsize(Pango.EllipsizeMode.END)
        words.set_hexpand(True)
        bottom.append(words)
        if fix is not None:
            _kind, text, tip = self.SERVER_FIXES[state]
            button = Gtk.Button(label=text)
            button.add_css_class("fix")
            button.set_valign(Gtk.Align.CENTER)
            button.set_tooltip_text(tip)
            button.fix = fix
            button.connect("clicked", lambda _b, h=host: self.fix_server(h))
            bottom.append(button)

        line = Gtk.Box(spacing=8)
        if has_sessions:
            chevron = Gtk.Button(icon_name="pan-down-symbolic" if opened
                                 else "pan-end-symbolic")
            chevron.set_has_frame(False)
            chevron.set_tooltip_text(("Hide" if opened else "Show")
                                     + f" the sessions on {host}")
            chevron.connect("clicked",
                            lambda _b, h=host: self.toggle_server(h))
        else:
            chevron = Gtk.Box()
        chevron.add_css_class("chevron")
        chevron.set_size_request(16, 16)
        chevron.set_valign(Gtk.Align.START)
        chevron.set_margin_top(1)
        row.chevron = chevron
        row.opened = opened
        dot = Gtk.Box()
        dot.add_css_class("dot")
        dot.set_size_request(8, 8)
        dot.set_valign(Gtk.Align.START)
        dot.set_margin_top(6)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        column.set_hexpand(True)
        column.append(top)
        column.append(bottom)
        line.append(chevron)
        line.append(dot)
        line.append(column)
        outer.append(line)

        parts = {"dot": dot, "count": count, "words": words, "tone": None}
        self.host_widgets[host] = parts
        self._paint_server(host, parts)

        menu = Gtk.GestureClick()
        menu.set_button(3)
        # The row is the gesture's widget, asked for when it fires. Held in
        # the handler instead (r=row), it made a cycle through C that
        # Python's collector cannot see -- row -> gesture -> closure -> row
        # -- so every row a rebuild threw away stayed alive, children and
        # all: about 1 MB a rebuild, forever. See render().
        menu.connect("pressed", lambda g, _n, x, y, h=host:
                     self.host_menu(g.get_widget(), h, x, y))
        row.add_controller(menu)
        row.set_child(outer)
        return row

    def _paint_server(self, host: str, parts: dict) -> None:
        """The words on a server row that change without its shape: the
        dot's tone, what is on it, uptime and disk."""
        tone = self.host_tone(host)
        if parts["tone"] != tone:
            if parts["tone"]:
                parts["dot"].remove_css_class(parts["tone"])
            parts["dot"].add_css_class(tone)
            parts["tone"] = tone
        parts["count"].set_text(self.server_summary(host))
        text, tip = self.server_words(host)
        parts["words"].set_text(text)
        parts["words"].set_tooltip_text(tip)

    def _add_server_row(self) -> Gtk.ListBoxRow:
        """The last row in the Servers tab: the Site Manager, to add one.
        A row rather than a button, so the arrows reach it too."""
        row = Gtk.ListBoxRow()
        row.key = None
        row.add_server = True
        row.zone = "servers"
        row.add_css_class("fold")
        box = Gtk.Box(spacing=6)
        plus = Gtk.Image.new_from_icon_name("list-add-symbolic")
        plus.set_pixel_size(12)
        plus.add_css_class("section")
        box.append(plus)
        label = Gtk.Label(label="Add server", xalign=0)
        label.add_css_class("section")
        label.set_hexpand(True)
        box.append(label)
        row.set_tooltip_text("Opens the Site Manager: add, edit or forget "
                             "a server, its passwords, and Watch live")
        row.set_child(box)
        return row

    def _note_row(self, text: str) -> Gtk.ListBoxRow:
        """A line of text in the list -- the Servers tab's "nothing
        matches", which has no Start one button to offer."""
        row = self._plain_row("servers")
        label = Gtk.Label(label=text, xalign=0, margin_top=12,
                          margin_bottom=8, margin_start=4)
        label.add_css_class("empty")
        label.set_wrap(True)
        row.set_child(label)
        return row

    def _session_row(self, host: str, session: dict,
                     slot: int) -> Gtk.ListBoxRow:
        state = session["agent"]["state"]
        row = Gtk.ListBoxRow()
        row.key = (host, session["name"])
        row.zone = "servers"
        row.add_css_class("session")
        row.add_css_class("branch")     # the tree look; see the CSS
        # Under its server, not beside it: indented past the chevron, with
        # a guide line down the left (CSS), smaller, and a plain shell
        # quieter than a chat -- they read as more servers before.
        if state == agent_state.SHELL:
            row.add_css_class("plain")

        box = Gtk.Box(spacing=6)
        # The state as a dot in its tone, as the server menu draws hosts --
        # the chat rows' vocabulary, not the TUI's punctuation. The alt-key
        # sits over it, shown only while alt is held, as on the chat rows.
        holder = Gtk.Overlay()
        holder.set_size_request(14, -1)
        mark = Gtk.Box()
        mark.add_css_class("dot")
        mark.add_css_class("mark")
        mark.set_size_request(6, 6)
        mark.set_halign(Gtk.Align.CENTER)
        mark.set_valign(Gtk.Align.CENTER)
        self._tone(mark, state)
        mark.set_tooltip_text(agent_state.LABELS.get(state, state))
        holder.set_child(mark)
        number = Gtk.Label(label=str(slot) if 0 < slot < 10 else "")
        number.add_css_class("slot-num")
        number.set_halign(Gtk.Align.CENTER)
        number.set_valign(Gtk.Align.CENTER)
        number.set_can_target(False)
        number.set_visible(0 < slot < 10)
        if (host, session["name"]) in self.open:
            number.add_css_class("slot-open")
        holder.add_overlay(number)
        box.append(holder)

        name = Gtk.Label(label=session["name"], xalign=0)
        name.add_css_class("name")
        name.set_ellipsize(Pango.EllipsizeMode.END)  # like the host label,
        name.set_width_chars(NAME_FLOOR)             # floor and all
        box.append(name)

        tail = Gtk.Label(label=self._detail(session), xalign=1)
        tail.set_tooltip_text(self._detail_tip(session))
        tail.add_css_class("detail")
        tail.set_hexpand(True)
        tail.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(tail)

        # Right-click is not a feature anyone finds. The same kill, on the
        # row, shown when the pointer is over it so eight of them are not
        # sitting there inviting a misclick.
        kill = Gtk.Button(icon_name="user-trash-symbolic")
        kill.add_css_class("kill")
        kill.set_has_frame(False)
        kill.set_visible(False)
        kill.set_tooltip_text(
            f"Kill {host}/{session['name']} -- everything running in it ends")
        kill.connect("clicked",
                     lambda _b, h=host, n=session["name"]: self.confirm_kill(h, n))
        box.append(kill)
        self._hover_reveal(row, kill)

        self.widgets[(host, session["name"])] = {
            "number": number, "mark": mark, "detail": tail, "row": row}

        menu = Gtk.GestureClick()
        menu.set_button(3)
        # The row from the gesture, not held: see _server_row.
        menu.connect("pressed", lambda g, _n, x, y:
                     self.row_menu(g.get_widget(), x, y))
        row.add_controller(menu)

        row.set_child(box)
        return row

    @staticmethod
    def _plain_row(zone: str = "chats") -> Gtk.ListBoxRow:
        """A row that is furniture: no highlight, no activation, and skipped
        by the arrow keys."""
        row = Gtk.ListBoxRow()
        row.key = None
        row.zone = zone
        row.set_selectable(False)
        row.set_activatable(False)
        row.set_focusable(False)
        return row

    def _section_row(self, label: str) -> Gtk.ListBoxRow:
        row = self._plain_row()
        row.add_css_class("heading-row")
        box = Gtk.Box(spacing=6)
        if label == "Starred":
            star = Gtk.Image.new_from_icon_name("starred-symbolic")
            star.set_pixel_size(10)
            star.add_css_class("section")
            box.append(star)
        title = Gtk.Label(label=label.upper(), xalign=0)
        title.add_css_class("section")
        box.append(title)
        row.set_child(box)
        return row

    def _fold_row(self, which: str, count: int, opened: bool) -> Gtk.ListBoxRow:
        """Older: a heading that opens and closes what is under it.
        Activatable and focusable, so enter on it works like a click."""
        row = Gtk.ListBoxRow()
        row.key = None
        row.fold = which
        row.zone = "chats"
        row.add_css_class("fold")
        row.set_selectable(False)
        box = Gtk.Box(spacing=6)
        arrow = Gtk.Image.new_from_icon_name(
            "pan-down-symbolic" if opened else "pan-end-symbolic")
        arrow.set_pixel_size(10)
        arrow.add_css_class("section")
        box.append(arrow)
        title = Gtk.Label(label=which.upper(), xalign=0)
        title.add_css_class("section")
        title.set_hexpand(True)
        box.append(title)
        meta = Gtk.Label(label=str(count), xalign=1)
        meta.add_css_class("meta")
        box.append(meta)
        row.set_tooltip_text("Chats from before this week")
        row.set_child(box)
        return row

    def _summary_row(self, text: str) -> Gtk.ListBoxRow:
        row = self._plain_row()
        row.add_css_class("heading-row")
        box = Gtk.Box(spacing=8)
        label = Gtk.Label(label=text, xalign=0)
        label.add_css_class("summary")
        label.set_hexpand(True)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(label)
        clear = Gtk.Button(label="Clear")
        clear.set_has_frame(False)
        clear.add_css_class("clear")
        clear.connect("clicked",
                      lambda _b: self.set_filters(chats.Filters()))
        box.append(clear)
        row.set_child(box)
        return row

    def _empty_row(self, text: str) -> Gtk.ListBoxRow:
        row = self._plain_row()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      margin_top=12, margin_bottom=8, margin_start=4)
        label = Gtk.Label(label=text, xalign=0)
        label.add_css_class("empty")
        label.set_wrap(True)
        box.append(label)
        start = Gtk.Button(label="Start one")
        start.add_css_class("start")
        start.set_halign(Gtk.Align.START)
        start.connect("clicked", lambda _b: self.new_chat())
        box.append(start)
        row.set_child(box)
        return row

    def _chat_row(self, chat, slot: int, now: float) -> Gtk.ListBoxRow:
        """One chat, as the design draws it: the project tile with the state
        badge on its corner, the title and how long ago, the server and what
        it last said, and the star.

        Everything that changes from frame to frame -- title, time, preview,
        badge, star, the tooltip's path -- is set by _paint_chat(), here and on every repaint, so
        a chat that only changed its words keeps its row (and the menu or
        hover on it)."""
        project = next((p for p in self.projects if p.id == chat.project), None)
        row = Gtk.ListBoxRow()
        row.key = (chat.host, chat.session)
        row.chat = chat.id
        row.zone = "chats"
        row.add_css_class("chat")

        box = Gtk.Box(spacing=0)

        corner = Gtk.Overlay(margin_start=8, margin_end=10,
                             margin_top=6, margin_bottom=6)
        corner.set_size_request(38, 38)
        corner.set_child(self._tile(project))
        badge = Gtk.Stack()
        badge.set_hhomogeneous(False)
        badge.set_vhomogeneous(False)
        badge.set_halign(Gtk.Align.END)
        badge.set_valign(Gtk.Align.END)
        badge.add_named(Gtk.Box(), "none")
        dot = Gtk.Box()
        dot.add_css_class("badge-needs")
        dot.set_size_request(13, 13)
        badge.add_named(dot, "needs")
        # Drawn, not the theme's spinner icon: that was a faint partial ring
        # on one theme and a solid dark disc on the selected row on another.
        spinner = WorkingBadge(self.palette)
        ring = Gtk.Box()
        ring.add_css_class("badge-working")
        ring.append(spinner)
        badge.add_named(ring, "working")
        pencil = Gtk.Image.new_from_icon_name("document-edit-symbolic")
        pencil.set_pixel_size(8)
        pen = Gtk.Box()
        pen.add_css_class("badge-draft")
        pen.append(pencil)
        badge.add_named(pen, "draft")
        what = Gtk.Label(label="?")
        what.add_css_class("badge-unknown")
        badge.add_named(what, "unknown")
        corner.add_overlay(badge)
        # The alt-key, on the tile's corner and shown only while alt is held
        # (decision E): the row is the design's the rest of the time, and
        # showing it moves nothing. Blank past nine; accent while open.
        number = Gtk.Label(label=str(slot) if 0 < slot < 10 else "")
        number.add_css_class("slot-num")
        number.set_halign(Gtk.Align.START)
        number.set_valign(Gtk.Align.START)
        number.set_can_target(False)
        number.set_visible(0 < slot < 10)
        corner.add_overlay(number)
        box.append(corner)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        text.set_valign(Gtk.Align.CENTER)
        top = Gtk.Box(spacing=8)
        title = Gtk.Label(label="", xalign=0)
        title.add_css_class("chat-title")
        title.set_hexpand(True)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        top.append(title)
        when = Gtk.Label(label="", xalign=1)
        when.add_css_class("chat-time")
        # The kill shown on hover sits over the time rather than beside it:
        # a hidden button that still took its width pushed every time 30px
        # off the star (commit 224a289 keeps the bin on the row because
        # right-click is not found; on hover only, so it is never a
        # misclick waiting on the selected row).
        clock = Gtk.Overlay()
        clock.set_child(when)
        kill = Gtk.Button(icon_name="user-trash-symbolic")
        kill.add_css_class("kill")
        kill.set_has_frame(False)
        kill.set_halign(Gtk.Align.END)
        kill.set_valign(Gtk.Align.CENTER)
        kill.set_visible(False)
        kill.set_tooltip_text(
            f"Kill {chat.host}/{chat.session} -- everything running in it ends")
        kill.connect("clicked", lambda _b, h=chat.host, n=chat.session:
                     self.confirm_kill(h, n))
        clock.add_overlay(kill)
        top.append(clock)
        text.append(top)
        line = Gtk.Box(spacing=5)
        server = Gtk.Label(label=chat.host, xalign=0)
        server.add_css_class("chat-server")
        # The server is the part that tells chats apart once the preview is
        # cut anyway, so it keeps its whole name up to a cap and gives way in
        # the middle past that; the preview takes what is left.
        server.set_width_chars(min(len(chat.host), SERVER_CHARS))
        server.set_max_width_chars(SERVER_CHARS)
        server.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        line.append(server)
        middot = Gtk.Label(label="·")
        middot.add_css_class("chat-dot")
        line.append(middot)
        preview = Gtk.Label(label="", xalign=0)
        preview.add_css_class("chat-preview")
        preview.set_hexpand(True)
        preview.set_ellipsize(Pango.EllipsizeMode.END)
        line.append(preview)
        text.append(line)
        box.append(text)
        if not chat.offline:
            self._hover_reveal(row, kill, when)

        star = Gtk.Button()
        star.set_has_frame(False)
        star.add_css_class("star")
        star.set_valign(Gtk.Align.CENTER)
        star.set_margin_end(2)
        star.connect("clicked", lambda _b, cid=chat.id:
                     self.toggle_chat_star(cid))
        # A star is an option on the tmux session: nothing to write it to.
        star.set_sensitive(not chat.offline)
        box.append(star)

        parts = {"row": row, "number": number, "badge": badge,
                 "spinner": spinner, "title": title, "time": when,
                 "preview": preview, "star": star}
        self.chat_widgets[chat.id] = parts
        self._paint_chat(parts, chat, now)

        menu = Gtk.GestureClick()
        menu.set_button(3)
        # The row from the gesture, not held: see _server_row.
        menu.connect("pressed", lambda g, _n, x, y:
                     self.row_menu(g.get_widget(), x, y))
        row.add_controller(menu)
        row.set_child(box)
        return row

    def _hover_reveal(self, row, shown: Gtk.Widget,
                      hidden: Gtk.Widget | None = None) -> None:
        """Show `shown` only while the pointer is on the row, and hide
        `hidden` under it meanwhile. Visibility, not opacity: a widget at
        opacity 0 still takes its width on every row."""
        motion = Gtk.EventControllerMotion()

        def enter(*_a):
            shown.set_visible(True)
            if hidden is not None:
                hidden.set_opacity(0)

        def leave(*_a):
            shown.set_visible(False)
            if hidden is not None:
                hidden.set_opacity(1)
        motion.connect("enter", enter)
        motion.connect("leave", leave)
        row.add_controller(motion)

    def _paint_chat(self, parts: dict, chat, now: float) -> None:
        state = chat.state
        parts["title"].set_label(chat.title)
        parts["time"].set_label(chats.relative_time(chat.last_event, now))
        # A finished chat you have not opened is needs you: amber, as a
        # blocked one is (SPEC §3).
        tone = "amber" if chats.is_needs_you(chat) else mark_tone(state)
        for name in ("amber", "blue"):
            if name == tone:
                parts["time"].add_css_class(name)
            else:
                parts["time"].remove_css_class(name)
        parts["preview"].set_label(chat.preview
                                   or agent_state.LABELS.get(state, ""))
        page = ("none" if chat.offline else "needs" if chat.finished
                else BADGES.get(state, "none"))
        if parts["badge"].get_visible_child_name() != page:
            parts["badge"].set_visible_child_name(page)
        spinning = state == agent_state.WORKING
        if parts["spinner"].get_spinning() != spinning:
            parts["spinner"].set_spinning(spinning)
        row = parts["row"]
        # The path is not in the shape -- a cd inside the project moves no
        # row -- so the tooltip is kept current here, not set once at build.
        tip = self._chat_tip(chat)
        if row.get_tooltip_text() != tip:
            row.set_tooltip_text(tip)
        # Offline is drawn as idle is -- the quieter tile and title, already
        # held to the theme's contrast floors -- plus its own class.
        for name, on in (("chat-idle", chat.offline
                          or (state == agent_state.READY
                              and not chat.finished)),
                         ("chat-needs", chats.is_needs_you(chat)),
                         ("chat-offline", chat.offline)):
            if on:
                row.add_css_class(name)
            else:
                row.remove_css_class(name)
        star = parts["star"]
        star.set_icon_name("starred-symbolic" if chat.starred
                           else "non-starred-symbolic")
        if chat.starred:
            star.add_css_class("on")
        else:
            star.remove_css_class("on")
        star.set_tooltip_text(("Unstar " if chat.starred else "Star ")
                              + chat.title + " (s)")
        if (chat.host, chat.session) in self.open:
            parts["number"].add_css_class("slot-open")
        else:
            parts["number"].remove_css_class("slot-open")

    def _chat_tip(self, chat) -> str:
        """What the row cuts off: the full title and the preview, then which
        session it is. The folder only when it says something -- every local
        chat runs in the home, and "/home/you" under each one says nothing."""
        lines = []
        if chat.title:
            lines.append(chat.title)
        if chat.offline:
            was = agent_state.LABELS.get(chat.last_state, "")
            lines.append(f"{chat.host} is not answering: this is the chat as "
                         "it was last seen" + (f" ({was})" if was else "")
                         + ". Open it to try the server again.")
        words = chat.preview or agent_state.LABELS.get(chat.state, "")
        if words:
            lines.extend(textwrap.wrap(words, 60)[:4])
        where = f"{chat.host}/{chat.session}"
        home = (self.rows.get(chat.host, {}).get("home") or "").rstrip("/")
        if chat.path and chat.path.rstrip("/") != home:
            path = chat.path
            if home and path.startswith(home + "/"):
                path = "~" + path[len(home):]
            where += f"  {path}"
        lines.append(where)
        return "\n".join(lines)

    @staticmethod
    def _tone(mark: Gtk.Widget, state: str) -> None:
        """A sidebar mark takes its colour from a class, not a Pango
        attribute: an attribute outranks every stylesheet, so on the selected
        row it stayed its own colour on the accent -- and once muted and the
        accent were both lifted to the same floor, a shell's "." was drawn
        1:1 on its own highlight."""
        for tone in MARK_TONES:
            if tone != mark_tone(state):
                mark.remove_css_class(tone)
        mark.add_css_class(mark_tone(state))

    @staticmethod
    def _colour_attrs(colour: str) -> Pango.AttrList:
        attrs = Pango.AttrList()
        value = rgba(colour)
        attrs.insert(Pango.attr_foreground_new(
            int(value.red * 65535), int(value.green * 65535),
            int(value.blue * 65535)))
        return attrs

    # -- opening one -------------------------------------------------------

    def row_activated(self, _list, row) -> None:
        fold = getattr(row, "fold", None)
        if fold == "older":
            self.showing_older = not self.showing_older
            self.render()
            return
        if getattr(row, "add_server", False):
            self.open_manager()
            return
        server = getattr(row, "server", None)
        if server is not None:
            self.open_server_shell(server)
            return
        key = getattr(row, "key", None)
        if key is None:
            return
        self.open_session(*key)

    def open_server_shell(self, host: str) -> None:
        """A server row, clicked: a terminal on it, in the main area.

        tmux's own "new-session -A" is attach-or-create, so the session
        named "shell" is attached when it is there and made when it is not
        -- the same path New session... ends in, without its prompt. On a
        server ssh does not trust yet, that terminal is where the
        fingerprint prompt appears, as it always was for New session. A
        *changed* key gets no prompt -- ssh refuses it outright -- so that
        is the row's Check new key button, not this.

        The server stays as it was, folded or not: a click on a server is
        "a terminal on it", not "show me its sessions" -- that is the
        chevron. Its own row keeps the highlight."""
        self.shell_from_server = True
        try:
            self.open_session(host, "shell")
        finally:
            self.shell_from_server = False
        if (self.sidebar_mode == "servers"
                and not self.server_open(host, self.query())):
            self.pending = None
            self.select_server(host)

    def fix_server(self, host: str) -> None:
        """The button on a broken server row: the one obvious fix for what
        is wrong with it, as server_fix() names it."""
        fix = self.server_fix(host)
        connlog.event(f"servers: {fix or 'no fix'} for {host} "
                      f"({self.server_state(host)})")
        if fix == "install":
            self.install_key(host)
        elif fix == "trust":
            self.trust_host(host)
        elif fix == "checkkey":
            self.check_host_key(host)
        elif fix == "recheck":
            self.recheck(host)

    def trust_host(self, host: str) -> None:
        """A new server's fingerprint prompt, in a terminal of its own --
        the answer is yours to give, not Conn's. The next sweep sees the
        key once it is accepted; the row says so meanwhile."""
        try:
            hosts.trust_key(host)
        except hosts.HostError as exc:
            self.complain(host, str(exc))
            return
        self.notice(f"answer {host}'s fingerprint prompt in the new "
                    "terminal -- Check again after")

    def check_host_key(self, host: str) -> None:
        """A changed host key, compared in a terminal: the key on file, the
        key offered, and a y/N that only you can answer. Conn never edits
        known_hosts itself -- the script runs ssh-keygen -R only on a y."""
        try:
            hosts.check_new_key(host)
        except hosts.HostError as exc:
            self.complain(host, str(exc))
            return
        self.notice(f"compare {host}'s keys in the new terminal -- "
                    "Check again after")

    def select_server(self, host: str) -> bool:
        """Highlight a server's row in the Servers tab, if it is listed."""
        index = 0
        while (row := self.list.get_row_at_index(index)) is not None:
            index += 1
            if getattr(row, "server", None) == host:
                self.selecting = True
                self.list.select_row(row)
                self.selecting = False
                return True
        return False

    def open_session(self, host: str, name: str, focus: bool = True) -> None:
        """Open one, or come back to it if it is already open.

        Both ends go through show(), so the list always agrees with the
        terminal about which session you are looking at -- opening one by its
        number used to move the terminal and leave the highlight behind.
        """
        # The key is the name tmux will actually use -- both connect paths
        # filter what they are handed, so a view keyed by the raw typed name
        # would wait forever for a row that arrives under the filtered one.
        name = hosts.session_name(name)
        key = (host, name)
        if self.open_when_up != key:
            self.open_when_up = None    # you have moved on from that one
        if key not in self.open and self.offline_refused(host, name,
                                                         attach=True):
            self.show_offline(key)
            return
        self.open_when_up = None
        # A notification can send you to a chat folded under Older, or to a
        # plain shell, which only the Servers tab lists -- and a view whose
        # row is not on screen has nothing for the list to highlight.
        if focus:
            self.reveal(key)
        if key in self.open:
            self.show(self.open[key], focus)
            return

        session = Session(host, name, self.palette, self._session_exited,
                          screen=lambda h=host, n=name: self.screen_of(h, n),
                          font=self.font, zoom=self.zoom, notice=self.notice,
                          last_reply=lambda h=host, n=name: self.copy_reply(h, n))
        self.open[key] = session
        self.stack.add_named(session, f"{host}/{name}")
        self.show(session, focus)
        # The number beside it goes accent once it is open, and that is drawn
        # by the list rather than by the stack.
        self.repaint()

    def reveal(self, key: tuple[str, str]) -> None:
        """Put the session's row on screen: open Older for an old chat, or
        go to the Servers tab for a session that is not a chat at all, its
        server opened out there.

        The Servers tab lists every session, chats too, so a chat opened
        from there stays there -- only a chat opened from Chats is looked
        for in Chats. A chat the chips hide is left hidden: switching tabs
        under someone for a highlight is worse than no highlight."""
        chat = self.chat_index.get(chats.chat_id(*key))
        if self.sidebar_mode == "servers":
            if self.shell_from_server:
                return              # see open_server_shell()
            # The tab folds its servers; one with a session being opened is
            # opened out, or the highlight has no row to land on. Remembered
            # like an open by hand -- it is what you are looking at.
            if (key[0] in self.rows
                    and not self.server_open(key[0], self.query())):
                self.open_out(key[0])
                self.render()
            return
        if chat is None:
            # Only a session known to be a plain shell moves you to Servers.
            # Not being in chat_index is not enough: a chat just started is
            # opened before the recheck brings it into self.rows (start_chat
            # stamps it first), and sending ctrl-n -- or a chat started from
            # a note -- off to the Servers tab is the opposite of easier.
            if self.is_plain_shell(*key):
                # Opened out first, so the tab arrives with its row showing.
                if not self.server_open(key[0], self.query()):
                    self.open_out(key[0])
                self.set_sidebar_mode("servers")
            return
        if (chat in self.visible_chats and not chat.starred
                and not self.showing_older and not self.filters.by_project
                and chats.bucket(chat.last_event, time.time()) == "Older"):
            self.showing_older = True
            self.render()

    def is_plain_shell(self, host: str, name: str) -> bool:
        """Whether host/name is a session the server reports, that is a
        plain shell rather than a chat -- and not a chat this window has
        just started, which can still read as a shell for a moment."""
        if chats.chat_id(host, name) in self.stamps:
            return False
        for session in self.rows.get(host, {}).get("sessions", []):
            if session.get("name") == name:
                return ((session.get("agent") or {}).get("state")
                        == agent_state.SHELL)
        return False

    def screen_of(self, host: str, name: str) -> str:
        """The last screen the watcher brought back for one session."""
        for session in self.rows.get(host, {}).get("sessions", []):
            if session["name"] == name:
                return session.get("screen", "")
        return ""

    def _session_exited(self, session: Session, status: int) -> None:
        """A terminal's attach ended. Closed, as it always was -- unless the
        connection went, rather than the session.

        Lost is ssh's own 255, or a signal, from a remote host: the view
        stays, scrollback and all, under a "Disconnected" banner, asks the
        server now if it is not already known to be dark, and attaches again
        when the server is back with the chat live (_host_back). Any other
        exit -- 0 for a detach or an exit, a program's own status, a kill --
        is the attach ending as asked, and closes the view whatever the
        server's row says: a probe that timed out on a busy box says "down"
        while this terminal's own ssh is fine, and a detach then must not
        come back by itself.

        Nor is an attach that never started lost: ssh-connect has already
        shown why, and its "press any key to close this window" means
        close. Its note (hosts.launch_error) is what tells it apart -- the
        hold behind that prompt is too long for any clock to.
        """
        if self.open.get((session.host, session.name)) is not session:
            return
        try:
            code = os.waitstatus_to_exitcode(status)
        except (ValueError, TypeError):
            code = -1
        lost = not hosts.is_local(session.host) and (code == 255 or code < 0)
        label = f"session {session.host}/{connlog.clean(session.name, 60)}"
        if lost:
            failed = hosts.launch_error(session.spawned)
            if failed.startswith(f"{session.host}/{session.name}:"):
                self.notice(f"{failed} -- did not reconnect"
                            if session.respawned else failed)
                connlog.event(f"{label}: attach failed (exit {code}"
                              + (", after a respawn)" if session.respawned
                                 else ")") + ", closed")
                lost = False
        if not lost:
            if not hosts.is_local(session.host):
                connlog.event(f"{label}: ended (exit {code}), closed")
            self.close_session(session)
            return
        if session.respawn_spent and \
                time.monotonic() - session.respawned >= RESPAWN_STABLE:
            # The respawn stayed up: this is a drop of its own.
            session.respawn_spent = False
        session.dead = True
        session.died = time.monotonic()
        connlog.event(f"{label}: disconnected (exit {code}); host "
                      + ("already dark, waiting for it"
                         if session.offline else
                         f"{self.rows.get(session.host, {}).get('state')}, "
                         "asking it now"))
        if not session.offline:
            self._retry(session.host)
        self.paint_banners()

    def _host_back(self, host: str, started: float | None = None) -> None:
        """A probe found `host` up: attach the terminals that died with it
        again, once each, if their chat is still there -- tmux would make a
        new, empty session of a name that has gone, so one that has gone is
        closed and said.

        Once per return: a terminal already respawned since the server was
        last seen dark (respawn_spent) died again with it answering, and is
        let go with a notice rather than respawned in a loop. An answer
        from a probe `started` before a terminal died says nothing about
        after: that one waits for a fresh probe, sent here."""
        if self.rows.get(host, {}).get("state") != "up":
            return
        live = {s.get("name") for s in self.rows[host].get("sessions", [])}
        stale = False
        for view in list(self.open.values()):
            if not isinstance(view, Session) or view.host != host \
                    or not view.dead:
                continue
            label = f"session {host}/{connlog.clean(view.name, 60)}"
            if started is not None and started < view.died:
                stale = True
                connlog.event(f"{label}: host up, but by a probe from before "
                              "it died -- asking again")
            elif view.name not in live:
                self.notice(f"{host}/{view.name} has ended")
                connlog.event(f"{label}: host up, session gone, closed")
                self.close_session(view)
            elif view.respawn_spent:
                self.notice(f"{host}/{view.name} did not stay connected "
                            "-- open it again to retry")
                connlog.event(
                    f"{label}: did not stay connected (respawned "
                    f"{time.monotonic() - view.respawned:.0f}s ago), closed")
                self.close_session(view)
            else:
                connlog.event(f"{label}: host up, respawning")
                view.respawn()
        if stale:
            self._retry(host)
        self.paint_banners()

    def close_session(self, session: Session) -> None:
        """A session whose command ended takes its terminal with it.

        The tmux session on the other side is untouched -- that is the whole
        point of it -- so this is closing a view, not ending any work.

        One death reports twice: a kill closes the view AND makes the child
        exit, so _exited arrives on a session already closed -- and removing
        it from the stack again is a Gtk-CRITICAL that ends with the visible
        view yanked somewhere unrelated. Identity, not just the key: by the
        time a late signal lands, the key can name a newly opened view.
        """
        if self.open.get((session.host, session.name)) is not session:
            return
        self.open.pop((session.host, session.name))
        showing = self.stack.get_visible_child() is session
        self.stack.remove(session)
        self.repaint()
        if not showing:
            # One closing behind what you are looking at -- the offline
            # page, Notes, another terminal -- leaves that where it is.
            return
        if self.open:
            # Which one to fall back to is ours to choose: left alone, the
            # stack picks whatever child it likes -- usually the placeholder,
            # while two sessions are still open behind it.
            self.show(list(self.open.values())[-1])
        else:
            self.stack.set_visible_child(self.placeholder)
            # The highlight stays where it was, as the cursor for the arrow
            # keys. Nothing is showing, and nothing pretends to be.

    # -- probing -----------------------------------------------------------

    def wake_tick(self) -> bool:
        """Notice a wake or a new network and reconnect at once.

        Either leaves every ssh connection made before it dead without
        knowing it: the streams sit out WATCH_STALL, reconnect onto the same
        dead ControlMaster (the ProxyJump hop under every host rides one),
        stall again, and the window shows nothing connected for minutes
        after the lid opens. So: masters that cannot answer are closed --
        which ends every stream riding them at once -- every stream's
        backoff is cut short, and every host is probed again now.
        """
        if self.stopping:
            return GLib.SOURCE_REMOVE
        connlog.flush()     # summaries of rate-limited lines now due
        slept = hosts.slept_seconds()
        gap = slept - self.slept
        woke = gap > WAKE_GAP
        self.slept = slept
        addresses = hosts.local_addresses()
        moved = addresses != self.addresses and bool(addresses)
        if woke:
            connlog.event(f"wake: slept {gap:.0f}s")
        if addresses != self.addresses:
            connlog.event(f"addresses: {connlog.addresses(self.addresses)} -> "
                          f"{connlog.addresses(addresses)}"
                          + ("" if moved else " (none: not a new network yet)"))
        self.addresses = addresses
        if woke or moved:
            if self.reviving:
                self.revive_again = True
                connlog.event("wake: a revive is already running -- "
                              "another queued after it (revive_again)")
            else:
                self.revive("woke from sleep" if woke else "network changed")
        return GLib.SOURCE_CONTINUE

    def revive(self, why: str) -> None:
        self.reviving = True
        self.notice(f"{why} -- reconnecting...")
        began = time.monotonic()
        connlog.event(f"revive: start ({why})")

        def work():
            dropped = []
            try:
                # Not before the network is back: see network_ready. If it
                # never comes, the masters are left to ServerAlive.
                if hosts.network_ready():
                    dropped = hosts.revive_masters()
            except Exception as exc:   # noqa: BLE001 -- landed must run
                connlog.event(f"revive: failed: {type(exc).__name__}: "
                              f"{connlog.clean(exc)}")
            GLib.idle_add(landed, dropped)

        def landed(dropped):
            self.reviving = False
            again = self.revive_again
            if self.revive_again:
                # Another wake or move landed while this one ran.
                self.revive_again = False
                GLib.idle_add(lambda: (self.revive(why), GLib.SOURCE_REMOVE)[1])
            # Only now: a stream poked before its dead master was closed
            # would reconnect straight back onto it.
            streams = list(self.streams.values())
            connlog.event(
                f"revive: done ({why}) in {time.monotonic() - began:.1f}s, "
                f"{len(dropped)} master{'' if len(dropped) == 1 else 's'} "
                f"closed; poking {len(streams)} stream"
                f"{'' if len(streams) == 1 else 's'}, sweep in 1.5s"
                + ("; another revive queued" if again else ""))
            for _thread, _halt, poke in streams:
                poke.set()
            # Probes that were stuck on a closed master fail the moment it
            # goes; a sweep now would skip them as still in flight, so it
            # waits for them to land, then asks every host again.
            GLib.timeout_add(1500, lambda: (self.sweep(force=True),
                                            GLib.SOURCE_REMOVE)[1])
            if dropped:
                self.notice(f"{why} -- reconnected ({len(dropped)} stale "
                            "ssh connection" + ("s" if len(dropped) > 1 else "")
                            + " closed)")
            return GLib.SOURCE_REMOVE

        threading.Thread(target=work, name="revive", daemon=True).start()

    def sweep_tick(self) -> bool:
        self.sweep()
        self.check_agent()
        return GLib.SOURCE_CONTINUE

    def restart(self) -> None:
        """Become the version that is installed now.

        The sessions are tmux and outlive this process, which is what makes
        replacing it in place reasonable rather than alarming: the views close
        and come back, and nothing running in them notices.

        exec replaces this process but not its children. A watcher ssh left
        behind holds its connection -- and the capture loop on the far side --
        until the remote next speaks, which for an idle host is never, so
        each restart would double the streams to every quiet starred host.
        The watchers come down first, with a moment for the threads to finish
        the job.

        Through the installed launcher when this is the installed copy, so a
        changed bin/conn takes effect too. A checkout run some other way
        restarts the way it was started.
        """
        self.shut_down()
        deadline = time.monotonic() + 2.0
        for thread, _halt, _poke in list(self.streams.values()):
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        connlog.event("conn restarting (exec the installed copy)")
        connlog.shutdown()
        # The dev copy restarts into the dev install (conn-dev), never into
        # the stable one -- CONN_CHANNEL is put back for the exec, but a
        # dev window running the stable code would be the old bug in a new
        # coat.
        launcher = paths.LAUNCHER
        installed = paths.INSTALLED
        # The switch came out of the environment at start (run()); back in
        # for the exec, so a checkout started as CONN_CHANNEL=dev python3
        # main.py comes back as dev, not as the stable channel.
        os.environ.update(CHANNEL_ENV)
        if APP_DIR == installed.resolve() and os.access(launcher, os.X_OK):
            try:
                os.execv(str(launcher), [str(launcher)])
            except OSError:
                pass
        os.execv(sys.executable, [sys.executable, str(APP_DIR / "main.py")])

    def check_source(self) -> None:
        self.updated.set_visible(source_stamp() != self.stamp)

    def sweep(self, force: bool = False) -> None:
        """Probe the starred every time; the rest on a long timer.

        Forty servers probed every 45 seconds is forty ssh connections every
        45 seconds, which is the real ceiling on how many this can hold --
        not screen space. The unstarred are still probed, so one of them
        going to needs-you still reaches you; it just takes minutes instead
        of seconds, which is the deal you make by not starring it.
        """
        self.check_source()
        # The agent gate. Probing before there is a key to ride would flip
        # every host to red "no key" -- the exact fan-out connect_hosts()
        # is holding back. See show_agent().
        if self.holding:
            return
        now = time.monotonic()
        if force:
            busy = [h for h in self.order if h in self.inflight]
            connlog.event(f"sweep: forced, probing "
                          f"{len(self.order) - len(busy)} host"
                          f"{'' if len(self.order) - len(busy) == 1 else 's'}"
                          + (f" ({len(busy)} still in flight: "
                             f"{', '.join(busy)})" if busy else ""))
        for host in self.order:
            if host in self.inflight:
                continue
            if not (force or self.watched(host)
                    or now - self.probed.get(host, 0.0) >= SLOW_REFRESH):
                continue
            if force:
                self.probe_report.add(host)
            self.probed[host] = now
            self.inflight.add(host)
            threading.Thread(target=self._probe, args=(host,),
                             name=f"probe-{host}", daemon=True).start()

    def _probe(self, host: str) -> None:
        started = time.monotonic()
        row = hosts.probe(host)
        GLib.idle_add(self._probed, host, row, started)

    def _probed(self, host: str, row: dict, started: float) -> bool:
        self.inflight.discard(host)
        if host in self.rows:
            self._log_probe(host, row, time.monotonic() - started)
            if self.streamed.get(host, 0.0) > started:
                # The stream spoke while this probe was in flight, so the
                # probe's captures -- taken back at connect time -- are the
                # older ones. The stream stays silent while a screen is
                # unchanged, so letting the probe win would show the stale
                # state until the next sweep -- and announce() would then
                # re-notify the change as if it were new.
                fresher = {s["name"]: s
                           for s in self.rows[host].get("sessions", [])}
                for session in row.get("sessions", []):
                    kept = fresher.get(session["name"])
                    if kept is not None:
                        session["screen"] = kept.get("screen", "")
                        session["agent"] = kept["agent"]
                        for field in META_KEYS:
                            if field in kept:
                                session[field] = kept[field]
            self.rows[host] = row
            self.render()
            self._host_back(host, started)
            self._owed_open(host)
        return GLib.SOURCE_REMOVE

    def _log_probe(self, host: str, row: dict, took: float) -> None:
        """A host's state, into the connection-event log when it changes
        -- not every sweep -- and any probe slower than SLOW_PROBE. A
        failure carries its reason, cleaned: it is ssh's last stderr line,
        which can be the server's own banner.

        Two more, because a host that stays down is the case the log is
        for. hosts.probe calls every non-auth failure "down", so a host
        that goes down on "timed out" (a mux stuck on a stale master) and
        then fails on "Broken pipe" or "Network is unreachable" for five
        minutes would show only the first reason: a change of reason
        inside a failing state is logged too (rate-limited per host). And
        every result of the forced sweep after a revive is logged, changed
        or not -- one line per host saying what the revive led to."""
        new = row.get("state", "unknown")
        old, old_reason = self.probe_state.get(host, ("unknown", ""))
        error = row.get("error") or ""
        cleaned = connlog.clean(error) if new != "up" and error else ""
        self.probe_state[host] = (new, cleaned)
        reason = f": {cleaned}" if cleaned else ""
        report = host in self.probe_report
        self.probe_report.discard(host)
        if new != old:
            key = ("state", host, old, new)
            message = f"host {host}: {old} -> {new} ({took:.1f}s){reason}"
        elif new != "up" and cleaned != old_reason:
            key = ("reason", host)
            message = (f"host {host}: still {new} ({took:.1f}s), reason now: "
                       f"{cleaned or '(none given)'}")
        elif report:
            key = None
            message = f"host {host}: still {new} ({took:.1f}s){reason}"
        elif took > SLOW_PROBE:
            key = ("slow", host)
            message = (f"host {host}: slow probe, {took:.1f}s, still "
                       f"{new}{reason}")
        else:
            return
        if report:
            # Once per host per revive, so never held back by the limiter:
            # this line is the answer to "what did the revive lead to".
            connlog.event(message + " [forced sweep]")
        else:
            connlog.limited(key, message)

    def _owed_open(self, host: str) -> None:
        """Settle the open the offline page is owed, once `host` answers:
        open the chat if the answer has it live; if it answered without it,
        the page says the chat has ended. Down again, the page keeps waiting
        -- its words already say so.

        Ended is settled even while the page is behind Notes or New chat,
        so coming back to it (_back_to_offline_page) says so rather than
        attaching, which would make tmux start an empty session of the old
        name. The open itself is made only while the page shows it -- from
        behind those it waits for you to come back -- and anything else
        shown since (show()) or opened (open_session) has cancelled it.

        The open takes focus and the list's highlight only if they are still
        where the page left them: a probe can take seconds, and keys typed
        into the search or arrows down the list meanwhile are not for the
        terminal on the far side."""
        owed = self.open_when_up
        page = self.offline_page
        if owed is None or owed[0] != host or page.key != owed:
            return
        if self.rows[host]["state"] != "up":
            return
        name = owed[1]
        # The row, not the chat list: a session whose agent has exited is a
        # plain shell now, and still there to open.
        if not any(s.get("name") == name for s in self.rows[host]["sessions"]):
            self.open_when_up = None
            page.ended = True
            self.paint_offline_page()
            self.notice(f"{host}/{name} has ended")
            return
        if not self.offline_showing():
            return
        self.open_when_up = None
        focus = self.get_focus()
        here = (focus is None or focus is page or focus.is_ancestor(page))
        self.open_session(host, name, focus=here)

    # -- streaming ---------------------------------------------------------

    def watched(self, host: str) -> bool:
        """Streamed: asked for (Watch live), or it has a chat on it."""
        return host in self.starred or host in self.auto_live

    def follow_chats(self, up: set[str], with_chats: set[str]) -> None:
        """Watch live every server with a chat on it; let go of one whose
        last chat has gone. Only an answering server can say it has none: a
        dark one keeps its stream, which is also what notices it is back."""
        if self.stopping:
            return
        for host in with_chats - self.auto_live:
            if host not in self.order:
                continue
            self.auto_live.add(host)
            connlog.event(f"watch {host}: has a chat, watching live")
            if not self.holding and host not in self.starred:
                self.start_stream(host)
        for host in [h for h in self.auto_live
                     if h in up and h not in with_chats]:
            self.auto_live.discard(host)
            if host not in self.starred:
                connlog.event(f"watch {host}: no chats left, back to "
                              "checking every few minutes")
                self.stop_stream(host)
                self.probed[host] = time.monotonic()

    def start_stream(self, host: str) -> None:
        """Give a host its watch thread, however many times it is asked.

        The gate is the thread itself, not a bookkeeping set: a set outlives
        the thread it stands for -- a spawn that failed, a stream just halted
        -- and gating re-star on stale bookkeeping is how star/unstar cycles
        used to stack up duplicate ssh streams.
        """
        if os.environ.get("CONN_NO_WATCH") == "1":
            return
        entry = self.streams.get(host)
        if entry is not None and entry[0].is_alive() and not entry[1].is_set():
            return
        halt, poke = threading.Event(), threading.Event()
        thread = threading.Thread(target=self._stream, args=(host, halt, poke),
                                  name=f"watch-{host}", daemon=True)
        self.streams[host] = (thread, halt, poke)
        thread.start()

    def stop_stream(self, host: str) -> None:
        """Take a host's stream down and keep it down.

        Terminating the process alone only looks like a dropped connection:
        the thread would back off and reconnect. The halt is what makes the
        stop stick.
        """
        entry = self.streams.get(host)
        if entry is not None:
            entry[1].set()
        # Halt first, pop under the lock second: a spawn that has not
        # registered yet will now see the halt and take itself down.
        with self.watch_lock:
            proc = self.watchers.pop(host, None)
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass

    def _stream(self, host: str, halt: threading.Event,
                poke: threading.Event) -> None:
        """Hold one channel open per host and read screens off it.

        Lifted from the TUI unchanged in spirit: the far side only speaks when
        a screen actually changed, so an idle host costs nothing. halt is this
        thread's own stop order -- unstar, forget, shutdown -- and poke cuts
        a pending backoff short without stopping anything.
        """
        delay = WATCH_RETRY_MIN
        attempt = 0
        while not (self.stopping or halt.is_set()):
            opened = time.monotonic()
            attempt += 1
            connlog.limited(("watch", host, "connect"),
                            f"watch {host}: connecting (attempt {attempt})")
            why = "EOF"
            heard = 0.0
            try:
                proc = hosts.watch_screens(host, WATCH_INTERVAL)
            except OSError as exc:
                proc = None     # spawn refused: back off like a dropped stream
                why = f"spawn error: {connlog.clean(exc, 120)}"
            if proc is not None:
                # Check and register as one step: checked first and written
                # after, a halt landing between the two let this thread
                # register over its successor's entry, then identity-pop and
                # terminate only its own process -- leaving the successor's
                # live ssh unreachable by stop_stream.
                with self.watch_lock:
                    halted = self.stopping or halt.is_set()
                    if not halted:
                        self.watchers[host] = proc
                if halted:
                    # Halted between spawn and registration: the stopper's
                    # pop found nothing, so this process is ours to take down.
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    connlog.event(f"watch {host}: stopped before it started")
                    return
                poke.clear()    # a check-again from before this connection
                buffer: list[str] = []
                pending = b""
                try:
                    fd = proc.stdout.fileno()
                    # poll, not select: select raises on any fd past
                    # FD_SETSIZE (1024), which would read as a dead stream
                    # -- a process holding enough fds churned every watch
                    # forever. poll has no fd-number ceiling.
                    poller = select.poll()
                    poller.register(fd, select.POLLIN)
                    while not (self.stopping or halt.is_set()):
                        # Bounded silence, not a blocking readline: past the
                        # heartbeat allowance the channel is dead however
                        # alive the process looks -- see WATCH_STALL.
                        if not poller.poll(WATCH_STALL * 1000):
                            why = f"stalled, silent {WATCH_STALL:g}s"
                            break
                        chunk = os.read(fd, 65536)
                        if not chunk:
                            why = "EOF"
                            break
                        pending += chunk
                        while b"\n" in pending:
                            raw, _, pending = pending.partition(b"\n")
                            line = raw.decode(errors="replace")
                            if not heard and (line.startswith(proc.frame)
                                              or line.startswith(proc.alive)):
                                heard = time.monotonic()
                                connlog.limited(
                                    ("watch", host, "connected"),
                                    f"watch {host}: connected in "
                                    f"{heard - opened:.1f}s")
                            if line.startswith(proc.frame):
                                frame = hosts.parse_frame(buffer, host,
                                                          proc.boundary)
                                buffer = []
                                try:
                                    self.frames.put_nowait((host, frame))
                                except queue.Full:
                                    pass
                            elif line.startswith(proc.alive):
                                pass    # its arrival was its whole content
                            else:
                                buffer.append(line)
                except (OSError, ValueError) as exc:
                    why = f"read error: {type(exc).__name__}"
                finally:
                    # Only our own entry: a successor stream may already
                    # have registered its process under this host.
                    with self.watch_lock:
                        if self.watchers.get(host) is proc:
                            self.watchers.pop(host, None)
                    try:
                        code = proc.poll()
                    except Exception:
                        code = None
                    if code is not None:
                        why += f", ssh exit {code}"
                    try:
                        proc.terminate()
                    except Exception:
                        pass

            if self.stopping or halt.is_set():
                connlog.event(f"watch {host}: stopped after "
                              f"{time.monotonic() - opened:.1f}s")
                return
            # A stream that ran a while and dropped is a network event: come
            # back quickly. One that died at once means we are being refused,
            # and trying harder is what turns that into being banned.
            lived = time.monotonic() - opened
            if lived >= WATCH_HEALTHY:
                attempt = 0
            delay = (WATCH_RETRY_MIN if lived >= WATCH_HEALTHY
                     else min(delay * 2, WATCH_RETRY_MAX))
            wait = delay * random.uniform(0.75, 1.25)
            connlog.limited(("watch", host, "dropped", why.split(":")[0]),
                            f"watch {host}: dropped ({why}) after {lived:.1f}s"
                            + ("" if heard else ", never heard from")
                            + f"; retry in {wait:.1f}s")
            deadline = time.monotonic() + wait
            while (not (self.stopping or halt.is_set())
                   and time.monotonic() < deadline):
                if poke.wait(0.25):
                    poke.clear()    # a check-again: try the host now
                    # Something changed (a wake, a recheck), so the failures
                    # behind the doubled delay say nothing about the next try.
                    delay = WATCH_RETRY_MIN
                    connlog.limited(
                        ("watch", host, "poke"),
                        f"watch {host}: poked, retrying now "
                        f"({deadline - time.monotonic():.1f}s of backoff cut)")
                    break
        connlog.event(f"watch {host}: stopped (between connections)")

    def drain_frames(self) -> bool:
        dirty = False
        while True:
            try:
                host, frame = self.frames.get_nowait()
            except queue.Empty:
                break
            row = self.rows.get(host)
            if row is None:
                continue
            known = {s["name"] for s in row["sessions"]}
            if set(frame) != known:
                # The stream only carries screens, so a session appearing or
                # going away needs a real probe to fill in the rest.
                if host not in self.inflight:
                    self.inflight.add(host)
                    threading.Thread(target=self._probe, args=(host,),
                                     daemon=True).start()
                continue
            for session in row["sessions"]:
                data = frame.get(session["name"])
                if not data:
                    continue
                session["screen"] = data["screen"]
                session["agent"] = agent_state.classify(
                    data["screen"], data["commands"], data.get("raw", ""))
                # The chat metadata rides in the same frame: a star or a
                # title set from another client lands here, not at the next
                # probe minutes later.
                for field in META_KEYS:
                    if field in data:
                        session[field] = data[field]
                self.streamed[host] = time.monotonic()
                self.heard[host] = time.time()
                dirty = True
        if dirty:
            self.render()
        return GLib.SOURCE_CONTINUE

    # -- leaving -----------------------------------------------------------

    def shut_down(self, *_args) -> bool:
        self.stopping = True
        connlog.event(f"window closing: stopping {len(self.streams)} "
                      f"stream{'' if len(self.streams) == 1 else 's'}")
        connlog.flush(force=True)
        self.save_known()       # so "last seen" is not up to KNOWN_FLUSH old
        # A star still on its way gets a few seconds to land. One that does
        # not is said out loud -- the window is going, so stderr, which the
        # journal keeps -- rather than coming back unstarred next launch.
        for host, session, _name in self.writer.stop():
            print(f"conn: star on {host}/{session} not saved -- the server "
                  "did not answer before quit", file=sys.stderr)
        for _thread, halt, _poke in list(self.streams.values()):
            halt.set()
        with self.watch_lock:
            procs = list(self.watchers.values())
        for proc in procs:
            try:
                proc.terminate()
            except Exception:
                pass
        return False


class ConnApp(Gtk.Application):
    def __init__(self, on_startup=None):
        super().__init__(application_id=APP_ID)
        # main.py's connection-log start. Called from do_startup, which
        # GApplication runs only in the primary instance: a second `conn`
        # registers as a remote, forwards 'activate' and exits without it.
        self.on_startup = on_startup

        # Clicking a notification lands here, whether or not the window was
        # the thing you were looking at.
        action = Gio.SimpleAction.new("open-session", GLib.VariantType("s"))
        action.connect("activate", self.open_from_notification)
        self.add_action(action)

        # `conn --notify` asks for this over D-Bus. It has to be the running
        # instance that sends: GLib's freedesktop backend delivers a click
        # back to the process that sent the notification, so one sent by a
        # command that then exits can never be clicked.
        sample = Gio.SimpleAction.new("notify-test", None)
        sample.connect("activate", lambda *_: self.send_sample_notification())
        self.add_action(sample)

        # `conn --quick-add` (Super+N): the one-line launcher over whatever
        # you are doing. If conn was not running the bus starts it for this,
        # and the main window is built but not shown.
        quick = Gio.SimpleAction.new("quick-add", None)
        quick.connect("activate", lambda *_: self.window().quick_add())
        self.add_action(quick)

        # Stop on the "Started X on Y" notification: Esc into the chat.
        stop = Gio.SimpleAction.new("interrupt-chat", GLib.VariantType("s"))
        stop.connect("activate", lambda _a, target: self.window().interrupt(
            *split_ref(target.get_string())))
        self.add_action(stop)

    def send_notification(self, ident, note) -> None:  # noqa: D401
        """Every notification the window sends comes through here, so the
        dev copy's are off in one place (paths.notifications): it watches the
        same chats as the stable conn, and with both sending, every needs-you
        and every finish would arrive twice. CONN_DEV_NOTIFY=1 turns them
        back on, for working on the notifications themselves."""
        if paths.NOTIFY:
            Gtk.Application.send_notification(self, ident, note)

    def send_sample_notification(self) -> None:
        """One notification about a real session, for looking at.

        The probe runs on a worker: it is a subprocess with a 15s ceiling,
        and this is called on the main loop by a D-Bus action.
        """
        def work():
            row = hosts.probe(hosts.LOCAL)
            sessions = row.get("sessions") or []
            if not sessions:
                return
            # Prefer one actually waiting on you: that is the notification
            # worth seeing, and the only kind that goes out at HIGH.
            session = next((s for s in sessions
                            if s["agent"]["state"] in (agent_state.NEEDS_YOU,
                                                       agent_state.DRAFT)),
                           sessions[0])
            def send():
                # Past the dev gate: `conn-dev --notify` is a request to see
                # one, not a chat that both copies noticed.
                Gtk.Application.send_notification(
                    self, f"conn-{session_ref(hosts.LOCAL, session['name'])}",
                    notification(hosts.LOCAL, session))
                return GLib.SOURCE_REMOVE
            GLib.idle_add(send)
        threading.Thread(target=work, name="notify-test", daemon=True).start()

    def open_from_notification(self, _action, target):
        """Open the session a notification was about.

        The window may not exist. A notification outlives the process that
        sent it, so clicking one an hour later is how the app gets started --
        the action arrives first and do_activate() never runs. Returning early
        when there was no window was why clicking did nothing at all.
        """
        host, name = split_ref(target.get_string())
        window = self.window()
        window.present()
        window.open_session(host, name)

    def window(self) -> "Conn":
        """The window -- the existing one, or a new one if there is none.

        Not active_window. GtkApplication reports that as None whenever none
        of its windows has focus, which is exactly the state you are in when
        you click a notification: you were somewhere else, that is why it was
        sent. So the old code built a *second* window every time, presented
        it, and opened the session in that one -- the app appeared to come up
        and then not go anywhere, because the session did open, in a window
        that was not the one you had.
        """
        windows = self.get_windows()
        return windows[0] if windows else Conn(self)

    def do_startup(self):
        Gtk.Application.do_startup(self)
        if self.on_startup is not None:
            try:
                self.on_startup()
            except Exception:   # noqa: BLE001 -- a log is never worth a crash
                pass

    def do_activate(self):
        window = self.window()
        window.present()


# CONN_CHANNEL and CONN_DEV_NOTIFY as this process was started with them,
# kept for restart() after run() has taken them out of the environment.
CHANNEL_ENV: dict[str, str] = {}


def run(service: bool = False, on_startup=None) -> int:
    """service is a D-Bus activation start (org.omarchy.conn.service).

    The flag is GApplication's own, so it is handed back as an argument
    rather than translated into ApplicationFlags here: parsing it puts the
    app in service mode -- register, wait for the activation that caused
    the start, exit quietly if none arrives -- which is what keeps `conn
    --notify` from flashing a window open just to send a notification.

    on_startup runs once this process is known to be the primary instance
    (ConnApp.do_startup) -- main.py starts the connection log there.
    """
    argv = [sys.argv[0], "--gapplication-service"] if service else None
    # Read already (paths, at import); out of the environment now, before
    # anything is started that would inherit it (paths.keep_to_self).
    CHANNEL_ENV.update(paths.keep_to_self())
    return ConnApp(on_startup).run(argv)


def send_test_notification() -> int:
    """Ask the running conn to send one, and let it own the click.

    Not sent from here. GLib's freedesktop backend -- which is what is in use,
    there being no org.gtk.Notifications on this session -- routes a click
    back to the application that sent the notification, so one sent by a
    command that exits a moment later is dead on arrival. The daemon has
    nothing to deliver to and the click does nothing at all, which is exactly
    what it looked like.

    So this activates an action on the app instead. org.freedesktop.Application
    is the interface DBusActivatable=true advertises, and the installed
    org.omarchy.conn.service file is what lets the bus start conn to answer
    this call when conn is not already running.
    """
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(
            APP_ID, "/" + APP_ID.replace(".", "/"),
            "org.freedesktop.Application", "ActivateAction",
            GLib.Variant("(sava{sv})", ("notify-test", [], {})),
            None, Gio.DBusCallFlags.NONE, -1, None)
    except GLib.Error as exc:
        print(f"could not reach conn: {exc.message}", file=sys.stderr)
        return 1
    print("asked conn to send one -- click it to prove the round trip")
    return 0


def ask_running(action: str) -> int:
    """Activate one of the app's actions over D-Bus -- in the running conn,
    or in one the bus starts for it (org.omarchy.conn.service). Quick add
    goes this way so Super+N never starts a second copy."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(
            APP_ID, "/" + APP_ID.replace(".", "/"),
            "org.freedesktop.Application", "ActivateAction",
            GLib.Variant("(sava{sv})", (action, [], {})),
            None, Gio.DBusCallFlags.NONE, -1, None)
    except GLib.Error as exc:
        print(f"could not reach conn: {exc.message}", file=sys.stderr)
        return 1
    return 0
