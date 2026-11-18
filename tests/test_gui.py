"""What the window does, checked without a pair of hands.

Every bug this file covers was found by hand first, which is the argument for
it: switching that moved the terminal and left the list behind, a menu whose
parent was rebuilt out from under it, controls kept somewhere fullscreen takes
away. None of those are visible to a test of hosts.py, and all of them are one
assertion each here.

Needs a display, because GTK needs one -- it skips rather than fails without.
Real tmux sessions are created for it -- on a tmux server of its own
(tmux -L conn-test-gui-<pid>, reached through a `tmux` shim on PATH), never
on yours, so the window's "local" host sees only the test's sessions and the
recency stamps it writes could not land on a session of yours even if the
writer were not stubbed. It is: hosts.set_chat_option only records here.
The server is killed at the end. The run is pointed at an ssh config, star
file, zoom file, filters file, projects.toml and mount root of its own
before the window is built, so nothing remote is touched and nothing of
yours is rewritten. The one check against real user state -- the keyring
round trip -- only runs with CONN_TEST_KEYRING=1, and clears what it stored
either way.

    python3 tests/test_gui.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("CONN_NO_WATCH", "1")
# Never call the real claude CLI for titles from a test.
os.environ["CONN_NO_RETITLE"] = "1"
# The stable channel, whatever terminal this was started from: the checks
# below are of conn, and test_paths.py covers the dev copy.
os.environ.pop("CONN_CHANNEL", None)

SESSIONS = ("conntest-alpha", "conntest-beta")
# Made by the window itself, the way you make one from the + button:
# it exists before anything has told the list about it.
NEW_SESSION = "conntest-gamma"


def display() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


# The shim main() puts on PATH, pointing every tmux call -- the test's, the
# window's probe, the terminals' attach -- at this run's own server.
TMUX = ["tmux"]

# Every hosts.set_chat_option call the window makes, recorded instead of run.
WRITES: list = []


def tmux(*args: str) -> str:
    done = subprocess.run([*TMUX, *args], capture_output=True, text=True)
    return done.stdout


def sessions_now() -> list[str]:
    return tmux("list-sessions", "-F", "#{session_name}").split()


class Checks:
    def __init__(self):
        self.failures = 0

    def __call__(self, name: str, passed: bool, detail: str = "") -> None:
        self.failures += not passed
        print(f"{'ok  ' if passed else 'FAIL'} {name}"
              + (f"   {detail}" if detail and not passed else ""))


def walk(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from walk(child)
        child = child.get_next_sibling()


def main() -> int:
    if not display():
        print("skipped: no display, and GTK needs one")
        return 0

    import tempfile

    # The window probes every host its config lists and writes stars and zoom
    # where the module constants point, so the fences go up before anything
    # is imported or built: a config of our own -- only "local" is listed,
    # so no ssh ever leaves this machine -- state files of our own, and a
    # mount root of our own for the tidy_mounts() run at construction.
    keep = tempfile.TemporaryDirectory(prefix="conn-test-",
                                       ignore_cleanup_errors=True)
    state = Path(keep.name)
    (state / "config").write_text("")
    os.environ["CONN_SSH_CONFIG"] = str(state / "config")

    # A tmux of our own. The shim is first on PATH, so the window's probe of
    # "local" (bash running `tmux list-sessions`), the attach a terminal view
    # runs, and tmux() below all reach this server and no other. TMUX is
    # unset so nothing here believes it is inside one of your sessions.
    import shutil
    real = shutil.which("tmux")
    sock = f"conn-test-gui-{os.getpid()}"
    shims = state / "bin"
    shims.mkdir()
    shim = shims / "tmux"
    shim.write_text(f'#!/bin/sh\nexec {real} -L {sock} -f /dev/null "$@"\n')
    shim.chmod(0o755)
    os.environ["PATH"] = f"{shims}{os.pathsep}{os.environ['PATH']}"
    os.environ.pop("TMUX", None)
    TMUX[:] = [str(shim)]

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Vte", "3.91")
    from gi.repository import GLib, Gtk
    gi.require_version("Graphene", "1.0")
    from gi.repository import Graphene

    import agent_state
    import gui
    import hosts

    import chats

    gui.STARS_STATE = state / "starred"
    gui.ORDER_STATE = state / "order"
    gui.ZOOM_STATE = state / "zoom"
    gui.EXPANDED_STATE = state / "expanded"
    hosts.MNT_ROOT = state / "mnt"
    chats.FILTERS_STATE = state / "filters.json"
    chats.PROJECTS_FILE = state / "projects.toml"
    chats.SEEN_STATE = state / "seen.json"
    chats.KNOWN_STATE = state / "known-chats.json"
    # Recorded, never run: the recency stamps and stars would otherwise go
    # over ssh (or to tmux) for every chat the window sees.
    hosts.set_chat_option = lambda *args: WRITES.append(args)

    check = Checks()
    option_writer(check, gui, hosts)
    hosts.set_chat_option = lambda *args: WRITES.append(args)
    for name in SESSIONS:
        tmux("new-session", "-d", "-s", name, "bash --norc")

    # The window builds on activate, but its first probe lands on a worker,
    # so the checks wait for its rows to arrive rather than guessing how long
    # that takes. The deadline turns a probe that never returns into failures
    # over the listing that actually arrived, not a hang.
    class Panel(Gtk.Application):
        def do_activate(self):
            window = gui.Conn(self)
            wanted = {("local", name) for name in SESSIONS}
            deadline = time.monotonic() + 30.0

            def listed():
                return {("local", s["name"])
                        for s in window.rows["local"]["sessions"]}

            def settled():
                if not (wanted <= listed()
                        or time.monotonic() >= deadline):
                    return True
                # The test's sessions are plain shells, which live in the
                # Servers tab; most of what follows is about their rows --
                # so "local" is opened out there, the tab folding its servers
                # by default (servers_tab() checks the folding itself).
                window.expanded.add("local")
                window.set_sidebar_mode("servers")
                window.render()
                try:
                    run(window, check, gui, hosts, agent_state, Gtk)
                except Exception:
                    # A crash is a verdict too. PyGObject swallows one thrown
                    # out of a callback, so left uncounted it skipped every
                    # check after it and still printed "all good".
                    check("the checks themselves ran to completion", False,
                          traceback.format_exc())
                finally:
                    try:
                        for session in list(window.open.values()):
                            window.close_session(session)
                    except Exception:
                        check("and cleaned up after themselves", False,
                              traceback.format_exc())
                    self.quit()
                return False

            GLib.timeout_add(200, settled)

    Panel(application_id="org.omarchy.conn.test").run(None)

    tmux("kill-server")
    socket = Path(f"/tmp/tmux-{os.getuid()}") / sock
    socket.unlink(missing_ok=True)
    keep.cleanup()

    print(f"\n{'all good' if not check.failures else str(check.failures) + ' failed'}")
    return 1 if check.failures else 0


def run(window, check, gui, hosts, agent_state, Gtk) -> None:
    alpha = ("local", SESSIONS[0])
    beta = ("local", SESSIONS[1])

    # Nothing in here should reach the desktop. A test run that leaves four
    # toasts in your notification centre is a test run you stop wanting to
    # run, and the states below are poked by hand precisely to fire them.
    sent: list = []
    window.notify = lambda host, s, finished=False: sent.append(
        (host, s["name"], s["agent"]["state"]))
    # And the rest -- "Started X on Y", "Saved to notes" -- go straight to
    # the application. Broadway does not stop them reaching the real desktop.
    window.get_application().send_notification = lambda *a, **k: None

    check("the stable window is called conn, with no dev mark",
          window.get_title() == "conn"
          and not any(isinstance(w, Gtk.Label) and w.has_css_class("channel")
                      for w in walk(window)),
          f"title={window.get_title()!r}")

    check("the app tells the desktop what it is called",
          gui.GLib.get_application_name() == "conn",
          f"name={gui.GLib.get_application_name()!r} -- "
          "an unnamed app is announced as python3")

    check("the local host lists its sessions",
          alpha in window.slots and beta in window.slots,
          f"slots={window.slots}")

    # -- switching ---------------------------------------------------------
    window.open_session(*alpha)
    window.open_session(*beta)

    def showing():
        current = window.stack.get_visible_child()
        return (current.host, current.name) if isinstance(current, gui.Session) else None

    def highlighted():
        row = window.list.get_selected_row()
        return getattr(row, "key", None) if row is not None else None

    check("opening one shows it", showing() == beta)
    window.show_nth(window.slots.index(alpha))
    check("alt-N goes to the numbered session", showing() == alpha)
    check("and the list agrees with the terminal", highlighted() == alpha,
          f"showing={showing()} highlighted={highlighted()}")
    window.cycle(1)
    check("ctrl-tab moves and the list follows",
          showing() == highlighted() and showing() != alpha)

    window.close_visible()
    check("closing a view falls back to one that is open",
          isinstance(window.stack.get_visible_child(), gui.Session))
    check("and the highlight follows that too", showing() == highlighted())

    # -- one made before the list has heard of it --------------------------
    # A session from the + button is running in the terminal a second before
    # the probe brings its row in. The highlight cannot go anywhere yet, and
    # the render that brings the row in used to put it back where it was --
    # so the list sat pointing at the session you had just left.
    fresh = ("local", NEW_SESSION)
    window.open_session(*fresh)
    check("a session opens before it is listed", showing() == fresh,
          f"showing={showing()}")
    check("and the highlight is owed to it", window.pending == fresh,
          f"pending={window.pending}")
    listed = window.rows["local"]["sessions"]
    listed.append(dict(listed[0], name=NEW_SESSION,
                       agent=dict(listed[0]["agent"], state=agent_state.READY,
                                  label="idle", detail="")))
    window.render()
    check("and it takes the highlight as soon as its row arrives",
          highlighted() == fresh,
          f"highlighted={highlighted()} showing={showing()}")
    check("which settles the debt", window.pending is None,
          f"pending={window.pending}")

    # The other half of it: the debt is only owed while that session is the
    # one on screen. Go somewhere else first and the highlight stays there.
    listed.pop()
    window.render()
    check("dropping the row leaves the highlight owed again",
          window.pending == fresh, f"pending={window.pending}")
    window.open_session(*alpha)
    listed.append(dict(listed[0], name=NEW_SESSION,
                       agent=dict(listed[0]["agent"], state=agent_state.READY,
                                  label="idle", detail="")))
    window.render()
    check("a session you have left behind does not take the highlight back",
          highlighted() == alpha, f"highlighted={highlighted()}")

    # -- the list under a changing screen ----------------------------------
    # The session becomes a chat first (an agent idle in it): a shell turning
    # into a chat is a new row in the chat list, which is a change of shape.
    # What must not rebuild is a chat whose state changes under it.
    session = next(s for s in window.rows["local"]["sessions"]
                   if s["name"] == alpha[1])
    session["agent"] = dict(session["agent"], state=agent_state.READY,
                            label="idle", detail="")
    window.render()
    row = next((r for r in rows(window) if getattr(r, "key", None) == alpha), None)
    before = id(row)
    session["agent"] = dict(session["agent"], state=agent_state.NEEDS_YOU,
                            label="needs you", detail="Do you want to")
    window.render()
    row = next((r for r in rows(window) if getattr(r, "key", None) == alpha), None)
    check("a screen changing repaints rather than replaces them",
          id(row) == before,
          "rebuilding rows kills the menu parented to one")
    check("and the mark changes with it",
          window.widgets[alpha]["mark"].has_css_class("amber"))

    # -- the condition of the whole fleet, in one bar -----------------------
    # The bar sums over every session on the machine, including whatever you
    # happen to be running right now, so the fleet is quieted first. Without
    # this the check passes or fails depending on what is busy in another
    # window, which is not a property of the code.
    for other in window.order:
        for quiet in window.rows[other]["sessions"]:
            quiet["agent"] = dict(quiet["agent"], state=agent_state.READY,
                                  label="idle", detail="")
    session["agent"] = dict(session["agent"], state=agent_state.NEEDS_YOU,
                            label="needs you", detail="Do you want to")
    window.render()
    check("one session waiting puts the fleet at red alert",
          window.alert == agent_state.NEEDS_YOU, f"alert={window.alert}")
    session["agent"] = dict(session["agent"], state=agent_state.WORKING,
                            label="working", detail="")
    window.render()
    check("nothing waiting but something working is yellow",
          window.alert == agent_state.WORKING, f"alert={window.alert}")
    check("and the words for it say so too",
          window.alert_text.endswith("working")
          and window.alert_text in (window.condition.get_tooltip_text() or ""),
          f"alert_text={window.alert_text!r}")
    session["agent"] = dict(session["agent"], state=agent_state.READY,
                            label="idle", detail="")
    window.render()
    # Working -> idle is a turn finishing. Until it is opened it waits on
    # you (decision D), unless it is the chat on screen in a window in use.
    cid = gui.chats.chat_id(*alpha)
    if cid in window.chat_index and window.visible_chat() != cid:
        check("a chat that finished while you were elsewhere waits on you",
              window.alert == agent_state.NEEDS_YOU
              and window.chat_index[cid].finished, f"alert={window.alert}")
    window.mark_seen(cid)
    window.render()
    check("and a quiet fleet is all clear once it has been seen",
          window.alert == agent_state.READY, f"alert={window.alert}")
    check("in both places",
          window.alert_text == "all clear",
          f"alert_text={window.alert_text!r}")

    # -- notifications -----------------------------------------------------
    sent.clear()
    window.render()
    check("no notification while the state stands", not sent)
    session["agent"] = dict(session["agent"], state=agent_state.READY, label="idle")
    window.render()
    session["agent"] = dict(session["agent"], state=agent_state.NEEDS_YOU,
                            label="needs you", detail="Do you want to")
    window.render()
    check("one when it starts waiting on you again",
          [s[2] for s in sent] == [agent_state.NEEDS_YOU], f"sent={sent}")

    # A draft is text you are in the middle of typing until it stops changing,
    # so it is announced on the silence, not on the first keystroke. The clock
    # watches the box, not the detail: the detail carries a background-work
    # suffix that ticks every frame, and a clock keyed on it never settles.
    sent.clear()
    session["agent"] = dict(session["agent"], state=agent_state.DRAFT,
                            label="unsent draft", detail="what's the health",
                            draft="what's the health")
    window.render()
    check("typing is not an interruption", not sent, f"sent={sent}")

    def rewind(by=gui.DRAFT_DWELL + 1):
        text, since, told = window.drafts[alpha]
        window.drafts[alpha] = (text, since - by, told)

    session["agent"] = dict(session["agent"], draft="what's the health check")
    rewind()
    window.render()
    check("and an edit puts the clock back", not sent, f"sent={sent}")

    # Background work ticking in the detail is not an edit -- the box is
    # unchanged, so the clock keeps running and the settled draft is said.
    session["agent"] = dict(session["agent"],
                            detail="what's the health check  (running tests)")
    rewind()
    window.render()
    check("a draft left sitting is worth saying",
          [s[2] for s in sent] == [agent_state.DRAFT], f"sent={sent}")
    rewind()
    window.render()
    check("but only the once", len(sent) == 1, f"sent={sent}")

    # -- filter ------------------------------------------------------------
    # The host rows under Servers; the chat rows were filtered before they
    # were built, and the fleet was quieted into idle chats above.
    window.filter.set_text(SESSIONS[0])
    check("filtering keeps what matches",
          window.matches(server_row(window, alpha)))
    check("and drops what does not",
          not window.matches(server_row(window, beta)))
    window.filter.set_text("local")
    check("a host name keeps the sessions under it",
          window.matches(server_row(window, alpha)))
    window.clear_filter()

    # -- controls that must outlive fullscreen -----------------------------
    icons = [c.get_icon_name() for c in walk(window.side)
             if isinstance(c, Gtk.Button) and c.get_icon_name()]
    footer_menu = [getattr(b, "label", None) for b in
                   walk(window.menu_button.get_popover())
                   if isinstance(b, Gtk.Button)]
    check("every action lives in the sidebar",
          "network-server-symbolic" in icons
          and window.menu_button.is_ancestor(window.side)
          and {"Add server…", "Edit projects…", "Refresh now",
               "Keys and marks…", "Close Conn"} <= set(footer_menu)
          and window.new_button.is_ancestor(window.side),
          f"icons={icons} menu={footer_menu}")
    # -- one window, however the app is reached -----------------------------
    # A notification click arrives while conn is unfocused, which is exactly
    # when GtkApplication reports no active window at all. Reaching for that
    # built a second window every time, presented it, and opened the session
    # in the one you were not looking at.
    app = window.get_application()
    check("the app reuses the window it already has",
          gui.ConnApp.window(app) is window,
          "a second window is what a notification click used to open")

    # -- the sidebar has to survive its own footer -------------------------
    side = window.side
    window.updated.set_visible(True)
    window.unlock.set_visible(True)
    smallest, _ = side.get_preferred_size()
    check("the sidebar still fits when the footer buttons appear",
          smallest.width <= gui.SIDEBAR_WIDTH,
          f"minimum={smallest.width}px against a {gui.SIDEBAR_WIDTH}px "
          "sidebar -- one that cannot fit its own minimum is drawn clipped")
    check("and the list cannot be resized by accident",
          not isinstance(window.get_child(), Gtk.Paned),
          "a drag handle beside a list you click all day is a handle you "
          "catch by mistake")

    # Ellipsizing the names stopped a long one widening the sidebar, but it
    # also let a squeezed sidebar shrink "local" to "lo..."; a width-chars
    # floor is what puts the readable minimum back.
    host_row = next(r for r in rows(window)
                    if getattr(r, "server", None))
    host_label = next(w for w in walk(host_row)
                      if isinstance(w, Gtk.Label) and w.has_css_class("host"))
    check("a host name keeps a width-chars floor, so it cannot shrink to 'lo...'",
          host_label.get_width_chars() == gui.NAME_FLOOR
          and int(host_label.get_ellipsize()) != 0,
          f"width_chars={host_label.get_width_chars()} "
          f"ellipsize={int(host_label.get_ellipsize())}")
    least, _ = host_row.get_preferred_size()
    check("so the row will not collapse a short name when the sidebar is squeezed",
          least.width >= 60,
          f"row minimum={least.width}px -- an ellipsis-only floor is ~17px")

    window.updated.set_visible(False)
    window.unlock.set_visible(False)

    # A server you just added, whose key ssh will not trust yet, is first
    # contact -- not a dead box. Its row says "new host key" in the accent
    # colour, not a red "can't reach", so it reads as "trust me", not
    # "broken".
    window.rows["newbox"] = dict(hosts.blank("newbox"), state="unverified",
                                 error="new server -- log in")
    window.rows["deadbox"] = dict(hosts.blank("deadbox"), state="down",
                                  error="timed out")
    try:
        first = window._server_row("newbox", "unverified", "trust")
        notes = [w for w in walk(first) if isinstance(w, Gtk.Label)
                 and w.get_label().startswith("new host key")]
        check("a first-contact host says 'new host key', standing out from a "
              "dead one",
              len(notes) == 1 and notes[0].has_css_class("attention"),
              f"labels={[w.get_label() for w in walk(first) if isinstance(w, Gtk.Label)]}")
        down = window._server_row("deadbox", "down", "recheck")
        check("while a genuinely down host stays a muted \"can't reach\", "
              "with ssh's reason",
              any(isinstance(w, Gtk.Label)
                  and w.get_label() == "can't reach \u00b7 timed out"
                  and w.has_css_class("detail") for w in walk(down)),
              f"labels={[w.get_label() for w in walk(down) if isinstance(w, Gtk.Label)]}")
    finally:
        window.rows.pop("newbox", None)
        window.rows.pop("deadbox", None)
        window.host_widgets.pop("newbox", None)
        window.host_widgets.pop("deadbox", None)

    check("there is no title bar to lose them with",
          window.get_titlebar() is None and not window.get_decorated())

    # -- the key guide, which is only useful if it is complete -------------
    guide = " ".join(w.get_label() or "" for w in walk(
        window.guide.get_child())
        if isinstance(w, Gtk.Label)).lower()
    # Add the row when you add the key: a guide that lists most of them reads
    # as the whole set, and the ones left out are the ones nobody finds.
    for wanted in ("alt-1..9", "ctrl-tab", "f12", "ctrl-shift-c / v",
                   "ctrl-shift-a", "ctrl-shift-w", "ctrl-shift-k", "ctrl-f",
                   "ctrl-+ - 0", "ctrl-shift-r", "ctrl-shift-s",
                   "f11 / ctrl-q", "f1 or ?",
                   "ctrl-click", "right-click a session", "right-click a server",
                   "ctrl-1 / ctrl-2 / ctrl-3", "click a server", "servers tab",
                   "right-click the screen", "hover a session"):
        check(f"the guide has {wanted}", wanted in guide,
              "a key nobody can find is a key that does not exist")
    _, natural = window.guide.get_child().get_preferred_size()
    check("and fits on a laptop screen, popovers being clipped and not scrolled",
          natural.height <= 600, f"height={natural.height}")
    check("and says what every mark means",
          all(word in guide for word in ("needs you", "unsent draft", "working",
                                         "idle", "shell", "unknown")))

    # -- font, and the size you set it to ----------------------------------
    import tempfile
    import theming

    with tempfile.TemporaryDirectory() as tmp:
        conf = Path(tmp) / "foot.ini"
        conf.write_text("[main]\n# a comment\n"
                        "font=JetBrainsMono Nerd Font:size=9, Noto Emoji:size=9\n")
        # "sh" stands in for the terminal's binary: what is being checked is
        # that a config is only read when that terminal is actually installed.
        found = theming.terminal_font([("sh", conf, theming._foot_font)])
        check("the session font comes from this machine's terminal",
              found == "JetBrainsMono Nerd Font 9", f"font={found}")
        check("a terminal that is not installed is not consulted",
              theming.terminal_font([("no-such-terminal-xyz", conf,
                                      theming._foot_font)]) == theming.FALLBACK_FONT)

        os.environ["CONN_FONT"] = "Fira Code 12"
        check("and CONN_FONT wins over both",
              theming.terminal_font([("sh", conf, theming._foot_font)])
              == "Fira Code 12")
        del os.environ["CONN_FONT"]

        was_zoom, gui.ZOOM_STATE = gui.ZOOM_STATE, Path(tmp) / "zoom"
        try:
            window.set_zoom(1.3)
            scales = [s.term.get_font_scale() for s in window.open.values()]
            check("zooming resizes every open session", scales and
                  all(abs(scale - 1.3) < 0.001 for scale in scales), f"scales={scales}")
            check("and it survives the next run", abs(gui.read_zoom() - 1.3) < 0.001,
                  f"read={gui.read_zoom()}")
            window.set_zoom(99.0)
            check("nothing can be zoomed off the screen", window.zoom == gui.ZOOM_MAX,
                  f"zoom={window.zoom}")
            window.set_zoom(1.0)
        finally:
            gui.ZOOM_STATE = was_zoom

    # -- the colours reach the widgets -------------------------------------
    # test_theming.py holds the palette to its floors; this is that the CSS
    # carries it, measured on what GTK actually resolved for a heading --
    # the label that used to be drawn in the theme's border shade.
    headings = [c for c in walk(window.list)
                if isinstance(c, Gtk.Label) and c.has_css_class("host")]
    if headings:
        drawn = headings[0].get_color()
        drawn_hex = "#" + "".join(f"{round(v * 255):02x}" for v in
                                  (drawn.red, drawn.green, drawn.blue))
        ratio = theming.contrast(drawn_hex, window.palette.panel) or 0
        check("a host heading is legible on the sidebar",
              ratio >= theming.LEGIBLE - 0.05,
              f"{drawn_hex} on {window.palette.panel} = {ratio:.2f} "
              f"({window.palette.source})")
    else:
        check("the sidebar has a host heading to measure", False)

    def drawn_in(label) -> str:
        colour = label.get_color()
        return "#" + "".join(f"{round(v * 255):02x}" for v in
                             (colour.red, colour.green, colour.blue))

    # A mark coloured by a Pango attribute outranks the stylesheet, so on the
    # highlighted row it stayed its own colour on the accent -- a shell's "."
    # at 1:1 on Nord. get_color() only tells the truth once the colour is CSS.
    held = window.list.get_selected_row()
    session_rows = [r for r in rows(window) if getattr(r, "key", None)
                    and getattr(r, "zone", "") == "servers"]
    if len(session_rows) >= 2:
        chosen, other = session_rows[0], session_rows[1]
        window.list.select_row(chosen)
        # The session rows' marks are dots in their state's tone, by class --
        # never a Pango attribute, which outranks the stylesheet -- and the
        # selected row is a card that keeps every tone, as the chat rows do.
        def tone_of(row):
            marks = [c for c in walk(row) if c.has_css_class("mark")]
            state = next(s for s in window.rows[row.key[0]]["sessions"]
                         if s["name"] == row.key[1])["agent"]["state"]
            return marks, state
        marks, state = tone_of(chosen)
        check("a mark on the highlighted row keeps its state's tone",
              bool(marks) and marks[0].has_css_class(gui.mark_tone(state))
              and not isinstance(marks[0], Gtk.Label),
              f"marks={marks} state={state}")
        other_marks, state = tone_of(other)
        check("and one on any other row too",
              bool(other_marks)
              and other_marks[0].has_css_class(gui.mark_tone(state)),
              f"state={state}")
        check("the selected session is the chat rows' card, not the accent",
              chosen.has_css_class("session"))
        if held is not None:
            window.list.select_row(held)
        else:
            window.list.unselect_all()
    else:
        check("the sidebar has two sessions to compare", False)

    # -- killing -----------------------------------------------------------
    kills = [c for c in walk(row_for(window, alpha))
             if isinstance(c, Gtk.Button) and c.has_css_class("kill")]
    check("each session carries its own kill", len(kills) == 1)

    # -- copying out of a session that has the mouse ------------------------
    session_view = window.open[beta] if beta in window.open else None
    if session_view is None:
        window.open_session(*beta)
        session_view = window.open[beta]
    session_view.menu(0.0, 0.0)
    menu = [c for c in walk(session_view.term) if isinstance(c, Gtk.Popover)]
    copy_labels = [b.get_label() for b in walk(menu[-1])
                   if isinstance(b, Gtk.Button)] if menu else []
    for pop in menu:
        pop.popdown()
    check("with nothing selected, copy says how to select",
          any("hold shift" in (label or "") for label in copy_labels),
          f"menu={copy_labels}")
    check("and the whole screen can be taken without selecting at all",
          any("whole screen" in (label or "") for label in copy_labels),
          f"menu={copy_labels}")

    window.show(session_view)
    session_view.term.unselect_all()
    window.on_terminal("copy")
    check("copying nothing says so, rather than looking like a dead key",
          "nothing selected" in window.footnote.get_text(),
          f"footer={window.footnote.get_text()!r}")
    window.copy_screen()
    check("and copying the screen reports what it took",
          "screen" in window.footnote.get_text(),
          f"footer={window.footnote.get_text()!r}")
    window.said = ("", 0.0)
    window.render()
    check("the footer goes back to the chat count afterwards",
          "chat" in window.footnote.get_text(),
          f"footer={window.footnote.get_text()!r}")

    # -- copying Claude's last reply -----------------------------------------
    # transcript.last_reply is the ssh; the clipboard is the desktop's. Both
    # stubbed, so nothing leaves this test and nothing lands on your
    # clipboard.
    check("the terminal menu offers Claude's last reply",
          "Copy Claude's last reply" in copy_labels, f"menu={copy_labels}")
    import sessionview
    import transcript
    from types import SimpleNamespace
    asked, copied = [], []
    answer = {"value": ("the reply, exactly", "transcript")}

    def fake_last_reply(host, name, screen=""):
        asked.append((host, name))
        return answer["value"]

    real_reply, real_clip = transcript.last_reply, gui.Session.to_clipboard
    transcript.last_reply = fake_last_reply
    gui.Session.to_clipboard = staticmethod(lambda text: copied.append(text))

    def settle(predicate, limit=5.0):
        ctx = gui.GLib.MainContext.default()
        deadline = time.monotonic() + limit
        while not predicate() and time.monotonic() < deadline:
            ctx.iteration(False)
            time.sleep(0.01)

    try:
        window.show(session_view)
        window.footnote.set_text("")
        handled = window.key_pressed(
            None, gui.Gdk.KEY_y, 0,
            gui.Gdk.ModifierType.CONTROL_MASK | gui.Gdk.ModifierType.SHIFT_MASK)
        settle(lambda: "reply" in window.footnote.get_text())
        check("ctrl-shift-y copies the open chat's last reply",
              handled and asked[-1:] == [beta]
              and copied[-1:] == ["the reply, exactly"],
              f"asked={asked} copied={copied}")
        check("and says so in the footer",
              window.footnote.get_text() == "Copied Claude's last reply",
              f"footer={window.footnote.get_text()!r}")

        answer["value"] = ("from the screen", "screen")
        window.footnote.set_text("")
        session_view.last_reply()
        settle(lambda: "reply" in window.footnote.get_text())
        check("the menu's item copies too, and a screen copy says so",
              copied[-1:] == ["from the screen"]
              and "from the screen" in window.footnote.get_text(),
              f"footer={window.footnote.get_text()!r}")

        answer["value"] = ("the turn before", "transcript-busy")
        window.footnote.set_text("")
        window.copy_reply(*beta)
        settle(lambda: "reply" in window.footnote.get_text())
        check("mid-turn, the copy says it is the previous reply",
              copied[-1:] == ["the turn before"]
              and "previous reply" in window.footnote.get_text()
              and "still working" in window.footnote.get_text(),
              f"footer={window.footnote.get_text()!r}")

        answer["value"] = ("", "")
        before = len(copied)
        window.footnote.set_text("")
        window.copy_reply(*beta)
        settle(lambda: "reply" in window.footnote.get_text())
        check("no reply anywhere leaves the clipboard alone, and says so",
              len(copied) == before
              and "No reply" in window.footnote.get_text(),
              f"footer={window.footnote.get_text()!r}")

        # The header's ⋯ menu, filled as ChatChrome fills it.
        answer["value"] = ("from the header", "transcript")
        popover = Gtk.Popover()
        chrome = SimpleNamespace(win=window, cid="%s/%s" % beta,
                                 session=session_view)
        sessionview.ChatChrome._fill_more(chrome, popover)
        items = [b for b in walk(popover) if isinstance(b, Gtk.Button)
                 and getattr(b, "label", "") == "Copy Claude's last reply"]
        hints = [w.get_label() for w in walk(items[0])
                 if isinstance(w, Gtk.Label)] if items else []
        check("the chat header's menu offers it, with its key",
              bool(items) and "ctrl-shift-y" in hints, f"hints={hints}")
        if items:
            asked.clear()
            items[0].emit("clicked")
            settle(lambda: copied[-1:] == ["from the header"])
            check("and copies the reply of the chat it heads",
                  asked == [beta] and copied[-1:] == ["from the header"],
                  f"asked={asked}")

    finally:
        transcript.last_reply = real_reply
        gui.Session.to_clipboard = real_clip
    window.said = ("", 0.0)
    window.render()

    # -- noticing it has been replaced on disk ------------------------------
    window.check_source()
    check("no restart nag while this is the installed version",
          not window.updated.get_visible())
    # As if an install had landed with cp -p: the mtimes it brings are the
    # source's own, so nothing on disk is newer -- but a file's size changed.
    path, (size, mtime) = next(iter(window.stamp.items()))
    window.stamp = {**window.stamp, path: (size + 1, mtime)}
    window.check_source()
    check("and one as soon as a different one is copied over it",
          window.updated.get_visible(),
          "three times running, a feature was missing only because the "
          "window was still the build from yesterday")
    window.stamp = gui.source_stamp()
    window.check_source()

    # -- the menus, which is where most of this is actually reached --------
    def menu_items(build, *args):
        """What a right-click offers. The popover is parented to the list, so
        it is still there to read after the call returns."""
        build(*args)
        popover = [c for c in walk(window.list) if isinstance(c, Gtk.Popover)]
        labels = [b.get_label() for b in walk(popover[-1])
                  if isinstance(b, Gtk.Button)] if popover else []
        for pop in popover:
            pop.popdown()
        return labels

    on_session = menu_items(window.row_menu, row_for(window, alpha), 0, 0)
    check("right-clicking a session offers to rename it",
          any("Rename" in (label or "") for label in on_session),
          f"menu={on_session}")
    check("as well as open and kill",
          any("Open" in (label or "") for label in on_session)
          and any("Kill" in (label or "") for label in on_session),
          f"menu={on_session}")
    on_host = menu_items(window.host_menu, row_for(window, alpha), "local", 0, 0)
    check("and a host offers a new session and its passwords",
          any("New session" in (label or "") for label in on_host)
          and any("Passwords" in (label or "") for label in on_host),
          f"menu={on_host}")
    check("and the live stream as Watch live, not a star",
          any(label in ("Watch live", "Stop watching live") for label in on_host)
          and not any(label in ("Star", "Unstar") for label in on_host),
          f"menu={on_host}")
    check("and a way to check it again, for one that came back down",
          any("Check again" in (label or "") for label in on_host),
          f"menu={on_host}")
    # "local" is invented when the config has no such entry, so there is no
    # line to take out and the menu must not offer to.
    check("and no offer to forget a host the config never named",
          any("Forget" in (label or "") for label in on_host)
          == ("local" in hosts.config_hosts()),
          f"menu={on_host}")
    # A host the config does name is offered for forgetting further down,
    # beside the forget flow itself, against a config of this run's own.

    # -- the new-session dialog names the host it is about ----------------
    def dialog_labels():
        """Every label in the modal that is currently up, with its classes."""
        tops = Gtk.Window.get_toplevels()
        found = []
        for i in range(tops.get_n_items()):
            top = tops.get_item(i)
            if top is window or not top.get_visible():
                continue
            for child in walk(top):
                if isinstance(child, Gtk.Label):
                    found.append((child.get_text(), child.get_css_classes()))
        return found, [tops.get_item(i) for i in range(tops.get_n_items())
                       if tops.get_item(i) is not window]

    window.prompt_new_session("local")
    labels, opened = dialog_labels()
    check("the new-session dialog says which host in its body",
          any(text == "local" for text, _ in labels),
          f"labels={[t for t, _ in labels]}")
    check("and sets it apart from the words around it",
          any(text == "local" and "heading" in classes
              for text, classes in labels),
          f"labels={labels}")
    for top in opened:
        top.destroy()

    asked = []
    real_ask = window.ask
    window.ask = lambda title, fields, done, **kw: asked.append(fields)
    try:
        saved = window.rows["local"]["sessions"]
        window.rows["local"]["sessions"] = [{"name": "shell"}, {"name": "shell-2"}]
        window.prompt_new_session("local")
        check("a new shell is offered a name not already taken -- one that "
              "is would just reopen that session",
              asked and asked[-1] == [("Name", "shell-3")], repr(asked))
    finally:
        window.ask = real_ask
        window.rows["local"]["sessions"] = saved

    # -- the ssh waits for a key rather than asking for one per connection --
    # Driven through show_agent directly: what matters is the decision, and
    # the real agent on the machine running this is unlocked either way.
    released = []
    real_connect = window.connect_hosts
    window.connect_hosts = lambda: (released.append(True),
                                    setattr(window, "holding", False))[0]
    window.holding = True
    window.show_agent(0)
    check("an agent holding nothing holds the connections back",
          window.holding and not released,
          f"holding={window.holding} released={released}")
    window.show_agent(None)
    check("and so does no agent at all",
          window.holding and not released,
          f"holding={window.holding} released={released}")
    window.show_agent(2)
    check("a key in it lets them go, once",
          released == [True] and not window.holding,
          f"holding={window.holding} released={released}")
    window.show_agent(2)
    check("and not again on the next sweep", released == [True],
          f"released={released}")
    window.connect_hosts = real_connect

    # -- renaming, which must not disturb what is running ------------------
    renamed = hosts.rename_session("local", alpha[1], alpha[1] + "-renamed")
    check("a session can be renamed", renamed == alpha[1] + "-renamed",
          f"got={renamed}")
    check("and tmux agrees", renamed in sessions_now(), f"left={sessions_now()}")
    was_open = window.open.get(alpha)
    window.renamed("local", alpha[1], renamed)
    check("the open view follows it",
          ("local", renamed) in window.open and alpha not in window.open,
          f"open={list(window.open)}")
    check("and it is the same terminal, not a new one",
          was_open is not None and window.open[("local", renamed)] is was_open)
    check("which now knows its own name",
          window.open[("local", renamed)].name == renamed)
    check("a name tmux cannot take is filtered, not refused",
          hosts.SESSION_NAME.sub("_", "two words.here") == "two_words_here")
    taken = None
    try:
        hosts.rename_session("local", renamed, beta[1])
    except hosts.HostError as exc:
        taken = str(exc)
    check("a name already in use comes back as tmux said it",
          taken and "duplicate" in taken, f"error={taken}")
    alpha = ("local", renamed)

    hosts.kill_session(*alpha)
    left = sessions_now()
    check("which ends that session", alpha[1] not in left, f"left={left}")
    check("and leaves the one beside it", beta[1] in left, f"left={left}")

    # -- links, including the ones tmux delivered in pieces -----------------
    wrapped = ("  Browser didn't open? Use the url below to sign in\n"
               "\n"
               "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a\n"
               "&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com\n"
               "&state=R5GTtVz03mLeyVZRzhDoEcK\n"
               "\n"
               "  Waiting, or press (c) to copy\n")
    found = hosts.screen_links(wrapped)
    check("a URL wrapped across rows comes back whole",
          found and found[0].endswith("R5GTtVz03mLeyVZRzhDoEcK") and len(found[0]) > 120,
          f"found={found}")
    check("and only that one", len(found) == 1, f"found={found}")
    prose = "see https://example.com/x, and (https://example.com/y) too\nplain text\n"
    check("ordinary links still come out one each",
          hosts.screen_links(prose) == ["https://example.com/x", "https://example.com/y"],
          f"found={hosts.screen_links(prose)}")
    check("two full-width lines are not glued into one",
          hosts.screen_links("a" * 130 + "\n" + "b" * 130 + "\n") == [])

    # -- forgetting a server ------------------------------------------------
    # Against a config of our own: the real one is not a fixture, and this
    # rewrites the file it is pointed at.
    import tempfile as _tempfile
    with _tempfile.TemporaryDirectory() as tmp:
        conf = Path(tmp) / "config"
        conf.write_text("Host alpha\n    HostName a.example\n"
                        "\n"
                        "Host beta gamma\n    HostName b.example\n"
                        "\n"
                        "# The defaults, which must stay last\n"
                        "Host *\n    ServerAliveInterval 30\n")
        was, hosts.SSH_CONFIG = hosts.SSH_CONFIG, conf
        try:
            backup = hosts.remove_host("alpha")
            left = conf.read_text()
            check("forgetting a server takes its whole block",
                  "alpha" not in left and "a.example" not in left, left)
            check("and keeps the old file beside it", backup.exists())
            check("and leaves every other block alone",
                  "Host beta gamma" in left and "Host *" in left, left)
            check("and does not eat the comment introducing the next block",
                  "# The defaults" in left, left)

            hosts.remove_host("gamma")
            left = conf.read_text()
            check("a name sharing a Host line is dropped from the line, "
                  "not the block",
                  "Host beta\n" in left and "b.example" in left, left)

            try:
                hosts.remove_host("nope")
                said = False
            except hosts.HostError:
                said = True
            check("and forgetting one that was never there says so", said)
        finally:
            hosts.SSH_CONFIG = was

    # -- starring: the few you actually work on -----------------------------
    # Against a state file of our own, or the run would rewrite yours.
    with _tempfile.TemporaryDirectory() as tmp:
        was, gui.STARS_STATE = gui.STARS_STATE, Path(tmp) / "starred"
        try:
            window.load_hosts(connect=False)
            check("a first run watches every server you already had live",
                  window.starred == set(window.order),
                  f"starred={window.starred}")
            check("and writes that down", gui.STARS_STATE.exists())

            live = next(s for s in window.rows["local"]["sessions"]
                        if s["name"] == beta[1])
            live["agent"] = dict(live["agent"], state=agent_state.NEEDS_YOU,
                                 label="needs you", detail="")
            window.toggle_star("local")
            check("not watching live takes a server out of the top list",
                  "local" not in window.starred, f"starred={window.starred}")
            check("but the Servers tab still lists it and its sessions -- "
                  "every server, live or not",
                  server_row(window, beta) is not None
                  and any(getattr(r, "server", None) == "local"
                          for r in rows(window)))
            check("and it is still counted -- unstarred is quieter, "
                  "not unwatched",
                  window.alert == agent_state.NEEDS_YOU,
                  f"alert={window.alert}")
            check("no row stands in for the unstarred any more",
                  not any(getattr(r, "rest", False) for r in rows(window)))

            window.filter.set_text(beta[1])
            window.refilter()       # GTK debounces search-changed; this is it
            check("a search turns up an unstarred server anyway",
                  server_row(window, beta) is not None,
                  "hiding what you are searching for is the one moment "
                  "this arrangement would be wrong")
            window.clear_filter()

            window.toggle_star("local")
            check("starring it again puts it back at the top",
                  "local" in window.starred
                  and server_row(window, beta) is not None)
        finally:
            gui.STARS_STATE = was
            window.load_hosts(connect=False)

    # -- a server with a chat on it is watched live, asked or not -----------
    started, stopped = [], []
    real = window.start_stream, window.stop_stream, window.holding
    window.start_stream, window.stop_stream = started.append, stopped.append
    window.holding = False
    try:
        saved_auto, saved_stars = set(window.auto_live), set(window.starred)
        window.auto_live, window.starred = set(), set()
        window.follow_chats({"local"}, {"local"})
        check("a server with a chat is streamed without Watch live",
              started == ["local"] and window.watched("local"),
              f"started={started}")
        window.follow_chats({"local"}, {"local"})
        check("and only once", started == ["local"])
        window.follow_chats(set(), set())
        check("a dark server keeps its stream: it says when it is back",
              stopped == [] and window.watched("local"))
        window.follow_chats({"local"}, set())
        check("its last chat gone, it goes back to the slow check",
              stopped == ["local"] and not window.watched("local"))
        window.starred = {"local"}
        window.follow_chats({"local"}, {"local"})
        window.follow_chats({"local"}, set())
        check("a server you asked to watch is never let go",
              stopped == ["local"] and window.watched("local"))
        window.follow_chats({"nowhere"}, {"nowhere"})
        check("a host not in the config is not streamed",
              "nowhere" not in window.auto_live)
    finally:
        window.start_stream, window.stop_stream, window.holding = real
        window.auto_live, window.starred = saved_auto, saved_stars

    # -- reordering the servers --------------------------------------------
    # arrange() and move_within_tier() are pure, so check the awkward cases
    # directly, then drive one move through the window against a config, order
    # file and stars of this run's own.
    check("arrange leads with the saved order, the rest behind in config order",
          gui.arrange(["local", "a", "b", "c"], ["c", "a"])
          == ["c", "a", "local", "b"])
    check("arrange drops a saved name whose host is gone",
          gui.arrange(["local", "a"], ["x", "a"]) == ["a", "local"])
    check("arrange tolerates a duplicate in a hand-edited order file",
          gui.arrange(["local", "a", "b"], ["b", "b", "a"])
          == ["b", "a", "local"])
    check("a move swaps with the neighbour in the same tier",
          gui.move_within_tier(["a", "b", "c"], {"a", "b", "c"}, "b", -1)
          == ["b", "a", "c"])
    check("and never crosses the fold into the unstarred",
          gui.move_within_tier(["a", "b", "c"], {"a", "b"}, "b", 1) is None,
          "b is last among the starred; c is unstarred and folded away")
    check("the top of a tier has nowhere to move up to",
          gui.move_within_tier(["a", "b"], {"a", "b"}, "a", -1) is None)

    with _tempfile.TemporaryDirectory() as tmp:
        conf = Path(tmp) / "config"
        conf.write_text("Host alpha\n    HostName a\n\n"
                        "Host bravo\n    HostName b\n\n"
                        "Host charlie\n    HostName c\n")
        saved = (hosts.SSH_CONFIG, gui.ORDER_STATE, gui.STARS_STATE)
        hosts.SSH_CONFIG = conf
        gui.ORDER_STATE = Path(tmp) / "order"
        gui.STARS_STATE = Path(tmp) / "stars"
        try:
            window.load_hosts(connect=False)    # first run stars all four
            check("the list starts in config order",
                  window.order == ["local", "alpha", "bravo", "charlie"],
                  f"order={window.order}")
            window.move_host("charlie", -1)
            check("moving a server up swaps it with the one above",
                  window.order == ["local", "alpha", "charlie", "bravo"],
                  f"order={window.order}")
            check("and the arrangement is written down",
                  gui.ORDER_STATE.exists()
                  and gui.read_order() == ["local", "alpha", "charlie", "bravo"],
                  f"saved={gui.read_order()}")
            window.load_hosts(connect=False)
            check("which a fresh read of the config preserves",
                  window.order == ["local", "alpha", "charlie", "bravo"],
                  f"order={window.order}")
            window.move_host("local", -1)
            check("and the top server has nowhere further up to go",
                  window.order[0] == "local", f"order={window.order}")
        finally:
            hosts.SSH_CONFIG, gui.ORDER_STATE, gui.STARS_STATE = saved
            window.load_hosts(connect=False)

    # -- mountpoints that outlived their mount -----------------------------
    with _tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "mnt"
        root.mkdir()
        (root / "stale").mkdir()
        keeper = root / "has-files"
        keeper.mkdir()
        (keeper / "notes.txt").write_text("written while unmounted")
        was, hosts.MNT_ROOT = hosts.MNT_ROOT, root
        try:
            gone = hosts.tidy_mounts()
            check("an empty mountpoint is taken away", gone == ["stale"],
                  f"removed={gone}")
            check("and one with anything in it is left alone",
                  keeper.exists() and (keeper / "notes.txt").exists(),
                  "rmdir cannot remove a directory with files in it, which is "
                  "the whole reason this is safe to run at startup")
        finally:
            hosts.MNT_ROOT = was

    # -- forgetting a server, above the config edit --------------------------
    # remove_host is proven against its own file above; this drives the
    # window's half: the views, the watcher, the bookkeeping and the star
    # all have to go with the Host block. The host is a fixture in a config
    # of this run's own, and nothing ever sshes to it -- forget_host edits
    # files and window state, which is the point.
    with _tempfile.TemporaryDirectory() as tmp:
        conf = Path(tmp) / "config"
        conf.write_text("Host conntest-remote\n    HostName remote.invalid\n")
        was, hosts.SSH_CONFIG = hosts.SSH_CONFIG, conf
        real_close = window.close_session
        closed: list = []

        class View:
            host, name = "conntest-remote", "deploy"

        class Watcher:
            terminated = False

            def terminate(self):
                self.terminated = True

        watcher = Watcher()
        try:
            anchor = next((r for r in rows(window)
                           if getattr(r, "key", None) is not None), None)
            on_named = menu_items(window.host_menu, anchor,
                                  "conntest-remote", 0, 0)
            check("a host that is in the config can be forgotten",
                  any("Forget conntest-remote" in (label or "")
                      for label in on_named),
                  f"menu={on_named}")

            window.close_session = lambda view: (
                closed.append((view.host, view.name)),
                window.open.pop((view.host, view.name), None))
            window.rows["conntest-remote"] = hosts.blank("conntest-remote")
            window.probed["conntest-remote"] = 1.0
            window.streamed["conntest-remote"] = 1.0
            window.inflight.add("conntest-remote")
            window.watchers["conntest-remote"] = watcher
            window.starred.add("conntest-remote")
            window.open[("conntest-remote", "deploy")] = View()

            window.forget_host("conntest-remote")
            check("forgetting a server closes its views and no others",
                  closed == [("conntest-remote", "deploy")]
                  and all(k[0] != "conntest-remote" for k in window.open),
                  f"closed={closed}")
            check("its watcher is terminated and dropped",
                  watcher.terminated
                  and "conntest-remote" not in window.watchers)
            check("and every trace leaves the books",
                  not any("conntest-remote" in place for place in
                          (window.rows, window.probed, window.streamed,
                           window.inflight)))
            check("the star goes too, and is written down",
                  "conntest-remote" not in window.starred
                  and "conntest-remote" not in (gui.read_stars() or set()))
            check("the Host block is out of the config",
                  "conntest-remote" not in conf.read_text())
            check("with the old config kept beside it",
                  any(entry.name.startswith("config.bak.")
                      for entry in Path(tmp).iterdir()))
            check("and the footer says what happened",
                  "forgot conntest-remote" in window.footnote.get_text(),
                  f"footer={window.footnote.get_text()!r}")
        finally:
            window.close_session = real_close
            hosts.SSH_CONFIG = was

    # -- a notification, and the click that answers it -----------------------
    # The builder's fields are checked hermetically in test_hosts.py; here
    # its actual output drives the click handler, because the two only agree
    # by both going through session_ref/split_ref -- change the target format
    # in one place and this is the check that fails.
    Gio = gui.Gio
    grab = {}
    orig_target = Gio.Notification.set_default_action_and_target

    def observed(note, action, target):
        grab["action"], grab["target"] = action, target
        return orig_target(note, action, target)

    Gio.Notification.set_default_action_and_target = observed
    try:
        gui.notification("local", {
            "name": beta[1],
            "agent": {"state": agent_state.NEEDS_YOU, "label": "needs you",
                      "detail": "Do you want to?"}})
    finally:
        Gio.Notification.set_default_action_and_target = orig_target
    check("the notification's click carries the shared ref",
          grab.get("action") == "app.open-session"
          and gui.split_ref(grab["target"].get_string()) == beta,
          f"grabbed={grab}")

    # The cold half of one-window: the click arrives first, do_activate
    # never runs, and the fresh window must be presented with the named
    # session open in it -- the historical bug window()'s docstring
    # describes. A second ConnApp stands in for the cold process; the panel
    # above keeps its own app and window. It has to be registered before it
    # can hold a window (GApplication forbids windows before ::startup), and
    # it takes a distinct, non-unique id so it neither owns nor is answered
    # by the real conn already holding APP_ID on the session bus.
    app2 = gui.ConnApp()
    app2.set_application_id(gui.APP_ID + ".test.cold")
    app2.set_flags(app2.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
    app2.register(None)
    app2.open_from_notification(None, grab["target"])
    twos = app2.get_windows()
    check("a click with no window builds exactly one", len(twos) == 1,
          f"windows={len(twos)}")
    window2 = twos[0] if twos else None
    if window2 is not None:
        window2.notify = lambda host, s, finished=False: None   # nothing reaches the desktop
        check("a fresh panel, not the one above",
              isinstance(window2, gui.Conn) and window2 is not window)
        # The panel above booked beta in known-chats.json, so the fresh one
        # lists it before "local" has answered -- remembered, not live. It
        # is not attached to on the strength of that: the open is owed, and
        # made when the first probe brings beta back live, a moment later.
        owed = window2.open_when_up
        ctx = gui.GLib.MainContext.default()
        deadline = time.monotonic() + 10.0
        while beta not in window2.open and time.monotonic() < deadline:
            ctx.iteration(False)
            time.sleep(0.01)
        check("a remembered chat is opened once its server answers",
              owed in (beta, None) and window2.open_when_up is None,
              f"owed={owed}")
        current = window2.stack.get_visible_child()
        check("with the named session open and showing",
              beta in window2.open and isinstance(current, gui.Session)
              and (current.host, current.name) == beta,
              f"open={list(window2.open)}")
        app2.open_from_notification(None, grab["target"])
        check("and a second click reuses it",
              len(app2.get_windows()) == 1
              and gui.ConnApp.window(app2) is window2)
        for view in list(window2.open.values()):
            window2.close_session(view)
        window2.destroy()

    # -- secrets, in the real keyring ---------------------------------------
    # The one check that touches real user state, so it is opt-in: it proves
    # the secret-tool plumbing against the actual daemon, which no fake can.
    # The entry is cleared on the way out whatever the verdicts were.
    if os.environ.get("CONN_TEST_KEYRING") == "1":
        try:
            hosts.secret_store("conntest-host", "db", "pa55w0rd")
            check("a secret goes into the keyring",
                  hosts.secret_names("conntest-host") == ["db"])
            check("and comes back out by name",
                  hosts.secret_value("conntest-host", "db") == "pa55w0rd")
        finally:
            try:
                hosts.secret_clear("conntest-host", "db")
            except (hosts.HostError, OSError):
                pass
        check("and can be forgotten", hosts.secret_names("conntest-host") == [])
    else:
        print("keyring round trip skipped -- CONN_TEST_KEYRING=1 runs it")

    chat_list(window, check, gui, hosts, agent_state, Gtk)
    offline_chats(window, check, gui, hosts, agent_state, Gtk)
    offline_views(window, check, gui, hosts, agent_state, Gtk)

    # -- the Site Manager ---------------------------------------------------
    # The window above came up on the empty config. A config of two named
    # hosts is written now so the manager has something to list, and the three
    # calls that would leave this machine -- resolve() and probe() shelling out
    # to ssh, migrate_secrets() to the real keyring -- are stubbed for the
    # duration. What is under test is the manager's own file edits, which go
    # through the temp config; the keyring move is asserted by the stub having
    # been called, the round trip itself being covered above under
    # CONN_TEST_KEYRING.
    hosts.SSH_CONFIG.write_text(
        "Host example1\n"
        "    HostName 10.0.0.1\n"
        "    User alice\n"
        "    Port 2222\n"
        "\n"
        "Host example2\n"
        "    HostName box.example.com\n")
    # As the real app does after any config change: pick the new hosts up so
    # self.order (which the manager's reorder swaps in) knows about them.
    window.load_hosts(connect=False)

    def pump(predicate, limit=5.0):
        # Save runs update_host on a worker and reports back on an idle, so the
        # check waits on the file rather than guessing how long that takes.
        ctx = gui.GLib.MainContext.default()
        deadline = time.monotonic() + limit
        while not predicate() and time.monotonic() < deadline:
            ctx.iteration(False)
            time.sleep(0.01)

    def manager_aliases():
        return [getattr(w, "alias", None) for w in walk(window.manager)
                if getattr(w, "alias", None) is not None]

    real_resolve, real_probe = hosts.resolve, hosts.probe
    real_migrate = hosts.migrate_secrets
    real_drop = hosts.drop_master
    hosts.drop_master = lambda h: None      # the suite never runs ssh
    migrated: list = []
    hosts.resolve = lambda h: {"user": "", "hostname": h,
                               "port": "22", "identityfile": "",
                               "proxyjump": ""}
    hosts.probe = lambda h: hosts.blank(h)
    hosts.migrate_secrets = lambda old, new: migrated.append((old, new))
    try:
        window.open_manager()
        check("the Site Manager lists the config's hosts",
              manager_aliases() == ["example1", "example2"],
              f"aliases={manager_aliases()}")

        window._manager_reload("example1")
        got = {k: e.get_text() for k, e in window.manager_fields.items()}
        check("selecting a host prefills its fields from host_config",
              got == {"hostname": "10.0.0.1", "user": "alice",
                      "port": "2222", "identityfile": ""},
              f"got={got}")

        window.manager_fields["hostname"].set_text("10.0.0.9")
        window.manager_fields["port"].set_text("22")   # ssh's default: dropped
        window._manager_save()
        pump(lambda: hosts.host_config("example1")["hostname"] == "10.0.0.9")
        saved = hosts.host_config("example1")
        check("Save writes the fields back through update_host",
              saved["hostname"] == "10.0.0.9" and saved["user"] == "alice"
              and saved["port"] == "", f"saved={saved}")

        # Connect: Default writes nothing, Direct writes ProxyJump none, and
        # the other servers write a jump through them.
        window._manager_reload("example1")
        check("Connect starts on Default and offers Direct and the others",
              window.manager_jumps == ["", "none", "example2"]
              and window.manager_jump.get_selected() == 0,
              f"jumps={window.manager_jumps}")
        pump(lambda: window.manager_jump.get_model().get_string(0)
             != "Default")
        check("Default spells out where it goes, from ssh -G",
              window.manager_jump.get_model().get_string(0)
              == "Default (direct)"
              and window.manager_jump.get_selected() == 0,
              f"label={window.manager_jump.get_model().get_string(0)}")
        window.manager_jump.set_selected(2)
        window._manager_save()
        pump(lambda: hosts.host_config("example1")["proxyjump"] == "example2")
        check("picking a server writes ProxyJump for it",
              hosts.host_config("example1")["proxyjump"] == "example2")
        window._manager_reload("example1")
        check("and reopening shows it picked",
              window.manager_jump.get_selected() == 2)
        window.manager_jump.set_selected(1)
        window._manager_save()
        pump(lambda: hosts.host_config("example1")["proxyjump"] == "none")
        check("Direct writes ProxyJump none, to beat a default jump",
              hosts.host_config("example1")["proxyjump"] == "none")
        window._manager_reload("example1")
        window.manager_jump.set_selected(0)
        window._manager_save()
        pump(lambda: hosts.host_config("example1")["proxyjump"] == "")
        check("Default takes the line out again",
              hosts.host_config("example1")["proxyjump"] == "")

        # All servers connect: one pick rewrites the default route for all.
        check("All servers connect starts on Direct",
              window.manager_routes == ["", "example1", "example2"]
              and window.manager_route.get_selected() == 0,
              f"routes={window.manager_routes}")
        window.manager_route.set_selected(2)
        pump(lambda: hosts.default_jump() == "example2")
        check("picking a server makes it every server's default jump",
              hosts.default_jump() == "example2")
        pump(lambda: window.manager_route.get_selected() == 2)
        check("and the rebuilt list still shows it picked",
              window.manager_route.get_selected() == 2,
              f"sel={window.manager_route.get_selected()}")
        window.manager_route.set_selected(0)
        pump(lambda: hosts.default_jump() == "")
        check("Direct takes the default route out",
              hosts.default_jump() == "")

        # The sidebar star, as a tickbox in the manager -- the same star, just
        # a nicer place to reach it.
        window._manager_reload("example1")
        check("the tickbox is called Watch live",
              window.manager_show.get_label() == "Watch live"
              and "stream" in (window.manager_show.get_tooltip_text() or ""),
              f"label={window.manager_show.get_label()!r}")
        check("the Watch live tickbox reflects the host's setting",
              window.manager_show.get_active() == ("example1" in window.starred),
              f"active={window.manager_show.get_active()} "
              f"starred={window.starred}")
        was = "example1" in window.starred
        window.manager_show.set_active(not was)
        check("ticking it stars the server, unticking unstars",
              ("example1" in window.starred) == (not was),
              f"was={was} starred={window.starred}")
        window.manager_show.set_active(was)      # leave it as we found it
        check("and setting it back restores the star",
              ("example1" in window.starred) == was, f"starred={window.starred}")

        # Reordering from the manager writes to the same order the sidebar reads.
        window._manager_reload("example2")
        before = window._manager_hosts()
        window.manager_move(-1)
        on_disk = [h for h in gui.read_order() if h in hosts.config_hosts()]
        check("Move up in the manager reorders the servers and persists",
              window._manager_hosts() == list(reversed(before))
              and on_disk == list(reversed(before)),
              f"before={before} after={window._manager_hosts()} disk={on_disk}")
        window.manager_move(-1)
        check("and a server already at the top does not move past it",
              window._manager_hosts()[0] == "example2",
              f"order={window._manager_hosts()}")

        # A rename is the whole migration: config, keyring (stubbed), the star
        # and the place in the hand-arranged order. The window is put in a
        # known state first.
        window.starred = {"example1"}
        gui.save_stars(window.starred)
        window.order = ["example1", "example2"]
        gui.save_order(window.order)
        import chats
        window.known["example1"] = {
            "seen": 1, "home": "", "hostnames": [],
            "sessions": [{"name": "kept", "options": {}, "agent": {"state": "ready"}}]}
        window.save_known()
        window.migrate_host("example1", "webhost")

        check("Rename moves the alias in the config",
              "webhost" in hosts.config_hosts()
              and "example1" not in hosts.config_hosts(),
              f"config={hosts.config_hosts()}")
        check("and the block travels with it, unchanged",
              hosts.host_config("webhost")["user"] == "alice",
              f"webhost={hosts.host_config('webhost')}")
        # The keyring migration runs on a worker so a locked keyring cannot
        # freeze the window, so wait for it rather than reading it too soon.
        pump(lambda: migrated)
        check("and migrate_secrets is called to carry the keyring",
              migrated == [("example1", "webhost")], f"migrated={migrated}")
        check("and the star moves to the new name",
              "webhost" in window.starred and "example1" not in window.starred,
              f"starred={window.starred}")
        check("and it keeps its place in the order, in memory and on disk",
              window.order[0] == "webhost" and "example1" not in window.order
              and gui.read_order()[0] == "webhost",
              f"order={window.order} file={gui.read_order()}")
        check("and the manager's own list is redrawn under the new name",
              "webhost" in manager_aliases() and "example1" not in manager_aliases(),
              f"aliases={manager_aliases()}")
        on_disk = chats.load_known(chats.KNOWN_STATE)
        check("and its remembered chats move to the new name, on disk too",
              "example1" not in window.known and "example1" not in on_disk
              and [s["name"] for s in window.known.get("webhost", {})
                   .get("sessions", [])] == ["kept"]
              and "webhost" in on_disk, f"known={sorted(window.known)}")
        window.known.pop("webhost", None)
        window.save_known()
    finally:
        hosts.resolve, hosts.probe = real_resolve, real_probe
        hosts.drop_master = real_drop
        hosts.migrate_secrets = real_migrate
        if window.manager is not None:
            window.manager.close()   # emits close-request -> clears self.manager


RULE = "─" * 100
# Claude Code screens, built from its chrome as test_agent_state.py does.
ASKING = f"Do you want to make this edit?\n 1. Yes\n{RULE}\n❯ \n{RULE}"
BUSY = "{glyph} Running the tests… (1m 2s · esc to interrupt)\n\n" \
       f"{RULE}\n❯ \n{RULE}\n  ⏵⏵ auto mode on · esc to interrupt"


def chat_list(window, check, gui, hosts, agent_state, Gtk) -> None:
    """Desktop phase 1: the recent-first chat list over four fake servers.

    The hosts are in memory only -- never in a config, never probed: the
    sweep is held for the duration and every set_chat_option is recorded in
    WRITES rather than run. What is under test is the window's half: rows,
    sections, order, the folds, stars, filters and keys.
    """
    import chats
    from gi.repository import Gdk

    now = time.time()
    day = 86400
    fakes = ["conntest-at", "conntest-app", "conntest-cal", "conntest-empty",
             "conntest-solo"]
    chats.PROJECTS_FILE.write_text(
        '[acme]\nname = "Acme"\nservers = { "conntest-app" = "~/acme", '
        '"conntest-cal" = "~/acme", "conntest-empty" = "~" }\n\n'
        '[atlas]\nname = "Atlas"\n'
        'servers = { "conntest-at" = "~/atlas" }\n')

    def fake(name, state, ago=None, created_ago=600, title="", starred=False,
             preview=""):
        return {"name": name, "windows": "1", "attached": False,
                "activity": "", "panes": [], "screen": "",
                "agent": {"state": state, "label": agent_state.LABELS[state],
                          "detail": "", "draft": "", "tokens": "",
                          "agent": "claude", "preview": preview, "recap": ""},
                "options": {"project": "", "title": title, "task": "",
                            "last_event": None if ago is None
                            else int(now - ago),
                            "starred": starred, "source": ""},
                "pane_title": "", "path": "", "created": int(now - created_ago)}

    sessions = {
        "conntest-at": [fake("csv", agent_state.NEEDS_YOU, 60, title="CSV export"),
                        fake("home", agent_state.READY, 10 * day,
                             title="Homepage", starred=True),
                        fake("old", agent_state.READY, 30 * day, title="Old one"),
                        fake("fresh", agent_state.READY, None, 500)],
        "conntest-app": [fake("telegram", agent_state.WORKING, 120,
                              title="Telegram", preview="Running the tests"),
                         fake("shell", agent_state.SHELL, 5)],
        "conntest-cal": [fake("calendar", agent_state.READY, 2 * day,
                              title="Calendar sync")],
        "conntest-empty": [],
        "conntest-solo": [],
    }

    saved_order, holding = window.order, window.holding
    saved_filters = window.filters
    window.holding = True           # no sweep reaches for a fake host
    try:
        # The fake fleet is the whole list: "local" stays in self.rows, where
        # a probe from the checks above can still land, but out of the order
        # nothing it brings can reshape the list under these checks.
        # To the Chats tab first, with the real hosts: switching renders,
        # and a render over the fakes here would stamp "fresh" before the
        # recency checks below get to watch it happen.
        window.set_sidebar_mode("chats")
        window.order = list(fakes)
        for host in fakes:
            row = hosts.blank(host)
            row.update(state="up", home="/home/u", sessions=sessions[host])
            window.rows[host] = row
        window.projects = chats.load_projects(chats.PROJECTS_FILE,
                                              hosts=window.order)
        window._tile_style()
        window.filters = chats.Filters()
        window.showing_older = False
        WRITES.clear()
        window.render()

        def chat_rows():
            return [r for r in rows(window) if getattr(r, "chat", None)]

        def chat_order():
            # The fake hosts' chats only: a probe of "local" from the checks
            # above can land at any point and change what is listed there.
            return [r.chat for r in chat_rows()
                    if not r.chat.startswith("local/")]

        def headings():
            return [w.get_label() for r in rows(window)
                    if not getattr(r, "chat", None) and getattr(r, "zone", "") == "chats"
                    for w in walk(r) if isinstance(w, Gtk.Label)
                    and w.has_css_class("section")]

        def pump(predicate, limit=3.0):
            ctx = gui.GLib.MainContext.default()
            deadline = time.monotonic() + limit
            while not predicate() and time.monotonic() < deadline:
                ctx.iteration(False)
                time.sleep(0.01)

        mine = chat_order()
        check("the chat list shows the chats, newest first in each section",
              mine[:3] == ["conntest-at/home", "conntest-at/csv",
                           "conntest-app/telegram"],
              f"order={mine}")
        check("a plain shell is not a chat",
              "conntest-app/shell" not in chat_order(), f"order={chat_order()}")
        labels = headings()
        check("sections run Starred, Today, then Earlier this week",
              labels[:1] == ["STARRED"] and "TODAY" in labels
              and "EARLIER THIS WEEK" in labels
              and labels.index("TODAY") < labels.index("EARLIER THIS WEEK"),
              f"headings={labels}")
        check("a starred chat is only under Starred",
              chat_order().count("conntest-at/home") == 1)
        check("older chats wait behind a folded Older",
              "conntest-at/old" not in chat_order() and "OLDER" in labels,
              f"headings={labels}")
        older = next(r for r in rows(window) if getattr(r, "fold", "") == "older")
        window.row_activated(window.list, older)
        check("which opens on a click",
              "conntest-at/old" in chat_order(), f"order={chat_order()}")
        window.row_activated(window.list, next(
            r for r in rows(window) if getattr(r, "fold", "") == "older"))

        check("the projects come from projects.toml, a host in none is its own",
              [p.id for p in window.projects][:2] == ["acme", "atlas"]
              and any(p.id == "conntest-solo" and p.implicit
                      for p in window.projects),
              f"projects={[p.id for p in window.projects]}")
        tile = next(w for w in walk(row_for(window, ("conntest-app", "telegram")))
                    if isinstance(w, Gtk.Label) and w.has_css_class("tile"))
        check("a chat's tile carries its project's letter",
              tile.get_label() == "A", f"tile={tile.get_label()!r}")
        import theming
        background, letter = gui.tile_colours(window.palette, window.projects[0], 0)
        check("and its letter is legible on its own tile",
              (theming.contrast(letter, background) or 0) >= theming.LEGIBLE - 0.05,
              f"{letter} on {background}")
        # Idle tiles are quieter by colour, not opacity: opacity faded the
        # letter with the tile, to about 3.4:1 on Nord and 2.9 on light.
        palettes = [window.palette,
                    theming.Palette(theming.FALLBACK_DARK, "dark"),
                    theming.Palette(theming.FALLBACK_LIGHT, "light")]
        idle = [(pal.source, gui.tile_colours(pal, project, index, idle=True))
                for pal in palettes
                for index, project in enumerate(window.projects)]
        low = [(src, fg, bg) for src, (bg, fg) in idle
               if (theming.contrast(fg, bg) or 0) < theming.LEGIBLE - 0.05]
        check("an idle tile's letter is held to 4.5:1 too", not low, f"low={low}")
        check("and no opacity is left to fade it",
              ".chat-idle .tile {" not in window.style_css)
        # The Needs you count and checked chip paint amber on a yellow wash.
        washes = []
        for pal in palettes:
            text = gui.amber_on_tints(pal)
            chip = theming.mix(pal.panel, pal.yellow, gui.CHIP_TINT)
            for bg in (chip, theming.mix(pal.panel, pal.yellow, gui.COUNT_TINT),
                       theming.mix(chip, pal.yellow, gui.COUNT_TINT)):
                washes.append((pal.source, text, bg,
                               theming.contrast(text, bg) or 0))
        low = [w for w in washes if w[3] < theming.LEGIBLE - 0.05]
        check("the Needs you count reads at 4.5:1 on its own wash, checked or not",
              not low, f"low={low}")
        # The selected row is accent; the badge keeps its panel disc and ring
        # there, because blue/amber on the accent fell to 1.2-1.4:1.
        css = window.style_css
        pal = window.palette
        rings = css.split(".sidebar row:selected .badge-unknown")[1].split("}")[0]
        badge = gui.WorkingBadge(pal)
        drawn = (badge.disc.to_string(), badge.blue.to_string())
        check("on the selected row the badge stays on its panel disc and ring",
              drawn == (gui.rgba(pal.panel).to_string(),
                        gui.rgba(pal.blue).to_string())
              and f"border-color: {pal.panel}" in rings
              and pal.accent not in rings
              and (theming.contrast(pal.blue, pal.panel) or 0) >= theming.SIGNAL - 0.05
              and (theming.contrast(pal.yellow, pal.panel) or 0) >= theming.SIGNAL - 0.05,
              f"drawn={drawn} rings={rings!r}")
        # The selected chat is a card, and every word on a row is held to
        # 4.5:1 on it as well as on the plain panel.
        card = gui.selected_card(pal)
        check("the selected chat is a lifted card with a ring, not the accent",
              f"background: {card}" in css.split(
                  ".sidebar row.session:selected")[1].split("}")[0]
              and "inset 0 0 0 1px" in css.split(
                  ".sidebar row.session:selected")[1].split("}")[0])
        for rule in (".chat-time.amber", ".chat-time.blue", ".chat-server",
                     ".chat-preview, .chat-dot"):
            colour = css.split(rule + " {")[1].split("color:")[1].split(";")[0].strip()
            check(f"{rule} reads at 4.5:1 on the selected card",
                  (theming.contrast(colour, card) or 0) >= theming.LEGIBLE - 0.05,
                  f"{colour} on {card}")

        # -- recency --------------------------------------------------------
        pump(lambda: any(w[:2] == ("conntest-at", "fresh") for w in WRITES))
        fresh = [w for w in WRITES if w[:2] == ("conntest-at", "fresh")]
        created = sessions["conntest-at"][3]["created"]
        check("a chat never stamped is stamped with when it was created",
              fresh == [("conntest-at", "fresh", "last_event", str(created))],
              f"writes={fresh}")
        check("and nothing else is written for chats that already have one",
              all(w[1] == "fresh" or w[0] == "local" for w in WRITES),
              f"writes={WRITES}")

        def frame(host, screens):
            data = {}
            for session in window.rows[host]["sessions"]:
                data[session["name"]] = {
                    "screen": screens.get(session["name"], ""), "raw": "",
                    "commands": (["bash"] if session["agent"]["state"]
                                 == agent_state.SHELL else ["claude"]),
                    **{k: session[k] for k in gui.META_KEYS}}
            return data

        check("the screens below classify as the states they stand for",
              agent_state.classify(ASKING, ["claude"])["state"] == agent_state.NEEDS_YOU
              and agent_state.classify(BUSY.format(glyph="✶"), ["claude"])["state"]
              == agent_state.WORKING)

        # Spinner only: the glyph turns and the clock moves, the state stays.
        window.frames.put((("conntest-app"), frame(
            "conntest-app", {"telegram": BUSY.format(glyph="✶"),
                             "shell": ""})))
        window.drain_frames()
        before = id(row_for(window, ("conntest-app", "telegram")))
        order = chat_order()
        WRITES.clear()
        window.frames.put(("conntest-app", frame(
            "conntest-app", {"telegram": BUSY.format(glyph="✷"),
                             "shell": ""})))
        window.drain_frames()
        pump(lambda: WRITES, 0.3)
        check("a chat only redrawing its spinner does not move",
              chat_order() == order
              and id(row_for(window, ("conntest-app", "telegram"))) == before,
              f"before={order} after={chat_order()}")
        check("and is not stamped", not WRITES, f"writes={WRITES}")

        # The dev copy beside a running stable conn: a real change still
        # moves the row (the stamp is held in memory), but the write is left
        # to the stable one -- two desktops would stamp every turn twice.
        # With the stable conn gone it stamps, as the only one there is.
        tid = "conntest-app/telegram"
        real_dev, real_up = gui.paths.DEV, window.stable_up
        real_grace = gui.STABLE_GRACE
        gui.paths.DEV, window.stable_up = True, True
        try:
            WRITES.clear()
            window.frames.put(("conntest-app", frame(
                "conntest-app", {"telegram": ASKING, "shell": ""})))
            window.drain_frames()
            pump(lambda: WRITES, 0.3)
            moved = window.chat_index[tid].last_event
            check("dev with stable running moves the row but writes no stamp",
                  window.defer_to_stable() and not WRITES
                  and abs(moved - time.time()) < 10,
                  f"writes={WRITES} last_event={moved}")
            # Titles: nothing queued while stable titles (a backlog worked
            # through when it quits would retitle what it already titled),
            # a vanished bus name believed only after the grace (its restart
            # is a blink), and a queue cleared when it comes back.
            real_due = window.retitler.due
            window.retitler.due = lambda *_a, **_k: True
            saved_queue = list(window.retitle_queue)
            window.retitle_queue.clear()
            window.frames.put(("conntest-app", frame(
                "conntest-app", {"telegram": ASKING, "shell": ""})))
            window.drain_frames()
            check("dev with stable running queues no titles",
                  window.retitle_queue == [], str(window.retitle_queue))
            window._stable_seen(False)
            blink = window.stable_up and window.stable_grace != 0
            window._stable_seen(True)
            check("stable's name gone for a moment (a restart) is not gone",
                  blink and window.stable_up and window.stable_grace == 0)
            window.retitle_queue[:] = ["conntest-app/telegram"]
            window._stable_seen(True)
            check("stable seen again clears dev's title queue",
                  window.retitle_queue == [], str(window.retitle_queue))
            gui.STABLE_GRACE = 1        # the wait itself, not its length
            window._stable_seen(False)
            pump(lambda: not window.stable_up, 4)
            check("gone past the grace, dev takes over",
                  not window.stable_up and window.stable_grace == 0)
            window.frames.put(("conntest-app", frame(
                "conntest-app", {"telegram": ASKING, "shell": ""})))
            window.drain_frames()
            check("and queues titles itself",
                  "conntest-app/telegram" in window.retitle_queue,
                  str(window.retitle_queue))
            window.retitler.due = real_due
            window.retitle_queue[:] = saved_queue
            window.frames.put(("conntest-app", frame(
                "conntest-app", {"telegram": BUSY.format(glyph="✶"),
                                 "shell": ""})))
            window.drain_frames()
            pump(lambda: any(w[:3] == ("conntest-app", "telegram",
                                       "last_event") for w in WRITES))
            check("and with stable gone, dev stamps it itself",
                  not window.defer_to_stable()
                  and any(w[:3] == ("conntest-app", "telegram", "last_event")
                          for w in WRITES), f"writes={WRITES}")
        finally:
            gui.STABLE_GRACE = real_grace
            if window.stable_grace:
                gui.GLib.source_remove(window.stable_grace)
                window.stable_grace = 0
            if "real_due" in locals():
                window.retitler.due = real_due
            gui.paths.DEV, window.stable_up = real_dev, real_up
            # Back as it was for what follows: the stamps were recorded,
            # never run, so the option still says two minutes ago.
            window.stamps.pop(tid, None)
            WRITES.clear()
            window.render()
        badge = window.chat_widgets["conntest-app/telegram"]["badge"]
        check("a working chat wears the spinner badge",
              badge.get_visible_child_name() == "working")

        # A cd inside the project changes no shape, so only a repaint runs --
        # and the tooltip's path has to follow it there.
        telegram = sessions["conntest-app"][0]
        telegram["path"] = "/home/u/acme/app"
        window.render()
        held = id(row_for(window, ("conntest-app", "telegram")))
        telegram["path"] = "/home/u/acme/app/web"
        window.render()
        tip = row_for(window, ("conntest-app", "telegram")).get_tooltip_text()
        check("a chat's tooltip follows its path on a repaint",
              id(row_for(window, ("conntest-app", "telegram"))) == held
              and tip.endswith("\nconntest-app/telegram  ~/acme/app/web")
              and tip.startswith("Telegram\n"),
              f"tip={tip!r}")
        telegram["path"] = "/home/u"
        window.render()
        tip = row_for(window, ("conntest-app", "telegram")).get_tooltip_text()
        check("and leaves the folder out when it is only the home",
              tip.endswith("\nconntest-app/telegram"), f"tip={tip!r}")
        telegram["path"] = ""
        window.render()

        # A state change: the calendar chat, idle since the day before
        # yesterday, starts asking -- and is at the top of Today in this drain.
        window.frames.put(("conntest-cal", frame("conntest-cal",
                                                 {"calendar": ASKING})))
        window.drain_frames()
        today = [cid for cid in chat_order()
                 if not window.chat_index[cid].starred]
        check("a chat that starts waiting on you jumps to the top in one drain",
              today[:1] == ["conntest-cal/calendar"], f"order={today}")
        pump(lambda: any(w[:3] == ("conntest-cal", "calendar", "last_event")
                         for w in WRITES))
        stamp = [w for w in WRITES if w[:2] == ("conntest-cal", "calendar")]
        check("and the stamp is written behind it, off the main thread",
              len(stamp) == 1 and abs(int(stamp[0][3]) - time.time()) < 10,
              f"writes={stamp}")
        check("with an amber badge and an amber time",
              window.chat_widgets["conntest-cal/calendar"]["badge"]
              .get_visible_child_name() == "needs"
              and window.chat_widgets["conntest-cal/calendar"]["time"]
              .has_css_class("amber"))
        check("the Needs you chip counts it",
              window.needs_count.get_text() == "2",
              f"count={window.needs_count.get_text()}")

        # The times tick: a repaint later is a new label on the same row.
        parts = window.chat_widgets["conntest-at/csv"]
        held = id(row_for(window, ("conntest-at", "csv")))
        before = parts["time"].get_label()
        window.repaint(time.time() + 3 * 3600)
        # "3h" -- or a weekday when three hours on is past midnight.
        check("the times tick on a repaint, without new rows",
              parts["time"].get_label() not in ("", before)
              and id(row_for(window, ("conntest-at", "csv"))) == held,
              f"time={parts['time'].get_label()!r}")
        window.render()

        # -- stars ----------------------------------------------------------
        WRITES.clear()
        parts["star"].emit("clicked")
        check("starring a chat moves it under Starred at once",
              window.chat_index["conntest-at/csv"].starred
              and chat_order().index("conntest-at/csv")
              < chat_order().index("conntest-at/home") + 2, f"order={chat_order()}")
        pump(lambda: WRITES)
        check("and writes @conn_starred on its session",
              WRITES == [("conntest-at", "csv", "starred", "1")], f"writes={WRITES}")
        window.render()
        check("the star survives a render before the server has echoed it",
              window.chat_index["conntest-at/csv"].starred)
        sessions["conntest-at"][0]["options"]["starred"] = True
        window.render()
        check("and the wish is settled once it has",
              "conntest-at/csv" not in window.star_wish)

        real_record = hosts.set_chat_option

        def refuse(host, session, name, value):
            raise hosts.HostError("host is dark")

        hosts.set_chat_option = refuse
        try:
            window.toggle_chat_star("conntest-at/csv")
            check("unstarring shows at once", not window.chat_index["conntest-at/csv"].starred)
            pump(lambda: "could not" in window.footnote.get_text())
        finally:
            hosts.set_chat_option = real_record
        check("a star that cannot be written goes back, and says why",
              window.chat_index["conntest-at/csv"].starred
              and "could not unstar" in window.footnote.get_text(),
              f"footer={window.footnote.get_text()!r}")

        menu_labels = []
        window.row_menu(row_for(window, ("conntest-at", "csv")), 0, 0)
        for pop in [c for c in walk(window.list) if isinstance(c, Gtk.Popover)]:
            menu_labels += [b.get_label() for b in walk(pop) if isinstance(b, Gtk.Button)]
            pop.popdown()
        check("right-clicking a chat offers Unstar beside open, rename and kill",
              "Unstar" in menu_labels and "Open" in menu_labels
              and any("Kill" in (x or "") for x in menu_labels), f"menu={menu_labels}")

        # -- filters --------------------------------------------------------
        window.said = ("", 0.0)         # the notice above has had its say
        window.set_filters(chats.pick_project(window.filters, "acme"))
        shown = chat_order()
        check("picking a project shows only its chats",
              shown and all(window.chat_index[c].project == "acme" for c in shown),
              f"shown={shown}")
        summary = [w.get_label() for r in rows(window) for w in walk(r)
                   if isinstance(w, Gtk.Label) and w.has_css_class("summary")]
        check("says so above the list", summary == ["Only Acme"], f"summary={summary}")
        check("and in the footer and the New chat button",
              " of " in window.footnote.get_text()
              and window.new_label.get_text() == "New chat in Acme",
              f"footer={window.footnote.get_text()!r} "
              f"button={window.new_label.get_text()!r}")
        check("and is remembered for the next launch",
              chats.load_filters(chats.FILTERS_STATE).project == "acme")
        window.set_filters(chats.pick_server(window.filters, "conntest-app"))
        check("a server narrows it further",
              window.new_label.get_text() == "New chat on conntest-app"
              and chat_order() == ["conntest-app/telegram"], f"shown={chat_order()}")
        window.set_filters(chats.pick_project(window.filters, "atlas"))
        check("and picking a project resets the server",
              window.filters.server is None
              and chats.load_filters(chats.FILTERS_STATE).server is None)
        window.set_filters(chats.pick_server(chats.Filters(), "conntest-empty"))
        empties = [w.get_label() for r in rows(window) for w in walk(r)
                   if isinstance(w, Gtk.Label) and w.get_label()
                   in ("No chats on conntest-empty yet.", "Start one")]
        check("a server with no chats says so, and offers to start one",
              sorted(empties) == ["No chats on conntest-empty yet.", "Start one"],
              f"found={empties}")
        check("and New chat goes there", window.new_chat_host() == "conntest-empty")
        window.needs_chip.set_active(True)
        check("the Needs you chip is a filter too",
              window.filters.needs_you and chats.load_filters(chats.FILTERS_STATE).needs_you)
        clear = next(w for r in rows(window) for w in walk(r)
                     if isinstance(w, Gtk.Button) and w.get_label() == "Clear")
        clear.emit("clicked")
        check("Clear takes every filter off, the chip included",
              not window.filters.active() and not window.needs_chip.get_active()
              and not chats.load_filters(chats.FILTERS_STATE).active())

        window.project_chip.get_popover()
        window.project_chip.popup()
        names = [w.get_label() for w in walk(window.project_chip.get_popover())
                 if isinstance(w, Gtk.Label)]
        window.project_chip.popdown()
        check("the Project menu lists every project with its server count",
              "All projects" in names and "Acme" in names and "3 servers" in names,
              f"menu={names}")
        window.set_filters(chats.pick_project(chats.Filters(), "acme"))
        window.server_chip.popup()
        names = [w.get_label() for w in walk(window.server_chip.get_popover())
                 if isinstance(w, Gtk.Label)]
        window.server_chip.popdown()
        check("and the Server menu only the picked project's servers",
              "conntest-app" in names and "conntest-at" not in names
              and "no chats" in names, f"menu={names}")
        window.set_filters(chats.Filters())

        # -- search, and the Servers tab -------------------------------------
        check("the Chats tab has no Servers fold any more",
              not any(getattr(r, "fold", "") == "servers" for r in rows(window))
              and not any(getattr(r, "server", None) for r in rows(window)),
              f"rows={[getattr(r, 'fold', '') or getattr(r, 'server', '') for r in rows(window)]}")
        check("and no plain shells -- the Chats tab is just chats",
              server_row(window, ("conntest-app", "shell")) is None)
        window.filter.set_text("telegram")
        window.refilter()
        check("a search filters the chats by what is on them",
              chat_order() == ["conntest-app/telegram"], f"shown={chat_order()}")
        window.clear_filter()

        servers_tab(window, check, gui, hosts, agent_state, Gtk, fakes)
        rebuilds_let_go(window, check, gui, agent_state, Gtk, frame)

        # -- keys -----------------------------------------------------------
        ctrl = Gdk.ModifierType.CONTROL_MASK
        none = Gdk.ModifierType(0)
        window.key_pressed(None, Gdk.KEY_k, 0, ctrl)
        check("ctrl-k opens the palette with the keyboard in it",
              window.cmdk.get_visible() and window.within(window.cmdk.entry))
        row = window.cmdk.list.get_first_child()
        real_pick = window.cmdk.pick
        window.cmdk.pick = lambda *_a: row
        window.cmdk._dim_clicked(None, 1, 0, 0)
        check("a click on a palette row is left to the row, not taken as a "
              "click outside", window.cmdk.get_visible() and row is not None)
        window.cmdk.pick = lambda *_a: window.cmdk
        window.cmdk._dim_clicked(None, 1, 0, 0)
        window.cmdk.pick = real_pick
        check("a click on the dim layer outside the card closes it",
              not window.cmdk.get_visible())
        window.key_pressed(None, Gdk.KEY_k, 0, ctrl)
        window.cmdk.close()
        check("and closing it hands the keyboard back",
              not window.cmdk.get_visible())
        window.key_pressed(None, Gdk.KEY_f, 0, ctrl)
        check("ctrl-f puts the keyboard in the search box",
              window.within(window.filter))
        check("where s is a letter, not a star",
              window.key_pressed(None, Gdk.KEY_s, 0, none) is False)
        window.key_pressed(None, Gdk.KEY_Down, 0, none)
        first = window.list.get_selected_row()
        check("down walks from the box into the list",
              window.within(window.list) and getattr(first, "chat", None)
              == chat_order()[0], f"selected={getattr(first, 'chat', None)}")
        was = window.chat_index[first.chat].starred
        WRITES.clear()
        check("s in the list stars the selected chat",
              window.key_pressed(None, Gdk.KEY_s, 0, none) is True
              and window.chat_index[first.chat].starred == (not was))
        pump(lambda: WRITES)
        window.toggle_chat_star(first.chat)     # and back, as it was
        window.key_pressed(None, Gdk.KEY_n, 0, ctrl)
        page = window.newchat
        check("ctrl-n opens the New chat screen on the chosen server",
              window.stack.get_visible_child() is page
              and page.target is not None
              and page.target.host == window.new_chat_host()
              and window.within(page.text),
              f"target={page.target}")
        check("and its command preview is the spec's",
              "tmux new -d -s <name>" in page.command_text.get_text()
              and "~/.conn/tasks/" in page.command_text.get_text(),
              page.command_text.get_text())
        page.text.get_buffer().set_text("Add a CSV export to the switches")
        check("Start wakes up with text, and the slug follows it",
              page.start.get_sensitive()
              and "csv-export-switches" in page.command_text.get_text())
        page.text.get_buffer().set_text("")
        menu = Gtk.Popover()
        page._fill_where(menu)
        scroller = menu.get_child()
        check("the Start on list scrolls, never taller than the window",
              isinstance(scroller, Gtk.ScrolledWindow)
              and 0 < scroller.get_max_content_height() < max(
                  window.get_height(), 800),
              f"child={type(scroller).__name__}")
        window.key_pressed(None, Gdk.KEY_Escape, 0, none)
        page._text_key(None, Gdk.KEY_Escape, 0, none)
        check("Esc leaves it", window.stack.get_visible_child() is not page)
        check("alt-1 still opens the first numbered row",
              window.slots[0] == (window.chat_index[chat_order()[0]].host,
                                  window.chat_index[chat_order()[0]].session))

        # Notes: ctrl-3 puts the projects in the sidebar and the notes where
        # the terminal goes; ctrl-1 puts back what was there.
        before_main = window.stack.get_visible_child()
        window.key_pressed(None, Gdk.KEY_3, 0, ctrl)
        board = window.notes_pane
        check("ctrl-3: the notes take the main area, the tree the sidebar",
              window.stack.get_visible_child() is board
              and window.side_stack.get_visible_child() is board.side
              and not window.project_chip.get_visible()
              and window.within(board.editor))
        check("and the sidebar's button and search are the notes' own",
              window.new_label.get_text() == "New note"
              and window.filter.get_placeholder_text() == "Search notes"
              and window.search_box.get_visible())
        guided = []
        real_guide = window.show_guide
        window.show_guide = lambda: guided.append(1) or True
        try:
            took = window.key_pressed(None, Gdk.KEY_question, 0,
                                      Gdk.ModifierType.SHIFT_MASK)
        finally:
            window.show_guide = real_guide
        check("? in a note is a question mark, not the guide",
              window.typing() and not guided and not took)
        pid = window.projects[0].id if window.projects else "p"
        fake = {"id": "20261105-1031-csv-export", "project": pid,
                "text": "CSV export\nfor the switches", "status": "open",
                "created": "2026-11-05T10:31:00+11:00"}
        pump(lambda: not window.notes_fetching, 5)  # ctrl-3's own fetch
        window.note_list = [fake]
        updates, created = [], []
        real_update, real_add = window.update_note, window.add_note
        # A real fetch landing mid-check would swap note_list back to the
        # test store's (empty) one, and the open note with it.
        real_refresh = window.refresh_notes
        window.refresh_notes = lambda: None
        window.update_note = lambda note, **kw: updates.append((note["id"], kw))
        window.add_note = lambda project, text, done=None, **kw: \
            created.append((project, text, done))
        try:
            board.render()
            def tree_rows():
                out, i = [], 0
                while (r := board.side.list.get_row_at_index(i)) is not None:
                    out.append(r)
                    i += 1
                return out
            notes_rows = [r for r in tree_rows()
                          if getattr(r, "kind", None) == "note"]
            check("the tree lists the note under its project, by first line",
                  any(getattr(r, "kind", None) == "folder" and r.pid == pid
                      for r in tree_rows())
                  and [r.key for r in notes_rows] == [fake["id"]])
            board.open_note(fake["id"])
            check("opening it puts its text in the editor",
                  notesview_text(board) == fake["text"]
                  and board.heading.get_text() == "CSV export")
            board.editor.get_buffer().set_text("CSV export\nand a total")
            check("typing marks it edited, not saved yet",
                  board.dirty and not updates)
            board.flush()
            check("and a save sends the new text",
                  updates == [(fake["id"], {"text": "CSV export\nand a total"})]
                  and not board.dirty, repr(updates))
            window.filter.set_text("nothing like this")
            # The entry waits a beat before it says the search changed.
            pump(lambda: not any(getattr(r, "kind", None) == "note"
                                 for r in tree_rows()), 2)
            check("the search box filters the tree",
                  not any(getattr(r, "kind", None) == "note"
                          for r in tree_rows()))
            window.filter.set_text("")
            pump(lambda: any(getattr(r, "kind", None) == "note"
                             for r in tree_rows()), 2)
            board.more.emit("clicked")
            pump(lambda: False, 0.3)
            from gi.repository import Gtk as _G
            def popovers(w, found):
                if isinstance(w, _G.Popover) and w.get_visible():
                    found.append(w)
                c = w.get_first_child()
                while c is not None:
                    popovers(c, found)
                    c = c.get_next_sibling()
                return found
            shown = popovers(board, [])
            check("the note's more button opens its menu", bool(shown),
                  repr((shown, board.current_key, board.draft_pid,
                        [n.get("id") for n in window.note_list],
                        board.more.get_visible())))
            for p_ in shown:
                p_.popdown()
            pump(lambda: False, 0.1)
            window.key_pressed(None, Gdk.KEY_n, 0, ctrl)
            check("ctrl-n in Notes is a new note in the same project",
                  board.current_key is None and board.draft_pid == pid
                  and notesview_text(board) == "")
            board.editor.get_buffer().set_text("a fresh thought")
            board.flush()
            check("a draft with words becomes a note in its project",
                  len(created) == 1 and created[0][:2] == (pid, "a fresh thought"),
                  repr(created))
            new = dict(fake, id="20261118-1000-fresh", text="a fresh thought")
            created[0][2](new, "")
            check("and carries on as that note",
                  board.current_key == new["id"] and board.draft_pid is None
                  and not board.dirty)
        finally:
            window.update_note, window.add_note = real_update, real_add
            window.refresh_notes = real_refresh
            window.note_list = []
            board.current_key, board.draft_pid, board.fresh = None, None, None
            board.dirty = False
        window.key_pressed(None, Gdk.KEY_1, 0, ctrl)
        from gi.repository import Graphene
        tab_y = []
        for mode in ("notes", "servers", "chats"):
            window.set_sidebar_mode(mode)
            pump(lambda: False, 0.15)
            got = window.mode_buttons["chats"].compute_point(
                window, Graphene.Point())
            tab_y.append(round(got[1].y) if got and got[0] else None)
        check("the tabs stay put whichever tab is showing",
              len(set(tab_y)) == 1 and tab_y[0] is not None, repr(tab_y))
        window.set_sidebar_mode("notes")
        window.key_pressed(None, Gdk.KEY_1, 0, ctrl)
        check("ctrl-1 puts back what was showing",
              window.stack.get_visible_child() is before_main
              and window.project_chip.get_visible())

        # Waking from sleep: dead masters closed, streams poked, all hosts
        # probed again -- not minutes of nothing connected.
        import threading as _th
        real = (hosts.slept_seconds, hosts.revive_masters, window.sweep,
                hosts.network_ready)
        hosts.network_ready = lambda: True
        asked, swept = [], []
        poke = _th.Event()
        window.streams["wake-test"] = (None, _th.Event(), poke)
        try:
            later = window.slept + 120
            hosts.slept_seconds = lambda: later
            hosts.revive_masters = lambda: (asked.append(1), ["a@b:22"])[1]
            window.sweep = lambda force=False: swept.append(force)
            window.wake_tick()
            pump(lambda: bool(swept), 4.0)
            # The window's own wake timer can land during the pump and ask
            # again (revive_again); one or two asks are both right.
            check("wake: masters checked, streams poked, a forced sweep",
                  asked in ([1], [1, 1]) and poke.is_set() and swept == [True]
                  and not window.reviving, repr((asked, swept)))
            asked.clear()
            window.wake_tick()
            pump(lambda: False, 0.3)
            check("no second revive without another sleep", asked == [])
        finally:
            (hosts.slept_seconds, hosts.revive_masters, window.sweep,
             hosts.network_ready) = real
            window.streams.pop("wake-test", None)

        # In a terminal ctrl-k and ctrl-n are the terminal's: readline's
        # kill-line and next-history, nano's cut, Claude's kill-to-end.
        from gi.repository import Vte
        term = Vte.Terminal()
        window.get_focus = lambda: term
        try:
            tops = Gtk.Window.get_toplevels()
            before = {id(tops.get_item(i)) for i in range(tops.get_n_items())}
            kept_k = window.key_pressed(None, Gdk.KEY_k, 0, ctrl) is False
            kept_n = window.key_pressed(None, Gdk.KEY_n, 0, ctrl) is False
            opened = [tops.get_item(i) for i in range(tops.get_n_items())
                      if id(tops.get_item(i)) not in before]
            took_f = window.key_pressed(None, Gdk.KEY_f, 0, ctrl) is True
        finally:
            del window.get_focus
        for top in opened:
            top.destroy()
        check("a focused terminal keeps ctrl-k and ctrl-n",
              kept_k and kept_n and not opened,
              f"k={kept_k} n={kept_n} opened={len(opened)}")
        check("while ctrl-f stays conn's everywhere", took_f)

        # Enter in the search box opens the top row, not the newest chat:
        # the list draws Starred first, and Down-then-Enter walks in that
        # order. csv (a minute ago) is newer than the starred home (10 days).
        sessions["conntest-at"][0]["options"]["starred"] = False
        window.star_wish.pop("conntest-at/csv", None)
        window.filter.set_text("conntest-at")
        window.refilter()
        opened_keys = []
        window.open_session = lambda host, name: opened_keys.append((host, name))
        try:
            window.open_first()
        finally:
            del window.open_session
        top = next(r for r in rows(window) if getattr(r, "chat", None))
        check("enter in the search box opens the first row shown",
              window.visible_chats[0].id == "conntest-at/csv"
              and opened_keys == [top.key] == [("conntest-at", "home")],
              f"opened={opened_keys} top={top.key} "
              f"newest={window.visible_chats[0].id}")
        window.clear_filter()
        new_features(window, check, gui, hosts, agent_state, Gtk, sessions,
                     pump)
    finally:
        window.order, window.holding = saved_order, holding
        window.filters = saved_filters
        for host in fakes:
            window.rows.pop(host, None)
        chats.PROJECTS_FILE.unlink(missing_ok=True)
        window.load_hosts(connect=False)
        window.set_sidebar_mode("servers")
        window.render()


def new_features(window, check, gui, hosts, agent_state, Gtk, sessions,
                 pump) -> None:
    """Rename (A), move to a project (B), the Projects window (C), finished
    and not seen (D), the numbers under alt (E), and the fixed width."""
    import chats
    from gi.repository import Gdk

    def menu_of(key):
        window.row_menu(row_for(window, key), 0, 0)
        pops = [c for c in walk(window.list) if isinstance(c, Gtk.Popover)]
        pop = pops[-1]
        labels = [getattr(b, "label", None) or b.get_label()
                  for b in walk(pop) if isinstance(b, Gtk.Button)]
        return pop, labels

    # -- A: rename -------------------------------------------------------
    pop, labels = menu_of(("conntest-at", "csv"))
    pop.popdown()
    check("a chat's menu has Rename…, Move to project › and Rename session…",
          "Rename…" in labels and "Move to project ›" in labels
          and "Rename session…" in labels
          and labels.index("Rename…") < labels.index("Rename session…"),
          f"menu={labels}")
    WRITES.clear()
    dialog = window.rename_chat("conntest-at/csv")
    check("the rename dialog starts from the title it has",
          dialog.entries["Name"].get_text() == "CSV export")
    dialog.entries["Name"].set_text("Switches CSV")
    dialog.submit()
    check("renaming shows at once and writes @conn_name",
          window.chat_index["conntest-at/csv"].title == "Switches CSV"
          and ("conntest-at", "csv", "name", "Switches CSV") in WRITES,
          f"writes={WRITES}")
    sessions["conntest-at"][0]["options"]["name"] = "Switches CSV"
    window.render()
    check("and the wish settles once the server has it",
          "conntest-at/csv" not in window.name_wish
          and window.chat_index["conntest-at/csv"].title == "Switches CSV")
    WRITES.clear()
    dialog = window.rename_chat("conntest-at/csv")
    dialog.entries["Name"].set_text("")
    dialog.submit()
    check("an empty rename clears @conn_name and the old title is back",
          ("conntest-at", "csv", "name", None) in WRITES
          and window.chat_index["conntest-at/csv"].title == "CSV export",
          f"writes={WRITES} title={window.chat_index['conntest-at/csv'].title}")
    sessions["conntest-at"][0]["options"]["name"] = ""
    window.render()

    real = hosts.set_chat_option

    def refuse(host, session, name, value):
        raise hosts.HostError("host is dark")
    hosts.set_chat_option = refuse
    try:
        dialog = window.rename_chat("conntest-at/csv")
        dialog.entries["Name"].set_text("Never lands")
        dialog.submit()
        pump(lambda: "could not rename" in window.footnote.get_text())
    finally:
        hosts.set_chat_option = real
    check("a rename that cannot be written goes back, and says why",
          window.chat_index["conntest-at/csv"].title == "CSV export"
          and "could not rename" in window.footnote.get_text(),
          f"footer={window.footnote.get_text()!r}")

    focus = window.get_focus
    window.get_focus = lambda: window.list
    window.within = lambda widget: widget is window.list
    opened = []
    window.rename_chat = lambda cid: opened.append(cid)
    try:
        window.select_key(("conntest-at", "csv"), promise=False, zone="chats")
        took = window.key_pressed(None, Gdk.KEY_F2, 0, 0)
    finally:
        del window.rename_chat
        del window.within
        window.get_focus = focus
    check("F2 in the list renames the selected chat",
          took is True and opened == ["conntest-at/csv"], f"opened={opened}")

    # -- B: move to a project ----------------------------------------------
    chat = window.chat_index["conntest-at/csv"]
    entries = window._move_entries(chat)
    names = [e[1] for e in entries]
    check("Move to project lists the file's projects and Automatic",
          names == ["Acme", "Atlas", "Automatic"]
          and entries[2][3] is True, f"entries={names}")
    WRITES.clear()
    entries[0][4]()
    check("moving a chat files it under the project at once",
          window.chat_index["conntest-at/csv"].project == "acme"
          and ("conntest-at", "csv", "project", "acme") in WRITES,
          f"writes={WRITES}")
    window.filters = chats.Filters(project="acme")
    window.render()
    check("and the project filter follows it",
          "conntest-at/csv" in [c.id for c in window.visible_chats])
    window.filters = chats.Filters()
    sessions["conntest-at"][0]["options"]["project"] = "acme"
    window.render()
    entries = window._move_entries(window.chat_index["conntest-at/csv"])
    check("the menu marks the project it is pinned to",
          entries[0][3] is True and entries[2][3] is False)
    WRITES.clear()
    entries[2][4]()
    check("Automatic unsets @conn_project",
          ("conntest-at", "csv", "project", None) in WRITES
          and window.chat_index["conntest-at/csv"].project == "atlas",
          f"writes={WRITES}")
    sessions["conntest-at"][0]["options"]["project"] = ""
    window.render()

    # -- C: the Projects window ----------------------------------------------
    chats.PROJECTS_FILE.write_text(
        '# hand notes\n[acme]\nname = "Acme"\nagent = "claude"\nmode = "auto"\n'
        'later = "kept"\n'
        'servers = { "conntest-app" = "~/acme", "conntest-cal" = "~/acme", '
        '"conntest-empty" = "~" }\n\n'
        '[atlas]\nname = "Atlas"\n'
        'servers = { "conntest-at" = "~/atlas" }\n')
    sessions["conntest-solo"].append(dict(sessions["conntest-cal"][0],
                                          name="solo", path="/home/u/solo"))
    proj = window.open_projects("conntest-solo")
    state = proj.state
    pid = state["current"]
    check("Projects… from a host in no project starts one with it ticked",
          pid in state["new"]
          and state["data"][pid]["servers"] == {"conntest-solo": "~/solo"},
          f"current={pid} data={state['data'].get(pid)}")
    proj.fields["name"].set_text("Solo Work")
    check("a new project's id follows its name",
          state["current"] == "solo-work" and "solo-work" in state["data"],
          f"current={state['current']}")
    proj.fields["colour"].set_text("blue")
    check("a bad colour blocks Save and says why",
          proj.save() is False and "#RRGGBB" in proj.status.get_text(),
          proj.status.get_text())
    proj.fields["colour"].set_text("#8EC5F0")
    proj.fields["letter"].set_text("W")
    saved = proj.save()
    data = chats.read_project_tables(chats.PROJECTS_FILE)
    check("Save writes projects.toml with the new project",
          saved and data.get("solo-work") == {
              "name": "Solo Work", "letter": "W", "color": "#8EC5F0",
              "servers": {"conntest-solo": "~/solo"}},
          f"data={data.get('solo-work')} status={proj.status.get_text()}")
    check("and keeps what it does not show: agent, mode, unknown keys",
          data["acme"].get("agent") == "claude"
          and data["acme"].get("mode") == "auto"
          and data["acme"].get("later") == "kept")
    backup = chats.PROJECTS_FILE.with_name("projects.toml.bak")
    check("with the old file kept as projects.toml.bak",
          backup.exists() and "# hand notes" in backup.read_text()
          and "projects.toml.bak" in proj.status.get_text())
    check("and the sidebar has the new project at once",
          any(p.id == "solo-work" and not p.implicit for p in window.projects),
          f"projects={[p.id for p in window.projects]}")
    proj.select_project("acme")
    tick, _folder = proj.server_rows["conntest-empty"]
    tick.set_active(False)
    proj.save()
    check("unticking a server removes it from the project",
          "conntest-empty" not in chats.read_project_tables(
              chats.PROJECTS_FILE)["acme"]["servers"])
    proj.remove_project("solo-work")
    proj.save()
    check("deleting a project removes it from the file",
          "solo-work" not in chats.read_project_tables(chats.PROJECTS_FILE))
    chats.PROJECTS_FILE.write_text("[broken\n")
    proj.revert()
    before = chats.PROJECTS_FILE.read_text()
    check("a file that does not parse is never saved over",
          proj.save() is False and chats.PROJECTS_FILE.read_text() == before
          and "does not parse" in proj.status.get_text())
    proj.close()
    check("closing it lets the next open start fresh",
          window.projects_window is None)
    sessions["conntest-solo"].clear()
    chats.PROJECTS_FILE.write_text(
        '[acme]\nname = "Acme"\nservers = { "conntest-app" = "~/acme", '
        '"conntest-cal" = "~/acme", "conntest-empty" = "~" }\n\n'
        '[atlas]\nname = "Atlas"\n'
        'servers = { "conntest-at" = "~/atlas" }\n')
    # Save reloads the hosts from the config, as the refresh button does,
    # which takes the in-memory fake fleet with it: put it back.
    window.order = list(sessions)
    for host in sessions:
        row = hosts.blank(host)
        row.update(state="up", home="/home/u", sessions=sessions[host])
        window.rows[host] = row
    window.projects = chats.load_projects(chats.PROJECTS_FILE,
                                          hosts=window.order)
    window._tile_style()
    window.shape = []
    window.render()

    # -- D: finished, not yet seen -------------------------------------------
    telegram = sessions["conntest-app"][0]
    cid = "conntest-app/telegram"
    sent = []
    window.notify = lambda host, s, finished=False: sent.append(
        (host, s["name"], finished))
    telegram["agent"] = dict(telegram["agent"], state=agent_state.WORKING)
    window.render()
    telegram["agent"] = dict(telegram["agent"], state=agent_state.READY,
                             label="idle")
    window.render()
    chat = window.chat_index[cid]
    parts = window.chat_widgets.get(cid)
    check("a chat Claude finished while you were elsewhere needs you",
          chat.finished and chats.is_needs_you(chat)
          and parts["badge"].get_visible_child_name() == "needs"
          and parts["time"].has_css_class("amber")
          and not parts["row"].has_css_class("chat-idle"),
          f"finished={chat.finished}")
    check("it counts in Needs you and says so once",
          window.needs_count.get_text() != "0"
          and sent.count(("conntest-app", "telegram", True)) == 1,
          f"sent={sent} count={window.needs_count.get_text()}")
    window.render()
    check("and not again while it stays that way",
          sent.count(("conntest-app", "telegram", True)) == 1)
    window.mark_seen(cid)
    check("opening it marks it seen, in the seen book on disk",
          not window.chat_index[cid].finished
          and chats.load_seen(chats.SEEN_STATE).get(cid)
          == window.chat_index[cid].last_event)
    window.render()
    check("and it is an idle chat again",
          window.chat_widgets[cid]["badge"].get_visible_child_name() == "none"
          and window.chat_widgets[cid]["row"].has_css_class("chat-idle"))
    del window.notify

    # -- E: the numbers come with alt -----------------------------------------
    number = window.chat_widgets[cid]["number"]
    check("the alt numbers are hidden until alt is held",
          not window.list.has_css_class("alt-held")
          and number.has_css_class("slot-num"))
    passed = window.key_pressed(None, Gdk.KEY_Alt_L, 0, 0)
    check("holding alt shows them, and leaves alt to the terminal",
          passed is False and window.list.has_css_class("alt-held"))
    window.key_released(None, Gdk.KEY_Alt_L, 0, 0)
    check("letting go hides them", not window.list.has_css_class("alt-held"))

    # -- the width does not move ------------------------------------------
    widths = []
    for filters in (chats.Filters(), chats.Filters(needs_you=True),
                    chats.Filters(project="acme", server="conntest-app"),
                    chats.Filters(project="acme", server="conntest-empty",
                                  needs_you=True)):
        window.filters = filters
        window.render()
        widths.append(window.side.measure(Gtk.Orientation.HORIZONTAL, -1)[:2])
        least = window.side.get_child().measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        widths[-1] += (least,)
    window.filters = chats.Filters()
    window.render()
    check("the sidebar is SIDEBAR_WIDTH whatever the filters say",
          all(w[1] == gui.SIDEBAR_WIDTH and w[2] <= gui.SIDEBAR_WIDTH
              for w in widths), f"(min, natural, inner min)={widths}")

    # -- popovers are drawn from the theme -----------------------------------
    css = window.style_css
    import theming
    pal = window.palette
    check("every conn popover gets the palette's panel, not GTK's",
          f"popover.conn-menu > contents {{ background: {pal.panel};" in css
          and (theming.contrast(pal.foreground, pal.surface) or 0)
          >= theming.LEGIBLE - 0.05)


def offline_chats(window, check, gui, hosts, agent_state, Gtk) -> None:
    """A server going dark keeps its chats listed, greyed, from the
    last-known book (known-chats.json) -- and nothing tries to reach them.

    The host is in memory only, never in a config: hosts.probe answers
    "down" for it without running anything, and its stream is a stand-in
    whose poke is all that is looked at. Nothing here leaves the machine.
    """
    import threading

    import chats

    far = "conntest-far"
    now = time.time()

    def fake(name, state, ago, title=""):
        return {"name": name, "windows": "1", "attached": False,
                "activity": "", "panes": [], "screen": "the screen",
                "agent": {"state": state, "label": agent_state.LABELS[state],
                          "detail": "", "draft": "", "tokens": "",
                          "agent": "claude", "preview": "said this",
                          "recap": ""},
                "options": {"project": "", "title": title,
                            "task": "a task nobody else should read",
                            "last_event": int(now - ago), "starred": False,
                            "source": ""},
                "pane_title": "", "path": "", "created": int(now - 3600)}

    def up_row(*sessions, checked=None):
        row = hosts.blank(far)
        row.update(state="up", home="/home/u", sessions=list(sessions),
                   checked=now - 400 if checked is None else checked)
        return row

    def pump(predicate, limit=3.0):
        ctx = gui.GLib.MainContext.default()
        deadline = time.monotonic() + limit
        while not predicate() and time.monotonic() < deadline:
            ctx.iteration(False)
            time.sleep(0.01)

    probed: list = []
    real_probe, real_remove = hosts.probe, hosts.remove_host
    real_notify = window.notify

    def probe(host):
        if host != far:
            return real_probe(host)
        probed.append(host)
        return dict(hosts.blank(far), state="down", error="timed out")

    sent: list = []
    saved_order, holding = window.order, window.holding
    saved_filters = window.filters
    poke = threading.Event()
    hosts.probe = probe
    window.notify = lambda host, s, finished=False: sent.append(host)
    window.holding = True
    try:
        window.set_sidebar_mode("chats")    # the chat rows are under test
        window.order = [far]
        window.rows[far] = up_row(fake("plan", agent_state.READY, 60, "Plan it"),
                                  fake("ask", agent_state.NEEDS_YOU, 120, "Ask"),
                                  fake("sh", agent_state.SHELL, 5))
        window.projects = chats.load_projects(chats.PROJECTS_FILE,
                                              hosts=window.order)
        window._tile_style()
        window.filters = chats.Filters()
        window.render()
        booked = chats.load_known(chats.KNOWN_STATE).get(far, {})
        names = sorted(s["name"] for s in booked.get("sessions", []))
        check("an answering server's chats are booked to known-chats.json",
              names == ["ask", "plan"], f"booked={names}")
        text = chats.KNOWN_STATE.read_text()
        check("without screens, previews or task text",
              "the screen" not in text and "said this" not in text
              and "nobody else" not in text)
        window.render()
        check("last seen is when the server answered, not when it was drawn",
              window.known[far]["seen"] == int(now - 400),
              f"seen={window.known[far]['seen']} answered={int(now - 400)}")
        window.heard[far] = now - 100
        window.render()
        check("or when its stream last spoke, if that is later",
              window.known[far]["seen"] == int(now - 100),
              f"seen={window.known[far]['seen']}")
        window.heard.pop(far, None)

        # -- it goes dark -------------------------------------------------
        window.rows[far] = dict(hosts.blank(far), state="down",
                                error="timed out")
        window.render()
        plan = window.chat_index.get(f"{far}/plan")
        check("a dark server's chats stay listed, offline",
              plan is not None and plan.offline
              and f"{far}/ask" in window.chat_index
              and row_for(window, (far, "plan")) is not None,
              f"chats={sorted(window.chat_index)}")
        check("while its row's sessions stay empty",
              window.rows[far]["sessions"] == [])
        parts = window.chat_widgets.get(f"{far}/plan", {})
        preview = parts["preview"].get_label() if parts else ""
        check("drawn greyed, saying it is unreachable and since when",
              parts and parts["row"].has_css_class("chat-offline")
              and parts["row"].has_css_class("chat-idle")
              and preview.startswith("unreachable · last seen")
              and not parts["star"].get_sensitive(), f"preview={preview!r}")
        ask = window.chat_index.get(f"{far}/ask")
        check("an offline chat that was waiting is not needs you",
              ask is not None and not chats.is_needs_you(ask)
              and window.needs_count.get_text() == "0"
              and not sent, f"count={window.needs_count.get_text()} sent={sent}")

        # -- opening one --------------------------------------------------
        window.streams[far] = (threading.Thread(target=lambda: None),
                               threading.Event(), poke)
        WRITES.clear()
        window.open_session(far, "plan")
        check("opening one does not attach",
              (far, "plan") not in window.open, f"open={list(window.open)}")
        check("it says the server is unreachable",
              "unreachable" in window.footnote.get_text(),
              f"footer={window.footnote.get_text()!r}")
        pump(lambda: probed)
        check("and asks it again now: the stream poked, a probe sent",
              poke.is_set() and probed == [far], f"probed={probed}")
        pump(lambda: far not in window.inflight)
        window.toggle_chat_star(f"{far}/plan")
        window.move_chat(f"{far}/plan", None)
        check("nothing is written to an offline chat",
              not [w for w in WRITES if w[0] == far], f"writes={WRITES}")
        window.row_menu(row_for(window, (far, "plan")), 0, 0)
        pop = [c for c in walk(window.list) if isinstance(c, Gtk.Popover)][-1]
        buttons = {b.get_label(): b.get_sensitive() for b in walk(pop)
                   if isinstance(b, Gtk.Button) and b.get_label()}
        pop.popdown()
        check("its menu offers Open and Check again, the rest greyed",
              buttons.get("Open") and buttons.get(f"Check {far} again")
              and buttons.get("Rename…") is False
              and buttons.get(f"Kill {far}/plan…") is False, f"menu={buttons}")

        # -- asked again: still not attachable while the answer is out -----
        # recheck() (Check <host> again) paints the row "unknown" while its
        # probe runs; that is not a host nobody has heard from.
        pump(lambda: far not in window.inflight)
        probed.clear()
        window.rows[far]["checked"] = now - 30      # it has failed this run
        window.recheck(far)
        state = window.rows[far]["state"]
        window.open_session(far, "plan")
        check("while Check again is out, opening still does not attach",
              state == "unknown" and (far, "plan") not in window.open
              and window.stack.get_visible_child() is window.offline_page,
              f"state={state} open={list(window.open)}")
        pump(lambda: far not in window.inflight)

        # -- the write paths refuse, and open nothing -----------------------
        asked: list = []
        window.ask = lambda *a, **k: asked.append(a)
        tops = len(Gtk.Window.get_toplevels())
        WRITES.clear()
        try:
            renamed = window.rename_chat(f"{far}/plan")
            window.prompt_rename(far, "plan")
            window.confirm_kill(far, "plan")
            pump(lambda: False, 0.2)
        finally:
            del window.ask
        check("rename, rename session and kill refuse an offline chat",
              renamed is None and not asked
              and len(Gtk.Window.get_toplevels()) == tops
              and not [w for w in WRITES if w[0] == far],
              f"asked={asked} writes={WRITES} "
              f"windows={len(Gtk.Window.get_toplevels())}/{tops}")
        pump(lambda: far not in window.inflight)

        # -- a launch: nothing heard from the host yet ----------------------
        # Opening a remembered chat then -- a notification can be what starts
        # the app -- is owed, not attached, and happens if the first answer
        # has it live. The probe is the test's to land, so none is sent.
        def owed_open(answer):
            window.rows[far] = hosts.blank(far)
            window.heard.pop(far, None)
            window.render()
            window.inflight.add(far)
            window.open_session(far, "plan")
            owed, footer = window.open_when_up, window.footnote.get_text()
            opened: list = []
            window.open_session = lambda h, n, focus=True: opened.append((h, n))
            try:
                window._probed(far, answer, time.monotonic())
            finally:
                del window.open_session
            return owed, footer, opened
        owed, footer, opened = owed_open(dict(hosts.blank(far), state="down",
                                              checked=now))
        check("at launch an offline chat is owed an open, not attached",
              owed == (far, "plan") and (far, "plan") not in window.open
              and "opening when it does" in footer, f"owed={owed} {footer!r}")
        status = window.offline_page.status.get_text()
        check("and a first answer of down keeps the page waiting, saying so",
              not opened and window.open_when_up == (far, "plan")
              and status == f"{far} is unreachable",
              f"opened={opened} owed={window.open_when_up} {status!r}")
        owed, footer, opened = owed_open(
            up_row(fake("plan", agent_state.READY, 60, "Plan it")))
        check("and a first answer that has it live opens it then",
              opened == [(far, "plan")] and window.open_when_up is None,
              f"opened={opened}")
        window.rows[far] = dict(hosts.blank(far), state="down", checked=now)
        window.render()
        window.inflight.add(far)
        window.open_session(far, "plan")
        check("once it has answered, an open is refused outright",
              (far, "plan") not in window.open
              and "unreachable" in window.footnote.get_text()
              and window.offline_page.status.get_text()
              == f"{far} is unreachable")
        window.inflight.discard(far)

        # -- New chat does not aim at a dark server ------------------------
        import newchat
        window.projects = [chats.Project(id="both", name="Both", letter="B",
                                         color="", servers={far: "~",
                                                            "local": "~"})]
        real_selected = newchat._selected_chat
        window.rows.setdefault("local", hosts.blank("local"))
        saved_local = window.rows["local"].get("state")
        window.rows["local"]["state"] = "up"
        plan = window.chat_index[f"{far}/plan"]
        saved_project, plan.project = plan.project, "both"
        newchat._selected_chat = lambda win: plan
        try:
            target = newchat.default_target(window)
            check("New chat from an offline chat picks a server that answers",
                  target is not None and target.host == "local",
                  f"target={target and target.host}")
            target.host = far
            check("and never offers to continue an offline chat",
                  newchat.continuable(window, target) is None)
        finally:
            newchat._selected_chat = real_selected
            plan.project = saved_project
            window.rows["local"]["state"] = saved_local
            window.projects = chats.load_projects(chats.PROJECTS_FILE,
                                                  hosts=window.order)

        # -- it comes back --------------------------------------------------
        window.rows[far] = up_row(fake("plan", agent_state.READY, 60, "Plan it"))
        window.render()
        plan = window.chat_index.get(f"{far}/plan")
        check("back up, its live sessions replace the remembered ones",
              plan is not None and not plan.offline
              and f"{far}/ask" not in window.chat_index
              and [r.chat for r in rows(window)
                   if getattr(r, "chat", None) == f"{far}/plan"]
              == [f"{far}/plan"], f"chats={sorted(window.chat_index)}")
        booked = chats.load_known(chats.KNOWN_STATE).get(far, {})
        check("and the book on disk agrees",
              [s["name"] for s in booked.get("sessions", [])] == ["plan"])

        # -- needs you, across a dark spell ---------------------------------
        def live(ask_state):
            window.rows[far] = up_row(
                fake("plan", agent_state.READY, 60, "Plan it"),
                fake("ask", ask_state, 120, "Ask"))
            window.render()
        live(agent_state.NEEDS_YOU)
        sent.clear()
        window.rows[far] = dict(hosts.blank(far), state="down", checked=now)
        window.render()
        window.render()
        check("an offline chat that was waiting says nothing while dark",
              not sent and window.chat_index[f"{far}/ask"].offline,
              f"sent={sent}")
        live(agent_state.NEEDS_YOU)
        check("nor when its server comes back and it is still waiting",
              not sent, f"sent={sent}")
        live(agent_state.READY)
        live(agent_state.NEEDS_YOU)
        check("but a new wait once it is live again is announced",
              sent == [far], f"sent={sent}")

        # -- a title that lands after the server went dark -------------------
        # Asked while the chat was live, answered once it is only remembered:
        # nothing is written to a server that is not answering.
        gate = threading.Event()
        real_ask = gui.retitle.ask
        gui.retitle.ask = lambda screen: (gate.wait(5), "A new title")[1]
        window.rows[far] = up_row(fake("plan", agent_state.READY, 60))
        window.render()
        cid = f"{far}/plan"
        saved_queue = list(window.retitle_queue)
        flag = os.environ.pop("CONN_NO_RETITLE", None)
        try:
            window.retitle_queue[:] = [cid]
            window.retitler.busy = False
            window._retitle_next()
        finally:
            if flag is not None:
                os.environ["CONN_NO_RETITLE"] = flag
        asked_title = window.retitler.busy
        WRITES.clear()
        try:
            window.rows[far] = dict(hosts.blank(far), state="down", checked=now)
            window.render()
            gate.set()
            pump(lambda: not window.retitler.busy)
            pump(lambda: False, 0.3)
        finally:
            gui.retitle.ask = real_ask
            window.retitle_queue[:] = saved_queue
        check("a title landing after its server went dark is not written",
              asked_title and window.chat_index[cid].offline
              and not [w for w in WRITES if w[0] == far]
              and cid not in window.autotitle_wish,
              f"asked={asked_title} writes={WRITES}")

        # -- a launch with the server dark -----------------------------------
        # A fresh window over a book holding a chat "local" no longer has:
        # it is listed before the first probe lands, as connecting.
        ghost = Path(chats.KNOWN_STATE).with_name("known-launch.json")
        chats.save_known({"local": {"seen": int(now - 7200), "home": "",
                                    "hostnames": [], "sessions": [{
                                        "name": "conntest-ghost",
                                        "options": {"title": "Ghost",
                                                    "last_event": int(now - 60)},
                                        "created": int(now - 9000),
                                        "agent": {"state": "ready"}}]}}, ghost)
        real_path = chats.KNOWN_STATE
        chats.KNOWN_STATE = ghost
        try:
            fresh = gui.Conn(window.get_application())
            fresh.notify = lambda host, s, finished=False: None
            seen = fresh.chat_index.get("local/conntest-ghost")
            check("chats are loaded from known-chats.json at launch",
                  seen is not None and seen.offline and seen.title == "Ghost"
                  and seen.preview.startswith("connecting · last seen 2h"),
                  f"chat={seen}")
            fresh.shut_down()
            fresh.destroy()
        finally:
            chats.KNOWN_STATE = real_path

        # -- forgotten ------------------------------------------------------
        window.rows[far] = dict(hosts.blank(far), state="down")
        window.render()
        hosts.remove_host = lambda host: Path(chats.KNOWN_STATE).with_name(
            "config.bak.test")
        window.forget_host(far)
        check("forgetting a server drops what was remembered of it",
              far not in window.known
              and far not in chats.load_known(chats.KNOWN_STATE)
              and f"{far}/plan" not in window.chat_index)
    finally:
        hosts.probe, hosts.remove_host = real_probe, real_remove
        window.notify = real_notify
        window.streams.pop(far, None)
        window.order, window.holding = saved_order, holding
        window.filters = saved_filters
        window.rows.pop(far, None)
        window.known.pop(far, None)
        window.load_hosts(connect=False)
        window.set_sidebar_mode("servers")
        window.render()


def offline_views(window, check, gui, hosts, agent_state, Gtk) -> None:
    """What the right-hand side says about a server that is not answering:
    the offline page for a chat opened while it is dark, the banner over a
    terminal already open on it, and that terminal attaching again when it
    is back.

    The server is in memory only, as in offline_chats: hosts.probe is a
    stand-in, and hosts.connect_argv hands the terminals a local sh or sleep
    for it -- no ssh, no remote tmux, ever.
    """
    import threading

    import chats

    far = "conntest-dark"
    now = time.time()

    def fake(name, title=""):
        return {"name": name, "windows": "1", "attached": False,
                "activity": "", "panes": [], "screen": "",
                "agent": {"state": agent_state.READY,
                          "label": agent_state.LABELS[agent_state.READY],
                          "detail": "", "draft": "", "tokens": "",
                          "agent": "claude", "preview": "", "recap": ""},
                "options": {"project": "", "title": title, "task": "",
                            "last_event": int(now - 60), "starred": False,
                            "source": ""},
                "pane_title": "", "path": "", "created": int(now - 3600)}

    def up(*names):
        row = hosts.blank(far)
        row.update(state="up", home="/home/u", checked=now - 400,
                   sessions=[fake(n, TITLES.get(n, "")) for n in names])
        return row

    def down():
        return dict(hosts.blank(far), state="down", error="timed out",
                    checked=now)

    def pump(predicate, limit=3.0):
        ctx = gui.GLib.MainContext.default()
        deadline = time.monotonic() + limit
        while not predicate() and time.monotonic() < deadline:
            ctx.iteration(False)
            time.sleep(0.01)

    TITLES = {"lic": "Widgets licencing", "other": "Other work",
              "gone": "Gone soon"}
    argv: dict = {}
    spawned: list = []
    probed: list = []
    gate = threading.Event()
    gate.set()
    real_probe, real_argv = hosts.probe, hosts.connect_argv
    real_notify = window.notify

    # What a probe of the dark server answers: down until a check says
    # otherwise, so a probe sent by a click cannot land "up" by accident.
    answer: list = []

    def probe(host):
        if host != far:
            return real_probe(host)
        probed.append(host)
        gate.wait(5)
        return answer[0] if answer else down()

    def land(row):
        """A probe answering `row` -- after any probe already out has
        landed, and with any sent meanwhile answering the same, so none of
        them comes in late and says otherwise."""
        answer[:] = [row]
        pump(lambda: far not in window.inflight)
        window._probed(far, row, time.monotonic())

    def connect_argv(host, name):
        if host != far:
            return real_argv(host, name)
        spawned.append(name)
        return argv.get(name, ["sleep", "30"])

    def showing():
        return window.stack.get_visible_child()

    def highlighted():
        row = window.list.get_selected_row()
        return getattr(row, "key", None) if row is not None else None

    saved_order, holding = window.order, window.holding
    saved_filters = window.filters
    for view in list(window.open.values()):
        window.close_session(view)
    hosts.probe, hosts.connect_argv = probe, connect_argv
    window.notify = lambda host, s, finished=False: None
    window.holding = True
    try:
        window.set_sidebar_mode("chats")    # the chat rows are under test
        window.order = [far]
        window.filters = chats.Filters()
        window.rows[far] = up("lic", "other", "gone")
        window.projects = chats.load_projects(chats.PROJECTS_FILE,
                                              hosts=window.order)
        window._tile_style()
        window.render()
        window.open_session(far, "other")
        other = window.open.get((far, "other"))
        check("a terminal opens on a server that answers",
              other is not None and showing() is other
              and not other.banner.get_reveal_child())

        # -- it goes dark ---------------------------------------------------
        window.rows[far] = down()
        window.render()
        head = other.banner_head.get_text()
        text = other.banner_text.get_text()
        check("the open terminal gets a banner saying its server is offline",
              other.banner.get_reveal_child() and head == "Offline"
              and f"{far} is unreachable" in text
              and "last seen 6m ago" in text
              and "reconnects when it's back" in text,
              f"head={head!r} text={text!r}")
        window.recheck(far)         # "unknown" while it is asked again
        pump(lambda: far not in window.inflight)
        window.rows[far]["state"] = "unknown"
        window.render()
        check("and keeps it through a recheck", other.banner.get_reveal_child())
        window.rows[far] = down()
        window.render()

        # -- clicking an offline chat ---------------------------------------
        probed.clear()
        window.row_activated(window.list, row_for(window, (far, "lic")))
        page = window.offline_page
        check("clicking an offline chat shows the offline page, not the "
              "terminal that was there",
              showing() is page and (far, "lic") not in window.open,
              f"showing={showing()}")
        check("which names the chat, its server and when it was last seen",
              page.title.get_text() == "Widgets licencing"
              and page.status.get_text() == f"{far} is unreachable"
              and page.seen.get_text() == "last seen 6m ago"
              and page.retry.get_visible(),
              f"title={page.title.get_text()!r} "
              f"status={page.status.get_text()!r} seen={page.seen.get_text()!r}")
        check("and the list's highlight follows to that chat",
              highlighted() == (far, "lic"), f"highlighted={highlighted()}")
        pump(lambda: far not in window.inflight)
        check("opening it asked the server again", probed == [far],
              f"probed={probed}")
        check("Trying again is shown only while a probe is out",
              not page.trying.get_visible())

        gate.clear()
        probed.clear()
        page.retry.emit("clicked")
        pump(lambda: probed)
        trying = page.trying.get_visible()
        gate.set()
        pump(lambda: far not in window.inflight)
        window.render()
        check("Check again probes, saying Trying again while it does",
              probed == [far] and trying and not page.trying.get_visible(),
              f"probed={probed} trying={trying}")

        # -- the keyboard ---------------------------------------------------
        window.cycle(1)
        check("ctrl-tab from the page goes to the open terminal",
              showing() is other and highlighted() == (far, "other")
              and window.open_when_up is None,
              f"showing={showing()} owed={window.open_when_up}")
        window.show_nth(window.slots.index((far, "lic")))
        check("alt-N on an offline chat shows its page too",
              showing() is page and window.open_when_up == (far, "lic")
              and highlighted() == (far, "lic"))
        window.focus_list()
        check("F12 still goes back to the list",
              window.list.get_focus_child() is not None
              or window.get_focus() is window.list)

        # -- it comes back, with the chat ------------------------------------
        # Back on the page (Check again has the focus there), so the open
        # is the one you are waiting for and takes the keyboard.
        page.retry.grab_focus()
        spawned.clear()
        land(up("lic", "other", "gone"))
        lic = window.open.get((far, "lic"))
        check("the server coming back opens the chat the page was showing",
              lic is not None and showing() is lic
              and highlighted() == (far, "lic") and spawned == ["lic"],
              f"open={list(window.open)} spawned={spawned}")
        check("and takes the banner off the terminal that was already open",
              not other.banner.get_reveal_child())

        # -- it comes back without the chat ----------------------------------
        answer.clear()
        window.rows[far] = down()
        window.render()
        window.open_session(far, "gone")
        check("another offline chat reuses the page",
              showing() is page and page.title.get_text() == "Gone soon")
        land(up("lic", "other"))
        check("back without that chat, the page says it has ended",
              showing() is page and page.ended
              and page.status.get_text() == "This chat has ended"
              and page.title.get_text() == "Gone soon"
              and not page.retry.get_visible()
              and (far, "gone") not in window.open
              and window.open_when_up is None
              and "has ended" in window.footnote.get_text(),
              f"status={page.status.get_text()!r} open={list(window.open)}")
        window.close_session(lic)
        check("a terminal closing behind the page leaves the page showing",
              showing() is page and (far, "lic") not in window.open)

        # -- it comes back while you are somewhere else in the window --------
        window.rows[far] = down()
        window.render()
        window.open_session(far, "lic")
        check("the page shows the chat again", showing() is page
              and page.key == (far, "lic"))
        window.focus_list()
        moved = row_for(window, (far, "other"))
        window.list.select_row(moved)
        moved.grab_focus()
        spawned.clear()
        land(up("lic", "other", "gone"))
        lic = window.open.get((far, "lic"))
        focus = window.get_focus()
        check("an open owed to the page does not take the keyboard or the "
              "highlight from where you moved them",
              lic is not None and showing() is lic and spawned == ["lic"]
              and highlighted() == (far, "other")
              and focus is not None and not focus.is_ancestor(lic)
              and focus is not lic.term,
              f"open={list(window.open)} highlighted={highlighted()} "
              f"focus={focus}")
        window.show(lic)
        check("and going to it then is an ordinary show",
              highlighted() == (far, "lic")
              and window.get_focus() is lic.term)
        window.close_session(lic)

        # -- it ends while the page is behind Notes or New chat --------------
        for leave, back, how in (
                (lambda: window.set_sidebar_mode("notes"),
                 lambda: window.set_sidebar_mode("chats"), "Notes"),
                (window.new_chat, window.leave_new_chat, "New chat")):
            answer.clear()
            window.rows[far] = up("lic", "other", "gone")
            window.render()
            window.rows[far] = down()
            window.render()
            window.open_session(far, "gone")
            leave()
            check(f"{how} takes the main area from the page",
                  showing() is not page)
            spawned.clear()
            land(up("lic", "other"))
            back()
            check(f"back from {how} to a chat that ended meanwhile says "
                  "so, and does not start an empty one of its name",
                  showing() is page and page.ended
                  and page.status.get_text() == "This chat has ended"
                  and (far, "gone") not in window.open and spawned == [],
                  f"showing={showing()} open={list(window.open)} "
                  f"spawned={spawned}")
        # And without a probe to settle it -- the row alone changed.
        window.rows[far] = up("lic", "other", "gone")
        window.render()
        window.rows[far] = down()
        window.render()
        window.open_session(far, "gone")
        window.new_chat()
        window.rows[far] = up("lic", "other")
        window.render()
        spawned.clear()
        window.leave_new_chat()
        check("coming back checks the row too",
              showing() is page and page.ended
              and (far, "gone") not in window.open and spawned == [],
              f"open={list(window.open)} spawned={spawned}")

        # -- a server that answers and turns you away ------------------------
        for state, words, login in (
                ("nokey", f"{far} refused the key -- is the ssh agent "
                          "unlocked?", False),
                ("unverified", f"{far} needs you to log in -- ssh does not "
                               "trust its key yet", True)):
            window.rows[far] = up("lic", "other")
            window.render()
            window.rows[far] = dict(down(), state=state)
            window.render()
            window.open_session(far, "lic")
            check(f"the page for a '{state}' server says so, not unreachable",
                  showing() is page and page.status.get_text() == words
                  and page.login.get_visible() == login
                  and page.retry.get_visible(),
                  f"status={page.status.get_text()!r} "
                  f"login={page.login.get_visible()}")
        window.rows[far] = up("lic", "other")
        window.render()
        window.open_session(far, "other")
        other = window.open.get((far, "other"))
        window.rows[far] = dict(down(), state="unverified")
        window.render()
        check("and a terminal open on it is not told it reconnects by itself",
              other is not None and other.banner.get_reveal_child()
              and other.banner_head.get_text() == "Needs log in"
              and "needs you to log in" in other.banner_text.get_text()
              and "reconnects when it's back"
              not in other.banner_text.get_text(),
              f"head={other.banner_head.get_text()!r} "
              f"text={other.banner_text.get_text()!r}")
        window.rows[far] = dict(down(), state="nokey")
        window.render()
        check("nor one on a server refusing the key",
              other.banner_head.get_text() == "Key refused"
              and "refused the key" in other.banner_text.get_text()
              and "unreachable" not in other.banner_text.get_text(),
              f"text={other.banner_text.get_text()!r}")
        prompted = []
        real_prompt = window.prompt_new_session
        window.prompt_new_session = lambda host=None: prompted.append(host)
        try:
            window.rows[far] = dict(down(), state="unverified")
            window.render()
            window.open_session(far, "lic")
            page.login.emit("clicked")
        finally:
            window.prompt_new_session = real_prompt
        check("Log in offers the host's New session, where ssh can ask",
              prompted == [far], f"prompted={prompted}")
        window.close_session(other)
        window.rows[far] = up("lic", "other")
        window.render()

        # -- an attach that ends while the server answers ---------------------
        argv["other"] = ["sh", "-c", "exit 0"]
        window.close_session(other)
        window.open_session(far, "other")
        quick = window.open.get((far, "other"))
        pump(lambda: (far, "other") not in window.open)
        check("an attach ending while its server answers closes the view",
              quick is not None and (far, "other") not in window.open,
              f"open={list(window.open)} state={window.rows[far]['state']}")

        # -- an attach that ends as asked while the server looks dark --------
        # A probe that timed out says "down" while the terminal's own ssh
        # is fine: a detach then (exit 0) is a detach.
        argv["other"] = ["sh", "-c", "sleep 0.4"]
        window.open_session(far, "other")
        view = window.open.get((far, "other"))
        land(down())
        window.render()
        pump(lambda: (far, "other") not in window.open, 5.0)
        check("a detach while its server is marked dark closes the view, "
              "and nothing attaches it again",
              (far, "other") not in window.open and not view.dead,
              f"open={list(window.open)} dead={view.dead}")
        spawned.clear()
        land(up("lic", "other"))
        check("not even when the server answers", spawned == []
              and (far, "other") not in window.open, f"spawned={spawned}")

        # -- an attach that dies while the server is dark --------------------
        argv["other"] = ["sh", "-c", "sleep 0.4; exit 255"]
        window.open_session(far, "other")
        view = window.open.get((far, "other"))
        land(down())
        window.render()
        pump(lambda: view.dead, 5.0)
        check("an attach dying while its server is dark keeps the view",
              view.dead and window.open.get((far, "other")) is view
              and view.get_parent() is window.stack
              and view.banner.get_reveal_child()
              and view.banner_head.get_text() == "Disconnected",
              f"dead={view.dead} open={list(window.open)} "
              f"head={view.banner_head.get_text()!r}")
        spawned.clear()
        # Dies again at once, and as a lost connection would: what the
        # respawn guard is for.
        argv["other"] = ["sh", "-c", "exit 255"]
        land(up("lic", "other"))
        respawned = spawned[:], view.dead, view.banner.get_reveal_child()

        def screen():
            return view.term.get_text_format(gui.Vte.Format.TEXT) or ""
        # VTE takes what it is fed on its own time.
        pump(lambda: "reconnecting to" in screen())
        check("and attaches again, once, when the server is back",
              respawned == (["other"], False, False)
              and f"reconnecting to {far}/other" in screen(),
              f"respawned={respawned} text={screen()!r}")
        pump(lambda: (far, "other") not in window.open, 5.0)
        check("an attach that dies again with the server answering is let "
              "go, not respawned in a loop",
              (far, "other") not in window.open and spawned == ["other"]
              and "did not stay connected" in window.footnote.get_text(),
              f"open={list(window.open)} spawned={spawned} "
              f"footer={window.footnote.get_text()!r}")

        # -- dropping again just after a reconnect, the server dark again ----
        # The row still says "up" from the probe that brought it back; that
        # is not an answer about after the drop.
        argv["other"] = ["sh", "-c", "sleep 0.3; exit 255"]
        window.open_session(far, "other")
        view = window.open.get((far, "other"))
        land(down())
        window.render()
        pump(lambda: view.dead, 5.0)
        answer[:] = [up("lic", "other")]
        land(up("lic", "other"))
        check("a terminal that died is respawned when its server is back",
              not view.dead and view.respawn_spent)
        answer[:] = [down()]
        spawned.clear()
        pump(lambda: view.dead, 5.0)
        pump(lambda: far not in window.inflight)
        window.render()
        check("dropping again straight after keeps it, the server asked "
              "rather than taken as up",
              view.dead and window.open.get((far, "other")) is view
              and not view.respawn_spent
              and view.banner_head.get_text() == "Disconnected",
              f"dead={view.dead} spent={view.respawn_spent} "
              f"open={list(window.open)}")
        argv["other"] = ["sleep", "30"]
        land(up("lic", "other"))
        check("and, dark and back again, it gets a respawn of its own",
              spawned == ["other"] and not view.dead
              and window.open.get((far, "other")) is view,
              f"spawned={spawned} dead={view.dead}")
        window.close_session(view)

        # -- a respawn that stayed up, then drops ----------------------------
        argv["other"] = ["sh", "-c", "sleep 0.3; exit 255"]
        window.open_session(far, "other")
        view = window.open.get((far, "other"))
        answer[:] = [up("lic", "other")]
        pump(lambda: view.dead, 5.0)
        pump(lambda: view.respawn_spent and not view.dead, 5.0)
        view.respawned -= gui.RESPAWN_STABLE + 1      # it stayed up a while
        argv["other"] = ["sleep", "30"]
        spawned.clear()
        pump(lambda: spawned, 5.0)
        check("a respawn that stayed up earns another for a later drop",
              spawned == ["other"] and window.open.get((far, "other")) is view,
              f"spawned={spawned} open={list(window.open)}")
        window.close_session(view)

        # -- an attach that never started ------------------------------------
        # ssh-connect holds the window with the reason and "press any key to
        # close this window", then exits with ssh's 255 and leaves a note.
        import shutil
        import tempfile
        real_note = hosts.LAUNCH_ERROR
        scratch = Path(tempfile.mkdtemp())
        hosts.LAUNCH_ERROR = scratch / "last-launch-error"
        try:
            argv["other"] = ["sh", "-c",
                             f"printf '%s\\t%s\\t%s\\n' \"$(date +%s)\" "
                             f"{far}/other 'Connection refused' "
                             f"> '{hosts.LAUNCH_ERROR}'; exit 255"]
            probed.clear()
            spawned.clear()
            window.open_session(far, "other")
            view = window.open.get((far, "other"))
            pump(lambda: (far, "other") not in window.open, 5.0)
            check("an attach that never started closes when it says it will, "
                  "with ssh-connect's reason, and is not retried",
                  (far, "other") not in window.open and not view.dead
                  and spawned == ["other"] and probed == []
                  and "Connection refused" in window.footnote.get_text(),
                  f"open={list(window.open)} spawned={spawned} "
                  f"probed={probed} footer={window.footnote.get_text()!r}")
        finally:
            hosts.LAUNCH_ERROR = real_note
            shutil.rmtree(scratch, ignore_errors=True)

        # -- ssh's own "connection lost" while the server looked up ----------
        argv["other"] = ["sh", "-c", "exit 255"]
        answer.clear()
        probed.clear()
        window.open_session(far, "other")
        view = window.open.get((far, "other"))
        pump(lambda: view.dead, 5.0)
        check("ssh losing the connection keeps the view and asks the server",
              view.dead and window.open.get((far, "other")) is view
              and view.banner_head.get_text() == "Disconnected"
              and probed == [far],
              f"dead={view.dead} probed={probed}")
        pump(lambda: far not in window.inflight)
        check("which, answering down, says so over the terminal",
              f"{far} is unreachable" in view.banner_text.get_text()
              and view.dead and (far, "other") in window.open,
              f"text={view.banner_text.get_text()!r}")
        window.close_session(view)
    finally:
        gate.set()
        hosts.probe, hosts.connect_argv = real_probe, real_argv
        window.notify = real_notify
        for view in list(window.open.values()):
            window.close_session(view)
        window.order, window.holding = saved_order, holding
        window.filters = saved_filters
        window.rows.pop(far, None)
        window.known.pop(far, None)
        window.load_hosts(connect=False)
        window.set_sidebar_mode("servers")
        window.render()


def option_writer(check, gui, hosts) -> None:
    """Quitting with a star still queued: it is sent, not dropped.

    The worker is held up for a second on a recency stamp to a slow host
    when the star is clicked, and stop() comes at once -- as ctrl-q would.
    """
    import threading
    sent = []
    started = threading.Event()

    def slow(host, session, name, value):
        if name == "last_event":
            started.set()
            time.sleep(1.0)
        sent.append((host, session, name, value))

    hosts.set_chat_option = slow
    writer = gui.OptionWriter(lambda *args: None)
    writer.put("h", "a", "last_event", "100")
    started.wait(2)
    writer.put("h", "b", "starred", "1")
    writer.put("h", "c", "last_event", "200")
    lost = writer.stop(timeout=3.0)
    check("a star queued at quit is sent before the writer stops",
          ("h", "b", "starred", "1") in sent and lost == [],
          f"sent={sent} lost={lost}")
    check("and a queued recency stamp is not waited for",
          ("h", "c", "last_event", "200") not in sent, f"sent={sent}")

    def dark(host, session, name, value):
        time.sleep(5)

    hosts.set_chat_option = dark
    writer = gui.OptionWriter(lambda *args: None)
    writer.pending[("h", "d", "starred")] = "1"     # queued, worker not run
    began = time.monotonic()
    lost = writer.stop(timeout=0.3)
    check("a star the server never confirms is reported, within the wait",
          lost == [("h", "d", "starred")] and time.monotonic() - began < 2,
          f"lost={lost}")


def servers_tab(window, check, gui, hosts, agent_state, Gtk, fakes) -> None:
    """The Servers tab (ctrl-2), over chat_list's fake fleet plus five more
    fake servers, one per broken state.

    Nothing here may reach a server: every handler that would open a
    session, a terminal or an ssh is stubbed on the window, and the fake
    hosts are never probed (chat_list holds the sweep).
    """
    import palette as cmdk
    from gi.repository import Gdk

    ctrl = Gdk.ModifierType.CONTROL_MASK
    broken = {
        "conntest-nokey": dict(state="nokey",
                               error="Permission denied (publickey)."),
        "conntest-new": dict(state="unverified",
                             error="new server -- ssh does not trust it"),
        "conntest-changed": dict(state="unverified", keychanged=True,
                                 error="host key changed -- compare them"),
        "conntest-down": dict(state="down",
                              error="ssh: connect to host conntest-down port "
                                    "22: Connection timed out"),
        "conntest-wait": dict(state="unknown"),
    }
    saved_order = list(window.order)
    saved_expanded = set(window.expanded)
    stubbed = ("open_session", "open_server_shell", "install_key",
               "trust_host", "check_host_key", "recheck", "open_manager",
               "move_host")
    calls: list = []
    try:
        for host, fields in broken.items():
            row = hosts.blank(host)
            row.update(fields)
            window.rows[host] = row
        app = window.rows["conntest-app"]
        app.update(uptime="3 days, 4 hours", disk="12G / 50G (24%)")
        window.order = list(fakes) + list(broken)
        # As a first run has it: nothing opened out.
        window.expanded = set()
        gui.EXPANDED_STATE.unlink(missing_ok=True)

        window.key_pressed(None, Gdk.KEY_2, 0, ctrl)
        check("ctrl-2 switches to the Servers tab",
              window.sidebar_mode == "servers"
              and window.mode_buttons["servers"].get_active()
              and not window.mode_buttons["chats"].get_active(),
              f"mode={window.sidebar_mode}")
        check("which hides the chat chips, keeping a search for servers",
              not window.project_chip.get_visible()
              and not window.server_chip.get_visible()
              and not window.needs_chip.get_visible()
              and window.search_box.get_visible()
              and window.filter.get_placeholder_text() == "Search servers")
        check("the Servers tab sits beside Chats and Notes",
              set(window.mode_buttons) == {"chats", "notes", "servers"}
              and window.mode_buttons["servers"].get_tooltip_text()
              == "Servers (ctrl-2)")
        tabs, child = [], window.mode_buttons["chats"].get_parent().get_first_child()
        while child is not None:
            tabs.append(next(m for m, b in window.mode_buttons.items()
                             if b is child))
            child = child.get_next_sibling()
        check("the tabs read Chats | Servers | Notes",
              tabs == ["chats", "servers", "notes"], str(tabs))

        listed = [r.server for r in rows(window) if getattr(r, "server", None)]
        check("every server is listed, in the order you keep them",
              listed == window.order, f"listed={listed}")
        check("servers start folded: no session is listed under any",
              not any(getattr(r, "key", None) for r in rows(window)),
              f"keys={[r.key for r in rows(window) if r.key]}")
        check("and none is remembered as opened out",
              window.expanded == set() and gui.read_expanded() == set(),
              f"expanded={window.expanded}")

        def chevron(host):
            row = next(r for r in rows(window)
                       if getattr(r, "server", None) == host)
            return row.chevron

        check("a server with sessions has a chevron to open it, an empty "
              "one only a blank",
              isinstance(chevron("conntest-app"), Gtk.Button)
              and chevron("conntest-app").get_icon_name()
              == "pan-end-symbolic"
              and not isinstance(chevron("conntest-empty"), Gtk.Button))
        chevron("conntest-app").emit("clicked")
        keys = [getattr(r, "server", None) or getattr(r, "key", None)
                for r in rows(window)]
        at = keys.index("conntest-app")
        check("clicking the chevron opens it out: its sessions under it, "
              "chats and plain shells alike",
              keys[at + 1:at + 3] == [("conntest-app", "telegram"),
                                      ("conntest-app", "shell")]
              and server_row(window, ("conntest-at", "csv")) is None
              and chevron("conntest-app").get_icon_name()
              == "pan-down-symbolic",
              f"after app={keys[at + 1:at + 3]}")
        check("and that is written down for next time",
              gui.read_expanded() == {"conntest-app"}
              and gui.EXPANDED_STATE.read_text().split() == ["conntest-app"],
              f"read={gui.read_expanded()}")
        chevron("conntest-app").emit("clicked")
        check("clicking it again folds it",
              server_row(window, ("conntest-app", "telegram")) is None
              and gui.read_expanded() == set())

        # The keys, from the list: Right / plus open, Left / minus fold, and
        # Left on a session folds its server and lands on it.
        none = Gdk.ModifierType(0)
        window.select_server("conntest-at")
        window.focus_list()
        took = window.key_pressed(None, Gdk.KEY_Right, 0, none)
        check("Right on a server opens it out",
              took is True
              and server_row(window, ("conntest-at", "csv")) is not None
              and "conntest-at" in window.expanded
              and getattr(window.list.get_selected_row(), "server", None)
              == "conntest-at")
        window.key_pressed(None, Gdk.KEY_Left, 0, none)
        check("Left folds it again",
              server_row(window, ("conntest-at", "csv")) is None
              and "conntest-at" not in window.expanded)
        window.key_pressed(None, Gdk.KEY_plus, 0, Gdk.ModifierType.SHIFT_MASK)
        check("plus opens it too, shift and all",
              server_row(window, ("conntest-at", "csv")) is not None)
        window.list.select_row(server_row(window, ("conntest-at", "home")))
        window.focus_list()
        window.key_pressed(None, Gdk.KEY_minus, 0, none)
        picked = window.list.get_selected_row()
        check("minus on a session folds its server and lands on it",
              server_row(window, ("conntest-at", "home")) is None
              and getattr(picked, "server", None) == "conntest-at"
              and window.within(window.list),
              f"picked={getattr(picked, 'server', None)}")
        check("the remembered set follows the keys",
              gui.read_expanded() == set(), f"read={gui.read_expanded()}")
        # Left on the session that is showing: render() owes that session
        # the highlight back when its row returns. Folding is a move to the
        # server, so opening it again must not jump back down.
        home = ("conntest-at", "home")
        window.key_pressed(None, Gdk.KEY_Right, 0, none)
        window.open[home] = object()
        try:
            window.list.select_row(server_row(window, home))
            window.focus_list()
            window.pending = home       # as render() arms it for the view
            window.key_pressed(None, Gdk.KEY_Left, 0, none)
            check("Left on the session showing leaves no promise behind",
                  window.pending is None
                  and getattr(window.list.get_selected_row(), "server", None)
                  == "conntest-at", f"pending={window.pending}")
            window.key_pressed(None, Gdk.KEY_Right, 0, none)
            picked = window.list.get_selected_row()
            check("so Right opens it again with the highlight on the server",
                  getattr(picked, "server", None) == "conntest-at"
                  and server_row(window, home) is not None,
                  f"picked={getattr(picked, 'server', None)} "
                  f"key={getattr(picked, 'key', None)}")
        finally:
            window.open.pop(home, None)
            window.pending = None
            window.expanded.clear()
            gui.save_expanded(window.expanded)
            window.render()
        # Read back as a new window would: the file, not the window's set.
        gui.save_expanded({"conntest-cal"})
        check("what is remembered is read back as written",
              gui.read_expanded() == {"conntest-cal"})
        gui.save_expanded(window.expanded)
        check("and Add server at the bottom",
              getattr(list(rows(window))[-1], "add_server", False))

        def words(host):
            return window.host_widgets[host]["words"].get_text()

        for host, wanted in (("conntest-nokey", "no key"),
                             ("conntest-new", "new host key"),
                             ("conntest-changed", "host key changed"),
                             ("conntest-down", "can't reach"),
                             ("conntest-wait", "checking"),
                             ("conntest-app", "up")):
            check(f"{host} reads {wanted!r}", words(host).startswith(wanted),
                  f"words={words(host)!r}")
        check("a key problem's row does not say it twice",
              words("conntest-changed") == "host key changed"
              and words("conntest-new") == "new host key"
              and "compare them" in (window.host_widgets[
                  "conntest-changed"]["words"].get_tooltip_text() or ""),
              f"changed={words('conntest-changed')!r} "
              f"new={words('conntest-new')!r}")
        check("a server that is up shows its uptime and disk",
              "3 days" in words("conntest-app")
              and "disk 24%" in words("conntest-app"),
              f"words={words('conntest-app')!r}")
        check("one that cannot be reached says what ssh said",
              "Connection timed out" in words("conntest-down")
              and "Connection timed out" in (window.host_widgets[
                  "conntest-down"]["words"].get_tooltip_text() or ""))
        dp_chats = sum(1 for c in window.chats if c.host == "conntest-at")

        def summary(host):
            return window.host_widgets[host]["count"].get_text()

        check("and each server says what its fold hides",
              dp_chats == 4 and summary("conntest-at") == "4 chats"
              and summary("conntest-app") == "1 chat \u00b7 1 shell"
              and summary("conntest-empty") == "no sessions"
              and summary("conntest-wait") == "",
              f"at={summary('conntest-at')!r} app={summary('conntest-app')!r} "
              f"empty={summary('conntest-empty')!r}")
        dp_sessions = window.rows["conntest-at"]["sessions"]
        extra = dict(window.rows["conntest-app"]["sessions"][1],
                     name="scratch")
        dp_sessions.append(extra)
        try:
            window.render()
            check("chats and shells both, in one line: 4 chats \u00b7 1 shell",
                  summary("conntest-at") == "4 chats \u00b7 1 shell",
                  f"at={summary('conntest-at')!r}")
        finally:
            dp_sessions.remove(extra)
            window.render()

        # One status on a session row, never the same thing twice.
        app_sessions = window.rows["conntest-app"]["sessions"]
        was = [dict(s["agent"]) for s in app_sessions]
        try:
            app_sessions[0]["agent"].update(detail="working")
            app_sessions[1]["agent"].update(detail="bash")
            window.expanded.add("conntest-app")
            window.render()

            def tail(key):
                return window.widgets[key]["detail"].get_label()

            check("a chat whose detail is its state says it once",
                  tail(("conntest-app", "telegram")) == "working",
                  f"tail={tail(('conntest-app', 'telegram'))!r}")
            check("a plain shell says what is running, not \"shell bash\"",
                  tail(("conntest-app", "shell")) == "bash",
                  f"tail={tail(('conntest-app', 'shell'))!r}")
            app_sessions[0]["agent"].update(detail="Crunched for 6s")
            window.repaint()
            check("a chat with more to say says that instead of the state",
                  tail(("conntest-app", "telegram")) == "Crunched for 6s"
                  and "working" in (window.widgets[(
                      "conntest-app", "telegram")]["detail"]
                      .get_tooltip_text() or ""),
                  f"tail={tail(('conntest-app', 'telegram'))!r}")
            repeats = []
            for host in window.order:
                for session in window.rows[host]["sessions"]:
                    words = gui.Conn._detail(session).lower().split()
                    if any(a == b for a, b in zip(words, words[1:])):
                        repeats.append((host, session["name"], words))
            check("no session row's status repeats a word", not repeats,
                  str(repeats))
            check("and a plain shell's row is the quieter kind",
                  server_row(window, ("conntest-app", "shell"))
                  .has_css_class("plain")
                  and not server_row(window, ("conntest-app", "telegram"))
                  .has_css_class("plain"))
        finally:
            for session, agent in zip(app_sessions, was):
                session["agent"] = agent
            window.expanded.discard("conntest-app")
            window.render()
        check("the tab counts the servers that need you",
              window.mode_buttons["servers"].count.get_text() == "4",
              f"count={window.mode_buttons['servers'].count.get_text()!r}")

        for name in stubbed:
            setattr(window, name,
                    lambda *a, _n=name, **_k: calls.append((_n,) + a))

        def server(host):
            return next(r for r in rows(window)
                        if getattr(r, "server", None) == host)

        def fix_button(host):
            return next((w for w in walk(server(host))
                         if isinstance(w, Gtk.Button)
                         and getattr(w, "fix", None)), None)

        del window.open_server_shell        # the real one, over the stub
        window.row_activated(window.list, server("conntest-app"))
        check("clicking a server opens its \"shell\" session, made if need be",
              calls == [("open_session", "conntest-app", "shell")],
              f"calls={calls}")
        # The real open_session reveals the shell; a folded server stays
        # folded and keeps the highlight rather than opening out.
        window.expanded.discard("conntest-app")
        window.render()
        stub = window.open_session
        window.open_session = lambda h, n: window.reveal((h, n))
        window.row_activated(window.list, server("conntest-app"))
        window.open_session = stub
        selected = window.list.get_selected_row()
        check("a server's click leaves it folded, its own row highlighted",
              "conntest-app" not in window.expanded
              and not window.server_open("conntest-app", window.query())
              and getattr(selected, "server", None) == "conntest-app",
              f"expanded={window.expanded} "
              f"selected={getattr(selected, 'server', selected)}")
        window.open_server_shell = (lambda *a: calls.append(
            ("open_server_shell",) + a))
        calls.clear()
        window.row_activated(window.list, server("local")
                             if "local" in window.order
                             else server("conntest-at"))
        check("the same for any server, by the one path",
              len(calls) == 1 and calls[0][0] == "open_server_shell",
              f"calls={calls}")
        calls.clear()
        window.expanded.add("conntest-app")     # its sessions on show
        window.render()
        window.row_activated(window.list,
                             server_row(window, ("conntest-app", "shell")))
        check("clicking a session under it opens that session, as the fold did",
              calls == [("open_session", "conntest-app", "shell")],
              f"calls={calls}")
        calls.clear()
        window.row_activated(window.list, list(rows(window))[-1])
        check("and Add server opens the Site Manager",
              calls == [("open_manager",)], f"calls={calls}")

        check("a server that is up has no fix to offer",
              fix_button("conntest-app") is None)
        for host, handler, label in (
                ("conntest-nokey", "install_key", "Install my key"),
                ("conntest-new", "trust_host", "Trust\u2026"),
                ("conntest-changed", "check_host_key", "Check new key\u2026"),
                ("conntest-down", "recheck", "Check again")):
            calls.clear()
            button = fix_button(host)
            if button is None:
                check(f"{host} has a fix button", False)
                continue
            button.emit("clicked")
            check(f"{host}'s button is {label!r} and calls {handler}",
                  button.get_label() == label
                  and calls == [(handler, host)],
                  f"label={button.get_label()!r} calls={calls}")

        # The highlight on a server row survives a rebuild, as a session's
        # does: it has no session key, so it is found again by its host.
        window.select_server("conntest-cal")
        window.shape = []
        window.render()
        picked = window.list.get_selected_row()
        check("a highlighted server stays highlighted through a rebuild",
              getattr(picked, "server", None) == "conntest-cal")
        calls.clear()
        window.move_selected(-1)
        check("and ctrl-shift-up moves the selected server",
              calls == [("move_host", "conntest-cal", -1)], f"calls={calls}")
        window.expanded.add("conntest-at")
        window.render()
        window.list.select_row(server_row(window, ("conntest-at", "csv")))
        window.shape = []
        window.render()
        picked = window.list.get_selected_row()
        check("a highlighted session in the tab stays through a rebuild",
              getattr(picked, "key", None) == ("conntest-at", "csv"))
        window.list.unselect_all()
        window.expanded.clear()
        gui.save_expanded(window.expanded)
        window.render()

        window.filter.set_text("telegram")
        window.refilter()
        listed = [r.server for r in rows(window) if getattr(r, "server", None)]
        check("a search in the tab keeps the servers with a match",
              listed == ["conntest-app"]
              and server_row(window, ("conntest-app", "telegram")) is not None
              and window.matches(server("conntest-app")),
              f"listed={listed}")
        check("opened out to show the match, without remembering it",
              server(("conntest-app")).opened
              and window.expanded == set() and gui.read_expanded() == set())
        app_chevron = server("conntest-app").chevron
        check("its chevron points down, as it is open",
              app_chevron.get_icon_name() == "pan-down-symbolic")
        app_chevron.emit("clicked")
        check("and clicking it folds it, the search's opening not saved",
              server_row(window, ("conntest-app", "telegram")) is None
              and not server("conntest-app").opened
              and window.expanded == set() and gui.read_expanded() == set(),
              f"expanded={window.expanded} read={gui.read_expanded()}")
        server("conntest-app").chevron.emit("clicked")
        check("clicking it again opens it, still not saved",
              server_row(window, ("conntest-app", "telegram")) is not None
              and window.expanded == set() and gui.read_expanded() == set())
        window.list.select_row(server_row(window, ("conntest-app",
                                                   "telegram")))
        window.focus_list()
        window.key_pressed(None, Gdk.KEY_Left, 0, none)
        check("Left on a match folds its server during the search too",
              server_row(window, ("conntest-app", "telegram")) is None
              and getattr(window.list.get_selected_row(), "server", None)
              == "conntest-app"
              and window.expanded == set() and gui.read_expanded() == set())
        window.key_pressed(None, Gdk.KEY_Right, 0, none)
        check("and Right opens it again, nothing saved",
              server_row(window, ("conntest-app", "telegram")) is not None
              and window.expanded == set() and gui.read_expanded() == set())
        window.key_pressed(None, Gdk.KEY_Left, 0, none)
        window.filter.set_text("telegra")
        window.refilter()
        check("a new search forgets what the last one folded",
              server_row(window, ("conntest-app", "telegram")) is not None)
        window.filter.set_text("conntest-cal")
        window.refilter()
        check("a match on a server's own name finds the server, still folded",
              not server("conntest-cal").opened
              and server_row(window, ("conntest-cal", "calendar")) is None)
        window.clear_filter()
        check("and clearing the search folds them back",
              not any(getattr(r, "key", None) for r in rows(window)))

        # Out of the tab and back in by the palette: a server picked in
        # ctrl-k lands on its row in the Servers tab.
        window.key_pressed(None, Gdk.KEY_1, 0, ctrl)
        check("ctrl-1 goes back to the chats",
              window.sidebar_mode == "chats"
              and not any(getattr(r, "server", None) for r in rows(window)))
        window.palette_run(cmdk.Item("Servers", "conntest-cal", kind="host",
                                     key=("conntest-cal",)))
        picked = window.list.get_selected_row()
        check("a server picked in the palette lands in the Servers tab, on it",
              window.sidebar_mode == "servers"
              and getattr(picked, "server", None) == "conntest-cal",
              f"mode={window.sidebar_mode} "
              f"picked={getattr(picked, 'server', None)}")
        window.set_sidebar_mode("chats")
        window.reveal(("conntest-app", "shell"))
        check("a plain shell opened from Chats is shown in the Servers tab",
              window.sidebar_mode == "servers")
        check("with its server opened out, so its row is there to highlight",
              "conntest-app" in window.expanded
              and server_row(window, ("conntest-app", "shell")) is not None)
        window.expanded.clear()
        window.render()
        # And from the palette, by the real open_session: the view is
        # already "open" (a stand-in -- nothing is attached) and show() is
        # recorded, so nothing reaches the fake server.
        del window.open_session
        shown_calls: list = []
        window.show = lambda *a, **_k: shown_calls.append(a)
        window.open[("conntest-app", "shell")] = object()
        try:
            window.palette_run(cmdk.Item("Shells", "shell", kind="shell",
                                         key=("conntest-app", "shell")))
            check("a plain shell picked in the palette opens its server out",
                  window.sidebar_mode == "servers"
                  and "conntest-app" in window.expanded
                  and server_row(window, ("conntest-app", "shell"))
                  is not None and len(shown_calls) == 1,
                  f"mode={window.sidebar_mode} expanded={window.expanded}")
        finally:
            window.open.pop(("conntest-app", "shell"), None)
            window.__dict__.pop("show", None)
            window.open_session = (lambda *a, _n="open_session", **_k:
                                   calls.append((_n,) + a))
            window.expanded.clear()
            gui.save_expanded(window.expanded)
            window.render()
        window.set_sidebar_mode("chats")
        window.reveal(("conntest-at", "csv"))
        check("while a chat stays in Chats", window.sidebar_mode == "chats")
        # A chat just started (ctrl-n, or from a note) is opened before the
        # recheck lists it: not in chat_index, not in rows, only stamped.
        # It must not send you to the Servers tab.
        fresh = ("conntest-at", "conntest-just-started")
        window.stamps[gui.chats.chat_id(*fresh)] = int(time.time())
        try:
            window.reveal(fresh)
            check("a chat just started stays in Chats, though not listed yet",
                  window.sidebar_mode == "chats",
                  f"mode={window.sidebar_mode}")
            window.reveal(("conntest-at", "conntest-unheard-of"))
            check("as does any session the server has not reported yet",
                  window.sidebar_mode == "chats",
                  f"mode={window.sidebar_mode}")
        finally:
            window.stamps.pop(gui.chats.chat_id(*fresh), None)
        check("and the palette offers the tab by name",
              any(a.title == "Servers" and a.hint == "ctrl-2"
                  for a in window._palette_actions("")))
    finally:
        for name in stubbed:
            window.__dict__.pop(name, None)
        for host in broken:
            window.rows.pop(host, None)
        window.expanded = saved_expanded
        gui.save_expanded(window.expanded)
        window.order = saved_order
        window.set_sidebar_mode("chats")
        window.filter.set_text("")
        window.render()


def rebuilds_let_go(window, check, gui, agent_state, Gtk, frame) -> None:
    """The 16 Nov freeze: a sidebar rebuild that kept every row it threw
    away, and one that never finished while a right-click menu was open.

    Rows were held by their own right-click gesture's handler (r=row) and
    the working badge by its own draw func -- cycles through C that Python's
    collector cannot see -- so each rebuild leaked the whole list, about
    1 MB in the Chats tab and half that in Servers, and a shape that kept
    changing ran the window into gigabytes. Here the shape is made to
    change on every frame, which is the worst case, and what is alive
    afterwards is counted.
    """
    import gc
    import signal

    def live(kind):
        gc.collect()
        return sum(1 for o in gc.get_objects() if isinstance(o, kind))

    def rss():
        with open("/proc/self/status") as status:
            for line in status:
                if line.startswith("VmRSS"):
                    return int(line.split()[1]) / 1024
        return 0.0

    def churn(frames):
        for i in range(frames):
            window.frames.put(("conntest-app", frame(
                "conntest-app", {"telegram": BUSY.format(glyph="✶✷"[i % 2]),
                                 "shell": f"$ ls\nfile{i}\n$ "})))
            window.shape = None     # a shape that changes on every frame
            window.drain_frames()

    saved_expanded = set(window.expanded)
    try:
        for mode in ("servers", "chats"):
            window.set_sidebar_mode(mode)
            if mode == "servers":
                window.set_expanded("conntest-app", True)
            churn(20)
            rows0, badges0, mb0 = (live(Gtk.ListBoxRow),
                                   live(gui.WorkingBadge), rss())
            churn(300)
            rows1, badges1, mb1 = (live(Gtk.ListBoxRow),
                                   live(gui.WorkingBadge), rss())
            check(f"300 rebuilds in the {mode} tab leave no rows behind",
                  rows1 <= rows0 + 2 and badges1 <= badges0 + 2,
                  f"rows {rows0} -> {rows1}, badges {badges0} -> {badges1}")
            check(f"and the {mode} tab's memory stays flat over them",
                  mb1 - mb0 < 40, f"rss {mb0:.0f} -> {mb1:.0f} MB")

        # A rebuild under an open right-click menu: the menu is a child of
        # the list but not a row, and remove-until-empty spun on it forever.
        window.set_sidebar_mode("servers")
        server = next(r for r in rows(window)
                      if getattr(r, "server", None) == "conntest-app")
        window.host_menu(server, "conntest-app", 5, 5)
        menu = window.list.get_last_child()

        def stuck(*_a):
            raise TimeoutError("render() did not return")
        old = signal.signal(signal.SIGALRM, stuck)
        signal.alarm(5)
        try:
            window.shape = None
            window.render()
            finished = True
        except TimeoutError:
            finished = False
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
        check("a rebuild with a right-click menu open finishes",
              finished and isinstance(menu, Gtk.Popover))
        check("and leaves the menu where it was",
              menu.get_parent() is window.list
              and any(getattr(r, "server", None) == "conntest-app"
                      for r in rows(window)))
        menu.popdown()
        if menu.get_parent() is not None:
            menu.unparent()
    finally:
        window.expanded = saved_expanded
        gui.save_expanded(window.expanded)
        window.set_sidebar_mode("chats")
        window.render()


def notesview_text(board):
    buf = board.editor.get_buffer()
    return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)


def rows(window):
    index = 0
    while (row := window.list.get_row_at_index(index)) is not None:
        yield row
        index += 1


def row_for(window, key):
    return next((r for r in rows(window) if getattr(r, "key", None) == key), None)


def server_row(window, key):
    """The session's row under Servers -- a chat has one in the chat list
    too, and row_for finds that one first."""
    return next((r for r in rows(window) if getattr(r, "key", None) == key
                 and getattr(r, "zone", "") == "servers"), None)


if __name__ == "__main__":
    raise SystemExit(main())
