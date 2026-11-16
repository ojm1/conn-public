#!/usr/bin/env python3
"""The chat list's model, checked without a display, a network or a clock.

Run it with plain python -- there is no test dependency on purpose:

    python3 tests/test_chats.py

Every time here is fixed, and so is the zone: the sections are calendar days
in local time, and a test that used the real clock would pass or fail by
what hour you ran it. Australia/Sydney, only because its daylight-saving
change sits inside a week the checks cross.

The copy is compared exactly against ProjectSidebar.dc.html's logic block:
the strings are the design.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_state as A  # noqa: E402
import chats as C  # noqa: E402

ZONE = ZoneInfo("Australia/Sydney")

failures = 0
total = 0


def check(label: str, passed: bool, detail: str = "") -> None:
    global failures, total
    total += 1
    failures += not passed
    print(f"{'ok  ' if passed else 'FAIL'} {label}")
    if not passed and detail:
        print(f"     {detail}")


def at(*parts) -> int:
    """Unix seconds for a wall-clock time in the test zone."""
    return int(datetime(*parts, tzinfo=ZONE).timestamp())


def chat(cid: str, last: int, state=A.READY, starred=False, project="p",
         created=0) -> C.Chat:
    host, _, session = cid.partition("/")
    return C.Chat(id=cid, host=host, session=session, project=project,
                  title=session, state=state, last_event=last,
                  starred=starred, created=created)


TOML = """
[acme]
name = "Acme"
color = "#BFB0FF"
servers = { "acme-app" = "~/acme", "acme-dev" = "~/acme", "acme-data" = "~/data" }

[atlas]
name = "Atlas"
servers = { "atlas" = "~/atlas" }

[home-a]
name = "Home A"
letter = "h"
servers = { "shared" = "~" }

[deep]
name = "Deep"
servers = { "shared" = "~/work/deep" }

[home-b]
name = "Home B"
color = "#123456"
servers = { "shared" = "~" }

[abs]
name = "Absolute"
servers = { "box" = "/srv/app" }

stray = "not a project"
"""


def test_titles() -> None:
    t = C.title_for
    check("@conn_title stands in until Claude titles the chat",
          t({"title": "CSV export"}, "", "s", "h") == "CSV export"
          and t({"title": "CSV export"}, "h", "s", "h") == "CSV export")
    check("Claude's own title replaces @conn_title (SPEC §2)",
          t({"title": "CSV export"}, "✳ Something else", "s", "h")
          == "Something else")
    check("@conn_name, the user's own name, beats everything",
          t({"name": "Mine", "title": "CSV export"}, "✳ Something else",
            "s", "h") == "Mine")
    check("an empty or blank @conn_name falls through",
          t({"name": "  ", "title": "CSV export"}, "", "s", "h")
          == "CSV export"
          and t({"name": ""}, "✳ Claude's", "s", "h") == "Claude's")
    check("Claude's status glyph is stripped",
          t({}, "✳ Telegram error reports", "s", "h") == "Telegram error reports")
    check("and a braille spinner frame",
          t({}, "⠙ Telegram error reports", "s", "h") == "Telegram error reports")
    check("and several glyphs with odd spacing",
          t({}, "  ✶ ⠹  Fix login", "s", "h") == "Fix login")
    check("a title of its own is left alone",
          t({}, "Fix login", "s", "h") == "Fix login")
    check("tmux's default (the hostname) is not a title",
          t({}, "atlas", "csv", "atlas") == "csv")
    check("nor the machine's own name when it differs from the alias",
          t({}, "data", "csv", {"atlas", "data"}) == "csv")
    check("nor a fully-qualified one",
          t({}, "data.example.com", "csv", {"data.example.com"}) == "csv")
    check("Claude Code's unnamed title says nothing",
          t({}, "✳ Claude Code", "csv", "h") == "csv")
    check("a bare glyph says nothing",
          t({}, "✳", "csv", "h") == "csv")
    check("nothing at all is the session name",
          t({}, "", "csv", "h") == "csv")
    check("a blank @conn_title does not count",
          t({"title": "  "}, "✳ Real", "csv", "h") == "Real")


def test_projects(tmp: Path) -> None:
    path = tmp / "projects.toml"
    path.write_text(TOML)
    hosts = ["acme-app", "loose", "atlas", "acme", "shared"]
    projects = C.load_projects(path, hosts=hosts)
    ids = [p.id for p in projects]
    check("file order, then hosts in no project in host order",
          ids == ["acme", "atlas", "home-a", "deep", "home-b", "abs",
                  "loose", "host-acme"], str(ids))
    byid = {p.id: p for p in projects}
    check("a stray top-level value is not a project", "stray" not in byid)
    check("a host in no project is a one-server project named after it",
          byid["loose"].name == "loose" and byid["loose"].servers == {"loose": "~"}
          and byid["loose"].implicit and byid["loose"].letter == "L")
    check("a host named like a project gets its own id",
          byid["host-acme"].name == "acme")
    check("letter defaults to the name's first, upper-cased",
          byid["acme"].letter == "A" and byid["home-a"].letter == "H")
    check("SPEC §8 tile for a known project",
          byid["atlas"].bg == "#1F3A37"
          and byid["atlas"].color == "#8FE0D2")
    check("a colour of your own drops the default background",
          byid["acme"].color == "#BFB0FF" and byid["acme"].bg == "")
    check("an unknown project with no colour leaves both to the GUI",
          byid["deep"].color == "" and byid["deep"].bg == "")

    missing = C.load_projects(tmp / "nope.toml", hosts=["a", "b"])
    check("missing file: every host is its own project",
          [p.id for p in missing] == ["a", "b"])
    broken = tmp / "broken.toml"
    broken.write_text("[acme\nname=")
    check("an unreadable file is the same as none",
          [p.id for p in C.load_projects(broken, hosts=["a"])] == ["a"])

    pf = C.project_for
    home = "/home/app"
    check("@conn_project wins over the path",
          pf("atlas", "acme-app", "/home/app/acme", home, projects)
          == "atlas")
    check("an @conn_project this client does not know falls through",
          pf("gone", "acme-app", "/home/app/acme", home, projects) == "acme")
    check("folder prefix with ~ expanded against the remote home",
          pf("", "acme-app", "/home/app/acme/src", home, projects) == "acme")
    check("a prefix is a whole path component",
          pf("", "acme-app", "/home/app/tracker", home, projects) == "acme")
    check("the deepest folder wins",
          pf("", "shared", "/home/u/work/deep/x", "/home/u", projects) == "deep")
    check("equal folders go to the first in the file",
          pf("", "shared", "/home/u/other", "/home/u", projects) == "home-a")
    check("a trailing slash on the home changes nothing",
          pf("", "shared", "/home/u/work/deep", "/home/u/", projects) == "deep")
    check("absolute folders need no home",
          pf("", "box", "/srv/app/web", "", projects) == "abs")
    check("an unknown home matches no ~ folder, and the listing wins",
          pf("", "shared", "/home/u/work/deep", "", projects) == "home-a")
    check("outside every folder: the first project listing the host",
          pf("", "acme-app", "/tmp", home, projects) == "acme")
    check("a host in no project is its own",
          pf("", "loose", "/anywhere", "/root", projects) == "loose")
    check("and a host nothing knows is still the host",
          pf("", "stranger", "/x", "/root", projects) == "stranger")


def session(name, state, *, options=None, pane_title="", path="",
            created=100, preview=""):
    opts = {"project": "", "title": "", "task": "", "last_event": None,
            "starred": False, "source": ""}
    opts.update(options or {})
    return {"name": name, "options": opts, "pane_title": pane_title,
            "path": path, "created": created,
            "agent": {"state": state, "preview": preview}}


def test_build(tmp: Path) -> None:
    path = tmp / "projects.toml"
    path.write_text(TOML)
    projects = C.load_projects(path, hosts=["acme-app", "loose"])
    rows = {
        "acme-app": {"home": "", "sessions": [
            session("tg", A.WORKING, pane_title="✳ Telegram error reports",
                    path="/home/app/acme", preview="Reading Channel.php"),
            session("shell", A.SHELL, pane_title="acme-app"),
            session("csv", A.READY, options={"last_event": 500,
                                             "starred": True,
                                             "title": "CSV export",
                                             "source": "desktop"}),
        ]},
        "loose": {"home": "/root", "sessions": [
            session("x", A.UNKNOWN)]},
    }
    built = {c.id: c for c in C.build_chats(rows, projects,
                                            {"acme-app": "/home/app"})}
    check("plain shells are not chats", "acme-app/shell" not in built)
    check("chat id is host/session",
          set(built) == {"acme-app/tg", "acme-app/csv", "loose/x"})
    tg = built["acme-app/tg"]
    check("the home from `homes` resolves the folder",
          tg.project == "acme", tg.project)
    check("title and preview carried through",
          tg.title == "Telegram error reports"
          and tg.preview == "Reading Channel.php")
    check("unset last_event falls back to created, unstamped",
          tg.last_event == 100 and not tg.stamped)
    csv = built["acme-app/csv"]
    check("options carried through",
          csv.starred and csv.title == "CSV export" and csv.source == "desktop"
          and csv.last_event == 500 and csv.stamped)
    check("an UNKNOWN agent session is still a chat",
          built["loose/x"].state == A.UNKNOWN and built["loose/x"].project == "loose")

    # A stamp no sane clock wrote -- milliseconds from another client, or
    # worse -- is unset: the row falls back to created, and the tracker sees
    # an unstamped chat and rewrites it. bucket() raised on these, inside
    # the GLib callback that drains frames, and the panel froze for good.
    odd = {"local": {"home": "", "sessions": [
        session("ms", A.READY, options={"last_event": 1793847000000}),
        session("huge", A.READY, options={"last_event": 10 ** 20}),
        session("text", A.READY, options={"last_event": "²"}),
        session("ok", A.READY, options={"last_event": "1700000000"})]}}
    odd = {c.session: c for c in C.build_chats(odd, projects)}
    check("an out-of-range last_event reads as unset",
          all(odd[n].last_event == 100 and not odd[n].stamped
              for n in ("ms", "huge", "text")),
          str({n: (c.last_event, c.stamped) for n, c in odd.items()}))
    check("and a digit string still reads as a stamp",
          odd["ok"].last_event == 1700000000 and odd["ok"].stamped)
    check("sections and times never raise on a stamp datetime cannot hold",
          C.bucket(1793847000000, 1_700_000_000) == "Older"
          and C.bucket(10 ** 20, 1_700_000_000) == "Older"
          and C.relative_time(-10 ** 20, 1_700_000_000) == ""
          and [label for label, _ in C.sections(
              [chat("h/x", 10 ** 20)], 1_700_000_000)] == ["Older"])

    # The hostname the probe reports is tmux's default title; on a host with
    # no plain shell it is the only way to know it.
    named = {"local": {"home": "", "hostname": "omarchy", "sessions": [
        session("work", A.NEEDS_YOU, pane_title="omarchy")]},
        "data": {"home": "", "hostnames": ("box",), "sessions": [
            session("guess", A.READY, pane_title="box")]}}
    named = {c.id: c for c in C.build_chats(named, projects)}
    check("a pane titled with the probed hostname is not a title",
          named["local/work"].title == "work", named["local/work"].title)
    check("nor one titled with the caller's extra guesses",
          named["data/guess"].title == "guess", named["data/guess"].title)


def test_sections() -> None:
    os.environ["TZ"] = "Australia/Sydney"
    time.tzset()

    # Thursday 5 November 2026, ten past midnight.
    now = at(2026, 11, 5, 0, 10)
    rows = [
        chat("h/late", at(2026, 11, 4, 23, 50)),       # Wed, 20 min ago
        chat("h/early", at(2026, 11, 5, 0, 5)),        # Thu
        chat("h/fri", at(2026, 10, 30, 12, 0)),        # 6 days back
        chat("h/thu", at(2026, 10, 29, 23, 59)),       # 7 days back
        chat("h/tue", at(2026, 11, 3, 9, 0)),
        chat("h/star", at(2026, 9, 1, 9, 0), starred=True),
        chat("h/star2", at(2026, 11, 5, 0, 9), starred=True),
    ]
    got = [(label, [c.session for c in items])
           for label, items in C.sections(rows, now, ZONE)]
    want = [("Starred", ["star2", "star"]), ("Today", ["early"]),
            ("Yesterday", ["late"]), ("Earlier this week", ["tue", "fri"]),
            ("Older", ["thu"])]
    check("sections by calendar day across midnight", got == want, str(got))
    check("the local zone is the default",
          [(label, [c.session for c in items])
           for label, items in C.sections(rows, now)] == want)
    check("a starred chat is only under Starred",
          sum(c.session == "star2" for _l, items in C.sections(rows, now, ZONE)
              for c in items) == 1)
    check("empty sections are left out",
          [label for label, _ in C.sections([rows[1]], now, ZONE)] == ["Today"])
    check("a clock running ahead is still Today",
          C.bucket(now + 3600 * 30, now, ZONE) == "Today")

    # Across the daylight-saving change (2am, 4 October 2026): calendar
    # days, not 24-hour blocks.
    dst_now = at(2026, 10, 5, 0, 30)
    check("Yesterday across the clock change",
          C.bucket(at(2026, 10, 4, 1, 30), dst_now, ZONE) == "Yesterday")
    check("and the week boundary across it",
          C.bucket(at(2026, 9, 29, 0, 1), dst_now, ZONE) == "Earlier this week"
          and C.bucket(at(2026, 9, 28, 23, 59), dst_now, ZONE) == "Older")

    # Sort: newest first; equal stamps by created, then id, never shuffled.
    tied = [chat("h/b", 50, created=1), chat("h/a", 50, created=1),
            chat("h/new", 60), chat("h/c", 50, created=9)]
    check("sorted by last_event, ties stable",
          [c.session for c in sorted(tied, key=C.sort_key)]
          == ["new", "c", "a", "b"])


def test_relative_time() -> None:
    now = at(2026, 11, 5, 10, 0)    # Thursday
    rt = lambda ts: C.relative_time(ts, now, ZONE)  # noqa: E731
    cases = [
        (now - 30, "now"), (now + 120, "now"), (now - 60, "1m"),
        (now - 14 * 60, "14m"), (now - 3 * 3600 - 59, "3h"),
        (at(2026, 11, 5, 0, 1), "9h"),
        (at(2026, 11, 4, 23, 0), "Wed"),
        (at(2026, 10, 30, 9, 0), "Fri"),
        (at(2026, 10, 29, 9, 0), "29 Oct"),
        (at(2026, 10, 12, 9, 0), "12 Oct"),
        (at(2025, 10, 12, 9, 0), "12 Oct 2025"),
    ]
    for ts, want in cases:
        check(f"relative time {want!r}", rt(ts) == want, f"got {rt(ts)!r}")
    check("minutes even across midnight",
          C.relative_time(at(2026, 11, 4, 23, 30), at(2026, 11, 5, 0, 20), ZONE)
          == "50m")


PROJECTS = [
    C.Project("atlas", "Atlas", "L", "", {"atlas": "~"}),
    C.Project("acme", "Acme", "A", "", {"acme-app": "~", "acme-dev": "~",
                                        "acme-data": "~"}),
    C.Project("personal", "Personal", "P", "", {"home-server": "~",
                                                "acme-app": "~"}),
]


def mock_chats() -> list[C.Chat]:
    # The mockup's sample, enough of it to count with.
    def c(cid, project, state, last):
        made = chat(cid, last, state=state, project=project)
        return made
    return [
        c("atlas/csv", "atlas", A.WORKING, 100),
        c("acme-app/tg", "acme", A.WORKING, 99),
        c("atlas/home", "atlas", A.NEEDS_YOU, 98),
        c("acme-app/draft", "acme", A.DRAFT, 97),
        c("acme-data/demo", "acme", A.READY, 50),
        c("home-server/disk", "personal", A.READY, 40),
        c("acme-dev/cal", "acme", A.NEEDS_YOU, 30),
    ]


def test_filters(tmp: Path) -> None:
    chats = mock_chats()
    f = C.Filters()
    check("no filter shows everything, newest first",
          [c.session for c in C.visible(chats, f)]
          == ["csv", "tg", "home", "draft", "demo", "disk", "cal"])
    f = C.pick_project(C.Filters(server="atlas", needs_you=True), "acme")
    check("picking a project resets the server, keeps needs-you",
          f == C.Filters(project="acme", server=None, needs_you=True))
    check("picking All projects clears it",
          C.pick_project(f, None).project is None)
    f = C.pick_server(C.Filters(project="acme"), "acme-app")
    check("picking a server keeps the project",
          f == C.Filters(project="acme", server="acme-app"))
    check("project and server narrow together",
          [c.session for c in C.visible(chats, f)] == ["tg", "draft"])
    check("needs-you is blocked or draft",
          [c.session for c in C.visible(chats, C.Filters(needs_you=True))]
          == ["home", "draft", "cal"])
    check("the needs-you count is scoped by project and server",
          C.needs_count(chats, C.Filters(project="acme")) == 2
          and C.needs_count(chats, C.Filters(project="acme",
                                             server="acme-dev")) == 1)
    check("but not narrowed by the toggle itself",
          C.needs_count(chats, C.Filters(project="acme", needs_you=True)) == 2
          and C.needs_count(chats, C.Filters()) == 3)
    check("search is every word, any field, any case",
          [c.session for c in C.visible(chats, C.Filters(), "ACME cal")]
          == ["cal"])
    check("search finds a project by name",
          {c.session for c in C.visible(chats, C.Filters(), "personal",
                                        PROJECTS)} == {"disk"})
    check("SHELL never reaches the list via a filter",
          all(c.state != A.SHELL for c in C.visible(chats, C.Filters())))

    # Persistence.
    saved = C.Filters(project="acme", server="acme-app", needs_you=True)
    check("filters round-trip through JSON",
          C.Filters.from_json(saved.to_json()) == saved)
    for junk in ("", "nope", "[]", '{"project": 3, "needs_you": "yes"}'):
        check(f"junk filters read as none: {junk!r}",
              C.Filters.from_json(junk) == C.Filters())
    path = tmp / "state" / "filters.json"
    C.save_filters(saved, path)
    check("saved and loaded from the state file",
          C.load_filters(path) == saved and not path.with_suffix(".tmp").exists())
    check("a missing state file is no filter",
          C.load_filters(tmp / "missing.json") == C.Filters())
    stale = C.Filters(project="gone", server="old-box", needs_you=True)
    check("filters naming what no longer exists are dropped",
          stale.valid_for(PROJECTS) == C.Filters(needs_you=True))


def test_by_project() -> None:
    ps = [C.Project(id="acme", name="Acme", letter="A", color="", servers={}),
          C.Project(id="at", name="Atlas", letter="L", color="",
                    servers={})]
    cs = [chat("a/zeta", 900, project="at"),
          chat("b/alpha", 100, project="at"),
          chat("c/mid", 500, project="at", starred=True),
          chat("d/new", 999, project="acme"),
          chat("e/lost", 50, project="gone")]
    got = [(label, [c.session for c in members])
           for label, members in C.project_sections(cs, ps)]
    check("by project: projects.toml order, starred then title, unknown last",
          got == [("Acme", ["new"]), ("Atlas", ["mid", "alpha", "zeta"]),
                  ("gone", ["lost"])])
    check("by project: time does not move a row",
          [c.session for _l, m in C.project_sections(
              [chat("b/alpha", 1, project="at"), chat("a/zeta", 2,
                                                       project="at")], ps)
           for c in m] == ["alpha", "zeta"])
    f = C.toggle_by_project(C.Filters(needs_you=True))
    check("by project survives the chips and a round trip",
          f.by_project and C.pick_project(f, "acme").by_project
          and C.pick_server(f, "x").by_project and C.toggle_needs(f).by_project
          and C.Filters.from_json(f.to_json()) == f
          and f.valid_for(PROJECTS).by_project)
    check("by project is not a filter", not C.Filters(by_project=True).active()
          and C.filter_summary(C.Filters(by_project=True), PROJECTS) == "")


def test_copy() -> None:
    P = PROJECTS
    check("summary: project and server",
          C.filter_summary(C.Filters(project="acme", server="acme-app"), P)
          == "Only Acme, acme-app")
    check("summary: needs you",
          C.filter_summary(C.Filters(needs_you=True), P) == "Only needs you")
    check("summary: nothing on",
          C.filter_summary(C.Filters(), P) == "")
    check("label: plain", C.new_chat_label(C.Filters(), P) == "New chat")
    check("label: project",
          C.new_chat_label(C.Filters(project="acme"), P) == "New chat in Acme")
    check("label: server beats project",
          C.new_chat_label(C.Filters(project="acme", server="acme-app"), P)
          == "New chat on acme-app")
    check("footer filtered", C.footer_text(3, 10, 7, True) == "3 of 10 chats")
    check("footer unfiltered",
          C.footer_text(10, 10, 7, False) == "10 chats · 7 projects")
    check("footer singulars",
          C.footer_text(1, 1, 1, False) == "1 chat · 1 project")
    check("empty on a server",
          C.empty_text(C.Filters(project="acme", server="acme-dev"), P)
          == "No chats on acme-dev yet.")
    check("empty in a project",
          C.empty_text(C.Filters(project="acme"), P) == "No chats in Acme yet.")
    check("empty waiting on you",
          C.empty_text(C.Filters(needs_you=True), P)
          == "No chats waiting on you.")
    check("empty search", C.empty_text(C.Filters(), P, " zzz ")
          == "No chats match “zzz”.")
    check("fleet summary", C.fleet_summary(10, 3) == "10 up · 3 dark"
          and C.fleet_summary(10, 0) == "10 up")

    items = C.project_menu(C.Filters(project="acme"), P)
    check("project menu: All first, with the distinct server count",
          (items[0].name, items[0].meta) == ("All projects", "5 servers"))
    check("project menu: server counts and the pick",
          [(i.name, i.meta, i.selected) for i in items[1:]]
          == [("Atlas", "1 server", False), ("Acme", "3 servers", True),
              ("Personal", "2 servers", False)])
    header, items = C.server_menu(C.Filters(project="acme"), P, mock_chats(),
                                  dark={"acme-data"})
    check("server menu: only the project's servers, under its name",
          header == "Servers in Acme"
          and [i.name for i in items]
          == ["All servers", "acme-app", "acme-dev", "acme-data"])
    check("server menu: counts, dark",
          [i.meta for i in items]
          == ["3 servers", "2 chats", "1 chat", "dark"])
    header, items = C.server_menu(C.Filters(), P, [])
    check("server menu: a server in two projects is listed once",
          header == "Servers" and [i.name for i in items].count("acme-app") == 1
          and items[1].meta == "no chats")


def test_recency() -> None:
    t = C.RecencyTracker()
    now = 1_000_000
    check("first sight, never stamped: stamped with created, not now",
          t.observe("h/a", A.READY, None, 500, now) == 500)
    check("and not again while the write is landing",
          t.observe("h/a", A.READY, None, 500, now + 1) is None)
    check("first sight already stamped: nothing",
          t.observe("h/b", A.WORKING, 900, 500, now) is None)
    check("first sight with no created yet: wait, never now",
          t.observe("h/c", A.READY, None, 0, now) is None
          and t.observe("h/c", A.READY, None, 400, now + 1) == 400)

    t = C.RecencyTracker()
    t.observe("h/a", A.READY, 10, 5, now)
    check("idle -> working stamps now",
          t.observe("h/a", A.WORKING, 10, 5, now + 1) == now + 1)
    check("the spinner turning is nothing",
          t.observe("h/a", A.WORKING, now + 1, 5, now + 2) is None)
    check("working -> needs you stamps now",
          t.observe("h/a", A.NEEDS_YOU, now + 1, 5, now + 3) == now + 3)
    check("needs you -> idle stamps now",
          t.observe("h/a", A.READY, now + 3, 5, now + 4) == now + 4)
    check("idle -> needs you stamps now",
          t.observe("h/a", A.NEEDS_YOU, now + 4, 5, now + 5) == now + 5)
    check("needs you -> working stamps now",
          t.observe("h/a", A.WORKING, now + 5, 5, now + 6) == now + 6)
    check("working -> idle stamps now",
          t.observe("h/a", A.READY, now + 6, 5, now + 7) == now + 7)

    # A draft is idle with text in the box: typing is not an event.
    check("idle -> draft is nothing",
          t.observe("h/a", A.DRAFT, now + 7, 5, now + 8) is None)
    check("draft -> idle is nothing",
          t.observe("h/a", A.READY, now + 7, 5, now + 9) is None)
    check("draft -> working is a real start",
          t.observe("h/a", A.DRAFT, now + 7, 5, now + 10) is None
          and t.observe("h/a", A.WORKING, now + 7, 5, now + 11) == now + 11)

    # UNKNOWN is missing information, never a state.
    check("working -> unknown is nothing",
          t.observe("h/a", A.UNKNOWN, now + 11, 5, now + 12) is None)
    check("unknown -> the same working is nothing",
          t.observe("h/a", A.WORKING, now + 11, 5, now + 13) is None)
    t.observe("h/a", A.UNKNOWN, now + 11, 5, now + 14)
    check("unknown -> a different phase did change",
          t.observe("h/a", A.NEEDS_YOU, now + 11, 5, now + 15) == now + 15)
    check("unknown at first sight records no phase",
          t.observe("h/u", A.UNKNOWN, 7, 5, now) is None
          and t.observe("h/u", A.WORKING, 7, 5, now + 1) is None)
    check("a shell is never stamped for its state",
          t.observe("h/s", A.SHELL, 7, 5, now) is None
          and t.observe("h/s", A.SHELL, 7, 5, now + 1) is None)

    # Batches and forgetting.
    t = C.RecencyTracker()
    due = t.update([("h/a", A.READY, None, 50), ("h/b", A.READY, 60, 50)], now)
    check("update returns only the stamps due", due == {"h/a": 50})
    due = t.update_chats([
        C.Chat(id="h/a", host="h", session="a", project="h", title="a",
               state=A.WORKING, last_event=50, created=50, stamped=True),
        C.Chat(id="h/b", host="h", session="b", project="h", title="b",
               state=A.READY, last_event=60, created=50, stamped=True),
    ], now + 1)
    check("update_chats reads build_chats output", due == {"h/a": now + 1})
    t.retain(["h/b"])
    # h/a was last seen working; remembered, idle now would be a transition.
    check("a recreated session is a first sighting again",
          t.observe("h/a", A.READY, None, 70, now + 2) == 70)
    t.retain(["h/b"])
    check("and its old phase is gone",
          t.observe("h/b", A.WORKING, 60, 50, now + 3) == now + 3
          and t.observe("h/a", A.READY, 70, 70, now + 4) is None)

    # An agent started in a session conn saw as a plain shell is a chat
    # starting now -- not a first sighting, whose stamp is created.
    t = C.RecencyTracker()
    old = now - 3 * 86400
    t.saw_shells(["h/w"])
    check("shell -> agent, never stamped: stamped now, not created",
          t.observe("h/w", A.WORKING, None, old, now) == now)
    check("and not again on the next frame",
          t.observe("h/w", A.WORKING, now, old, now + 1) is None)
    t.saw_shells(["h/w"])           # claude quit; the option stays set
    check("restarted in a session with an old stamp: stamped now",
          t.observe("h/w", A.READY, now - 500, old, now + 10) == now + 10)
    t.saw_shells(["h/v"])
    check("shell -> UNKNOWN waits for a known phase",
          t.observe("h/v", A.UNKNOWN, None, old, now) is None
          and t.observe("h/v", A.READY, None, old, now + 2) == now + 2)
    t.saw_shells(["h/z"])
    t.retain(["h/w"])
    check("a shell that has gone is forgotten",
          t.observe("h/z", A.READY, None, old, now) == old)


def test_names_and_moves() -> None:
    rows = {"h": {"home": "/home/u", "sessions": [
        {"name": "a", "created": 100, "activity": 900, "path": "/home/u",
         "pane_title": "✳ Claude's title",
         "options": {"name": "Mine", "project": "", "last_event": None},
         "agent": {"state": A.READY}},
    ]}}
    projects = C.load_projects(Path("/nonexistent"), hosts=["h"]) + [
        C.Project("acme", "Acme", "A", "", {"h": "~/elsewhere"})]
    built = C.build_chats(rows, projects)[0]
    check("a chat carries its raw @conn_name and @conn_project",
          built.title == "Mine" and built.name == "Mine"
          and built.project_option == "" and built.activity == 900)
    over = C.build_chats(rows, projects,
                         overrides={"h/a": {"name": None, "project": "acme"}})[0]
    check("an override clears the name and moves the chat at once",
          over.title == "Claude's title" and over.name == ""
          and over.project == "acme" and over.project_option == "acme")
    check("overrides never touch the rows they were laid over",
          rows["h"]["sessions"][0]["options"]["name"] == "Mine")

    t = C.RecencyTracker()
    now = 1_000_000
    check("first sight takes session_activity when it is newer than created",
          t.observe("h/x", A.READY, None, 500, now, activity=9000) == 9000)
    check("but never later than now",
          C.RecencyTracker().observe("h/y", A.READY, None, 500, now,
                                     activity=now + 99) == now)
    check("a stamp equal to session_created is repaired once, on first sight",
          t.observe("h/r", A.READY, 500, 500, now, activity=7000) == 7000
          and t.observe("h/r", A.READY, 500, 500, now + 1, 7000) is None)
    check("a stamp equal to created with no activity since is left alone",
          C.RecencyTracker().observe("h/q", A.READY, 500, 500, now, 400)
          is None)
    # A polled server: a whole turn runs between two looks, idle both times.
    t = C.RecencyTracker()
    t.observe("h/p", A.READY, 100, 50, now, 100, "Worked for 8s\nFixed the test")
    check("idle -> idle with a new finished turn stamps its last output",
          t.observe("h/p", A.READY, 100, 50, now + 300, now + 120,
                    "Brewed for 24s\nRenamed it") == now + 120)
    check("and not again while the stamp is landing",
          t.observe("h/p", A.READY, 100, 50, now + 600, now + 120,
                    "Brewed for 24s\nRenamed it") is None)
    check("a different turn with no output since the stamp is nothing",
          t.observe("h/p", A.READY, now + 120, 50, now + 900, now + 120,
                    "Brewed for 24s\nRenamed it, and the docs") is None)
    check("typing a draft (same turn, new output) is nothing",
          t.observe("h/p", A.DRAFT, now + 120, 50, now + 1000, now + 990,
                    "Brewed for 24s\nRenamed it, and the docs") is None)
    check("a draft that was sent and answered is a turn",
          t.observe("h/p", A.READY, now + 120, 50, now + 1300, now + 1250,
                    "Worked for 5s\nDone") == now + 1250)
    check("never later than now",
          t.observe("h/p", A.READY, now + 1250, 50, now + 1400, now + 1500,
                    "Worked for 9s\nAgain") == now + 1400)
    check("a frame with no turn on screen does not make the next look new",
          t.observe("h/p", A.READY, now + 1400, 50, now + 1500, now + 1450,
                    "") is None
          and t.observe("h/p", A.READY, now + 1400, 50, now + 1600, now + 1450,
                        "Worked for 9s\nAgain") is None)
    t2 = C.RecencyTracker()
    check("first sight records the turn, stamps nothing new",
          t2.observe("h/f", A.READY, 100, 50, now, now, "x") is None)
    built = C.build_chats({"h": {"sessions": [
        {"name": "p", "created": 50, "activity": 60, "options": {},
         "agent": {"state": A.READY, "detail": "Worked for 8s",
                   "preview": "Fixed the test"}}]}},
        C.load_projects(Path("/nonexistent"), hosts=["h"]))[0]
    check("a chat carries its last turn from the agent reading",
          built.turn == "Worked for 8s\nFixed the test")

    t.observe("h/m", A.READY, 10, 5, now)
    t.rename("h/m", "h/n")
    check("a renamed session keeps its phase: not a first sighting",
          t.observe("h/n", A.READY, 10, 5, now + 1) is None
          and t.observe("h/n", A.WORKING, 10, 5, now + 2) == now + 2)


def test_finished() -> None:
    f = C.FinishTracker()
    check("idle on first sight is not a finish",
          not f.observe("h/a", A.READY) and not f.finished("h/a"))
    check("working -> ready is a finish, reported once",
          not f.observe("h/b", A.WORKING) and f.observe("h/b", A.READY)
          and not f.observe("h/b", A.READY) and f.finished("h/b"))
    check("through UNKNOWN still counts",
          not f.observe("h/c", A.WORKING) and not f.observe("h/c", A.UNKNOWN)
          and f.observe("h/c", A.READY))
    check("working -> draft is a finish too (a ghost suggestion)",
          not f.observe("h/d", A.WORKING) and f.observe("h/d", A.DRAFT))
    f.observe("h/b", A.DRAFT)
    f.observe("h/b", A.READY)
    check("typing and clearing a draft does not end a finish",
          f.finished("h/b"))
    f.observe("h/b", A.WORKING)
    check("working again ends it",
          not f.finished("h/b"))
    f.observe("h/b", A.READY)
    f.observe("h/b", A.NEEDS_YOU)
    check("and so does blocking", not f.finished("h/b"))

    seen: dict[str, int] = {}
    c = chat("h/c", 2000)
    check("finished and never opened is unseen",
          C.unseen_finished(c, f, seen))
    seen["h/c"] = 2000
    check("opened at that stamp, it is seen",
          not C.unseen_finished(c, f, seen))
    f.observe("h/c", A.WORKING)
    f.observe("h/c", A.READY)
    check("a later finish with a newer stamp is unseen again",
          C.unseen_finished(chat("h/c", 2500), f, seen))
    check("a draft is needs you on its own, not through this",
          not C.unseen_finished(chat("h/d", 3000, state=A.DRAFT), f, {}))
    marked = [chat("h/c", 2500), chat("h/a", 2500)]
    C.mark_finished(marked, f, seen)
    check("mark_finished sets the flag and is_needs_you follows it",
          marked[0].finished and C.is_needs_you(marked[0])
          and not marked[1].finished and not C.is_needs_you(marked[1]))
    check("the needs-you count and filter count a finished chat",
          C.needs_count(marked, C.Filters()) == 1
          and [x.id for x in C.visible(marked, C.Filters(needs_you=True))]
          == ["h/c"])
    f.rename("h/c", "h/c2")
    check("a renamed chat keeps its finish", f.finished("h/c2")
          and not f.finished("h/c"))
    f.retain(["h/a"])
    check("retain forgets the rest", not f.finished("h/c2"))


def test_seen_book(tmp: Path) -> None:
    path = tmp / "state" / "seen.json"
    check("a missing seen book is empty", C.load_seen(path) == {})
    C.save_seen({"h/a": 10, "h/b": 20}, path)
    check("the seen book round-trips", C.load_seen(path) ==
          {"h/a": 10, "h/b": 20})
    path.write_text('{"h/a": "x", "h/b": 5, "h/c": true}')
    check("junk values are dropped, good ones kept",
          C.load_seen(path) == {"h/b": 5})
    path.write_text("not json")
    check("an unreadable book is empty", C.load_seen(path) == {})
    check("no temp file is left behind",
          not (tmp / "state" / "seen.tmp").exists())


def test_project_file(tmp: Path) -> None:
    import tomllib
    data = {
        "stray": "top-level value",
        "acme": {"name": "Acme \"quoted\"\nline", "letter": "A",
                 "color": "#BFB0FF",
                 "servers": {"acme-app": "~/acme", "odd.host": "/srv/x"},
                 "agent": "claude", "mode": "auto",
                 "unknown": ["a", 1, True], "nested": {"k": "v"}},
        "café-ünï": {"name": "Ünïcode ☕", "servers": {"h": "~"}},
        "tabs": {"name": "a\tb\x01c", "servers": {}},
    }
    text = C.dump_projects(data)
    check("dump_projects round-trips through tomllib",
          tomllib.loads(text) == data, text)
    check("project keys are written in the SPEC's order",
          text.index("name =") < text.index("letter =")
          < text.index("servers =") < text.index("agent =")
          < text.index("unknown ="))
    raised = ""
    try:
        C.dump_projects({"p": {"name": "x", "when": object()}})
    except ValueError as exc:
        raised = str(exc)
    check("a value it cannot write is refused, never dropped",
          "when in p" in raised, raised)

    path = tmp / "cfg" / "projects.toml"
    check("writing a first file keeps no backup",
          C.write_projects(path, {"a": {"name": "A", "servers": {"h": "~"}}})
          is None and C.read_project_tables(path)["a"]["name"] == "A")
    backup = C.write_projects(path, data)
    check("writing over one keeps the old file as projects.toml.bak",
          backup is not None and backup.name == "projects.toml.bak"
          and "\"A\"" in backup.read_text()
          and C.read_project_tables(path) == data)
    before = path.read_text()
    try:
        C.write_projects(path, {"p": {"name": "x", "when": object()}})
    except ValueError:
        pass
    check("a failed write leaves the file untouched",
          path.read_text() == before
          and not (tmp / "cfg" / "projects.toml.tmp").exists())
    check("a missing file reads as no tables",
          C.read_project_tables(tmp / "none.toml") == {})
    check("and the projects it wrote load as projects",
          [p.id for p in C.read_projects(path)] == ["acme", "café-ünï",
                                                    "tabs"])

    good = {"acme": {"name": "Acme", "servers": {"h": "~/t"}}}
    check("a sound project has no errors", C.project_errors(good) == [])
    errors = C.project_errors({
        "Bad Id": {"name": "X", "servers": {"h": "~"}},
        "noname": {"name": " ", "servers": {"h": "~"}},
        "letters": {"name": "L", "letter": "LL", "servers": {"h": "~"}},
        "colour": {"name": "C", "color": "blue", "servers": {"h": "~"}},
        "noservers": {"name": "N", "servers": {}},
        "folder": {"name": "F", "servers": {"h": "relative/path"}},
        "dupe": {"name": "acme", "servers": {"h": "~"}},
        "dupe2": {"name": "Acme", "servers": {"h": "~"}},
    })
    joined = " | ".join(errors)
    check("ids, names, letters, colours, servers and folders are checked",
          "Bad Id" in joined and "noname needs a name" in joined
          and "letter" in joined and "#RRGGBB" in joined
          and "at least one server" in joined and "~ or /" in joined
          and "two projects are called" in joined, joined)
    check("slug makes an id from a name",
          C.slug("North Wind!") == "north-wind" and C.slug("??") == "project")

    sessions = [{"path": "/home/u/acme"}, {"path": "/home/u/acme/"},
                {"path": "/srv/app"}, {"path": "/home/u"}, {"path": ""}]
    check("folder suggestions: most used first, home as ~, ~ always there",
          C.folder_suggestions(sessions, "/home/u")
          == ["~/acme", "/srv/app", "~"]
          and C.folder_suggestions([], "") == ["~"])


def test_known(tmp: Path) -> None:
    """The last-known book: a dark server's chats stay listed, offline."""
    path = tmp / "projects.toml"
    path.write_text(TOML)
    projects = C.load_projects(path, hosts=["acme-app"])
    now = at(2026, 11, 7, 12, 0)
    live = {"state": "up", "home": "/home/app", "hostname": "app-box",
            "sessions": [
                session("tg", A.NEEDS_YOU, pane_title="✳ Telegram",
                        path="/home/app/acme", preview="Wants to run rm",
                        options={"last_event": now - 600, "starred": True,
                                 "task": "the secret prompt"}),
                session("shell", A.SHELL, pane_title="app-box"),
                session("box", A.READY, pane_title="app-box")]}
    live["sessions"][0]["screen"] = "the whole screen"
    known: dict = {}
    check("an answering host is booked", C.remember(known, "acme-app", live, now)
          and set(known) == {"acme-app"})
    entry = known["acme-app"]
    kept = {s["name"]: s for s in entry["sessions"]}
    check("plain shells are not booked", set(kept) == {"tg", "box"}, str(kept))
    check("neither screen, preview nor the task is kept",
          "screen" not in kept["tg"] and "preview" not in kept["tg"]["agent"]
          and "task" not in kept["tg"]["options"], str(kept["tg"]))
    check("the same rows again is not a change",
          not C.remember(known, "acme-app", live, now + 30)
          and known["acme-app"]["seen"] == now + 30)
    live["sessions"][0]["activity"] = now
    check("nor is activity alone",
          not C.remember(known, "acme-app", live, now + 30))
    # While Claude Code works it keeps a spinner at the front of its title,
    # a new frame each second the stream speaks: not a reason to write.
    busy = {"state": "up", "home": "/home/app", "sessions": [
        session("fix", A.WORKING, pane_title="⠂ Fix bug")]}
    spun: dict = {}
    C.remember(spun, "h", busy, now)
    turns = []
    for glyph in ("⠐", "⠂", "⠐"):
        busy["sessions"][0]["pane_title"] = f"{glyph} Fix bug"
        turns.append(C.remember(spun, "h", busy, now))
    check("the title's spinner turning is not a change",
          turns == [False, False, False], str(turns))
    check("but the title is still kept as it last was",
          spun["h"]["sessions"][0]["pane_title"] == "⠐ Fix bug")
    busy["sessions"][0]["agent"]["state"] = A.READY
    check("nor is working flapping to ready",
          not C.remember(spun, "h", busy, now)
          and spun["h"]["sessions"][0]["agent"]["state"] == A.READY)
    busy["sessions"][0]["pane_title"] = "✳ Fix the other bug"
    check("a new title is",
          C.remember(spun, "h", busy, now))
    busy["sessions"][0]["options"]["starred"] = True
    check("and so is a new star", C.remember(spun, "h", busy, now))

    C.save_known(known, tmp / "known.json")
    check("it round-trips through the file",
          C.load_known(tmp / "known.json") == known)
    # Valid JSON with the wrong types in it -- a hand edit, a half-bad
    # copy -- costs the sessions that have them, never the chat list.
    good = {"name": "good", "options": {"title": "Good", "starred": True,
                                        "last_event": now - 60},
            "created": now - 900, "pane_title": "✳ Good", "path": "/srv",
            "agent": {"state": "ready"}}
    bad = [{"name": "c1", "created": "abc"},
           {"name": "c2", "options": {"name": 5}},
           {"name": "c3", "pane_title": 5},
           {"name": "c4", "path": ["x"]},
           {"name": "c5", "activity": "soon"},
           {"name": "c6", "agent": {"state": 3}},
           {"name": "c7", "options": {"title": {"nested": 1}}},
           {"name": "c8", "options": {"project": None}},
           {"name": "c9", "created": True}]
    (tmp / "typed.json").write_text(json.dumps(
        {"version": 1, "hosts": {"h": {"seen": now, "sessions": bad + [good]},
                                 "all-bad": {"seen": now, "sessions": bad}}}))
    typed = C.load_known(tmp / "typed.json")
    check("a stored session with a field of the wrong type is dropped",
          set(typed) == {"h"}
          and [s["name"] for s in typed["h"]["sessions"]] == ["good"],
          str(typed))
    try:
        built = C.build_chats({"h": {"state": "down", "sessions": []},
                               "all-bad": {"state": "down", "sessions": []}},
                              projects, known=typed)
        crashed = ""
    except Exception as exc:  # noqa: BLE001 -- the failure being tested for
        built, crashed = [], repr(exc)
    check("and what is left builds without raising",
          not crashed and [c.id for c in built] == ["h/good"]
          and built[0].title == "Good" and built[0].offline, crashed)
    (tmp / "bad.json").write_text("{not json")
    (tmp / "odd.json").write_text('{"hosts": {"h": {"sessions": [1, {}]}}}')
    check("a missing, corrupt or odd file is an empty book",
          C.load_known(tmp / "none.json") == {}
          and C.load_known(tmp / "bad.json") == {}
          and C.load_known(tmp / "odd.json") == {})

    down = {"state": "down", "home": "", "sessions": []}
    built = {c.id: c for c in C.build_chats({"acme-app": down}, projects,
                                            known=known)}
    tg = built.get("acme-app/tg")
    check("a dark host's chats come back offline",
          set(built) == {"acme-app/tg", "acme-app/box"}
          and all(c.offline and c.seen == now + 30 for c in built.values()),
          str(built))
    check("with their title, project, star and place, but no state",
          tg is not None and tg.title == "Telegram" and tg.project == "acme"
          and tg.starred and tg.last_event == now - 600
          and tg.state == A.UNKNOWN and tg.last_state == A.NEEDS_YOU
          and tg.preview == "" and tg.task == "")
    check("the booked hostname still is not a title",
          built["acme-app/box"].title == "box")
    check("and an offline chat is never needs you",
          not C.is_needs_you(tg)
          and C.needs_count(list(built.values()), C.Filters()) == 0
          and not C.visible(list(built.values()), C.Filters(needs_you=True)))
    tracker = C.FinishTracker()
    tracker.observe(tg.id, A.WORKING)
    tracker.observe(tg.id, A.READY)
    tg.state = A.READY
    check("not even one this client watched finish",
          not C.unseen_finished(tg, tracker, {}))

    partial = {"state": "down", "sessions": [session("tg", A.READY)]}
    built = C.build_chats({"acme-app": partial}, projects, known=known)
    check("a chat the row lists is not built twice",
          sorted(c.id for c in built) == ["acme-app/box", "acme-app/tg"]
          and not next(c for c in built if c.id == "acme-app/tg").offline)
    back = {"state": "up", "home": "/home/app",
            "sessions": [session("tg", A.READY)]}
    built = C.build_chats({"acme-app": back}, projects, known=known)
    check("an answering host's live sessions are the whole truth",
          [c.id for c in built] == ["acme-app/tg"] and not built[0].offline)
    check("and booking it replaces what was kept",
          C.remember(known, "acme-app", back, now + 60)
          and [s["name"] for s in known["acme-app"]["sessions"]] == ["tg"])
    check("a host with no chats left leaves the book",
          C.remember(known, "acme-app", {"state": "up", "sessions": []}, now)
          and known == {})

    check("the second line says unreachable and since when",
          C.offline_text(now - 3 * 3600, now, "down", ZONE)
          == "unreachable · last seen 3h ago"
          and C.offline_text(now - 30, now, "down", ZONE)
          == "unreachable · last seen just now"
          and C.offline_text(now - 600, now, "unknown", ZONE)
          == "connecting · last seen 10m ago"
          and C.offline_text(0, now, "down", ZONE) == "unreachable",
          C.offline_text(now - 3 * 3600, now, "down", ZONE))
    check("a server that answers and turns you away says why, not "
          "unreachable",
          C.offline_text(now - 30, now, "nokey", ZONE)
          == "no key · last seen just now"
          and C.offline_text(now - 30, now, "unverified", ZONE)
          == "log in · last seen just now",
          C.offline_text(now - 30, now, "nokey", ZONE))
    check("the page and the banner say last seen the same way",
          C.seen_text(now - 14 * 60, now, ZONE) == "last seen 14m ago"
          and C.seen_text(0, now, ZONE) == "",
          C.seen_text(now - 14 * 60, now, ZONE))


def main() -> int:
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        test_titles()
        test_projects(tmp)
        test_build(tmp)
        test_sections()
        test_relative_time()
        test_filters(tmp)
        test_by_project()
        test_copy()
        test_recency()
        test_names_and_moves()
        test_finished()
        test_seen_book(tmp)
        test_known(tmp)
        test_project_file(tmp)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
