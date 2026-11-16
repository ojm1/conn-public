#!/usr/bin/env python3
"""conn-agent A0, checked without a network, a server or ntfy.

    python3 tests/test_agent.py
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_state as A  # noqa: E402
import chats as C  # noqa: E402
import conn_agent as G  # noqa: E402

failures = 0
total = 0


def check(label: str, passed: bool, detail: str = "") -> None:
    global failures, total
    total += 1
    failures += not passed
    print(f"{'ok  ' if passed else 'FAIL'} {label}"
          + (f"\n     {detail}" if detail and not passed else ""))


def chat(cid: str, state: str, title: str = "Homepage") -> C.Chat:
    host, _, session = cid.partition("/")
    return C.Chat(id=cid, host=host, session=session, project=host,
                  title=title, state=state, last_event=1)


def test_alerts() -> None:
    a = G.Alerter()
    check("first sight sends nothing, even a chat already waiting",
          a.update([chat("h/a", A.READY), chat("h/b", A.NEEDS_YOU)],
                   set()) == [])
    check("starting work sends nothing",
          a.update([chat("h/a", A.WORKING), chat("h/b", A.NEEDS_YOU)],
                   set()) == [])
    check("the spinner turning sends nothing",
          a.update([chat("h/a", A.WORKING)], set()) == [])
    pings = a.update([chat("h/a", A.READY)], set())
    check("a finished turn sends one ping",
          [(p.chat_id, p.reason, p.message) for p in pings]
          == [("h/a", "finished", G.FINISHED)], repr(pings))
    check("and not again while it stays idle",
          a.update([chat("h/a", A.READY)], set()) == [])
    check("a draft being typed is not a second finish",
          a.update([chat("h/a", A.DRAFT)], set()) == []
          and a.update([chat("h/a", A.READY)], set()) == [])

    a.update([chat("h/a", A.WORKING)], set())
    pings = a.update([chat("h/a", A.NEEDS_YOU)], {"h/a"})
    check("a permission prompt says so",
          [(p.reason, p.message) for p in pings]
          == [("permission", G.PERMISSION)], repr(pings))
    check("and is sent once",
          a.update([chat("h/a", A.NEEDS_YOU)], {"h/a"}) == [])
    check("a flicker to unknown and back is not a new prompt",
          a.update([chat("h/a", A.UNKNOWN)], set()) == []
          and a.update([chat("h/a", A.NEEDS_YOU)], {"h/a"}) == [])
    a.update([chat("h/a", A.WORKING)], set())
    pings = a.update([chat("h/a", A.NEEDS_YOU)], set())
    check("a question that is not a permission prompt",
          [(p.reason, p.message) for p in pings]
          == [("asking", G.ASKING)], repr(pings))
    check("the next finish after a prompt is news again",
          [p.reason for p in a.update([chat("h/a", A.WORKING)], set())
           + a.update([chat("h/a", A.READY)], set())] == ["finished"])

    q = G.Alerter()
    q.update([chat("h/a", A.WORKING)], set())
    check("nothing is sent while you typed into a terminal just now",
          q.update([chat("h/a", A.READY)], set(), typed_ago=30) == [])
    q.update([chat("h/a", A.WORKING)], set())
    check("but is once you have been away a while",
          len(q.update([chat("h/a", A.READY)], set(), typed_ago=600)) == 1)
    q.update([chat("h/a", A.WORKING)], set())
    check("or nobody is at a terminal at all",
          len(q.update([chat("h/a", A.READY)], set(), typed_ago=None)) == 1)
    loud = G.Alerter(quiet_after_typing=0)
    loud.update([chat("h/a", A.WORKING)], set())
    check("and always, with quiet_after_typing = 0",
          len(loud.update([chat("h/a", A.READY)], set(), typed_ago=1)) == 1)

    # Typed the prompt, shut the lid: held while it reads as typing, sent
    # after -- the laptop's client stays frozen at your last key.
    h = G.Alerter()
    h.update([chat("h/a", A.WORKING)], set(), 5, now=0)
    check("a finish while you are typing is held",
          h.update([chat("h/a", A.READY)], set(), 90, now=90) == [])
    check("and still held while the frozen client reads as recent",
          h.update([chat("h/a", A.READY)], set(), 110, now=110) == [])
    pings = h.update([chat("h/a", A.READY)], set(), 200, now=200)
    check("then sent once you have been away",
          [p.reason for p in pings] == ["finished"], repr(pings))
    check("once", h.update([chat("h/a", A.READY)], set(), None, now=300) == [])
    h.update([chat("h/a", A.WORKING)], set(), 5, now=400)
    h.update([chat("h/a", A.READY)], set(), 10, now=410)
    h.update([chat("h/a", A.WORKING)], set(), 20, now=420)
    check("a held finish is dropped when the chat works again",
          h.update([chat("h/a", A.WORKING)], set(), None, now=430) == [])
    h.update([chat("h/a", A.READY)], set(), 10, now=440)
    check("and one held past HOLD_MAX is dropped: you were there",
          h.update([chat("h/a", A.READY)], set(), None,
                   now=440 + G.Alerter.HOLD_MAX + 1) == [])
    h.update([chat("h/a", A.WORKING)], set(), None, now=2000)
    h.update([chat("h/a", A.NEEDS_YOU)], {"h/a"}, 10, now=2010)
    check("a held prompt goes out while it is still asking",
          [p.reason for p in h.update([chat("h/a", A.NEEDS_YOU)], {"h/a"},
                                      None, now=2200)] == ["permission"])

    n = G.Alerter()
    n.update([chat("h/a", A.READY)], set(), None, now=0, up={"h"})
    pings = n.update([chat("h/a", A.READY), chat("h/new", A.NEEDS_YOU)],
                     set(), None, now=4, up={"h"})
    check("a chat that turns up waiting on a server already watched is news",
          [(p.chat_id, p.reason) for p in pings] == [("h/new", "asking")],
          repr(pings))
    pings = n.update([chat("g/old", A.NEEDS_YOU)], set(), None, now=8,
                     up={"h", "g"})
    check("but one already waiting on a server first seen now is not",
          pings == [], repr(pings))

    r = G.Alerter()
    r.update([chat("h/a", A.WORKING)], set())
    r.retain([])
    check("a session that went is forgotten: back, idle, is first sight",
          r.update([chat("h/a", A.READY)], set()) == [])


def test_titles() -> None:
    check("a title is one line with no control characters",
          G._title("Fix\x1b]0;evil\x07 the\nthing") == "Fix the thing",
          repr(G._title("Fix\x1b]0;evil\x07 the\nthing")))
    long = G._title("x" * 200)
    check("and capped", len(long) == G.TITLE_CAP and long.endswith("…"))


def test_digest() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_agent_state import CLAUDE
    screen = CLAUDE["boxed prompt"]
    edit = CLAUDE["permission"]
    rows = {
        "h": {"state": "up", "home": "/home/u", "hostname": "h",
              "typed_ago": 400, "sessions": [
            {"name": "a", "attached": True, "created": 5, "options": {},
             "screen": "", "agent": {"state": A.READY}},
            {"name": "b", "attached": False, "created": 5, "options": {},
             "screen": screen, "agent": {"state": A.NEEDS_YOU}},
            {"name": "e", "attached": False, "created": 5, "options": {},
             "screen": edit, "agent": A.classify(edit, ["claude"])},
            {"name": "q", "attached": False, "created": 5, "options": {},
             "screen": "", "agent": {"state": A.NEEDS_YOU,
                                     "detail": "Which colour do you want?"}},
            {"name": "sh", "attached": False, "created": 5, "options": {},
             "screen": "$ ", "agent": {"state": A.SHELL}},
        ]},
        "g": {"state": "up", "home": "/home/u", "hostname": "g",
              "typed_ago": 50, "sessions": []},
        "n": {"state": "up", "home": "/home/u", "hostname": "n",
              "typed_ago": None, "sessions": []},
        "dark": {"state": "down", "typed_ago": 1, "sessions": []},
    }
    live, permission, typed_ago, up = G.digest(
        rows, C.load_projects(Path("/nonexistent"), hosts=list(rows)))
    check("digest: chats only, plain shells left out",
          sorted(c.id for c in live) == ["h/a", "h/b", "h/e", "h/q"])
    check("digest: the most recent typing on any answering server",
          typed_ago == 50, repr(typed_ago))
    check("digest: permission prompts, run or edit, told from a question",
          permission == {"h/b", "h/e"}, repr(permission))
    check("digest: only answering servers are up", up == {"h", "g", "n"})


def test_config() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "conf" / "agent.toml"
        config = G.load_config(path)
        check("a first run writes the defaults, ntfy off",
              path.exists() and config["ntfy_topic"] == ""
              and config["port"] == 8742)
        check("in a file only you can read",
              stat.S_IMODE(path.stat().st_mode) == 0o600)
        topic = "conn-" + "x" * 30
        path.write_text('ntfy_topic = "short"\n')
        try:
            G.load_config(path)
            refused = False
        except SystemExit:
            refused = True
        check("a short, guessable topic is refused", refused)
        path.write_text(f'ntfy_topic = "{topic}"\ninterval = 0.1\n')
        check("an interval is never faster than 2s",
              G.load_config(path)["interval"] == 2.0)
        path.write_text(f'ntfy_topic = "{topic}"\nntfy_server = "ntfy.sh"\n')
        try:
            G.load_config(path)
            refused = False
        except SystemExit:
            refused = True
        check("a server without https:// is refused", refused)
        check("and a new file says how long it stays quiet",
              G.load_config(Path(tmp) / "new.toml")["quiet_after_typing"]
              == 120)


def test_send() -> None:
    sent = []

    class Answer:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake(request, timeout):
        sent.append((request.full_url, json.loads(request.data),
                     dict(request.headers), timeout))
        return Answer()

    real = G.urllib.request.urlopen
    G.urllib.request.urlopen = fake
    try:
        G.send({"ntfy_server": "https://ntfy.example/", "ntfy_topic": "t" * 30},
               G.Ping("h/a", "Café ✳ build", G.FINISHED, "finished"))
    finally:
        G.urllib.request.urlopen = real
    url, body, headers, timeout = sent[0]
    check("sent as JSON to the server root", url == "https://ntfy.example/")
    check("with the title in any script, and only title and phrase",
          body["title"] == "Café ✳ build" and body["message"] == G.FINISHED
          and set(body) == {"topic", "title", "message", "priority", "tags"},
          repr(body))
    check("and a timeout", timeout == G.SEND_TIMEOUT)


def test_round_guard() -> None:
    calls = []
    real = G.one_round

    def boom(*args):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("a bad answer")
        G_stop.set()
        return set()

    import threading
    G_stop = threading.Event()
    G.one_round = boom
    real_event = G.threading.Event
    G.threading.Event = lambda: G_stop
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CONN_LOG"] = str(Path(tmp) / "agent.log")
            real_path = G._log_path
            G._log_path = lambda: Path(tmp) / "agent.log"
            try:
                G.run({"ntfy_topic": "t" * 30, "ntfy_server": "https://x",
                       "interval": 2.0, "quiet_after_typing": 120.0,
                       "port": 0})
            finally:
                G._log_path = real_path
            logged = (Path(tmp) / "agent.log").read_text()
    finally:
        G.one_round = real
        G.threading.Event = real_event
    check("a round that raises is logged and the loop goes on",
          len(calls) == 2 and "round failed: ValueError" in logged, logged)


def test_backoff() -> None:
    a = G.Agent({}, G.Alerter())
    hl = ["good", "bad"]
    a.settle({"good": {"state": "up"}, "bad": {"state": "nokey"}}, 0.0)
    check("a refused host waits a minute", a.due(hl, 30.0) == ["good"]
          and a.due(hl, 60.0) == hl)
    a.settle({"bad": {"state": "nokey"}}, 60.0)
    check("then two", a.due(hl, 150.0) == ["good"] and a.due(hl, 180.0) == hl)
    for _ in range(20):
        a.settle({"bad": {"state": "unverified"}}, 0.0)
    check("capped at half an hour", a.backoff["bad"][1] == G.BACKOFF_MAX)
    a.settle({"bad": {"state": "unknown"}}, 0.0)
    check("an overrun probe does not clear it", "bad" in a.backoff)
    a.settle({"bad": {"state": "up"}}, 0.0)
    check("an answer clears it", "bad" not in a.backoff
          and a.due(hl, 0.0) == hl)


def main() -> int:
    test_alerts()
    test_titles()
    test_digest()
    test_config()
    test_send()
    test_round_guard()
    test_backoff()
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
