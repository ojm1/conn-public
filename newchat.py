"""New chat and quick add: desktop phase 2 (ChatNew / QuickAdd mockups).

Both start a chat the same way -- Conn.start_chat(), which runs
starting.start_chat() on a worker -- and differ only in how much they ask:
the New chat screen shows every choice, quick add shows one line and takes
the defaults the screen would have picked.

What is chosen where, so the two never disagree:
- project: the filter's project, else the focused chat's, else the last one
  used (remembered in newchat.json in conn's state folder);
- server: the filter's server if it belongs to the project, else the
  project's first server that answers, else its first;
- folder: the project's folder for that server (projects.toml);
- mode: the project's `mode`, else the mode you last picked.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from gi.repository import Gdk, GLib, Gtk, Pango

import agent_state
import chats
import paths
import starting

PREFS = paths.STATE / "newchat.json"


def load_prefs(path: Path = PREFS) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_prefs(changes: dict, path: Path = PREFS) -> None:
    data = load_prefs(path)
    data.update(changes)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)
    except OSError:
        pass


@dataclass
class Target:
    """Where a new chat goes and how it starts."""
    project: chats.Project
    host: str
    folder: str
    mode: str = ""
    resume: bool = False
    include_notes: bool = False

    @property
    def label(self) -> str:
        return f"{self.host} {self.folder}"


def choose_server(project: chats.Project, rows: dict,
                  wanted: str | None = None) -> str:
    servers = list(project.servers)
    if wanted in servers:
        return wanted
    up = [h for h in servers if rows.get(h, {}).get("state") == "up"]
    return (up or servers or [chats_host_fallback(project)])[0]


def chats_host_fallback(project: chats.Project) -> str:
    return project.id


def default_target(win, project_id: str | None = None,
                   host: str | None = None) -> Target | None:
    """The New chat screen's starting choices -- see the module docstring."""
    projects = [p for p in win.projects if p.servers]
    if not projects:
        return None
    by_id = {p.id: p for p in projects}
    prefs = load_prefs()
    project = None
    selected = _selected_chat(win)
    for pid in (project_id, win.filters.project, _focused_project(win),
                selected.project if selected else None,
                prefs.get("project")):
        if pid in by_id:
            project = by_id[pid]
            break
    if project is None and host:
        project = next((p for p in projects if host in p.servers), None)
    project = project or projects[0]
    # The server: asked for, the filter's, the selected chat's when it is
    # in this project -- the + button always meant "here" -- else the
    # project's first that answers.
    # Not an offline chat's: its server is the one not answering.
    wanted = host or win.filters.server or (
        selected.host if selected and selected.project == project.id
        and not selected.offline else None)
    server = choose_server(project, win.rows, wanted)
    mode = project.mode if project.mode in starting.MODE_LABEL else \
        prefs.get("mode", "")
    if mode not in starting.MODE_LABEL:
        mode = ""
    return Target(project=project, host=server,
                  folder=project.servers.get(server, "~"), mode=mode,
                  include_notes=bool(prefs.get("include_notes", True)))


def _selected_chat(win):
    key = win.selected_key() if hasattr(win, "selected_key") else None
    return win.chat_index.get(chats.chat_id(*key)) if key else None


def _focused_project(win) -> str | None:
    cid = win.visible_chat() if hasattr(win, "visible_chat") else None
    chat = win.chat_index.get(cid) if cid else None
    return chat.project if chat else None


def recent_in(win, target: Target, limit: int = 5) -> list:
    """The project's chats, newest first -- "Recent on <project>"."""
    found = [c for c in win.chats if c.project == target.project.id]
    found.sort(key=lambda c: -c.last_event)
    return found[:limit]


def continuable(win, target: Target):
    """The most recent chat on this server in this folder, which is what
    `claude --continue` there picks up -- or None."""
    for chat in recent_in(win, target, limit=50):
        if chat.host == target.host and not chat.offline:
            # Remembered, not live: its server is not answering, so there
            # is nothing to continue there now.
            return chat
    return None


def _label(text: str, *classes: str, xalign: float = 0,
           wrap: bool = False) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=xalign)
    for name in classes:
        label.add_css_class(name)
    if wrap:
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    return label


def _clear(box: Gtk.Box) -> None:
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


class Choice(Gtk.MenuButton):
    """A chip in the prompt box: a label and a chevron opening a menu that
    is filled when it opens."""

    def __init__(self, fill, tooltip: str = ""):
        super().__init__()
        self.add_css_class("nc-chip")
        inner = Gtk.Box(spacing=6)
        self.dot = Gtk.Box()
        self.dot.set_size_request(7, 7)
        self.dot.set_valign(Gtk.Align.CENTER)
        self.dot.set_visible(False)
        inner.append(self.dot)
        self.main = _label("", "nc-chip-main")
        self.main.set_ellipsize(Pango.EllipsizeMode.END)
        inner.append(self.main)
        self.extra = _label("", "nc-chip-extra")
        self.extra.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.extra.set_max_width_chars(22)
        self.extra.set_visible(False)
        inner.append(self.extra)
        chevron = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        chevron.set_pixel_size(12)
        inner.append(chevron)
        self.set_child(inner)
        if tooltip:
            self.set_tooltip_text(tooltip)
        popover = Gtk.Popover()
        popover.add_css_class("conn-menu")
        popover.set_has_arrow(False)
        popover.set_halign(Gtk.Align.START)
        self.set_popover(popover)
        self.set_create_popup_func(lambda b: fill(b.get_popover()))

    def show_text(self, main: str, extra: str = "", tone: str = "",
                  mono: bool = False) -> None:
        self.main.set_text(main)
        if mono:
            self.main.add_css_class("mono")
        else:
            self.main.remove_css_class("mono")
        self.extra.set_text(extra)
        self.extra.set_visible(bool(extra))
        for cls in ("green", "red", "muted"):
            self.dot.remove_css_class(cls)
        if tone:
            self.dot.add_css_class("dot")
            self.dot.add_css_class(tone)
        self.dot.set_visible(bool(tone))


class Clamp(Gtk.Widget):
    """One child, as wide as it likes up to `limit`, centred in the rest.
    It asks for no more than the child's own minimum, so it never makes
    the window wider than the child needs."""

    def __init__(self, child: Gtk.Widget, limit: int):
        super().__init__()
        self.limit = limit
        self.child = child
        child.set_parent(self)

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            low, nat, _b, _c = self.child.measure(orientation, for_size)
            return low, max(low, min(nat, self.limit)), -1, -1
        width = for_size if for_size < 0 else min(for_size, self.limit)
        low, nat, _b, _c = self.child.measure(orientation, width)
        return low, nat, -1, -1

    def do_size_allocate(self, width, height, baseline):
        low = self.child.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        inner = max(low, min(width, self.limit))
        box = Gdk.Rectangle()
        box.x, box.y = max(0, (width - inner) // 2), 0
        box.width, box.height = inner, height
        self.child.size_allocate(box, baseline)

    def do_dispose(self):
        if self.child is not None:
            self.child.unparent()
            self.child = None


class NewChatPage(Gtk.Box):
    """The New chat screen. One per window, kept in the stack as
    "new-chat"; open() points it at a project and gives it the focus."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.target: Target | None = None
        self.busy = False
        self.add_css_class("newchat")

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        self.append(scroller)

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        column.set_margin_top(96)
        column.set_margin_bottom(48)
        column.set_margin_start(32)
        column.set_margin_end(32)
        # 760 wide where there is room, narrower where there is not: a hard
        # 760 made the whole window at least that wide, and a tiled window
        # narrower than that had its terminals cut off on the right.
        scroller.set_child(Clamp(column, 760 + 64))

        self.crumb = _label("", "nc-crumb")
        column.append(self.crumb)
        self.heading = _label("", "nc-heading", wrap=True)
        self.heading.set_margin_top(6)
        self.heading.set_margin_bottom(20)
        column.append(self.heading)

        # The prompt box: text, then the chips and Start along its foot.
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("nc-card")
        self.text = Gtk.TextView()
        self.text.add_css_class("nc-text")
        self.text.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.text.set_top_margin(4)
        self.text.set_left_margin(6)
        self.text.set_right_margin(6)
        self.text.set_size_request(-1, 110)
        self.text.set_accepts_tab(False)
        self.text.get_buffer().connect("changed", lambda _b: self.refresh())
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._text_key)
        self.text.add_controller(keys)
        self.placeholder = _label("Describe the job. Claude starts on it "
                                  "straight away.", "nc-placeholder")
        self.placeholder.set_can_target(False)
        self.placeholder.set_valign(Gtk.Align.START)
        self.placeholder.set_margin_top(4)
        self.placeholder.set_margin_start(6)
        over = Gtk.Overlay()
        over.set_child(self.text)
        over.add_overlay(self.placeholder)
        card.append(over)

        foot = Gtk.Box(spacing=6)
        self.where = Choice(self._fill_where,
                            "Project, server and the folder Claude starts in")
        self.agent = Choice(self._fill_agent, "Which agent runs the chat")
        self.mode = Choice(self._fill_mode,
                           "Claude's permission mode (--permission-mode)")
        foot.append(self.where)
        foot.append(self.agent)
        foot.append(self.mode)
        self.notes_toggle = Gtk.ToggleButton()
        self.notes_toggle.add_css_class("nc-chip")
        self.notes_toggle.add_css_class("nc-toggle")
        self.notes_toggle.set_tooltip_text(
            "Add this project's open notes to the task, for context")
        self.notes_label = _label("Project notes", "nc-chip-main")
        self.notes_toggle.set_child(self.notes_label)
        self.notes_toggle.connect("toggled", self._notes_toggled)
        foot.append(self.notes_toggle)
        spring = Gtk.Box()
        spring.set_hexpand(True)
        foot.append(spring)
        self.start = Gtk.Button()
        self.start.add_css_class("primary")
        self.start.add_css_class("nc-start")
        start_inner = Gtk.Box(spacing=8)
        self.start_label = _label("Start")
        start_inner.append(self.start_label)
        start_inner.append(_label("ctrl-↵", "kbd"))
        self.start.set_child(start_inner)
        self.start.connect("clicked", lambda _b: self.submit())
        foot.append(self.start)
        card.append(foot)
        column.append(card)

        # Fresh or continue, and where it starts.
        how = Gtk.Box(spacing=12)
        how.set_margin_top(14)
        seg = Gtk.Box(spacing=0)
        seg.add_css_class("nc-seg")
        self.fresh = Gtk.ToggleButton(label="Fresh chat")
        self.fresh.add_css_class("nc-seg-btn")
        self.fresh.set_active(True)
        self.cont = Gtk.ToggleButton(label="Continue")
        self.cont.add_css_class("nc-seg-btn")
        self.cont.set_group(self.fresh)
        self.cont.get_child().set_ellipsize(Pango.EllipsizeMode.END)
        self.cont.get_child().set_max_width_chars(32)
        self.fresh.connect("toggled", lambda _b: self._resume_changed())
        seg.append(self.fresh)
        seg.append(self.cont)
        how.append(seg)
        self.starts_in = _label("", "nc-starts")
        self.starts_in.set_ellipsize(Pango.EllipsizeMode.END)
        self.starts_in.set_hexpand(True)
        how.append(self.starts_in)
        column.append(how)

        self.status = _label("", "nc-status", wrap=True)
        self.status.set_margin_top(10)
        self.status.set_visible(False)
        column.append(self.status)

        self.recent_head = _label("", "section")
        self.recent_head.set_margin_top(34)
        self.recent_head.set_margin_bottom(8)
        column.append(self.recent_head)
        self.recent = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        column.append(self.recent)

        self.command = Gtk.Expander(label="Command Conn will run")
        self.command.add_css_class("nc-command")
        self.command.set_margin_top(28)
        self.command_text = _label("", "mono", "nc-command-text")
        self.command_text.set_selectable(True)
        self.command_text.set_margin_top(8)
        self.command.set_child(self.command_text)
        column.append(self.command)

    # -- pointing it somewhere -------------------------------------------

    def open(self, project_id: str | None = None,
             host: str | None = None) -> bool:
        target = default_target(self.win, project_id, host)
        if target is None:
            self.win.notice("No servers yet -- add one first.")
            return False
        self.target = target
        self.fresh.set_active(True)
        self.status.set_visible(False)
        self.refresh()
        self.text.grab_focus()
        return True

    def text_value(self) -> str:
        buf = self.text.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def refresh(self) -> None:
        t = self.target
        if t is None:
            return
        text = self.text_value()
        self.placeholder.set_visible(not text)
        self.crumb.set_text(f"New chat · {t.project.name}")
        self.heading.set_text(f"What's the job on {t.host}?"
                              if len(t.project.servers) > 1
                              else f"What's the job on {t.project.name}?")
        tone = self.win.host_tone(t.host) if hasattr(self.win, "host_tone") \
            else ""
        self.where.show_text(t.host, t.folder, tone=tone, mono=True)
        self.agent.show_text(starting.AGENT_LABEL.get(
            t.project.agent or "claude", "Claude Code"))
        self.mode.show_text(starting.MODE_LABEL.get(t.mode, "Ask first"))
        notes_on = self.win.notes_ready()
        self.notes_toggle.set_visible(notes_on)
        self.notes_toggle.set_active(t.include_notes and notes_on)
        count = self.win.open_note_count(t.project.id) if notes_on else 0
        self.notes_label.set_text(f"Project notes {count}" if count
                                  else "Project notes")

        prior = continuable(self.win, t)
        self.cont.set_visible(prior is not None)
        if prior is not None:
            self.cont.set_label(f"Continue “{prior.title}”")
            self.cont.get_child().set_ellipsize(Pango.EllipsizeMode.END)
            self.cont.get_child().set_max_width_chars(30)
        elif self.cont.get_active():
            self.fresh.set_active(True)
        t.resume = self.cont.get_active() and prior is not None
        self.starts_in.set_text(
            f"Continues the last conversation in {t.folder}" if t.resume
            else f"Starts in {t.folder} and reads its CLAUDE.md")

        slug = starting.task_slug(text) if text.strip() else "<name>"
        self.command_text.set_text(starting.preview_command(
            slug, t.folder, t.mode, t.resume))

        self.start.set_sensitive(bool(text.strip()) and not self.busy)
        self.start_label.set_text("Starting…" if self.busy else "Start")

        _clear(self.recent)
        recent = recent_in(self.win, t)
        self.recent_head.set_text(f"RECENT ON {t.project.name.upper()}")
        self.recent_head.set_visible(bool(recent))
        now = GLib.get_real_time() / 1e6
        for chat in recent:
            self.recent.append(self._recent_row(chat, now))

    def _recent_row(self, chat, now: float) -> Gtk.Widget:
        button = Gtk.Button()
        button.set_has_frame(False)
        button.add_css_class("nc-recent")
        box = Gtk.Box(spacing=12)
        dot = Gtk.Box()
        dot.set_size_request(10, 10)
        dot.set_valign(Gtk.Align.CENTER)
        dot.add_css_class("dot")
        tone = ("amber" if chats.is_needs_you(chat) else
                "blue" if chat.state == agent_state.WORKING else "muted")
        dot.add_css_class(tone)
        box.append(dot)
        words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        words.set_hexpand(True)
        title = _label(chat.title, "chat-title")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        words.append(title)
        line = f"{chat.host} · {chat.preview}" if chat.preview else chat.host
        sub = _label(line, "chat-preview")
        sub.set_ellipsize(Pango.EllipsizeMode.END)
        words.append(sub)
        box.append(words)
        when = ("needs you" if tone == "amber" else
                "working" if tone == "blue" else
                chats.relative_time(chat.last_event, now))
        stamp = _label(when, "chat-time", xalign=1)
        if tone in ("amber", "blue"):
            stamp.add_css_class(tone)
        box.append(stamp)
        button.set_child(box)
        button.connect("clicked", lambda _b: self.win.open_session(
            chat.host, chat.session))
        return button

    # -- the chips ---------------------------------------------------------

    def _fill_where(self, popover: Gtk.Popover) -> None:
        box = self.win._menu_box(popover, "Start on")
        box.set_size_request(380, -1)
        # Every project's servers make a list taller than the window; scroll
        # it instead of letting it run off the bottom of the screen.
        popover.set_child(None)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(True)
        scroller.set_max_content_height(
            max(240, (self.win.get_height() or 800) - 260))
        scroller.set_child(box)
        popover.set_child(scroller)
        for project in self.win.projects:
            if not project.servers:
                continue
            head = Gtk.Box(spacing=8, margin_top=6, margin_start=6)
            head.append(self.win._tile(project, small=True))
            head.append(_label(project.name, "nc-menu-project"))
            box.append(head)
            for host, folder in project.servers.items():
                dot = Gtk.Box()
                dot.set_size_request(8, 8)
                dot.set_valign(Gtk.Align.CENTER)
                dot.add_css_class("dot")
                dot.add_css_class(self.win.host_tone(host))
                picked = (self.target is not None
                          and self.target.project.id == project.id
                          and self.target.host == host)
                box.append(self.win._menu_row(
                    popover, dot, host, folder, picked,
                    lambda p=project, h=host: self.pick(p, h), mono=True,
                    dark=self.win.host_dark(host)))

    def pick(self, project: chats.Project, host: str) -> None:
        mode = self.target.mode if self.target else ""
        if project.mode in starting.MODE_LABEL and project.mode:
            mode = project.mode
        notes = self.target.include_notes if self.target else True
        self.target = Target(project=project, host=host,
                             folder=project.servers.get(host, "~"),
                             mode=mode, include_notes=notes)
        self.fresh.set_active(True)
        self.refresh()
        self.text.grab_focus()

    def _fill_agent(self, popover: Gtk.Popover) -> None:
        box = self.win._menu_box(popover, "Agent")
        for key, label in starting.AGENTS:
            box.append(self.win._menu_row(popover, Gtk.Box(), label, "",
                                          True, lambda: None))

    def _fill_mode(self, popover: Gtk.Popover) -> None:
        box = self.win._menu_box(popover, "Permission mode")
        box.set_size_request(300, -1)
        notes = {"": "asks before edits and commands",
                 "auto": "Claude decides what is safe",
                 "acceptEdits": "edits without asking",
                 "plan": "plans, changes nothing",
                 "bypassPermissions": "never asks"}
        for key, label in starting.MODES:
            box.append(self.win._menu_row(
                popover, Gtk.Box(), label, notes.get(key, ""),
                self.target is not None and self.target.mode == key,
                lambda k=key: self._set_mode(k)))

    def _set_mode(self, mode: str) -> None:
        if self.target is None:
            return
        self.target.mode = mode
        save_prefs({"mode": mode})
        self.refresh()

    def _notes_toggled(self, button) -> None:
        if self.target is None:
            return
        on = button.get_active()
        if on != self.target.include_notes and button.get_visible():
            self.target.include_notes = on
            save_prefs({"include_notes": on})

    def _resume_changed(self) -> None:
        self.refresh()

    # -- starting ----------------------------------------------------------

    def _text_key(self, _controller, keyval, _code, state) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.submit()
            return True
        if keyval == Gdk.KEY_Escape:
            self.win.leave_new_chat()
            return True
        return False

    def submit(self) -> None:
        text = self.text_value().strip()
        t = self.target
        if not text or t is None or self.busy:
            return
        self.busy = True
        self.status.set_visible(False)
        self.refresh()
        save_prefs({"project": t.project.id})

        def done(started, error):
            self.busy = False
            if error:
                self.status.set_text(error)
                self.status.set_visible(True)
                self.refresh()
                return
            self.text.get_buffer().set_text("")
            self.refresh()

        self.win.start_chat(t, text, source="desktop", open_after=True,
                            done=done)


# --------------------------------------------------------------------------
# Quick add: `conn --quick-add`, bound to Super+N.
# --------------------------------------------------------------------------

QUICK_TITLE = "conn quick add"


class QuickAdd(Gtk.Window):
    """One line, borderless, over whatever you were doing.

    Tab and shift-Tab walk the projects; ctrl-Tab switches Chat / Note.
    Enter starts in the background (or saves the note), ctrl-Enter starts
    and brings Conn up on it, Esc closes. The window is titled QUICK_TITLE
    so the compositor can float, centre and pin it (see the README).
    """

    def __init__(self, win):
        super().__init__(title=QUICK_TITLE, application=win.get_application())
        self.win = win
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(700, -1)
        self.add_css_class("quick-window")
        self.mode_note = False
        self.busy = False

        base = default_target(win)
        self.projects = [p for p in win.projects if p.servers]
        self.index = 0
        if base is not None:
            ids = [p.id for p in self.projects]
            self.index = ids.index(base.project.id) if base.project.id in ids \
                else 0
        self.target = base

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.add_css_class("quick")
        line = Gtk.Box(spacing=12)
        line.add_css_class("quick-line")
        self.token = Gtk.Button()
        self.token.add_css_class("quick-token")
        token_inner = Gtk.Box(spacing=7)
        self.token_dot = Gtk.Box()
        self.token_dot.set_size_request(7, 7)
        self.token_dot.set_valign(Gtk.Align.CENTER)
        self.token_dot.add_css_class("dot")
        token_inner.append(self.token_dot)
        self.token_label = _label("", "mono", "quick-token-label")
        token_inner.append(self.token_label)
        self.token.set_child(token_inner)
        self.token.set_tooltip_text("Tab changes the project")
        self.token.set_can_focus(False)
        self.token.connect("clicked", lambda _b: self.step(1))
        line.append(self.token)
        self.entry = Gtk.Entry()
        self.entry.add_css_class("quick-entry")
        self.entry.set_hexpand(True)
        self.entry.connect("activate", lambda _e: self.submit(False))
        self.entry.connect("changed", lambda _e: self.status.set_visible(False))
        line.append(self.entry)
        self.mode_chip = _label("", "quick-mode")
        line.append(self.mode_chip)
        outer.append(line)

        self.meta = _label("", "quick-meta")
        self.meta.add_css_class("quick-row")
        self.meta.set_ellipsize(Pango.EllipsizeMode.END)
        outer.append(self.meta)
        self.status = _label("", "quick-status", wrap=True)
        self.status.add_css_class("quick-row")
        self.status.set_visible(False)
        outer.append(self.status)
        self.hints = _label("", "quick-hints", "mono")
        self.hints.add_css_class("quick-row")
        outer.append(self.hints)
        self.set_child(outer)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        # Clicking anywhere else is the same as Esc: a launcher that stays
        # up behind your next window is one you have to go and find.
        self.connect("notify::is-active", self._active_changed)
        self.was_active = False
        self.refresh()

    def _active_changed(self, *_a) -> None:
        if self.is_active():
            self.was_active = True
        elif self.was_active and not self.busy:
            self.close()

    def refresh(self) -> None:
        t = self.target
        if t is None:
            self.token_label.set_text("no servers")
            self.meta.set_text("Add a server in Conn first.")
            self.entry.set_sensitive(False)
            return
        self.token_label.set_text(t.project.name if not self.mode_note
                                  else t.project.name)
        for cls in ("green", "red", "muted"):
            self.token_dot.remove_css_class(cls)
        self.token_dot.add_css_class(self.win.host_tone(t.host))
        notes_ok = self.win.notes_ready(optimistic=True)
        if self.mode_note and not notes_ok:
            self.mode_note = False
        self.mode_chip.set_text("Note" if self.mode_note else "Chat")
        self.mode_chip.set_visible(notes_ok)
        if self.mode_note:
            self.entry.set_placeholder_text(f"Jot a note for {t.project.name}")
            self.meta.set_text(f"Saves to {t.project.name}'s notes")
            self.hints.set_text("⇥ project   ctrl-⇥ chat   ↵ save   esc cancel")
        else:
            self.entry.set_placeholder_text(f"What's the job on {t.host}?")
            bits = [f"New chat on {t.host}  {t.folder}",
                    starting.AGENT_LABEL.get(t.project.agent or "claude",
                                             "Claude Code"),
                    starting.MODE_LABEL.get(t.mode, "Ask first")]
            if t.include_notes and self.win.open_note_count(t.project.id):
                bits.append("with project notes")
            else:
                bits.append("reads CLAUDE.md")
            self.meta.set_text("  ·  ".join(bits))
            self.hints.set_text("⇥ project" + ("   ctrl-⇥ note" if notes_ok
                                               else "")
                                + "   ↵ start in the background"
                                "   ctrl-↵ start and open   esc cancel")

    def step(self, delta: int) -> None:
        if not self.projects:
            return
        self.index = (self.index + delta) % len(self.projects)
        project = self.projects[self.index]
        mode = project.mode if project.mode in starting.MODE_LABEL and \
            project.mode else (self.target.mode if self.target else "")
        host = choose_server(project, self.win.rows)
        self.target = Target(project=project, host=host,
                             folder=project.servers.get(host, "~"), mode=mode,
                             include_notes=self.target.include_notes
                             if self.target else True)
        self.refresh()

    def _key(self, _controller, keyval, _code, state) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab, Gdk.KEY_KP_Tab):
            if ctrl:
                if self.win.notes_ready(optimistic=True):
                    self.mode_note = not self.mode_note
                    self.refresh()
                return True
            self.step(-1 if shift or keyval == Gdk.KEY_ISO_Left_Tab else 1)
            return True
        if ctrl and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.submit(True)
            return True
        return False

    def fail(self, text: str) -> None:
        self.busy = False
        self.set_visible(True)
        self.present()
        self.entry.set_sensitive(True)
        self.status.set_text(text)
        self.status.set_visible(True)
        self.entry.grab_focus()

    def submit(self, open_after: bool) -> None:
        text = self.entry.get_text().strip()
        t = self.target
        if not text or t is None or self.busy:
            return
        self.busy = True
        self.entry.set_sensitive(False)
        save_prefs({"project": t.project.id})
        if self.mode_note:
            def saved(note, error):
                if error:
                    self.fail(error)
                    return
                self.close()
            self.win.add_note(t.project.id, text, source="quick-add",
                              done=saved, announce=True)
            return

        if open_after:
            # Up straight away, so the 3 seconds are spent watching it start.
            self.win.present()

        def done(started, error):
            if error:
                if open_after:
                    self.win.notice(f"Could not start: {error}")
                self.fail(error)
                return
            self.close()

        self.win.start_chat(t, text, source="quick-add",
                            open_after=open_after, done=done,
                            announce=not open_after)
        if not open_after:
            self.set_visible(False)
