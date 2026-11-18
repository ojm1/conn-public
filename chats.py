"""Chats on projects: the model behind the recent-first chat list.

A chat is a tmux session running an agent, plus the @conn_* user options
stored on it (SPEC §2). Everything here is pure -- no GTK, no ssh, no clock
it does not take as an argument -- because it moves into the shared core the
mini agent runs too, and because a list that sorts
by time is only testable with a "now" the test chooses.

What it does not own: reading the options (hosts.py), writing them
(hosts.set_chat_option, on a worker), and drawing anything (gui.py).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import tomllib
import unicodedata
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Iterable

import agent_state
import paths

PROJECTS_FILE = Path("~/.config/conn/projects.toml").expanduser()
FILTERS_STATE = paths.STATE / "filters.json"
# Which finished chats have been looked at, per device (SPEC §3): {chat id:
# the last_event that was showing when it was opened}.
SEEN_STATE = paths.STATE / "seen.json"
# The chats each server last showed while it answered, so a server going dark
# leaves its chats on the list (greyed) rather than taking them with it --
# a chat you cannot see is one you forget. See remember() and build_chats().
KNOWN_STATE = paths.STATE / "known-chats.json"

# SPEC §8's tiles for the projects it knows, keyed by project id and by name
# folded to letters, so "North Wind" finds northwind whatever its id is.
# These are dark-theme values: the GUI mixes the background toward its own
# panel colour and runs the letter through theming.legible(), so a light
# theme never gets a dark tile with a pale letter it cannot read.
TILE_DEFAULTS = {
    "atlas": ("#1F3A37", "#8FE0D2"),
    "acme": ("#2E2A4A", "#BFB0FF"),
    "widgets": ("#43263A", "#F5AECF"),
    "orbit": ("#2C3A22", "#B7DE8F"),
    "northwind": ("#233240", "#8EC5F0"),
    "summit": ("#3A2E26", "#E9BC9A"),
    "personal": ("#2A2F38", "#D0D5DE"),
}


# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Project:
    id: str
    name: str
    letter: str
    color: str                      # tile foreground; "" lets the GUI pick
    servers: dict[str, str]         # host -> folder, "~" = the remote home
    bg: str = ""                    # SPEC §8 tile background, "" if none
    agent: str = ""
    mode: str = ""
    implicit: bool = False          # a host in no project, standing in for one


def _fold(text: str) -> str:
    return "".join(char for char in text.lower() if char.isalnum())


def _tile(key: str, name: str) -> tuple[str, str]:
    return TILE_DEFAULTS.get(_fold(key)) or TILE_DEFAULTS.get(_fold(name)) \
        or ("", "")


def _host_project(host: str, taken: set[str]) -> Project:
    # A host named like a project that does not list it would otherwise
    # share that project's id, and every chat on it would wear the wrong
    # tile. The id only has to be unique; the name is what anyone reads.
    pid = host if host not in taken else f"host-{host}"
    bg, fg = _tile(host, host)
    return Project(id=pid, name=host, letter=(host[:1] or "?").upper(),
                   color=fg, servers={host: "~"}, bg=bg, implicit=True)


def read_projects(path: Path) -> list[Project]:
    """The projects a projects.toml defines, in file order. Raises OSError
    or tomllib.TOMLDecodeError; load_projects is the forgiving one."""
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    found = []
    for key, table in data.items():
        if not isinstance(table, dict):
            continue                # a stray top-level value, not a project
        servers = table.get("servers") or {}
        if not isinstance(servers, dict):
            continue
        name = str(table.get("name") or key)
        bg, fg = _tile(key, name)
        found.append(Project(
            id=str(key), name=name,
            letter=str(table.get("letter") or name[:1] or "?")[:1].upper(),
            color=str(table.get("color") or fg),
            # A colour of your own outranks the default tile, background
            # included: a pale letter on another project's purple is no one's
            # choice. The GUI derives a muted background from the colour.
            bg="" if table.get("color") else bg,
            servers={str(h): str(f or "~") for h, f in servers.items()},
            agent=str(table.get("agent") or ""),
            mode=str(table.get("mode") or "")))
    return found


def load_projects(path: Path | None = None,
                  hosts: Iterable[str] = ()) -> list[Project]:
    """The file's projects, then one for every host no project lists.

    A missing or unreadable file is no projects, not an error: before
    anyone writes one, every server is simply its own project -- which is
    exactly the panel as it was, so nothing disappears (SPEC §1).
    """
    try:
        projects = read_projects(path or PROJECTS_FILE)
    except FileNotFoundError:
        projects = []
    except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
        print(f"conn: ignoring {path or PROJECTS_FILE}: {error}",
              file=sys.stderr)
        projects = []
    listed = {host for project in projects for host in project.servers}
    taken = {project.id for project in projects}
    for host in hosts:
        if host in listed:
            continue
        project = _host_project(host, taken)
        projects.append(project)
        listed.add(host)
        taken.add(project.id)
    return projects


def _expand(folder: str, home: str) -> str | None:
    """A folder from projects.toml as an absolute remote path, or None when
    it is home-relative and the home is not known yet -- an unknown home
    must not match everything, nor nothing silently forever."""
    folder = folder.strip() or "~"
    if folder == "~" or folder.startswith("~/"):
        if not home:
            return None
        folder = home.rstrip("/") + folder[1:]
    return folder.rstrip("/") or "/"


def _inside(path: str, folder: str) -> bool:
    if folder == "/":
        return path.startswith("/")
    return path == folder or path.startswith(folder + "/")


def project_for(option: str, host: str, path: str, home: str,
                projects: list[Project]) -> str:
    """Which project a chat belongs to (SPEC §2), as a project id.

    @conn_project first -- but only one this client knows: a projects.toml
    behind the one that started the chat would otherwise file it under a
    project with no tile and no menu entry. Then the deepest folder of a
    project listing this server that holds the pane's path; equal depth goes
    to the first in the file. Then the host's own project, and if the host
    has none because it is listed, the first project listing it -- the
    SPEC's "otherwise the host" names a project that only exists for hosts
    in no project, and a chat must never point at an id the list lacks.
    """
    known = {project.id for project in projects}
    if option and option in known:
        return option

    best, depth = None, -1
    for project in projects:
        if host not in project.servers:
            continue
        folder = _expand(project.servers[host], home)
        if folder is None or not path or not _inside(path.rstrip("/") or "/",
                                                     folder):
            continue
        if len(folder) > depth:         # strictly longer: ties keep file order
            best, depth = project.id, len(folder)
    if best is not None:
        return best

    listing = [project for project in projects if host in project.servers]
    for project in listing:
        if project.implicit:
            return project.id
    if listing:
        return listing[0].id
    return host


# --------------------------------------------------------------------------
# Chats
# --------------------------------------------------------------------------

# What Claude Code draws in front of its terminal title: a status glyph
# ("✳ Telegram error reports"), or a braille spinner frame while working.
# Symbols (So), plus the ASCII-ish ones spinners also use.
_LEAD = set("*·•")


def _is_glyph(char: str) -> bool:
    return ("⠀" <= char <= "⣿" or char in _LEAD
            or unicodedata.category(char) == "So")


# Titles that say nothing about the chat: Claude Code's own before it has
# named the conversation, and the name of the program.
_GENERIC = {"claude code", "claude", "opencode"}


def clean_title(pane_title: str) -> str:
    text = pane_title.strip()
    index = 0
    while index < len(text) and (_is_glyph(text[index])
                                 or text[index].isspace()):
        index += 1
    return text[index:].strip()


def autotitle_in_force(options: dict, pane_title: str) -> str:
    """@conn_autotitle, while Claude's title is still the one it replaced:
    a new conversation in the same session brings a new pane title, and
    its own name back. "" otherwise."""
    auto = clean_title(options.get("autotitle") or "")
    made_for = clean_title(options.get("autotitle_for") or "")
    if auto and (not made_for or made_for == clean_title(pane_title)):
        return auto
    return ""


def title_for(options: dict, pane_title: str, session: str,
              hostname: str | Iterable[str] = ()) -> str:
    """@conn_name, else @conn_autotitle, else the pane title without its
    status glyph, else @conn_title, else the session name (SPEC §2).

    A name the user gave the chat outranks everything: they typed it because
    the rest was wrong. @conn_title is only what the starter wrote down
    before Claude had read the task, so Claude's own title replaces it once
    it arrives.

    tmux's default pane title is the machine's hostname, so a pane title
    equal to any name the host goes by is tmux talking, not the agent.
    """
    own = (options.get("name") or "").strip()
    if own:
        return own
    # Conn's own up-to-date title (retitle.py) over Claude's, which is
    # fixed at the conversation's first topic.
    auto = autotitle_in_force(options, pane_title)
    if auto:
        return auto
    names = {hostname} if isinstance(hostname, str) else set(hostname)
    names = {name.lower() for name in names if name}
    names |= {name.split(".")[0] for name in names}
    raw = pane_title.strip()
    if raw and raw.lower() not in names:
        cleaned = clean_title(raw)
        if cleaned and cleaned.lower() not in _GENERIC \
                and cleaned.lower() not in names:
            return cleaned
    title = (options.get("title") or "").strip()
    if title:
        return title
    return session


@dataclass
class Chat:
    id: str                 # "host/session"
    host: str
    session: str
    project: str            # a Project.id
    title: str
    state: str              # an agent_state constant
    last_event: int         # @conn_last_event, else session_created
    preview: str = ""
    starred: bool = False
    source: str = ""
    created: int = 0
    task: str = ""
    path: str = ""
    # Whether last_event came from the option or is the created fallback --
    # RecencyTracker needs to know a chat has never been stamped.
    stamped: bool = False
    # tmux's session_activity: the last output, whatever caused it. Never the
    # sort key (the spinner moves it), only the first-sight stamp's floor.
    activity: int = 0
    # What the screen says about the last finished turn: its reply and its
    # "Worked for 8s" line. A new one on an idle chat is a turn that started
    # and ended between two looks -- see RecencyTracker.
    turn: str = ""
    name: str = ""          # @conn_name as set, "" when the user gave none
    project_option: str = ""    # @conn_project as set, "" for automatic
    # Finished while you were elsewhere and not opened since: needs you
    # (SPEC §3). Set by the caller from a FinishTracker and the seen book.
    finished: bool = False
    # When restore.py rebuilt it after a reboot (0: never). Shown until the
    # chat has something newer to say.
    restored: int = 0
    pane_title: str = ""    # Claude's own title, as the pane has it
    autotitle: str = ""     # @conn_autotitle, "" when none is in force
    # Remembered, not live: its server is not answering, so this is the chat
    # as it was when it last did (`seen`, unix seconds), in what state
    # (`last_state`). Its own state is UNKNOWN -- nothing is known now.
    offline: bool = False
    seen: int = 0
    last_state: str = ""


def chat_id(host: str, session: str) -> str:
    return f"{host}/{session}"


# A day of clock skew between a server and here is allowed; past that a
# stamp is not seconds-since-1970 from a sane clock.
_SKEW = 86400


def _plausible(stamp, now: float | None = None) -> bool:
    """Whether an @conn_last_event is a usable unix time in seconds."""
    if not isinstance(stamp, int) or isinstance(stamp, bool):
        return False
    now = time.time() if now is None else now
    return 0 < stamp <= now + _SKEW


def _turn(agent: dict) -> str:
    """The last finished turn, as the screen shows it: "" when it shows none."""
    detail, preview = agent.get("detail") or "", agent.get("preview") or ""
    return f"{detail}\n{preview}" if detail or preview else ""


def _chat(host: str, session: dict, home: str, hostnames: set,
          projects: list[Project], overrides: dict) -> Chat | None:
    """One session as a Chat, or None for a plain shell."""
    agent = session.get("agent") or {}
    state = agent.get("state", agent_state.UNKNOWN)
    if state == agent_state.SHELL:
        return None             # not a chat: it lives under Servers
    name = session.get("name", "")
    options = dict(session.get("options") or {})
    for key, value in overrides.get(chat_id(host, name), {}).items():
        options[key] = value if value is not None else ""
    created = int(session.get("created") or 0)
    last = options.get("last_event")
    if isinstance(last, str):
        last = int(last) if last.isascii() and last.isdigit() else None
    if last is not None and not _plausible(last):
        # Another client's mistake -- milliseconds is the obvious one -- read
        # as unset: the row falls back to session_created, and RecencyTracker
        # sees an unstamped chat and rewrites it.
        last = None
    return Chat(
        id=chat_id(host, name), host=host, session=name,
        project=project_for(options.get("project", ""), host,
                            session.get("path", ""), home, projects),
        title=title_for(options, session.get("pane_title", ""), name,
                        hostnames),
        state=state,
        last_event=last if last is not None else created,
        preview=agent.get("preview", ""),
        starred=bool(options.get("starred")),
        source=options.get("source", "") or "",
        created=created,
        task=options.get("task", "") or "",
        path=session.get("path", "") or "",
        stamped=last is not None,
        activity=_int(session.get("activity")),
        turn=_turn(agent),
        name=(options.get("name") or "").strip(),
        restored=_int(options.get("restored")),
        pane_title=session.get("pane_title", "") or "",
        autotitle=autotitle_in_force(options,
                                     session.get("pane_title", "") or ""),
        project_option=options.get("project", "") or "")


def build_chats(rows: dict, projects: list[Project],
                homes: dict[str, str] | None = None,
                overrides: dict[str, dict] | None = None,
                known: dict[str, dict] | None = None) -> list[Chat]:
    """Every agent session in `rows` as a Chat, plain shells left out.

    `rows` is the GUI's per-host dict: {"sessions": [...]} with each
    session carrying hosts.py's options/pane_title/path/created and the
    classify() result under "agent". A host's home comes from `homes`
    first, then the row -- frames never carry it, probes do.

    `overrides` is {chat id: {option: value}} laid over a session's options
    before anything is derived from them: a rename or a move written behind
    and not echoed back yet, so the title, tile and filters follow at once.
    A value of None there means unset.

    `known` is the last-known book (remember()): for a host in `rows` that
    is not "up", its remembered chats come back as offline Chats -- never
    needs you, state UNKNOWN. An up host's live sessions are the whole truth,
    so its book entry is ignored; a remembered chat the live row also lists
    is never built twice.
    """
    homes = homes or {}
    overrides = overrides or {}
    known = known or {}
    chats = []
    for host, row in rows.items():
        home = homes.get(host) or row.get("home", "") or ""
        # The probe's hostname, and any other name the caller has reason to
        # think tmux used as the default title (see gui._machine_name).
        hostnames = {host, row.get("hostname", "") or "",
                     *(row.get("hostnames") or ())}
        live = set()
        for session in row.get("sessions", []):
            chat = _chat(host, session, home, hostnames, projects, overrides)
            live.add(session.get("name", ""))
            if chat is not None:
                chats.append(chat)
        entry = known.get(host)
        if row.get("state") == "up" or not entry:
            continue
        home = home or entry.get("home", "")
        hostnames |= set(entry.get("hostnames") or ())
        for session in entry.get("sessions", []):
            if session.get("name", "") in live:
                continue
            chat = _chat(host, session, home, hostnames, projects, overrides)
            if chat is None:
                continue
            chat.last_state, chat.state = chat.state, agent_state.UNKNOWN
            chat.offline, chat.seen = True, _int(entry.get("seen"))
            chat.preview = ""
            chats.append(chat)
    return chats


def _int(value) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return 0


def is_needs_you(chat: Chat) -> bool:
    """The "Needs you" filter and badge: blocked on a prompt, a draft you
    left in the box, or a turn that finished while you were elsewhere and
    you have not opened since (SPEC §3). The count, the filter, the badge
    and the summary all ask this, so they cannot disagree."""
    if chat.offline:
        return False        # a remembered chat: nothing is known about it now
    return agent_state.needs_attention(chat.state) or chat.finished


def sort_key(chat: Chat):
    # Newest first; created then id break ties so equal stamps never shuffle
    # between frames.
    return (-chat.last_event, -chat.created, chat.id)


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------

@dataclass
class Filters:
    project: str | None = None
    server: str | None = None
    needs_you: bool = False
    # Not a filter but how the list is laid out: one section per project in
    # projects.toml order, rows in a fixed order, instead of by time. Kept
    # here so it is remembered with the chips and survives them changing.
    by_project: bool = False

    def active(self) -> bool:
        return bool(self.project or self.server or self.needs_you)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Filters":
        """Whatever a hand-edited or half-written file holds, the result is
        usable: anything unreadable is simply no filter."""
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        project = data.get("project")
        server = data.get("server")
        return cls(project=project if isinstance(project, str) and project
                   else None,
                   server=server if isinstance(server, str) and server
                   else None,
                   needs_you=data.get("needs_you") is True,
                   by_project=data.get("by_project") is True)

    def valid_for(self, projects: list[Project]) -> "Filters":
        """These filters without a project or server that no longer exists,
        so a renamed server cannot leave the list empty with no visible
        reason."""
        ids = {project.id for project in projects}
        servers = {host for project in projects for host in project.servers}
        return Filters(project=self.project if self.project in ids else None,
                       server=self.server if self.server in servers else None,
                       needs_you=self.needs_you, by_project=self.by_project)


def load_filters(path: Path = FILTERS_STATE) -> Filters:
    try:
        return Filters.from_json(path.read_text())
    except OSError:
        return Filters()


def save_filters(filters: Filters, path: Path = FILTERS_STATE) -> None:
    # Written whole then renamed: a crash mid-write must leave the old
    # filters, not a truncated file that silently reads as none.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(filters.to_json() + "\n")
        os.replace(temp, path)
    except OSError:
        pass        # a filter that does not persist is not worth an error


def pick_project(filters: Filters, project: str | None) -> Filters:
    """Choosing a project resets the server: the one picked before may not be
    in it, and a list filtered to nothing reads as broken."""
    return replace(filters, project=project or None, server=None)


def pick_server(filters: Filters, server: str | None) -> Filters:
    return replace(filters, server=server or None)


def toggle_needs(filters: Filters) -> Filters:
    return replace(filters, needs_you=not filters.needs_you)


def toggle_by_project(filters: Filters) -> Filters:
    return replace(filters, by_project=not filters.by_project)


def in_scope(chat: Chat, filters: Filters) -> bool:
    return ((not filters.project or chat.project == filters.project)
            and (not filters.server or chat.host == filters.server))


def matches(chat: Chat, query: str,
            names: dict[str, str] | None = None) -> bool:
    """Every word of the query somewhere in the chat's title, server,
    session, preview or project name -- case-insensitive."""
    words = query.lower().split()
    if not words:
        return True
    hay = " ".join((chat.title, chat.host, chat.session, chat.preview,
                    (names or {}).get(chat.project, chat.project))).lower()
    return all(word in hay for word in words)


def visible(chats: list[Chat], filters: Filters, query: str = "",
            projects: list[Project] | None = None) -> list[Chat]:
    """The chats the list shows, newest first."""
    names = {project.id: project.name for project in projects or []}
    shown = [chat for chat in chats
             if in_scope(chat, filters)
             and (not filters.needs_you or is_needs_you(chat))
             and matches(chat, query, names)]
    return sorted(shown, key=sort_key)


def needs_count(chats: list[Chat], filters: Filters) -> int:
    """The count on the Needs you chip: within the project and server picked,
    but not narrowed by the toggle itself -- otherwise switching it on could
    only ever show the number it already showed."""
    return sum(1 for chat in chats
               if in_scope(chat, filters) and is_needs_you(chat))


# --------------------------------------------------------------------------
# Sections and time
# --------------------------------------------------------------------------

SECTIONS = ("Starred", "Today", "Yesterday", "Earlier this week", "Older")


def _when(ts: float, tz: tzinfo | None) -> datetime | None:
    """ts as a local time, or None when it is not one datetime can hold.

    build_chats already turns a nonsense stamp into the created fallback,
    but this runs inside every render, and a raise here would take the GLib
    callback that drains frames with it for good -- so no input may."""
    try:
        return datetime.fromtimestamp(ts, tz)
    except (OverflowError, ValueError, OSError):
        return None


def _day(ts: float, tz: tzinfo | None) -> date | None:
    when = _when(ts, tz)
    return when.date() if when is not None else None


def bucket(ts: float, now: float, tz: tzinfo | None = None) -> str:
    """Which dated section a time falls in, by calendar day in local time --
    not by hours elapsed: 23:50 last night is Yesterday at 00:10."""
    today = _day(now, tz)
    day = _day(ts, tz)
    if day is None or today is None:
        return "Older"                  # no telling when: not a recent one
    if day >= today:                    # a clock a little ahead is still today
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    if day > today - timedelta(days=7):
        return "Earlier this week"
    return "Older"


def sections(chats: list[Chat], now: float,
             tz: tzinfo | None = None) -> list[tuple[str, list[Chat]]]:
    """The list's sections in order, empty ones left out. A starred chat is
    under Starred and nowhere else."""
    grouped: dict[str, list[Chat]] = {label: [] for label in SECTIONS}
    for chat in chats:
        label = "Starred" if chat.starred else bucket(chat.last_event, now, tz)
        grouped[label].append(chat)
    return [(label, sorted(grouped[label], key=sort_key))
            for label in SECTIONS if grouped[label]]


def project_key(chat: Chat):
    """Where a chat sits inside its project's section: starred first, then
    by title. Nothing about time, so a row stays put while it works."""
    return (not chat.starred, chat.title.casefold(), chat.host,
            chat.session)


def project_sections(chats: list[Chat], projects: list[Project]
                     ) -> list[tuple[str, list[Chat]]]:
    """The list by project: one section per project with a chat, in
    projects.toml order, named by the project. A chat whose project is not
    in the list (a row from before a projects.toml edit) still shows, in a
    section of its own id, after the rest -- never dropped."""
    grouped: dict[str, list[Chat]] = {}
    for chat in chats:
        grouped.setdefault(chat.project, []).append(chat)
    names = {project.id: project.name for project in projects}
    order = [project.id for project in projects if project.id in grouped]
    order += sorted(pid for pid in grouped if pid not in names)
    return [(names.get(pid, pid) or "No project",
             sorted(grouped[pid], key=project_key)) for pid in order]


# Spelt out rather than strftime'd: GTK sets the process locale, and the
# list's copy is English everywhere else.
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def relative_time(ts: float, now: float, tz: tzinfo | None = None) -> str:
    """The time on a row: "now", "14m", "3h" today, the weekday within the
    week, then a date ("12 Oct", with the year once it is not this one)."""
    delta = now - ts
    if delta < 60:
        return "now"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    today = _day(now, tz)
    when = _when(ts, tz)
    if when is None or today is None:
        return ""
    if when.date() == today:
        return f"{int(delta // 3600)}h"
    if when.date() > today - timedelta(days=7):
        return _DAYS[when.weekday()]
    month = _MONTHS[when.month - 1]
    if when.year == today.year:
        return f"{when.day} {month}"
    return f"{when.day} {month} {when.year}"


# --------------------------------------------------------------------------
# Copy
# --------------------------------------------------------------------------

def _name(projects: list[Project], pid: str | None) -> str:
    for project in projects:
        if project.id == pid:
            return project.name
    return pid or ""


def filter_summary(filters: Filters, projects: list[Project]) -> str:
    """"Only Acme, acme-app" -- or "" with no filter on."""
    parts = []
    if filters.project:
        parts.append(_name(projects, filters.project))
    if filters.server:
        parts.append(filters.server)
    if filters.needs_you:
        parts.append("needs you")
    return "Only " + ", ".join(parts) if parts else ""


def new_chat_label(filters: Filters, projects: list[Project]) -> str:
    if filters.server:
        return f"New chat on {filters.server}"
    if filters.project:
        return f"New chat in {_name(projects, filters.project)}"
    return "New chat"


def _count(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def footer_text(shown: int, total: int, nprojects: int,
                filtered: bool) -> str:
    """"3 of 10 chats" while filtered, else "10 chats · 7 projects"."""
    if filtered:
        return f"{shown} of {_count(total, 'chat')}"
    return f"{_count(total, 'chat')} · {_count(nprojects, 'project')}"


def empty_text(filters: Filters, projects: list[Project],
               query: str = "") -> str:
    """"No chats on acme-dev yet." -- what an empty list says instead."""
    if query.strip():
        return f"No chats match “{query.strip()}”."
    where = (f" on {filters.server}" if filters.server
             else f" in {_name(projects, filters.project)}" if filters.project
             else "")
    return f"No chats{where}" + (" waiting on you." if filters.needs_you
                                 else " yet.")


def fleet_summary(up: int, dark: int) -> str:
    """"10 up · 3 dark", the dark half only when there is any."""
    return f"{up} up · {dark} dark" if dark else f"{up} up"


@dataclass
class MenuItem:
    id: str | None          # None is the "All ..." entry
    name: str
    meta: str
    selected: bool
    project: Project | None = None
    dark: bool = False


def project_menu(filters: Filters, projects: list[Project]) -> list[MenuItem]:
    """The Project chip's menu: "All projects", then each with its server
    count."""
    total = len({host for project in projects for host in project.servers})
    items = [MenuItem(None, "All projects", _count(total, "server"),
                      filters.project is None)]
    for project in projects:
        items.append(MenuItem(project.id, project.name,
                              _count(len(project.servers), "server"),
                              filters.project == project.id, project))
    return items


def server_menu(filters: Filters, projects: list[Project], chats: list[Chat],
                dark: Iterable[str] = ()) -> tuple[str, list[MenuItem]]:
    """The Server chip's header and menu: only the picked project's servers
    when one is picked, each with its chat count, "no chats", or "dark".
    Counts are every chat on the server, not the filtered view -- the menu
    is how you find chats the filter is hiding."""
    dark = set(dark)
    hosts: list[str] = []
    for project in projects:
        if filters.project and project.id != filters.project:
            continue
        for host in project.servers:
            if host not in hosts:       # a server in two projects, once
                hosts.append(host)
    header = (f"Servers in {_name(projects, filters.project)}"
              if filters.project else "Servers")
    items = [MenuItem(None, "All servers", _count(len(hosts), "server"),
                      filters.server is None)]
    for host in hosts:
        n = sum(1 for chat in chats if chat.host == host)
        meta = ("dark" if host in dark
                else "no chats" if n == 0 else _count(n, "chat"))
        items.append(MenuItem(host, host, meta, filters.server == host,
                              dark=host in dark))
    return header, items


# --------------------------------------------------------------------------
# Recency
# --------------------------------------------------------------------------

# The states recency cares about. A draft is idle with text in the box:
# typing is not an event (SPEC §2 lists the agent's transitions and sends,
# and a send is stamped by whoever sends), so a draft appearing, changing or
# being cleared never moves a chat. UNKNOWN is missing, not a state: a
# capture that came back torn says nothing about whether the chat moved.
_PHASE = {
    agent_state.READY: "idle",
    agent_state.DRAFT: "idle",
    agent_state.WORKING: "working",
    agent_state.NEEDS_YOU: "needs-you",
}


@dataclass
class RecencyTracker:
    """Decides when the desktop stamps @conn_last_event, until conn-agent
    does it instead (SPEC §2, decision 6).

    Feed it every frame's chats; it answers with the stamps now due. It
    keeps only what it needs to tell a real transition from a redraw: the
    last *known* phase per chat. The caller writes the stamps over ssh on a
    worker and puts them in its in-memory chats at once, so the row moves
    this frame rather than after the round trip.

    - First sight of a chat that has never been stamped: stamp it with the
      newer of its session_created and session_activity, never past now --
      not now itself, or opening conn would make every old chat look brand
      new; and not created alone, or a chat begun last month and used an
      hour ago files under last month. An idle chat's last output is about
      when its last turn finished, which is the event SPEC §2 means.
    - A stamp exactly equal to session_created, on first sight, is the old
      rule's first-sight stamp rather than an event, and is redone once by
      the rule above when the session has been active since (SPEC §2).
    - A change of phase between idle, working and needs-you: stamp now.
    - Idle both times, but the screen shows a different finished turn (a new
      reply, a new "Worked for 8s") and the session has had output since the
      stamp: a whole turn ran between two looks -- a server that is polled
      every few minutes, not streamed, never sees the working in between.
      Stamp its last output, never past now.
    - Anything else -- the spinner turning, the step text changing, a draft
      being typed -- is not an event. Neither is a flip into UNKNOWN or back
      to the phase it left; but a chat that comes out of UNKNOWN in a
      different phase than it went in did change, and is stamped.
    - A session conn saw as a plain shell that now runs an agent is a chat
      starting (SPEC §2: "set it when the chat starts"): stamp now, whatever
      the option held from an earlier run. That is not a first sighting --
      conn watched it happen -- so session_created would file a chat begun
      this minute under a tmux session's age. Told by saw_shells().
    """
    _phase: dict[str, str] = field(default_factory=dict)
    _stamped: set[str] = field(default_factory=set)
    _shells: set[str] = field(default_factory=set)
    _turn: dict[str, str] = field(default_factory=dict)

    def saw_shells(self, chat_ids: Iterable[str]) -> None:
        """Sessions seen this frame as plain shells (agent_state.SHELL) --
        the ones whose next agent will be a chat starting now."""
        for chat_id in chat_ids:
            self._shells.add(chat_id)
            self._phase.pop(chat_id, None)   # its last chat's phase is over
            self._turn.pop(chat_id, None)

    def observe(self, chat_id: str, state: str, last_event: int | None,
                created: int, now: float, activity: int = 0,
                turn: str = "") -> int | None:
        """The stamp due for one chat this frame, or None."""
        phase = _PHASE.get(state)
        if chat_id in self._shells:
            if phase is None:
                # An agent, but no reading of it yet: wait for one rather
                # than stamp on UNKNOWN, and do not fall through to the
                # session_created first-sight stamp either.
                return None
            self._shells.discard(chat_id)
            self._stamped.add(chat_id)
            self._phase[chat_id] = phase
            self._turn[chat_id] = turn
            return int(now)

        due = None
        repair = (last_event is not None and chat_id not in self._stamped
                  and created and last_event == created
                  and activity > created)
        if last_event is not None and not repair:
            self._stamped.add(chat_id)
        elif chat_id not in self._stamped and created:
            # Remembered, because the option will read unset for a frame or
            # two until the write lands, and that is not a second first sight.
            # No created time means the metadata did not arrive this frame:
            # wait for it rather than stamp "now" onto an old chat.
            due = min(int(now), max(int(created), int(activity or 0)))
            self._stamped.add(chat_id)

        if phase is None:
            return due              # UNKNOWN (or SHELL): no information
        before = self._phase.get(chat_id)
        self._phase[chat_id] = phase
        if before is not None and before != phase:
            due = int(now)
        elif phase == "idle" and before == "idle":
            seen = self._turn.get(chat_id)
            stamp = last_event if last_event is not None else created
            if (seen is not None and turn and turn != seen
                    and activity and activity > (stamp or 0)):
                due = min(int(now), int(activity))
        if turn or chat_id not in self._turn:
            # A frame that shows no turn at all (scrolled, redrawing) leaves
            # the last one in place rather than making the next look new.
            self._turn[chat_id] = turn
        return due

    def update(self, observations: Iterable[tuple], now: float
               ) -> dict[str, int]:
        """observe() over (chat id, state, option last_event, created[,
        activity[, turn]]) tuples: {chat id: stamp} for every chat with one
        due."""
        due = {}
        for chat_id, state, last_event, created, *rest in observations:
            stamp = self.observe(chat_id, state, last_event, created, now,
                                 rest[0] if rest else 0,
                                 rest[1] if len(rest) > 1 else "")
            if stamp is not None:
                due[chat_id] = stamp
        return due

    def update_chats(self, chats: Iterable[Chat], now: float) -> dict[str, int]:
        """update() straight from build_chats() output."""
        return self.update(((chat.id, chat.state,
                             chat.last_event if chat.stamped else None,
                             chat.created, chat.activity, chat.turn)
                            for chat in chats), now)

    def retain(self, chat_ids: Iterable[str]) -> None:
        """Forget sessions that are gone -- `chat_ids` is every chat and plain
        shell still there -- so a session recreated under the same name is a
        first sighting again rather than a transition from the last one's
        phase."""
        keep = set(chat_ids)
        self._phase = {k: v for k, v in self._phase.items() if k in keep}
        self._stamped &= keep
        self._shells &= keep
        self._turn = {k: v for k, v in self._turn.items() if k in keep}

    def rename(self, old: str, new: str) -> None:
        """A session renamed in tmux is the same chat under a new id: what
        is known about it moves with it, so the rename is not a sighting."""
        for book in (self._phase, self._turn):
            if old in book:
                book[new] = book.pop(old)
        for book in (self._stamped, self._shells):
            if old in book:
                book.discard(old)
                book.add(new)


# --------------------------------------------------------------------------
# Finished, not yet seen
# --------------------------------------------------------------------------

@dataclass
class FinishTracker:
    """Which chats Claude finished while you were elsewhere (SPEC §3).

    A turn ending is a working -> idle edge seen happen: WORKING into READY,
    or into DRAFT (a ghost suggestion, or text you left), through UNKNOWN
    or not -- the same phases RecencyTracker uses, so a draft being typed
    and cleared under a finished chat is not a second edge, and does not
    end it either. Only an edge counts: a chat idle on first sight was
    finished before conn looked, and is not news. Leaving idle -- working
    again, or blocked -- ends it; the next finish starts it again.

    Whether it is still unseen is the seen book's business, not this one's:
    see unseen_finished().
    """
    _phase: dict[str, str] = field(default_factory=dict)
    _finished: set[str] = field(default_factory=set)

    def observe(self, chat_id: str, state: str) -> bool:
        """Feed one chat's state; True exactly on the frame it finishes."""
        phase = _PHASE.get(state)
        if phase is None:
            return False        # UNKNOWN: no information, keep the last phase
        before = self._phase.get(chat_id)
        self._phase[chat_id] = phase
        if phase != "idle":
            self._finished.discard(chat_id)
            return False
        if before == "working":
            self._finished.add(chat_id)
            return True
        return False

    def update(self, chats: Iterable[Chat]) -> set[str]:
        """observe() over build_chats() output: the ids that finished now."""
        return {chat.id for chat in chats if self.observe(chat.id, chat.state)}

    def finished(self, chat_id: str) -> bool:
        return chat_id in self._finished

    def retain(self, chat_ids: Iterable[str]) -> None:
        keep = set(chat_ids)
        self._phase = {k: v for k, v in self._phase.items() if k in keep}
        self._finished &= keep

    def rename(self, old: str, new: str) -> None:
        if old in self._phase:
            self._phase[new] = self._phase.pop(old)
        if old in self._finished:
            self._finished.discard(old)
            self._finished.add(new)


def unseen_finished(chat: Chat, tracker: FinishTracker,
                    seen: dict[str, int]) -> bool:
    """Idle after a turn this client watched end, and not opened since.

    Opening a chat books its last_event as seen; the finish stamps a newer
    one (RecencyTracker), so only a finish after the last look is unseen.
    A draft is already needs you on its own, so this is about READY."""
    return (chat.state == agent_state.READY and not chat.offline
            and tracker.finished(chat.id)
            and chat.last_event > seen.get(chat.id, 0))


def mark_finished(chats: Iterable[Chat], tracker: FinishTracker,
                  seen: dict[str, int]) -> None:
    for chat in chats:
        chat.finished = unseen_finished(chat, tracker, seen)


def load_seen(path: Path = SEEN_STATE) -> dict[str, int]:
    """The seen book, or {} for a missing or unreadable one: forgetting what
    was seen only ever turns a chat amber that was not, never the reverse
    -- and only after it next finishes."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items()
            if isinstance(v, int) and not isinstance(v, bool)}


def save_seen(seen: dict[str, int], path: Path = SEEN_STATE) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(seen, sort_keys=True) + "\n")
        os.replace(temp, path)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Last known: the chats of a server that has stopped answering
# --------------------------------------------------------------------------

# What a remembered session keeps: only what build_chats draws a row from.
# Never the screen or the preview -- the file stays small and holds no
# conversation -- and the options without @conn_task, the prompt a chat was
# started with.
_KNOWN_FIELDS = ("name", "options", "pane_title", "path", "created",
                 "activity")
# Moves on every probe of a busy chat: kept, but not a reason to write.
_KNOWN_CHURN = ("activity",)


def known_sessions(row: dict) -> list[dict]:
    """A live row's agent sessions, trimmed to what is remembered of them."""
    kept = []
    for session in row.get("sessions", []):
        state = (session.get("agent") or {}).get("state", agent_state.UNKNOWN)
        name = session.get("name")
        if state == agent_state.SHELL or not isinstance(name, str) or not name:
            continue
        entry = {key: session[key] for key in _KNOWN_FIELDS
                 if session.get(key) not in (None, "")}
        options = {key: value for key, value
                   in (session.get("options") or {}).items()
                   if key != "task" and value not in (None, "")}
        entry["options"] = options
        entry["agent"] = {"state": state}
        kept.append(entry)
    return kept


def _settled(sessions: list[dict]) -> list[dict]:
    """What a write is decided on: a session without what moves while it
    works. That is its `activity`, the spinner Claude Code keeps at the
    front of its pane title (a new frame every second the stream speaks),
    and its agent state, which flaps between working and ready turn by
    turn. All three are still stored -- written with the next real change
    or the KNOWN_FLUSH write -- but none is a change worth a write alone."""
    settled = []
    for session in sessions:
        kept = {k: v for k, v in session.items() if k not in _KNOWN_CHURN
                and k != "agent"}
        if isinstance(kept.get("pane_title"), str):
            kept["pane_title"] = clean_title(kept["pane_title"])
        settled.append(kept)
    return settled


def remember(known: dict, host: str, row: dict, now: float) -> bool:
    """Book what an answering host shows now, replacing whatever was kept --
    a remembered chat the server no longer lists has really ended. A host
    with no chats leaves the book. True when it changed enough to be worth
    writing now; a newer `seen` or `activity` alone waits for the next write.
    """
    sessions = known_sessions(row)
    before = known.get(host)
    if not sessions:
        return known.pop(host, None) is not None
    hostnames = sorted({name for name in (row.get("hostname"),
                                          *(row.get("hostnames") or ()))
                        if isinstance(name, str) and name})
    entry = {"seen": int(now), "home": row.get("home", "") or "",
             "hostnames": hostnames, "sessions": sessions}
    known[host] = entry
    if before is None:
        return True
    return (_settled(before.get("sessions", [])) != _settled(sessions)
            or before.get("home") != entry["home"]
            or before.get("hostnames") != hostnames)


# The options _chat() treats as text: a number or a flag there is a bad file.
_KNOWN_TEXT = ("name", "project", "title", "source", "autotitle",
               "autotitle_for", "task")


def _plain_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _known_session(session) -> dict | None:
    """One remembered session as the file has it, checked field by field
    against what _chat() will do with it -- or None. The file is ours, but a
    hand edit or a half-bad copy that still parses must cost that session,
    not every render: build_chats runs on each frame, and a dark host's
    remembered chats are built every time."""
    if not isinstance(session, dict):
        return None
    name = session.get("name")
    if not isinstance(name, str) or not name:
        return None
    kept = {"name": name}
    for key in ("pane_title", "path"):
        if key in session:
            if not isinstance(session[key], str):
                return None
            kept[key] = session[key]
    for key in ("created", "activity"):
        if key in session:
            if not _plain_int(session[key]):
                return None
            kept[key] = session[key]
    options = session.get("options", {})
    if not isinstance(options, dict):
        return None
    clean = {}
    for key, value in options.items():
        # Flags and stamps go through bool() and _int(), which take
        # anything; the rest is text that gets stripped and matched.
        if not isinstance(key, str) or not isinstance(value, (str, int)) \
                or (key in _KNOWN_TEXT and not isinstance(value, str)):
            return None
        clean[key] = value
    kept["options"] = clean
    agent = session.get("agent", {})
    if not isinstance(agent, dict):
        return None
    state = agent.get("state", agent_state.UNKNOWN)
    if not isinstance(state, str):
        return None
    kept["agent"] = {"state": state}
    return kept


def load_known(path: Path = KNOWN_STATE) -> dict[str, dict]:
    """The last-known book, or {} for a missing or unreadable one -- a lost
    book only means a dark server's chats are not shown until it answers."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    hosts = data.get("hosts") if isinstance(data, dict) else None
    if not isinstance(hosts, dict):
        return {}
    known = {}
    for host, entry in hosts.items():
        if not isinstance(entry, dict):
            continue
        raw = entry.get("sessions")
        sessions = [kept for kept in map(_known_session,
                                         raw if isinstance(raw, list) else [])
                    if kept is not None]
        if not sessions:
            continue
        names = entry.get("hostnames")
        known[str(host)] = {
            "seen": _int(entry.get("seen")),
            "home": entry.get("home") if isinstance(entry.get("home"), str)
            else "",
            "hostnames": [n for n in names if isinstance(n, str)]
            if isinstance(names, list) else [],
            "sessions": sessions}
    return known


def save_known(known: dict[str, dict], path: Path = KNOWN_STATE) -> None:
    # Whole, then renamed, like the seen book: a crash mid-write keeps the
    # old file rather than a truncated one that reads as nothing.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"version": 1, "hosts": known},
                                   sort_keys=True, default=str) + "\n")
        os.replace(temp, path)
    except OSError:
        pass


def offline_text(seen: int, now: float, host_state: str,
                 tz: tzinfo | None = None) -> str:
    """A remembered chat's second line: "unreachable · last seen 3h ago".
    "connecting" while its host has not answered yet this run (or is being
    asked again) rather than refused; and in the host row's own words for a
    server that answers and turns you away -- "no key", "log in" -- which
    is not unreachable, and is not fixed by waiting."""
    head = {"unknown": "connecting", "nokey": "no key",
            "unverified": "log in"}.get(host_state, "unreachable")
    when = seen_text(seen, now, tz)
    return f"{head} · {when}" if when else head


def seen_text(seen: float, now: float, tz: tzinfo | None = None) -> str:
    """"last seen 3h ago", "last seen just now", "last seen Tue" -- or ""
    when nothing has been seen. Shared by the row, the offline page and the
    banner over a terminal whose server has gone dark."""
    if not seen:
        return ""
    when = relative_time(seen, now, tz)
    if not when:
        return ""
    if when == "now":
        when = "just now"
    elif when[:-1].isdigit():
        when += " ago"          # "14m ago", "3h ago"; a day name stands alone
    return f"last seen {when}"


# --------------------------------------------------------------------------
# Editing projects.toml
# --------------------------------------------------------------------------

PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
COLOUR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# The keys a project table is written with, in this order; anything else a
# hand-edited file holds goes after them, untouched.
_KEY_ORDER = ("name", "letter", "color", "servers", "agent", "mode")


def slug(text: str) -> str:
    """A project id from a name: "North Wind" -> "north-wind"."""
    out = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return out or "project"


def read_project_tables(path: Path) -> dict:
    """projects.toml as tomllib reads it, {} when there is no file. Raises
    on a file that does not parse -- the editor must not save over one."""
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}


def _toml_string(text: str) -> str:
    out = []
    for char in text:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char in "\b\t\n\f\r":
            out.append({"\b": "\\b", "\t": "\\t", "\n": "\\n",
                        "\f": "\\f", "\r": "\\r"}[char])
        elif ord(char) < 0x20 or ord(char) == 0x7f:
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(key: str) -> str:
    return key if _BARE.match(key) else _toml_string(key)


def _toml_value(value, where: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v, where) for v in value) + "]"
    if isinstance(value, dict):
        # Always-quoted keys: a server name like "acme-reports" is bare-safe,
        # but one with a dot would become a nested table if left bare.
        inner = ", ".join(f"{_toml_string(str(k))} = {_toml_value(v, where)}"
                          for k, v in value.items())
        return "{ " + inner + " }" if inner else "{}"
    raise ValueError(f"cannot keep {where}")


def dump_projects(data: dict) -> str:
    """The text of a projects.toml holding `data` -- exactly this schema:
    top-level values, then one table per project. tomllib.loads() of the
    result is `data` again; comments are the one thing not kept."""
    lines = []
    for key, value in data.items():
        if not isinstance(value, dict):
            lines.append(f"{_toml_key(key)} = {_toml_value(value, key)}")
    for pid, table in data.items():
        if not isinstance(table, dict):
            continue
        if lines:
            lines.append("")
        lines.append(f"[{_toml_key(pid)}]")
        keys = [k for k in _KEY_ORDER if k in table]
        keys += [k for k in table if k not in _KEY_ORDER]
        for key in keys:
            lines.append(f"{_toml_key(key)} = "
                         f"{_toml_value(table[key], f'{key} in {pid}')}")
    return "\n".join(lines) + "\n"


def write_projects(path: Path, data: dict) -> Path | None:
    """Write `data` as projects.toml, atomically, the old file kept beside it
    as projects.toml.bak. Returns the backup, or None when there was no old
    file. Nothing is touched if `data` cannot be written in full."""
    text = dump_projects(data)          # raises before anything is touched
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup)
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    return backup


def project_errors(data: dict) -> list[str]:
    """What stops `data` being saved, as sentences -- [] when nothing does."""
    errors = []
    names = {}
    for pid, table in data.items():
        if not isinstance(table, dict):
            continue
        if not PROJECT_ID.match(pid):
            errors.append(f"“{pid}” is not an id: lowercase letters, digits "
                          "and hyphens only")
        name = str(table.get("name", "")).strip()
        if not name:
            errors.append(f"{pid} needs a name")
        elif name.lower() in names:
            errors.append(f"two projects are called {name}")
        names[name.lower()] = pid
        letter = str(table.get("letter", ""))
        if letter and len(letter) != 1:
            errors.append(f"{name or pid}: the letter is one character")
        colour = str(table.get("color", ""))
        if colour and not COLOUR.match(colour):
            errors.append(f"{name or pid}: colour is #RRGGBB, or empty for "
                          "automatic")
        servers = table.get("servers")
        if not isinstance(servers, dict) or not servers:
            errors.append(f"{name or pid}: tick at least one server")
            continue
        for host, folder in servers.items():
            folder = str(folder)
            if (not folder or _CONTROL.search(folder)
                    or not folder.startswith(("~", "/"))):
                errors.append(f"{name or pid}: the folder on {host} starts "
                              "with ~ or /")
    return errors


def folder_suggestions(sessions: Iterable[dict], home: str = "") -> list[str]:
    """The folders chats on one server actually run in, most used first, the
    home written "~" -- what a project's folder for that server most likely
    is. "~" is always offered."""
    counts: dict[str, int] = {}
    home = home.rstrip("/")
    for session in sessions:
        path = (session.get("path") or "").rstrip("/")
        if not path:
            continue
        if home and (path == home or path.startswith(home + "/")):
            path = "~" + path[len(home):]
        counts[path] = counts.get(path, 0) + 1
    ranked = sorted(counts, key=lambda p: (-counts[p], p))
    if "~" not in ranked:
        ranked.append("~")
    return ranked
