#!/usr/bin/env python3
"""Notes (SPEC §5): the store, the §5.3 block, and the desktop client.

    python3 tests/test_notes.py

The client runs against "local" with CONN_NOTES_ROOT pointing at a temp
directory, so it goes through exactly the ssh-less path a real call takes
(bash, `python3 -`, the program text) without touching ~/conn-notes.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notes as N  # noqa: E402
import notes_client as NC  # noqa: E402

failures = 0
total = 0


def check(label: str, passed: bool, detail: str = "") -> None:
    global failures, total
    total += 1
    if not passed:
        failures += 1
    print(("ok   " if passed else "FAIL ") + label
          + ("" if passed or not detail else f"\n     {detail}"))


def test_format() -> None:
    note = {"id": "20261105-1031-csv-export", "project": "atlas",
            "created": "2026-11-05T10:31:00+11:00",
            "updated": "2026-11-05T10:31:00+11:00", "source": "quick-add",
            "chat": "", "status": "open", "kind": "note",
            "text": "CSV export for the switches table\n\nsecond para"}
    text = N.render(note)
    back = N.parse(text)
    check("render/parse round trip", back == note, repr(back))
    check("no empty fields written", "chat:" not in text)
    spec = ("---\nid: 20261105-1031-csv-export\nproject: atlas\n"
            "source: quick-add        # quick-add | desktop\nstatus: started\n"
            "---\nCSV export\n")
    parsed = N.parse(spec)
    check("parses the spec's example, comments dropped",
          parsed["source"] == "quick-add" and parsed["status"] == "started"
          and parsed["text"] == "CSV export", repr(parsed))
    evil = dict(note, chat="x\nstatus: done")
    check("a newline cannot forge a field",
          N.parse(N.render(evil))["status"] == "open")


def test_store(tmp: Path) -> None:
    store = N.Store(tmp / "notes")
    a = store.create("atlas", "CSV export for the switches table",
                     source="quick-add", created="2026-11-05T10:31:00+11:00")
    check("id is YYYYMMDD-HHMM-slug", a["id"] == "20261105-1031-csv-export-switches",
          a["id"])
    b = store.create("atlas", "CSV export for the switches table",
                     created="2026-11-05T10:31:00+11:00")
    check("same minute, same words: suffixed", b["id"].endswith("-2"), b["id"])
    c = store.create("acme", "Calendar sync note",
                     created="2026-11-06T09:00:00+11:00")
    check("file lands in the project folder",
          (tmp / "notes/atlas" / f"{a['id']}.md").exists())
    check("list newest first", [n["id"] for n in store.list()][0] == c["id"])
    check("list by project", len(store.list(project="atlas")) == 2)
    store.update(a["id"], status="started", chat="atlas/csv-export")
    got = store.get(a["id"])
    check("update status and chat", got["status"] == "started"
          and got["chat"] == "atlas/csv-export")
    check("list by status", [n["id"] for n in store.list(status="started")]
          == [a["id"]])
    store.delete(b["id"])
    check("delete removes the file", len(store.list(project="atlas")) == 1)
    log = store._git("log", "--format=%s").stdout.split("\n")
    check("every change committed, no note text in messages",
          log[:5] == [f"delete {b['id']}", f"update {a['id']}",
                      f"create {c['id']}", f"create {b['id']}",
                      f"create {a['id']}"], repr(log))
    for bad, call in (("bad project", lambda: store.create("Bad/id", "x")),
                      ("empty text", lambda: store.create("acme", "  ")),
                      ("bad status", lambda: store.update(c["id"], status="x")),
                      ("path id", lambda: store.get("../../etc/passwd"))):
        try:
            call()
            check(f"refuses {bad}", False)
        except N.NoteError:
            check(f"refuses {bad}", True)


def test_block() -> None:
    now = N._stamp("2026-11-05T12:00:00+11:00")
    items = [
        {"text": "Privacy page before early access opens", "status": "open",
         "kind": "note", "created": "2026-11-04T11:40:00+11:00"},
        {"text": "Started one", "status": "started", "kind": "note",
         "created": "2026-11-04T11:39:00+11:00"},
        {"text": "A recap", "status": "open", "kind": "recap",
         "created": "2026-11-04T11:38:00+11:00"},
    ]
    os.environ["TZ"] = "Australia/Sydney"
    block = N.notes_block(items, now)
    check("block: header, open notes only",
          block.startswith("---\nNotes on this project from the user")
          and "Privacy page" in block and "Started one" not in block
          and "A recap" not in block, block)
    check("block: day and time", "(Wed 11:40)" in block, block)
    check("block: none is empty", N.notes_block(items[1:], now) == "")
    many = [dict(items[0], text=f"n{i}") for i in range(30)]
    check("block: at most 20", N.notes_block(many, now).count("\n- ") == 20)


def test_client(tmp: Path) -> None:
    os.environ["CONN_NOTES_ROOT"] = str(tmp / "client-notes")
    client = NC.NotesClient("local", outbox=tmp / "outbox",
                            cache=tmp / "cache.json")
    made = client.create("widgets", "Help centre search should rank titles")
    check("client: create through python3 -", made.get("id", "").endswith(
        "help-centre-search"), repr(made))
    check("client: list", [n["id"] for n in client.list()] == [made["id"]])
    check("client: cache written", client.cached()[0]["id"] == made["id"])
    changed = client.update(made["id"], status="started", chat="widgets-new/x")
    check("client: update", changed["status"] == "started"
          and changed["chat"] == "widgets-new/x", repr(changed))
    edited = client.update(made["id"], text="Help centre search ranks titles")
    check("client: edit text", edited["text"] == "Help centre search ranks titles")
    try:
        client.create("Not An Id", "x")
        check("client: refusal raised, not queued", False)
    except N.NoteError:
        check("client: refusal raised, not queued",
              not list((tmp / "outbox").glob("*.json")))

    offline = NC.NotesClient("no-such-host.invalid", outbox=tmp / "outbox",
                             cache=tmp / "cache.json")
    NC.hosts.CONNECT_TIMEOUT = 2
    q = offline.create("widgets", "Jotted on the train")
    check("offline: queued", bool(q.get("queued")) and len(offline.queued()) == 1,
          repr(q))
    client.list()
    check("back online: outbox sent and emptied",
          not offline.queued()
          and any(n["text"] == "Jotted on the train" for n in client.list()))
    try:
        offline.create("Not_A_Project", "never sendable")
        check("offline: an unsendable note is refused, not queued", False)
    except N.NoteError:
        check("offline: an unsendable note is refused, not queued",
              not offline.queued())
    # A bad item already in the outbox (an older conn wrote it) goes to
    # rejected/ and does not block the good one behind it.
    import json as _json
    (tmp / "outbox").mkdir(exist_ok=True)
    (tmp / "outbox" / "1-bad.json").write_text(_json.dumps(
        {"project": "Bad Id", "text": "x", "source": "desktop"}))
    (tmp / "outbox" / "2-good.json").write_text(_json.dumps(
        {"project": "widgets", "text": "good one behind it", "source": "desktop"}))
    sent = client.flush()
    check("flush: bad item set aside, good one sent, nothing duplicated",
          sent == 1 and (tmp / "outbox/rejected/1-bad.json").exists()
          and not list((tmp / "outbox").glob("*.json"))
          and sum(n["text"] == "good one behind it" for n in client.list()) == 1)
    check("program starts with REQUEST", NC.program({"op": "ping"})
          .startswith("REQUEST = "))


def main() -> int:
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        test_format()
        test_store(tmp)
        test_block()
        test_client(tmp)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
