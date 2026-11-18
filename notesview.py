"""Notes: the sidebar's third tab, laid out like an editor.

  sidebar     NotesTree -- every project as a folder with its notes under
              it, each named by its first line. The sidebar's search box
              filters them; the + on a folder starts a note there.
  main area   NotesBoard -- where the terminal usually is: the open note in
              a full-height editor. Above it the project / title path, its
              state and what you can do with it (Start chat, Mark done, ...);
              below it a status line (saved or not, line and column).

Typing saves by itself once you pause (AUTOSAVE_MS), and on ctrl-s, on
leaving the editor and on switching notes. A new note is a draft until it
has text, then it is created in its project and carries on as that note.

The notes themselves are the window's (Conn.note_list, fetched by
Conn.refresh_notes through notes_client); this draws them and hands every
change back to the window.
"""

from __future__ import annotations

import time

from gi.repository import Gdk, GLib, Gtk, Pango

import agent_state
import notes as notes_store

SOURCE_WORDS = {"quick-add": "quick add", "desktop": "desktop",
                "phone": "phone", "recap": "recap"}

# A pause in typing this long saves. Every save is a git commit on the notes
# host, so not on every keystroke.
AUTOSAVE_MS = 2000


def when_text(iso: str, now: float | None = None) -> str:
    return notes_store._short_when(iso, now)


def meta_text(note: dict, now: float | None = None) -> str:
    bits = [when_text(note.get("created", ""), now)]
    source = SOURCE_WORDS.get(note.get("source", ""), note.get("source", ""))
    if source and note.get("kind") != "recap":
        bits.append(source)
    if note.get("queued"):
        bits.append("not sent yet")
    return " · ".join(b for b in bits if b)


def shown_notes(note_list: list[dict], project: str | None) -> list[dict]:
    """A project's notes (every note for None), newest first; done last."""
    picked = [n for n in note_list
              if project is None or n.get("project") == project]
    return sorted(picked, key=lambda n: (n.get("status") == "done",
                                         -notes_store._stamp(n.get("created",
                                                                   ""))))


def open_counts(note_list: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for note in note_list:
        if note.get("status", "open") == "open":
            pid = note.get("project", "")
            counts[pid] = counts.get(pid, 0) + 1
    return counts


def title_of(note: dict) -> str:
    """A note's name in the tree: its first line with words on it."""
    for line in note.get("text", "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:80]
    return "Untitled"


def note_key(note: dict) -> str:
    """What the tree and the editor call a note: its id, or for one still
    waiting to be sent, its outbox name."""
    return note.get("id") or f"queued:{note.get('queued', '')}"


def matches(note: dict, needle: str) -> bool:
    return not needle or needle in note.get("text", "").lower()


def _clear(box: Gtk.Widget) -> None:
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


def _text_of(view: Gtk.TextView) -> str:
    buf = view.get_buffer()
    return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)


# --------------------------------------------------------------------------
# The sidebar: projects as folders, notes in them
# --------------------------------------------------------------------------

class NotesTree(Gtk.Box):
    def __init__(self, win, board: "NotesBoard"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.board = board
        self.syncing = False
        # Folders you closed. Everything starts open: a tree you have to
        # unfold before you can see a note is a tree with nothing in it.
        self.collapsed: set[str] = set()
        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list.add_css_class("sidebar")
        self.list.add_css_class("notes-tree")
        self.list.connect("row-activated", self._activated)
        click = Gtk.GestureClick()
        click.set_button(Gdk.BUTTON_SECONDARY)
        click.connect("pressed", self._right_click)
        self.list.add_controller(click)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(self.list)
        self.append(scroller)

    def needle(self) -> str:
        return self.win.filter.get_text().strip().lower()

    def folders(self) -> list[tuple[str, object, str]]:
        """(id, project or None, name) for every project, then any project
        id a note is filed under that projects.toml no longer has."""
        rows = [(p.id, p, p.name) for p in self.win.projects]
        known = {p.id for p in self.win.projects}
        for pid in sorted({n.get("project", "") for n in self.win.note_list}
                          - known):
            if pid:
                rows.append((pid, None, pid))
        return rows

    def render(self) -> None:
        self.syncing = True
        try:
            _clear(self.list)
            needle = self.needle()
            counts = open_counts(self.win.note_list)
            shown_any = False
            for pid, project, name in self.folders():
                notes = shown_notes(self.win.note_list, pid)
                hits = [n for n in notes if matches(n, needle)]
                if needle and not hits and needle not in name.lower():
                    continue
                shown_any = True
                drafting = (self.board.current_key is None
                            and self.board.draft_pid == pid)
                is_open = bool(needle) or pid not in self.collapsed \
                    or drafting
                self.list.append(self._folder(pid, project, name,
                                              counts.get(pid, 0), is_open))
                if not is_open:
                    continue
                if drafting:
                    row = self._note_row(None, "New note", "", draft=True)
                    self.list.append(row)
                    self.list.select_row(row)
                for note in (hits if needle else notes):
                    row = self._note_row(note, title_of(note),
                                         when_text(note.get("created", "")))
                    self.list.append(row)
                    if note_key(note) == self.board.current_key:
                        self.list.select_row(row)
            if not shown_any:
                row = Gtk.ListBoxRow()
                row.set_activatable(False)
                row.set_selectable(False)
                words = Gtk.Label(
                    label=f"No notes match “{needle}”" if needle
                    else "No projects yet -- add a server to one first",
                    xalign=0, wrap=True)
                words.add_css_class("note-meta")
                words.set_margin_start(14)
                words.set_margin_top(10)
                row.set_child(words)
                self.list.append(row)
        finally:
            self.syncing = False

    def _folder(self, pid, project, name, count, is_open) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.kind = "folder"
        row.pid = pid
        row.add_css_class("notes-folder")
        box = Gtk.Box(spacing=8)
        box.set_margin_start(8)
        box.set_margin_end(6)
        box.set_margin_top(5)
        box.set_margin_bottom(5)
        chevron = Gtk.Image.new_from_icon_name(
            "pan-down-symbolic" if is_open else "pan-end-symbolic")
        chevron.set_pixel_size(12)
        chevron.add_css_class("tree-chevron")
        box.append(chevron)
        if project is not None:
            box.append(self.win._tile(project, small=True))
        words = Gtk.Label(label=name, xalign=0)
        words.set_hexpand(True)
        words.set_ellipsize(Pango.EllipsizeMode.END)
        words.add_css_class("notes-project-name")
        box.append(words)
        num = Gtk.Label(label=str(count) if count else "")
        num.add_css_class("mode-count")
        box.append(num)
        if project is not None:
            plus = Gtk.Button(icon_name="list-add-symbolic")
            plus.set_has_frame(False)
            plus.add_css_class("tree-add")
            plus.set_tooltip_text(f"New note in {name}")
            plus.connect("clicked", lambda _b, p=pid: self.board.new_note(p))
            box.append(plus)
        row.set_child(box)
        return row

    def _note_row(self, note, title, when, draft=False) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.kind = "draft" if draft else "note"
        row.note = note
        row.key = None if note is None else note_key(note)
        row.add_css_class("notes-note")
        if note is not None and note.get("status") == "done":
            row.add_css_class("note-done")
        box = Gtk.Box(spacing=8)
        box.set_margin_start(30)
        box.set_margin_end(12)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        if draft:
            icon = "document-new-symbolic"
        elif note.get("kind") == "recap":
            icon = "media-playlist-repeat-symbolic"
        elif note.get("status") == "done":
            icon = "object-select-symbolic"
        elif note.get("status") == "started":
            icon = "utilities-terminal-symbolic"
        else:
            icon = "text-x-generic-symbolic"
        image = Gtk.Image.new_from_icon_name(icon)
        image.set_pixel_size(13)
        image.add_css_class("tree-icon")
        box.append(image)
        words = Gtk.Label(label=title, xalign=0)
        words.set_hexpand(True)
        words.set_ellipsize(Pango.EllipsizeMode.END)
        words.add_css_class("note-row-title")
        if draft:
            words.add_css_class("note-placeholder")
        box.append(words)
        stamp = Gtk.Label(label=when)
        stamp.add_css_class("mode-count")
        box.append(stamp)
        if note is not None:
            row.set_tooltip_text(textwrap_short(note.get("text", "")))
        row.set_child(box)
        return row

    def _activated(self, _list, row) -> None:
        if self.syncing:
            return
        kind = getattr(row, "kind", None)
        if kind == "folder":
            if row.pid in self.collapsed:
                self.collapsed.discard(row.pid)
            else:
                self.collapsed.add(row.pid)
            self.render()
        elif kind == "note":
            self.board.open_note(row.key)

    def _right_click(self, gesture, _n, x, y) -> None:
        row = self.list.get_row_at_y(int(y))
        kind = getattr(row, "kind", None)
        if kind == "note" and row.note is not None:
            self.board.menu(self.list, row.note, x, y, parent=self)
        elif kind == "folder":
            pid = row.pid
            self.win.popup_at(self.list, x, y, [
                ("New note here", lambda: self.board.new_note(pid), False)],
                parent=self)

    def open_first(self) -> None:
        """Enter in the search box: open the first note it found."""
        index = 0
        while (row := self.list.get_row_at_index(index)) is not None:
            index += 1
            if getattr(row, "kind", None) == "note":
                self.board.open_note(row.key)
                return


def textwrap_short(text: str, width: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width - 1] + "…"


# --------------------------------------------------------------------------
# The main area: the open note
# --------------------------------------------------------------------------

class NotesBoard(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.add_css_class("notes-board")
        # The note in the editor (note_key), or None for a new one being
        # written in draft_pid.
        self.current_key: str | None = None
        self.draft_pid: str | None = None
        # The note as created, until a fetch brings it into note_list.
        self.fresh: dict | None = None
        self.loaded = ""        # the text as last loaded or saved
        self.dirty = False
        self.loading = False
        self.creating = False
        self.save_timer = 0
        self.side = NotesTree(win, self)

        # -- the path and what you can do --------------------------------
        head = Gtk.Box(spacing=10)
        head.add_css_class("note-head")
        self.head_tile = Gtk.Box()
        self.head_tile.set_valign(Gtk.Align.CENTER)
        head.append(self.head_tile)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        titles.set_hexpand(True)
        titles.set_valign(Gtk.Align.CENTER)
        path = Gtk.Box(spacing=6)
        self.crumb = Gtk.Label(xalign=0)
        self.crumb.add_css_class("note-crumb")
        path.append(self.crumb)
        self.where = Gtk.DropDown()
        self.where.add_css_class("note-where")
        self.where.set_tooltip_text("The project this note goes in")
        self.where.connect("notify::selected", self._where_changed)
        self.where_ids: list[str] = []
        path.append(self.where)
        slash = Gtk.Label(label="/")
        slash.add_css_class("note-crumb")
        path.append(slash)
        self.heading = Gtk.Label(xalign=0)
        self.heading.add_css_class("note-title")
        self.heading.set_ellipsize(Pango.EllipsizeMode.END)
        self.heading.set_hexpand(True)
        path.append(self.heading)
        titles.append(path)
        self.subhead = Gtk.Label(xalign=0)
        self.subhead.add_css_class("note-meta")
        self.subhead.set_ellipsize(Pango.EllipsizeMode.END)
        titles.append(self.subhead)
        head.append(titles)

        self.action_slot = Gtk.Box()
        self.action_slot.set_valign(Gtk.Align.CENTER)
        head.append(self.action_slot)
        self.done_button = Gtk.Button()
        self.done_button.add_css_class("note-link")
        self.done_button.set_valign(Gtk.Align.CENTER)
        self.done_button.connect("clicked", lambda _b: self._toggle_done())
        head.append(self.done_button)
        self.more = Gtk.Button(icon_name="view-more-symbolic")
        self.more.add_css_class("action")
        self.more.set_tooltip_text("More")
        self.more.set_valign(Gtk.Align.CENTER)
        self.more.connect("clicked", lambda b: self.current_note() and
                          self.menu(b, self.current_note(),
                                    b.get_width() / 2, b.get_height()))
        head.append(self.more)
        self.append(head)

        self.state = Gtk.Label(xalign=0)
        self.state.add_css_class("notes-state")
        self.state.set_wrap(True)
        self.state.set_visible(False)
        self.append(self.state)

        # -- the editor ----------------------------------------------------
        self.editor = Gtk.TextView()
        self.editor.add_css_class("note-editor")
        self.editor.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.editor.set_monospace(True)
        self.editor.set_left_margin(28)
        self.editor.set_right_margin(28)
        self.editor.set_top_margin(18)
        self.editor.set_bottom_margin(40)
        self.editor.set_pixels_below_lines(3)
        buf = self.editor.get_buffer()
        buf.connect("changed", self._changed)
        buf.connect("notify::cursor-position", lambda *_a: self._paint_status())
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.editor.add_controller(keys)
        leave = Gtk.EventControllerFocus()
        leave.connect("leave", lambda _c: self.flush())
        self.editor.add_controller(leave)
        self.hint = Gtk.Label(xalign=0)
        self.hint.add_css_class("note-placeholder")
        self.hint.set_can_target(False)
        self.hint.set_valign(Gtk.Align.START)
        self.hint.set_halign(Gtk.Align.START)
        self.hint.set_margin_start(30)
        self.hint.set_margin_top(18)
        over = Gtk.Overlay()
        over.set_child(self.editor)
        over.add_overlay(self.hint)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_hexpand(True)
        scroller.set_child(over)
        self.append(scroller)

        # -- the status line -----------------------------------------------
        bar = Gtk.Box(spacing=12)
        bar.add_css_class("note-statusbar")
        self.saved = Gtk.Label(xalign=0)
        self.saved.set_hexpand(True)
        bar.append(self.saved)
        self.position = Gtk.Label(xalign=1)
        bar.append(self.position)
        keys_hint = Gtk.Label(label="ctrl-s saves · ctrl-n new note")
        bar.append(keys_hint)
        self.append(bar)

    # -- which note ----------------------------------------------------------

    def current_note(self) -> dict | None:
        if self.current_key is None:
            return None
        for note in self.win.note_list:
            if note_key(note) == self.current_key:
                if self.fresh is not None and \
                        note_key(self.fresh) == self.current_key:
                    self.fresh = None       # the fetch has it now
                return note
        if self.fresh is not None and note_key(self.fresh) == self.current_key:
            return self.fresh
        return None

    def project_of(self, pid: str | None):
        return next((p for p in self.win.projects if p.id == pid), None)

    def default_pid(self) -> str | None:
        """Where a new note goes: the open note's project, else the
        filter's, else the one New chat would use."""
        note = self.current_note()
        if note is not None and self.project_of(note.get("project")):
            return note.get("project")
        if self.draft_pid and self.project_of(self.draft_pid):
            return self.draft_pid
        if self.project_of(self.win.filters.project):
            return self.win.filters.project
        import newchat
        target = newchat.default_target(self.win)
        if target is not None:
            return target.project.id
        return self.win.projects[0].id if self.win.projects else None

    def focus(self) -> None:
        self.editor.grab_focus()

    def open_note(self, key: str | None) -> None:
        self.flush()
        self.current_key = key
        self.draft_pid = None
        note = self.current_note()
        self._load(note.get("text", "") if note else "")
        self.render()
        buf = self.editor.get_buffer()
        buf.place_cursor(buf.get_end_iter())
        self.editor.grab_focus()

    def new_note(self, pid: str | None = None) -> None:
        """A blank page in a project (the open note's, by default). It
        becomes a note once it has text and you pause."""
        self.flush()
        pid = pid or self.default_pid()
        self.current_key = None
        self.draft_pid = pid
        if pid in self.side.collapsed:
            self.side.collapsed.discard(pid)
        self._load("")
        self.render()
        self.editor.grab_focus()

    # -- drawing -------------------------------------------------------------

    def render(self) -> None:
        if self.current_key is not None and self.current_note() is None:
            # Deleted, here or elsewhere: a blank page in its project.
            self.current_key = None
            self.draft_pid = self.draft_pid or self.default_pid()
            self._load("")
        if self.current_key is None and self.draft_pid is None:
            self.draft_pid = self.default_pid()
        note = self.current_note()
        if note is not None and not self.dirty and \
                note.get("text", "").strip() != self.loaded.strip():
            # Changed somewhere else (the phone) and not here: take it.
            self._load(note.get("text", ""), keep_cursor=True)
        self.side.render()
        self._paint_head(note)
        self._paint_status()
        problem = self.win.notes_problem
        self.state.set_text(problem or "")
        self.state.set_visible(bool(problem))

    def _paint_head(self, note: dict | None) -> None:
        pid = note.get("project") if note else self.draft_pid
        project = self.project_of(pid)
        _clear(self.head_tile)
        if project is not None:
            self.head_tile.append(self.win._tile(project, small=True))
        drafting = note is None
        # A new note can still be moved to another project; a saved one
        # stays where it was filed.
        self._paint_where(pid if drafting else None)
        self.crumb.set_text(project.name if project else (pid or ""))
        self.crumb.set_visible(not drafting or not self.where_ids)
        self.heading.set_text(title_of(note) if note else "New note")
        if note is not None:
            bits = [meta_text(note)]
            status = note.get("status", "open")
            if note.get("kind") == "recap":
                bits.insert(0, "recap")
            if status != "open":
                bits.append(status)
            self.subhead.set_text(" · ".join(b for b in bits if b))
        else:
            self.subhead.set_text("not saved yet -- start typing"
                                  if project else
                                  "add a server to a project to keep notes")
        self.hint.set_text("" if note else
                           (f"Write a note for {project.name}…" if project
                            else "No project to file it under yet"))
        self.hint.set_visible(self.editor.get_buffer().get_char_count() == 0)
        editable = project is not None and not (note or {}).get("queued")
        self.editor.set_editable(editable)
        self.editor.set_cursor_visible(editable)

        _clear(self.action_slot)
        action = self._action(note) if note else None
        if action is not None:
            self.action_slot.append(action)
        live = note is not None and not note.get("queued")
        self.done_button.set_visible(live)
        if live:
            done = note.get("status") == "done"
            self.done_button.set_label("Reopen" if done else "Mark done")
        self.more.set_visible(note is not None)

    def _paint_where(self, pid: str | None) -> None:
        self.where.set_visible(pid is not None and bool(self.win.projects))
        if pid is None:
            return
        ids = [p.id for p in self.win.projects]
        self.loading = True
        try:
            if ids != self.where_ids:
                self.where_ids = ids
                self.where.set_model(Gtk.StringList.new(
                    [p.name for p in self.win.projects]))
            if pid in ids:
                self.where.set_selected(ids.index(pid))
        finally:
            self.loading = False

    def _where_changed(self, *_a) -> None:
        if self.loading or self.current_key is not None:
            return
        index = self.where.get_selected()
        if 0 <= index < len(self.where_ids):
            self.draft_pid = self.where_ids[index]
            self.side.render()
            self._paint_head(None)

    def _paint_status(self) -> None:
        buf = self.editor.get_buffer()
        it = buf.get_iter_at_mark(buf.get_insert())
        words = len(_text_of(self.editor).split())
        self.position.set_text(f"Ln {it.get_line() + 1}, Col "
                               f"{it.get_line_offset() + 1} · {words} words")
        note = self.current_note()
        if note is not None and note.get("queued"):
            words_ = "waiting to send -- read only until it goes"
        elif self.creating:
            words_ = "saving…"
        elif self.dirty:
            words_ = "edited"
        elif note is not None:
            words_ = "saved"
        else:
            words_ = ""
        self.saved.set_text(words_)

    # -- the buffer ----------------------------------------------------------

    def _load(self, text: str, keep_cursor: bool = False) -> None:
        buf = self.editor.get_buffer()
        offset = buf.get_property("cursor-position") if keep_cursor else 0
        self.loading = True
        try:
            buf.set_text(text)
            buf.place_cursor(buf.get_iter_at_offset(min(offset, len(text))))
        finally:
            self.loading = False
        self.loaded = text
        self.dirty = False
        self._cancel_timer()
        self.hint.set_visible(not text)

    def _changed(self, buf) -> None:
        self.hint.set_visible(buf.get_char_count() == 0)
        if self.loading:
            return
        self.dirty = True
        self._cancel_timer()
        self.save_timer = GLib.timeout_add(AUTOSAVE_MS, self._autosave)
        self._paint_status()

    def _cancel_timer(self) -> None:
        if self.save_timer:
            GLib.source_remove(self.save_timer)
            self.save_timer = 0

    def _autosave(self) -> bool:
        self.save_timer = 0
        self.save()
        return GLib.SOURCE_REMOVE

    def flush(self) -> None:
        """Save now whatever is waiting for the pause."""
        if self.dirty:
            self._cancel_timer()
            self.save()

    def save(self) -> None:
        if not self.dirty:
            return
        text = _text_of(self.editor)
        if not text.strip():
            if self.current_key is not None:
                self.saved.set_text("empty -- not saved; delete it from … "
                                    "instead")
            return
        note = self.current_note()
        if note is not None:
            if note.get("queued"):
                return
            self.dirty = False
            if text.strip() != self.loaded.strip():
                self.win.update_note(note, text=text)
            self.loaded = text
            self._paint_status()
            return
        pid = self.draft_pid
        if pid is None or self.creating or self.project_of(pid) is None:
            return
        self.creating = True
        self.dirty = False
        sent = text
        self._paint_status()

        def done(created, error):
            self.creating = False
            if error or created is None:
                self.dirty = True
                self.saved.set_text(f"not saved: {error or 'no answer'}")
                return
            if self.current_key is None and self.draft_pid == pid:
                self.fresh = created
                self.current_key = note_key(created)
                self.draft_pid = None
                self.loaded = sent
                # Typed on while it was being created: that goes next.
                if _text_of(self.editor) != sent:
                    self.dirty = True
                    self._cancel_timer()
                    self.save_timer = GLib.timeout_add(AUTOSAVE_MS,
                                                       self._autosave)
            self.render()
        self.win.add_note(pid, text, source="desktop", done=done)

    def _key(self, _c, keyval, _code, state) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and keyval in (Gdk.KEY_s, Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.flush()
            return True
        if keyval == Gdk.KEY_Escape:
            if self.current_key is None and not _text_of(self.editor):
                self.win.set_sidebar_mode("chats")
            else:
                self.flush()
                self.side.list.grab_focus()
            return True
        return False

    # -- acting on a note ----------------------------------------------------

    def _toggle_done(self) -> None:
        note = self.current_note()
        if note is None:
            return
        self.flush()
        done = note.get("status") == "done"
        self.win.update_note(note, status="open" if done else "done")

    def _action(self, note: dict):
        if note.get("queued"):
            return None
        recap = note.get("kind") == "recap"
        chat = self.win.chat_index.get(note.get("chat", "")) \
            if note.get("chat") else None
        if recap or (note.get("status") == "started" and note.get("chat")):
            button = Gtk.Button()
            button.add_css_class("note-link")
            inner = Gtk.Box(spacing=6)
            if chat is not None and chat.state == agent_state.WORKING:
                spin = Gtk.Spinner()
                spin.start()
                inner.append(spin)
            label = "Open chat" if recap else (chat.title if chat else
                                               note["chat"].split("/")[-1])
            words = Gtk.Label(label=label)
            words.set_ellipsize(Pango.EllipsizeMode.END)
            words.set_max_width_chars(18)
            inner.append(words)
            button.set_child(inner)
            host, _, session = note.get("chat", "").partition("/")
            button.set_sensitive(bool(host and session))
            button.set_tooltip_text(note.get("chat", ""))
            button.connect("clicked", lambda _b: self.win.open_session(
                host, session))
            return button
        if note.get("status", "open") == "open":
            button = Gtk.Button(label="Start chat")
            button.add_css_class("note-start")
            button.set_tooltip_text("Start a chat with this note as the task")
            button.connect("clicked", lambda _b: self._start(note))
            return button
        return None

    def _start(self, note: dict) -> None:
        self.flush()
        fresh = self.current_note() or note
        self.win.start_from_note(fresh)

    def menu(self, widget, note: dict, x: float, y: float,
             parent=None) -> None:
        if note.get("queued"):
            items = [("Copy text", lambda: self.win.to_clipboard(note["text"]),
                      False),
                     (None, None, False),
                     ("Discard (not sent yet)",
                      lambda: self.win.discard_queued(note), True)]
        else:
            open_ = note.get("status", "open") != "done"
            items = [("Open", lambda: self.open_note(note_key(note)), False),
                     ("Mark done" if open_ else "Reopen",
                      lambda: self.win.update_note(
                          note, status="done" if open_ else "open"), False),
                     ("Copy text", lambda: self.win.to_clipboard(note["text"]),
                      False),
                     ("New note here",
                      lambda: self.new_note(note.get("project")), False)]
            if note.get("status", "open") == "open":
                items.insert(1, ("Start chat", lambda: self._start(note),
                                 False))
            items += [(None, None, False),
                      ("Delete…", lambda: self.win.confirm_delete_note(note),
                       True)]
        self.win.popup_at(widget, x, y, items, parent=parent)
