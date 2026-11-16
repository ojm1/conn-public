"""Notes: the sidebar's second mode (desktop phase 5, iOSNotes).

Notes belong to projects, so the Notes mode is laid out that way:

  sidebar     NotesProjects -- All notes, then every project with its count
              of open notes. Picking one is the same Project filter the chat
              list uses, so switching back to Chats keeps it.
  main area   NotesBoard -- where the terminal usually is: the project's
              name, a writing box with room to write in (Enter is a new
              line, ctrl-enter saves), and the notes as cards. Click a
              note's text, or its pencil, to edit it in place.

An open note has Start chat; a started one links to its chat; a recap is
tagged and links back to the chat it came from. Right-click a card, or its
... button, to edit, finish, copy or delete it.

The notes themselves are the window's (Conn.note_list, fetched by
Conn.refresh_notes through notes_client); this only draws them and hands
every change back to the window.
"""

from __future__ import annotations

import time

from gi.repository import Gdk, Gtk, Pango

import agent_state
import notes as notes_store

SOURCE_WORDS = {"quick-add": "quick add", "desktop": "desktop",
                "phone": "phone", "recap": "recap"}


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
    """The cards for a project filter, newest first; done notes last."""
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


def _clear(box: Gtk.Widget) -> None:
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


def _text_of(view: Gtk.TextView) -> str:
    buf = view.get_buffer()
    return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)


def _placeheld(view: Gtk.TextView, hint: str) -> tuple[Gtk.Overlay, Gtk.Label]:
    """A TextView with a placeholder laid over it while it is empty."""
    label = Gtk.Label(label=hint, xalign=0)
    label.add_css_class("note-placeholder")
    label.set_can_target(False)
    label.set_valign(Gtk.Align.START)
    label.set_halign(Gtk.Align.START)
    over = Gtk.Overlay()
    over.set_child(view)
    over.add_overlay(label)
    view.get_buffer().connect("changed", lambda b: label.set_visible(
        b.get_char_count() == 0))
    return over, label


# --------------------------------------------------------------------------
# The sidebar: projects
# --------------------------------------------------------------------------

class NotesProjects(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.syncing = False
        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list.add_css_class("sidebar")
        self.list.add_css_class("notes-projects")
        self.list.connect("row-selected", self._picked)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(self.list)
        self.append(scroller)

    def render(self) -> None:
        self.syncing = True
        try:
            _clear(self.list)
            counts = open_counts(self.win.note_list)
            rows = [(None, None, "All notes", sum(counts.values()))]
            rows += [(p.id, p, p.name, counts.get(p.id, 0))
                     for p in self.win.projects]
            # Notes filed under a project that is no longer in the list
            # still have to be reachable.
            known = {p.id for p in self.win.projects}
            for pid in sorted(set(counts) - known):
                rows.append((pid, None, pid, counts[pid]))
            for pid, project, name, count in rows:
                row = Gtk.ListBoxRow()
                row.pid = pid
                row.add_css_class("notes-project")
                box = Gtk.Box(spacing=10)
                box.set_margin_start(14)
                box.set_margin_end(14)
                box.set_margin_top(7)
                box.set_margin_bottom(7)
                if pid is None:
                    icon = Gtk.Image.new_from_icon_name("view-list-symbolic")
                    icon.set_pixel_size(14)
                    icon.set_size_request(22, 22)
                    box.append(icon)
                else:
                    box.append(self.win._tile(project, small=True))
                words = Gtk.Label(label=name, xalign=0)
                words.set_hexpand(True)
                words.set_ellipsize(Pango.EllipsizeMode.END)
                words.add_css_class("notes-project-name")
                box.append(words)
                num = Gtk.Label(label=str(count) if count else "")
                num.add_css_class("mode-count")
                box.append(num)
                row.set_child(box)
                self.list.append(row)
                if pid == self.win.filters.project:
                    self.list.select_row(row)
        finally:
            self.syncing = False

    def _picked(self, _list, row) -> None:
        if self.syncing or row is None:
            return
        if row.pid != self.win.filters.project:
            import chats
            self.win.set_filters(chats.pick_project(self.win.filters, row.pid))
        self.win.notes_pane.jot.grab_focus()


# --------------------------------------------------------------------------
# The main area: one project's notes
# --------------------------------------------------------------------------

class NotesBoard(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.add_css_class("notes-board")
        self.editing: str | None = None     # the id of the card being edited
        self.drafts: dict[str, str] = {}    # its unsaved text, across renders
        self.side = NotesProjects(win)

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        column.set_margin_start(28)
        column.set_margin_end(28)
        column.set_margin_top(22)
        column.set_margin_bottom(22)

        head = Gtk.Box(spacing=12)
        self.head_tile = Gtk.Box()
        head.append(self.head_tile)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        titles.set_hexpand(True)
        self.heading = Gtk.Label(xalign=0)
        self.heading.add_css_class("notes-heading")
        self.heading.set_ellipsize(Pango.EllipsizeMode.END)
        titles.append(self.heading)
        self.subhead = Gtk.Label(xalign=0)
        self.subhead.add_css_class("note-meta")
        titles.append(self.subhead)
        head.append(titles)
        column.append(head)

        # The writing box: a notepad, not a one-line jot.
        composer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        composer.add_css_class("note-composer")
        self.jot = Gtk.TextView()
        self.jot.add_css_class("note-write")
        self.jot.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.jot.set_accepts_tab(False)
        self.jot.set_size_request(-1, 84)
        over, self.jot_hint = _placeheld(self.jot, "")
        composer.append(over)
        foot = Gtk.Box(spacing=8)
        self.where = Gtk.DropDown()
        self.where.add_css_class("note-where")
        self.where.set_tooltip_text("The project this note is filed under")
        self.where.connect("notify::selected", lambda *_a: self._paint_hint())
        foot.append(self.where)
        hint = Gtk.Label(label="ctrl-enter saves", xalign=0)
        hint.add_css_class("note-meta")
        hint.set_hexpand(True)
        foot.append(hint)
        self.save_button = Gtk.Button(label="Save note")
        self.save_button.add_css_class("note-start")
        self.save_button.connect("clicked", lambda _b: self.save_jot())
        foot.append(self.save_button)
        composer.append(foot)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._jot_key)
        self.jot.add_controller(keys)
        self.jot.get_buffer().connect("changed",
                                      lambda _b: self._paint_save())
        column.append(composer)

        self.state = Gtk.Label(xalign=0)
        self.state.add_css_class("notes-state")
        self.state.set_wrap(True)
        self.state.set_visible(False)
        column.append(self.state)

        self.cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.cards.set_valign(Gtk.Align.START)
        self.cards.set_vexpand(False)
        column.append(self.cards)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_hexpand(True)
        column.set_valign(Gtk.Align.START)
        scroller.set_child(column)
        self.append(scroller)
        self.where_ids: list[str] = []

    # -- what goes where -----------------------------------------------------

    def jot_project(self):
        """The project the writing box saves to: the one chosen under it,
        else the filter's, else the one New chat would use."""
        index = self.where.get_selected()
        if 0 <= index < len(self.where_ids):
            pid = self.where_ids[index]
            found = next((p for p in self.win.projects if p.id == pid), None)
            if found is not None:
                return found
        pid = self.win.filters.project
        found = next((p for p in self.win.projects if p.id == pid), None)
        if found is not None:
            return found
        import newchat
        target = newchat.default_target(self.win)
        return target.project if target else None

    def _paint_where(self) -> None:
        projects = list(self.win.projects)
        ids = [p.id for p in projects]
        if ids != self.where_ids:
            self.where_ids = ids
            self.where.set_model(Gtk.StringList.new([p.name for p in projects]))
        pid = self.win.filters.project
        if pid in ids:
            self.where.set_selected(ids.index(pid))
            # Filtered to a project, the heading already says where.
            self.where.set_visible(False)
        else:
            if self.where.get_selected() >= len(ids) or \
                    self.where.get_selected() == Gtk.INVALID_LIST_POSITION:
                fallback = self.jot_project()
                if fallback is not None and fallback.id in ids:
                    self.where.set_selected(ids.index(fallback.id))
            self.where.set_visible(bool(ids))

    def _paint_hint(self) -> None:
        target = self.jot_project()
        self.jot_hint.set_text(
            f"Write a note for {target.name}…" if target
            else "Add a server to a project to keep notes")
        self.jot.set_sensitive(target is not None)
        self._paint_save()

    def _paint_save(self) -> None:
        self.save_button.set_sensitive(bool(_text_of(self.jot).strip())
                                       and self.jot_project() is not None)

    def render(self) -> None:
        self.side.render()
        self._paint_where()
        pid = self.win.filters.project
        project = next((p for p in self.win.projects if p.id == pid), None)
        _clear(self.head_tile)
        if project is not None:
            self.head_tile.append(self.win._tile(project))
        self.heading.set_text(project.name if project else
                              (pid or "All notes"))
        shown = shown_notes(self.win.note_list, pid)
        open_ = sum(1 for n in shown if n.get("status", "open") == "open")
        bits = [f"{open_} open" if open_ else "nothing open"]
        done = sum(1 for n in shown if n.get("status") == "done")
        if done:
            bits.append(f"{done} done")
        if project is None and pid is None:
            bits.append(f"across {len({n.get('project') for n in shown})} "
                        "projects" if shown else "")
        self.subhead.set_text(" · ".join(b for b in bits if b))
        self._paint_hint()

        _clear(self.cards)
        now = time.time()
        live = {n.get("id") for n in shown}
        if self.editing not in live:
            self.editing = None
        for note in shown:
            self.cards.append(self._card(note, now))
        problem = self.win.notes_problem
        if not shown:
            where = f" on {project.name}" if project else ""
            text = (f"No notes{where} yet. Write one above, or press "
                    "Super+N then ctrl-tab from anywhere.")
            self.state.set_text(problem or text)
            self.state.set_visible(True)
        else:
            self.state.set_text(problem or "")
            self.state.set_visible(bool(problem))

    # -- a card --------------------------------------------------------------

    def _card(self, note: dict, now: float) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("note-card")
        if note.get("status") == "done":
            card.add_css_class("note-done")
        recap = note.get("kind") == "recap"
        chat = self.win.chat_index.get(note.get("chat", "")) \
            if note.get("chat") else None
        if recap:
            tag_row = Gtk.Box(spacing=8)
            tag = Gtk.Label(label="RECAP")
            tag.add_css_class("note-tag")
            tag_row.append(tag)
            origin = Gtk.Label(xalign=0)
            origin.add_css_class("note-meta")
            origin.set_ellipsize(Pango.EllipsizeMode.END)
            origin.set_text(f"from {chat.title}" if chat
                            else f"from {note.get('chat', '').split('/')[-1]}")
            tag_row.append(origin)
            card.append(tag_row)
        if not self.win.filters.project:
            project = next((p for p in self.win.projects
                            if p.id == note.get("project")), None)
            head = Gtk.Box(spacing=6)
            if project is not None:
                head.append(self.win._tile(project, small=True))
            pname = Gtk.Label(label=project.name if project
                              else note.get("project", ""), xalign=0)
            pname.add_css_class("note-meta")
            head.append(pname)
            card.append(head)

        card.set_vexpand(False)
        card.set_valign(Gtk.Align.START)
        if self.editing == note.get("id") and not note.get("queued"):
            card.add_css_class("note-editing")
            card.append(self._editor(note))
            return card

        text = Gtk.Label(label=note.get("text", ""), xalign=0)
        text.add_css_class("note-text")
        text.set_wrap(True)
        text.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        text.set_lines(14)
        text.set_ellipsize(Pango.EllipsizeMode.END)
        text.set_max_width_chars(48)
        text.set_selectable(False)
        if not note.get("queued"):
            text.set_tooltip_text("Click to edit")
            click = Gtk.GestureClick()
            click.set_button(Gdk.BUTTON_PRIMARY)
            click.connect("released", lambda *_a, n=note: self.edit(n))
            text.add_controller(click)
            text.set_cursor_from_name("text")
        card.append(text)

        foot = Gtk.Box(spacing=6)
        meta = Gtk.Label(label=meta_text(note, now), xalign=0)
        meta.add_css_class("note-meta")
        meta.set_hexpand(True)
        meta.set_ellipsize(Pango.EllipsizeMode.END)
        foot.append(meta)
        if not note.get("queued"):
            pencil = Gtk.Button(icon_name="document-edit-symbolic")
            pencil.add_css_class("action")
            pencil.set_tooltip_text("Edit")
            pencil.set_valign(Gtk.Align.CENTER)
            pencil.connect("clicked", lambda _b, n=note: self.edit(n))
            foot.append(pencil)
        more = Gtk.Button(icon_name="view-more-symbolic")
        more.add_css_class("action")
        more.set_tooltip_text("More")
        more.set_valign(Gtk.Align.CENTER)
        more.connect("clicked", lambda b, n=note: self.menu(
            b, n, b.get_width() / 2, b.get_height()))
        foot.append(more)
        action = self._action(note, chat, recap)
        if action is not None:
            foot.append(action)
        card.append(foot)

        click = Gtk.GestureClick()
        click.set_button(Gdk.BUTTON_SECONDARY)
        click.connect("pressed", lambda _g, _n, x, y, c=card, n=note:
                      self.menu(c, n, x, y))
        card.add_controller(click)
        return card

    def _editor(self, note: dict) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        view = Gtk.TextView()
        view.add_css_class("note-write")
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.set_accepts_tab(False)
        nid = note["id"]
        view.get_buffer().set_text(self.drafts.get(nid, note.get("text", "")))
        view.get_buffer().connect("changed", lambda _b: self.drafts.__setitem__(
            nid, _text_of(view)))
        # Grows with the note up to a point, then scrolls.
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_propagate_natural_height(True)
        scroll.set_min_content_height(110)
        scroll.set_max_content_height(360)
        scroll.set_child(view)
        box.append(scroll)
        foot = Gtk.Box(spacing=8)
        hint = Gtk.Label(label="ctrl-enter saves, esc cancels", xalign=0)
        hint.add_css_class("note-meta")
        hint.set_hexpand(True)
        foot.append(hint)
        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("note-link")
        cancel.connect("clicked", lambda _b: self.stop_editing(nid))
        foot.append(cancel)
        save = Gtk.Button(label="Save")
        save.add_css_class("note-start")
        save.connect("clicked", lambda _b: self.save_edit(note, view))
        foot.append(save)
        box.append(foot)
        keys = Gtk.EventControllerKey()

        def on_key(_c, keyval, _code, state):
            if keyval == Gdk.KEY_Escape:
                self.stop_editing(nid)
                return True
            if (state & Gdk.ModifierType.CONTROL_MASK
                    and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_s)):
                self.save_edit(note, view)
                return True
            return False
        keys.connect("key-pressed", on_key)
        view.add_controller(keys)
        # Focus once the card is on screen; the buffer's end is where a
        # second thought gets added.
        def focus():
            view.grab_focus()
            buf = view.get_buffer()
            buf.place_cursor(buf.get_end_iter())
            return False
        from gi.repository import GLib
        GLib.idle_add(focus)
        return box

    def edit(self, note: dict) -> None:
        if note.get("queued"):
            return
        self.editing = note.get("id")
        self.render()

    def stop_editing(self, nid: str) -> None:
        self.drafts.pop(nid, None)
        if self.editing == nid:
            self.editing = None
        self.render()
        self.jot.grab_focus()

    def save_edit(self, note: dict, view: Gtk.TextView) -> None:
        text = _text_of(view).strip()
        nid = note["id"]
        if not text:
            self.win.notice("A note needs some text -- delete it from its "
                            "… menu instead.")
            return
        self.drafts.pop(nid, None)
        self.editing = None
        if text != note.get("text", "").strip():
            self.win.update_note(note, text=text)
        self.render()

    def _action(self, note: dict, chat, recap: bool):
        if note.get("queued"):
            return None
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
            words.set_max_width_chars(16)
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
            button.connect("clicked", lambda _b: self.win.start_from_note(note))
            return button
        if note.get("status") == "done":
            done = Gtk.Label(label="done")
            done.add_css_class("note-meta")
            return done
        return None

    def menu(self, widget, note: dict, x: float, y: float) -> None:
        if note.get("queued"):
            items = [("Copy text", lambda: self.win.to_clipboard(note["text"]),
                      False),
                     (None, None, False),
                     ("Discard (not sent yet)",
                      lambda: self.win.discard_queued(note), True)]
        else:
            open_ = note.get("status", "open") != "done"
            items = [("Edit", lambda: self.edit(note), False),
                     ("Mark done" if open_ else "Reopen",
                      lambda: self.win.update_note(
                          note, status="done" if open_ else "open"), False),
                     ("Copy text", lambda: self.win.to_clipboard(note["text"]),
                      False)]
            if note.get("status", "open") == "open":
                items.insert(0, ("Start chat",
                                 lambda: self.win.start_from_note(note), False))
            items += [(None, None, False),
                      ("Delete…", lambda: self.win.confirm_delete_note(note),
                       True)]
        self.win.popup_at(widget, x, y, items)

    # -- writing -------------------------------------------------------------

    def _jot_key(self, _c, keyval, _code, state) -> bool:
        if (state & Gdk.ModifierType.CONTROL_MASK
                and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_s)):
            self.save_jot()
            return True
        if keyval == Gdk.KEY_Escape and not _text_of(self.jot):
            self.win.set_sidebar_mode("chats")
            return True
        return False

    def save_jot(self) -> None:
        text = _text_of(self.jot).strip()
        project = self.jot_project()
        if not text or project is None:
            return
        self.jot.set_sensitive(False)
        self.save_button.set_sensitive(False)

        def done(note, error):
            self.jot.set_sensitive(True)
            if error:
                self.win.notice(f"Note not saved: {error}")
                self._paint_save()
                return
            self.jot.get_buffer().set_text("")
            self.jot.grab_focus()
        self.win.add_note(project.id, text, source="desktop", done=done)
