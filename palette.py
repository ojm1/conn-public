"""The Ctrl+K palette (Palette.dc.html): one box that reaches everything.

Chats, plain shells on every server (watched live or not), notes, servers
and the things Conn can do, ranked against what you type. Enter goes,
Tab peeks at a session's screen without attaching, ctrl-Enter opens the
session in a terminal window of its own, Esc closes.

The matching is pure (rank(), items_for()) so it is testable without a
display; Palette is the widget.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import agent_state
import chats

SECTIONS = ("Chats", "Sessions", "Notes", "Servers", "Actions")
LIMITS = {"Chats": 8, "Sessions": 6, "Notes": 4, "Servers": 5, "Actions": 6}


@dataclass
class Item:
    section: str
    title: str
    sub: str = ""            # second line, mono
    meta: str = ""           # right-hand words
    tone: str = ""           # amber / blue / muted / green / red
    kind: str = ""           # chat, shell, note, host, action
    key: tuple = ()          # (host, session) for chat and shell
    run: Callable | None = None
    hint: str = ""           # key chip on the right
    score: int = 0
    words: str = ""          # what is matched against
    match_in: str = ""       # which line the query is underlined in
    peekable: bool = False
    icon: str = ""           # an action's icon name


def rank(query: str, *fields: str) -> int:
    """0 for no match; higher is better. Every word of the query must be
    found somewhere; a word that starts a field beats one inside it, and
    the first field (the title) beats the rest."""
    q = query.lower().split()
    if not q:
        return 1
    hay = [f.lower() for f in fields if f]
    total = 0
    for word in q:
        best = 0
        for i, text in enumerate(hay):
            weight = 3 if i == 0 else 2 if i == 1 else 1
            at = text.find(word)
            if at < 0:
                continue
            starts = at == 0 or not text[at - 1].isalnum()
            best = max(best, weight * (10 if starts else 4))
        if not best:
            return 0
        total += best
    return total


def tone_of(state: str, finished: bool = False) -> str:
    if agent_state.needs_attention(state) or finished:
        return "amber"
    if state == agent_state.WORKING:
        return "blue"
    return "muted"


STATE_WORDS = {
    agent_state.NEEDS_YOU: "needs you",
    agent_state.DRAFT: "unsent draft",
    agent_state.WORKING: "working",
    agent_state.READY: "idle",
    agent_state.SHELL: "shell",
    agent_state.UNKNOWN: "",
}


def chat_items(chat_list, query: str) -> list[Item]:
    out = []
    for chat in chat_list:
        score = rank(query, chat.title, chat.host, chat.session, chat.project,
                     chat.preview)
        if not score:
            continue
        state = "needs you" if chat.finished else STATE_WORDS.get(chat.state, "")
        out.append(Item("Chats", chat.title, f"{chat.host} / {chat.session}",
                        state, tone_of(chat.state, chat.finished), "chat",
                        (chat.host, chat.session), score=score,
                        peekable=True,
                        # Needs-you and working first, then recency.
                        words=f"{chat.last_event:012d}"))
    out.sort(key=lambda i: (-i.score, i.tone not in ("amber", "blue"),
                            -int(i.words)))
    return out


def shell_items(rows: dict, chat_ids: set[str], query: str) -> list[Item]:
    out = []
    for host, row in rows.items():
        for session in row.get("sessions") or []:
            name = session.get("name", "")
            if chats.chat_id(host, name) in chat_ids:
                continue
            score = rank(query, name, host)
            if not score:
                continue
            out.append(Item("Sessions", name, host, "shell", "muted", "shell",
                            (host, name), score=score, peekable=True))
    out.sort(key=lambda i: -i.score)
    return out


def note_items(notes: list[dict], projects: dict, query: str) -> list[Item]:
    if not query.strip():
        return []
    out = []
    for note in notes:
        text = " ".join((note.get("text") or "").split())
        project = projects.get(note.get("project", ""))
        pname = project.name if project else note.get("project", "")
        score = rank(query, text, pname)
        if not score:
            continue
        out.append(Item("Notes", text[:90], pname,
                        "recap" if note.get("kind") == "recap"
                        else note.get("status", ""), "", "note",
                        (note.get("id", ""),), score=score))
    out.sort(key=lambda i: -i.score)
    return out


def host_items(order: list[str], rows: dict, query: str,
               tone: Callable[[str], str]) -> list[Item]:
    if not query.strip():
        return []
    out = []
    for host in order:
        score = rank(query, host)
        if not score:
            continue
        sessions = len(rows.get(host, {}).get("sessions") or [])
        state = rows.get(host, {}).get("state", "")
        meta = (f"{sessions} session{'s' if sessions != 1 else ''}"
                if state == "up" else state or "")
        out.append(Item("Servers", host, "", meta, tone(host), "host",
                        (host,), score=score))
    out.sort(key=lambda i: -i.score)
    return out


def items_for(query: str, *, chat_list, rows, chat_ids, notes, projects,
              order, tone, actions: list[Item]) -> list[Item]:
    """Everything the palette lists for `query`, in section order, each
    section trimmed to its limit."""
    by_id = {p.id: p for p in projects}
    groups = {
        "Chats": chat_items(chat_list, query),
        "Sessions": shell_items(rows, chat_ids, query),
        "Notes": note_items(notes, by_id, query),
        "Servers": host_items(order, rows, query, tone),
        "Actions": [a for a in actions
                    if rank(query, a.title, a.words) or not query.strip()],
    }
    if not query.strip():
        groups["Sessions"] = groups["Sessions"][:3]
    out = []
    for section in SECTIONS:
        out += groups[section][:LIMITS[section]]
    return out


# --------------------------------------------------------------------------
# The widget
# --------------------------------------------------------------------------

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402


def _markup(text: str, query: str) -> str:
    """text with the query's words underlined, escaped for Pango."""
    words = [w for w in query.lower().split() if w]
    low = text.lower()
    marks = [False] * len(text)
    for word in words:
        at = low.find(word)
        while at >= 0:
            for i in range(at, at + len(word)):
                marks[i] = True
            at = low.find(word, at + len(word))
    out, run, on = [], "", False
    for char, flag in zip(text, marks):
        if flag != on:
            if run:
                out.append(f"<u>{GLib.markup_escape_text(run)}</u>" if on
                           else GLib.markup_escape_text(run))
            run, on = "", flag
        run += char
    if run:
        out.append(f"<u>{GLib.markup_escape_text(run)}</u>" if on
                   else GLib.markup_escape_text(run))
    return "".join(out)


class Palette(Gtk.Box):
    """Laid over the whole window: a dim layer and the box on it."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.items: list[Item] = []
        self.peeking = False
        self.add_css_class("palette-dim")
        self.set_halign(Gtk.Align.FILL)
        self.set_valign(Gtk.Align.FILL)
        self.set_visible(False)

        click = Gtk.GestureClick()
        click.connect("pressed", self._dim_clicked)
        self.add_controller(click)

        self.card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.card.add_css_class("palette")
        self.card.set_halign(Gtk.Align.CENTER)
        self.card.set_valign(Gtk.Align.START)
        self.card.set_hexpand(True)
        self.card.set_margin_top(90)
        self.card.set_size_request(680, -1)
        # A click inside the card is not a click on the dim layer: that is
        # told by where it lands (_dim_clicked), never by claiming the click
        # here -- a claim takes it from the list too, and a row then cannot
        # be clicked at all.
        self.append(self.card)

        top = Gtk.Box(spacing=12)
        top.add_css_class("palette-top")
        icon = Gtk.Image.new_from_icon_name("system-search-symbolic")
        icon.add_css_class("palette-icon")
        top.append(icon)
        self.entry = Gtk.Text()
        self.entry.add_css_class("palette-entry")
        self.entry.set_hexpand(True)
        self.entry.set_placeholder_text("Jump to a chat, a server, a note, "
                                        "or something to do")
        self.entry.connect("changed", lambda _e: self.refill())
        self.entry.connect("activate", lambda _e: self.activate(False))
        top.append(self.entry)
        esc = Gtk.Label(label="esc")
        esc.add_css_class("kbd-chip")
        top.append(esc)
        self.card.append(top)

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER,
                                 Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_max_content_height(430)
        self.scroller.set_propagate_natural_height(True)
        self.list = Gtk.ListBox()
        self.list.add_css_class("palette-list")
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list.connect("row-activated",
                          lambda _l, row: self.activate(False, row))
        self.list.connect("row-selected", lambda *_: self._selected())
        self.scroller.set_child(self.list)
        self.card.append(self.scroller)

        self.peek = Gtk.Label(xalign=0, yalign=0)
        self.peek.add_css_class("palette-peek")
        self.peek.add_css_class("mono")
        self.peek.set_selectable(False)
        self.peek.set_wrap(False)
        self.peek.set_ellipsize(Pango.EllipsizeMode.END)
        self.peek.set_visible(False)
        self.card.append(self.peek)

        self.empty = Gtk.Label(label="Nothing matches.", xalign=0)
        self.empty.add_css_class("palette-empty")
        self.empty.set_visible(False)
        self.card.append(self.empty)

        foot = Gtk.Label(xalign=0)
        foot.add_css_class("palette-foot")
        foot.add_css_class("mono")
        foot.set_text("↑↓ move   ↵ open   ⇥ peek without attaching   "
                      "ctrl-↵ new window")
        self.card.append(foot)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)

    # -- open and close ------------------------------------------------------

    def open(self, query: str = "") -> bool:
        self.peeking = False
        self.peek.set_visible(False)
        self.set_visible(True)
        self.entry.set_text(query)
        self.refill()
        self.entry.grab_focus()
        self.entry.set_position(-1)
        return True

    def close(self) -> None:
        if not self.get_visible():
            return
        self.set_visible(False)
        self.win.palette_closed()

    def _dim_clicked(self, gesture, _n, x, y) -> None:
        """Close on a click outside the card; inside it, leave the click to
        whatever it landed on."""
        widget = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        while widget is not None:
            if widget is self.card:
                return
            widget = widget.get_parent()
        self.close()

    # -- contents --------------------------------------------------------------

    def refill(self) -> None:
        query = self.entry.get_text()
        self.items = self.win.palette_items(query)
        child = self.list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.list.remove(child)
            child = nxt
        section = None
        first = None
        for item in self.items:
            if item.section != section:
                section = item.section
                head = Gtk.ListBoxRow()
                head.set_selectable(False)
                head.set_activatable(False)
                head.set_can_focus(False)
                label = Gtk.Label(label=section.upper(), xalign=0)
                label.add_css_class("section")
                head.set_child(label)
                head.add_css_class("palette-head")
                self.list.append(head)
            row = self._row(item, query)
            self.list.append(row)
            first = first or row
        self.empty.set_visible(not self.items)
        self.scroller.set_visible(bool(self.items))
        if first is not None:
            self.list.select_row(first)
        self._selected()

    def _row(self, item: Item, query: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.item = item
        row.add_css_class("palette-row")
        box = Gtk.Box(spacing=12)
        lead = Gtk.Box()
        lead.set_size_request(10, 10)
        lead.set_valign(Gtk.Align.CENTER)
        if item.kind in ("chat", "shell", "host"):
            lead.add_css_class("dot")
            lead.add_css_class(item.tone or "muted")
        elif item.kind == "action":
            icon = Gtk.Image.new_from_icon_name(item.icon
                                                or "list-add-symbolic")
            icon.set_pixel_size(14)
            lead = icon
        else:
            icon = Gtk.Image.new_from_icon_name("text-x-generic-symbolic")
            icon.set_pixel_size(14)
            lead = icon
        box.append(lead)
        words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        words.set_hexpand(True)
        title = Gtk.Label(xalign=0)
        title.set_markup(_markup(item.title, query))
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.add_css_class("palette-title")
        words.append(title)
        if item.sub:
            sub = Gtk.Label(xalign=0)
            sub.set_markup(_markup(item.sub, query))
            sub.set_ellipsize(Pango.EllipsizeMode.END)
            sub.add_css_class("palette-sub")
            sub.add_css_class("mono")
            words.append(sub)
        box.append(words)
        if item.meta:
            meta = Gtk.Label(label=item.meta, xalign=1)
            meta.add_css_class("meta")
            if item.tone in ("amber", "blue"):
                meta.add_css_class(f"tone-{item.tone}")
            box.append(meta)
        if item.hint:
            chip = Gtk.Label(label=item.hint)
            chip.add_css_class("kbd-chip")
            box.append(chip)
        row.set_child(box)
        return row

    # -- keys and going --------------------------------------------------------

    def _current(self):
        row = self.list.get_selected_row()
        return getattr(row, "item", None), row

    def _selected(self) -> None:
        if self.peeking:
            self._show_peek()

    def _move(self, step: int) -> None:
        rows = []
        child = self.list.get_first_child()
        while child is not None:
            if getattr(child, "item", None) is not None:
                rows.append(child)
            child = child.get_next_sibling()
        if not rows:
            return
        _item, row = self._current()
        at = rows.index(row) if row in rows else -1
        nxt = rows[max(0, min(len(rows) - 1, at + step))]
        self.list.select_row(nxt)
        nxt.grab_focus()
        self.entry.grab_focus()
        self.entry.set_position(-1)

    def _show_peek(self) -> None:
        item, _row = self._current()
        if item is None or not item.peekable:
            self.peek.set_text("Nothing to peek at here.")
        else:
            screen = self.win.screen_of(*item.key) or "(no screen yet)"
            lines = [l.rstrip() for l in screen.splitlines()]
            while lines and not lines[-1]:
                lines.pop()
            self.peek.set_text("\n".join(lines[-14:]))
        self.peek.set_visible(True)

    def _key(self, _controller, keyval, _code, state) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if keyval == Gdk.KEY_Escape:
            if self.peeking:
                self.peeking = False
                self.peek.set_visible(False)
            else:
                self.close()
            return True
        if keyval in (Gdk.KEY_Down,) or (ctrl and keyval in (Gdk.KEY_n, Gdk.KEY_j)):
            self._move(1)
            return True
        if keyval in (Gdk.KEY_Up,) or (ctrl and keyval in (Gdk.KEY_p, Gdk.KEY_k)):
            self._move(-1)
            return True
        if keyval == Gdk.KEY_Page_Down:
            self._move(5)
            return True
        if keyval == Gdk.KEY_Page_Up:
            self._move(-5)
            return True
        if keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab) and not ctrl:
            self.peeking = not self.peeking
            if self.peeking:
                self._show_peek()
            else:
                self.peek.set_visible(False)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.activate(ctrl)
            return True
        return False

    def activate(self, new_window: bool, row=None) -> None:
        item = getattr(row, "item", None) if row is not None else \
            self._current()[0]
        if item is None:
            return
        self.close()
        self.win.palette_run(item, new_window)
