"""The chrome around an open chat: desktop phase 3, Terminal mode.

ChatRunning.dc.html and Main.dc.html put a header over the terminal (state
pill, title, a mono meta line, a more menu), a strip with Claude's recap
under it, and a reply box at the foot. The terminal stays exactly what it
was -- typing straight into it still works -- and a plain shell gets none
of this, because none of it means anything there.

The window owns the data; ChatChrome.paint() is called from the window's
repaint() with the chat as it stands, so the header is never a second
source of truth.
"""

from __future__ import annotations

import time

from gi.repository import Gdk, GLib, Gtk, Pango

import agent_state
import chats
import hosts
import starting

PILL = {
    "needs": ("Needs you", "pill-amber"),
    agent_state.WORKING: ("Working", "pill-blue"),
    agent_state.DRAFT: ("Unsent draft", "pill-amber"),
    agent_state.READY: ("Idle", "pill-muted"),
    agent_state.UNKNOWN: ("Unknown", "pill-muted"),
}


def elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    return f"{seconds // 86400}d"


def meta_line(chat, folder: str, now: float) -> str:
    """"atlas  ~/atlas / Claude Code / 1m 04s" -- the host goes
    in its own chip, so this is the rest."""
    bits = [folder or "~", "Claude Code"]
    age = elapsed(now - chat.last_event) if chat.last_event else ""
    if age:
        if chats.is_needs_you(chat):
            bits.append(f"waiting {age}")
        elif chat.state == agent_state.WORKING:
            bits.append(age)
        else:
            bits.append(f"{age} ago")
    if chat.restored and chat.last_event <= chat.restored + 60:
        bits.append("restored after reboot")
    return " / ".join(bits)


def home_relative(path: str, home: str) -> str:
    home = (home or "").rstrip("/")
    if home and (path == home or path.startswith(home + "/")):
        return "~" + path[len(home):]
    return path


class ChatChrome:
    """Built once per open chat view and laid into its Session box."""

    def __init__(self, session, win):
        self.session = session
        self.win = win
        self.cid = chats.chat_id(session.host, session.name)
        self.sending = False
        # Both read off the screen, so both come and go as Claude redraws.
        # Hiding them when they scroll away resized the terminal, Claude
        # redrew to fit, they came back, and the view bounced up and down
        # by two lines forever. Once seen they stay, showing the latest.
        self.last_recap = ""
        self.url = ""

        header = Gtk.Box(spacing=12)
        header.add_css_class("chat-header")
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        left.set_hexpand(True)
        top = Gtk.Box(spacing=10)
        self.pill = Gtk.Label(label="")
        self.pill.add_css_class("pill")
        self.pill.set_valign(Gtk.Align.CENTER)
        top.append(self.pill)
        self.title = Gtk.Label(xalign=0)
        self.title.add_css_class("chat-header-title")
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        top.append(self.title)
        left.append(top)
        meta = Gtk.Box(spacing=8)
        self.host_chip = Gtk.Box(spacing=6)
        self.host_chip.add_css_class("host-chip")
        self.dot = Gtk.Box()
        self.dot.set_size_request(7, 7)
        self.dot.set_valign(Gtk.Align.CENTER)
        self.dot.add_css_class("dot")
        self.host_chip.append(self.dot)
        host = Gtk.Label(label=session.host)
        host.add_css_class("mono")
        self.host_chip.append(host)
        meta.append(self.host_chip)
        self.meta = Gtk.Label(xalign=0)
        self.meta.add_css_class("chat-header-meta")
        self.meta.set_ellipsize(Pango.EllipsizeMode.END)
        meta.append(self.meta)
        left.append(meta)
        header.append(left)

        self.preview_link = Gtk.Button(label="Open preview")
        self.preview_link.add_css_class("start")
        self.preview_link.set_valign(Gtk.Align.CENTER)
        self.preview_link.set_visible(False)
        self.preview_link.connect("clicked", lambda _b: self._open_preview())
        header.append(self.preview_link)

        self.more = Gtk.MenuButton(icon_name="view-more-symbolic")
        self.more.add_css_class("action")
        self.more.set_valign(Gtk.Align.CENTER)
        self.more.set_tooltip_text("Save recap to notes, rename, star, "
                                   "open in a window, kill")
        popover = Gtk.Popover()
        popover.add_css_class("conn-menu")
        popover.set_has_arrow(False)
        self.more.set_popover(popover)
        self.more.set_create_popup_func(lambda b: self._fill_more(
            b.get_popover()))
        header.append(self.more)

        self.recap = Gtk.Label(xalign=0)
        self.recap.add_css_class("recap-strip")
        self.recap.set_wrap(True)
        self.recap.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.recap.set_lines(2)
        self.recap.set_ellipsize(Pango.EllipsizeMode.END)
        self.recap.set_visible(False)

        # The reply box: the terminal is still there to type into, this is
        # for a longer answer than a prompt line wants, sent the SPEC §4
        # way (bracketed paste, then Enter).
        reply = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        reply.add_css_class("reply")
        self.reply_view = Gtk.TextView()
        self.reply_view.add_css_class("reply-text")
        self.reply_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.reply_view.set_accepts_tab(False)
        self.reply_view.set_top_margin(2)
        self.reply_view.set_left_margin(2)
        self.reply_hint = Gtk.Label(label="Reply to Claude", xalign=0)
        self.reply_hint.add_css_class("reply-placeholder")
        self.reply_hint.set_can_target(False)
        self.reply_hint.set_valign(Gtk.Align.START)
        self.reply_hint.set_margin_start(2)
        over = Gtk.Overlay()
        over.set_child(self.reply_view)
        over.add_overlay(self.reply_hint)
        self.reply_view.get_buffer().connect(
            "changed", lambda b: self.reply_hint.set_visible(
                b.get_char_count() == 0))
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._reply_key)
        self.reply_view.add_controller(keys)
        reply.append(over)
        foot = Gtk.Box(spacing=8)
        self.reply_note = Gtk.Label(
            label="Enter sends, Shift+Enter for a new line, Esc interrupts",
            xalign=0)
        self.reply_note.add_css_class("reply-note")
        self.reply_note.set_hexpand(True)
        self.reply_note.set_ellipsize(Pango.EllipsizeMode.END)
        foot.append(self.reply_note)
        send = Gtk.Button(icon_name="go-up-symbolic")
        send.add_css_class("send")
        send.set_tooltip_text("Send (Enter)")
        send.connect("clicked", lambda _b: self.send())
        foot.append(send)
        reply.append(foot)

        session.prepend(self.recap)
        session.prepend(header)
        session.append(reply)
        self.header = header
        self.reply = reply

    # -- painting ------------------------------------------------------------

    def paint(self, chat, row_session: dict, home: str, now: float) -> None:
        if chat is None:
            return
        key = "needs" if chats.is_needs_you(chat) else chat.state
        text, css = PILL.get(key, PILL[agent_state.UNKNOWN])
        self.pill.set_text(text)
        for name in ("pill-amber", "pill-blue", "pill-muted"):
            if name == css:
                self.pill.add_css_class(name)
            else:
                self.pill.remove_css_class(name)
        self.title.set_text(chat.title)
        for tone in ("green", "red", "muted"):
            self.dot.remove_css_class(tone)
        self.dot.add_css_class(self.win.host_tone(chat.host))
        folder = home_relative(chat.path, home) if chat.path else "~"
        self.meta.set_text(meta_line(chat, folder, now))
        recap = ((row_session or {}).get("agent") or {}).get("recap", "")
        if recap and recap != self.last_recap:
            self.last_recap = recap
            self.recap.set_text(f"※ {recap}")
        self.recap.set_visible(bool(self.last_recap))
        links = hosts.screen_links((row_session or {}).get("screen", ""))
        web = [u for u in links if u.startswith(("http://", "https://"))]
        if web and web[-1] != self.url:
            self.url = web[-1]
            self.preview_link.set_tooltip_text(self.url)
        self.preview_link.set_visible(bool(self.url))

    def _open_preview(self) -> None:
        if self.url:
            Gtk.UriLauncher.new(self.url).launch(self.win, None, None)

    def _fill_more(self, popover: Gtk.Popover) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        chat = self.win.chat_index.get(self.cid)
        host, name = self.session.host, self.session.name
        entries = [("Copy Claude's last reply", "ctrl-shift-y",
                    lambda: self.win.copy_reply(host, name)),
                   ("Save recap to notes", "",
                    lambda: self.win.save_recap(self.cid))]
        if chat is not None:
            entries += [("Unstar" if chat.starred else "Star", "",
                         lambda: self.win.toggle_chat_star(self.cid)),
                        ("Rename…", "F2", lambda: self.win.rename_chat(self.cid))]
        entries += [
            ("Open in a new window", "",
             lambda: hosts.launch(hosts.connect_argv(host, name))),
            ("Close the view", "ctrl-shift-w", self.win.close_visible),
            (None, None, None),
            (f"Kill {host}/{name}…", "",
             lambda: self.win.confirm_kill(host, name)),
        ]
        for label, hint, handler in entries:
            if label is None:
                box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
                continue
            box.append(self.win._menu_item(popover, label, hint, handler))
        popover.set_child(box)

    # -- replying ------------------------------------------------------------

    def text(self) -> str:
        buf = self.reply_view.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def _reply_key(self, _controller, keyval, _code, state) -> bool:
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not shift:
            self.send()
            return True
        if keyval == Gdk.KEY_Escape:
            self.win.interrupt(self.session.host, self.session.name)
            self.win.notice("Esc sent -- Claude stops what it is doing.")
            return True
        return False

    def send(self) -> None:
        text = self.text().strip("\n")
        if not text.strip() or self.sending:
            return
        self.sending = True
        self.reply_note.set_text("Sending…")
        host, name = self.session.host, self.session.name

        def work():
            try:
                starting.send_text(host, name, text)
                GLib.idle_add(landed, "")
            except Exception as exc:  # noqa: BLE001 -- landed must run
                GLib.idle_add(landed, str(exc))

        def landed(error):
            self.sending = False
            self.reply_note.set_text(
                "Enter sends, Shift+Enter for a new line, Esc interrupts")
            if error:
                self.win.notice(f"Not sent: {error}")
            else:
                self.reply_view.get_buffer().set_text("")
                self.win.sent_to(self.cid)
            return GLib.SOURCE_REMOVE

        import threading
        threading.Thread(target=work, name="send", daemon=True).start()
