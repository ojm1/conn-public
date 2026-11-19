#!/usr/bin/env python3
"""Claude's last reply from its transcript: picking it, finding it.

    python3 tests/test_transcript.py

The transcripts are fixtures (tests/fixtures/transcript_*.jsonl), made up
for this; no real conversation is read. The lookup runs the real sh script
against a throwaway tmux server (TMUX_TMPDIR in a temp dir, TMUX unset) and
a HOME of its own, with a fake `claude` -- a shell script by that name, so
ps calls it claude -- standing in the pane.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures"
SID = "0b6f7c1e-2a3d-4e5f-8a9b-0c1d2e3f4a5b"
NEXT = "9e8d7c6b-5a49-4382-a716-1f2e3d4c5b6a"
REPLY = ("Fixed the CSV export. Two things changed:\n\n"
         "- the header row is written once\n- dates use ISO format\n\n"
         "All 41 tests pass.")

failures = 0
total = 0


def check(label: str, passed: bool, detail: str = "") -> None:
    global failures, total
    total += 1
    if not passed:
        failures += 1
    print(("ok   " if passed else "FAIL ") + label
          + ("" if passed or not detail else f"\n     {detail}"))


def lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text().splitlines()


def test_pick(T) -> None:
    reply = T.pick(T.records(lines("transcript_turn.jsonl")))
    check("pick: the final message's text blocks, joined in order",
          reply is not None and reply.text == REPLY, repr(reply))
    check("pick: narration before a tool call is not the reply",
          reply is not None and "Let me look" not in reply.text)
    check("pick: thinking, tool calls, request blobs, sidechains, API "
          "errors and synthetic lines are skipped",
          reply is not None and "NOT A REPLY" not in reply.text
          and "private thoughts" not in reply.text)
    check("pick: a block written twice is joined once",
          reply is not None and reply.text.count("All 41 tests pass.") == 1)
    check("pick: a finished turn is not busy, and names its last line",
          reply is not None and not reply.busy and reply.uuid == "a6")

    busy = T.pick(T.records(lines("transcript_busy.jsonl")))
    check("pick: mid-turn, the last finished reply -- across a compaction",
          busy is not None and busy.text == "Four.", repr(busy))
    check("pick: and says Claude is busy past it",
          busy is not None and busy.busy)

    only_tools = [l for l in lines("transcript_busy.jsonl")
                  if '"msg_B"' in l]
    check("pick: a turn with nothing finished in reach is None",
          T.pick(T.records(only_tools)) is None)
    check("pick: nothing at all is None", T.pick([]) is None)

    # A tail starts mid-line and can end mid-append.
    raw = (FIXTURES / "transcript_turn.jsonl").read_bytes()
    cut = raw[raw.index(b'"a2"') - 40:]
    cut = cut[:-30]                                  # last line half-written
    got = T.records(cut.decode().splitlines())
    check("records: a cut first line and a half-written last line drop out",
          all(isinstance(r, dict) for r in got)
          and not any(r.get("uuid") == "a1" for r in got)
          and not any(r.get("type") == "last-prompt" for r in got), repr(got[:1]))
    reply = T.pick(got)
    check("pick: still finds the reply in a tail cut that way",
          reply is not None and reply.text == REPLY)
    check("records: blank lines and junk are ignored",
          T.records(["", "garbage", "[1,2]", '{"type":"x"}']) == [{"type": "x"}])


def test_conversation(T) -> None:
    import json as _j
    def user(text, **extra):
        d = {"type": "user", "message": {"role": "user", "content": text},
             "timestamp": "2026-11-19T01:00:00.000Z"}
        d.update(extra)
        return d
    def claude(mid, blocks, stop="end_turn", at="2026-11-19T01:05:00.000Z"):
        return {"type": "assistant", "timestamp": at,
                "message": {"id": mid, "model": "m", "stop_reason": stop,
                            "content": blocks}}
    entries = [
        user("first question", origin={"kind": "human"}),
        claude("m1", [{"type": "text", "text": "looking"},
                      {"type": "tool_use", "id": "t", "name": "Bash"}], "tool_use"),
        user([{"type": "tool_result", "tool_use_id": "t", "content": "out"}]),
        claude("m2", [{"type": "text", "text": "first answer"}]),
        user("<command-name>/clear</command-name>"),
        user("a notice", origin={"kind": "task-notification"}),
        user("injected", isMeta=True),
        user([{"type": "text", "text": "second"}, {"type": "image"}],
             origin={"kind": "human"}),
        claude("m3", [{"type": "text", "text": "second answer"}],
               at="2026-11-19T01:20:00.000Z"),
    ]
    got = T.conversation(entries)
    check("conversation: your messages and Claude's answers, in order",
          [(g["role"], g["text"]) for g in got] == [
              ("you", "first question"), ("claude", "first answer"),
              ("you", "second"), ("claude", "second answer")], repr(got))
    check("conversation: tool narration, results, commands and notices left out",
          not any(g["text"] in ("looking", "out", "a notice", "injected")
                  for g in got))
    check("conversation: times are unix seconds",
          got[-1]["at"] - got[-2]["at"] == 20 * 60, repr([g["at"] for g in got]))
    many = []
    for i in range(50):
        many += [user(f"q{i}", origin={"kind": "human"}),
                 claude(f"a{i}", [{"type": "text", "text": f"a{i}"}])]
    got = T.conversation(many)
    check("conversation: only the last HISTORY_ITEMS",
          len(got) == T.HISTORY_ITEMS and got[-1]["text"] == "a49")


def test_parse(T) -> None:
    tok = "abc123"
    tail = "\n".join(lines("transcript_turn.jsonl"))
    out = (f"###{tok}:SESSION {SID}\n###{tok}:STATUS idle\n###{tok}:MOVED \n"
           f"###{tok}:SIZE 4000\n###{tok}:TAIL\n{tail}\n")
    reply = T.parse(out, tok)
    check("parse: markers then the tail", reply is not None
          and reply.text == REPLY and reply.session_id == SID
          and not reply.moved and not reply.busy, repr(reply))
    check("parse: status busy is busy",
          T.parse(out.replace("STATUS idle", "STATUS busy"), tok).busy)
    check("parse: a hand-off is reported",
          T.parse(out.replace("MOVED ", f"MOVED {NEXT}"), tok).moved)
    check("parse: NONE is None", T.parse(f"###{tok}:NONE no claude\n", tok) is None)
    check("parse: a session id that is not one is None",
          T.parse(out.replace(SID, "../../etc"), tok) is None)
    check("parse: another run's markers are not ours",
          T.parse(out, "other") is None)
    check("parse: empty output is None", T.parse("", tok) is None)


def test_fallback(T) -> None:
    screen = (FIXTURES / "claude_recap.txt").read_text()
    text, source = T.last_reply("web", "x", screen, fetcher=lambda h, s: None)
    check("last_reply: no transcript -> the screen's reading of it",
          source == "screen" and text == T.latest_reply(screen) and text,
          repr((text[:30], source)))
    check("last_reply: neither -> nothing",
          T.last_reply("web", "x", "", fetcher=lambda h, s: None) == ("", ""))
    text, source = T.last_reply("web", "x", screen,
                                fetcher=lambda h, s: T.Reply("exact"))
    check("last_reply: the transcript wins", (text, source)
          == ("exact", "transcript"))
    text, source = T.last_reply("web", "x", screen,
                                fetcher=lambda h, s: T.Reply("before",
                                                             busy=True))
    check("last_reply: mid-turn, the answer before is said to be that",
          (text, source) == ("before", "transcript-busy"))

    import hosts
    real = hosts.run_argv
    try:
        hosts.run_argv = lambda host, opts=None: ["false"]
        check("fetch: a host that fails is None, not an exception",
              T.fetch("web", "x") is None)
        hosts.run_argv = lambda host, opts=None: ["/nonexistent/ssh"]
        check("fetch: no ssh at all is None too", T.fetch("web", "x") is None)
    finally:
        hosts.run_argv = real


def wait_for(path: Path, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists() and path.read_text().strip():
            return True
        time.sleep(0.05)
    return False


def test_lookup(T, tmp: Path) -> None:
    """The script itself, on a tmux server of its own."""
    home = tmp / "home"
    sessions = home / ".claude" / "sessions"
    project = home / ".claude" / "projects" / "-home-x-proj"
    sessions.mkdir(parents=True)
    project.mkdir(parents=True)
    fake = tmp / "bin" / "claude"
    fake.parent.mkdir()
    pidfile = tmp / "pid"
    fake.write_text(f"#!/bin/sh\necho $$ > {pidfile}\n"
                    "while :; do sleep 1; done\n")
    fake.chmod(0o755)
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        def tmux(*args):
            return subprocess.run(["tmux", *args], capture_output=True,
                                  text=True, timeout=10)

        tmux("new-session", "-d", "-s", "plain", "sleep 600")
        check("lookup: a pane with no claude in it is None",
              T.fetch("local", "plain") is None)

        tmux("new-session", "-d", "-s", "chat", str(fake))
        if not wait_for(pidfile):
            check("lookup: the fake claude started", False)
            return
        pid = int(pidfile.read_text())
        # A stale file naming the same pane, for a process long gone: the
        # pane's own process tree decides, not a file's say-so.
        (sessions / "999999.json").write_text(json.dumps(
            {"pid": 999999, "sessionId": NEXT, "tmux": "chat:@0.%1"}))
        check("lookup: a claude without a sessions file is None",
              T.fetch("local", "chat") is None)

        (sessions / f"{pid}.json").write_text(json.dumps(
            {"pid": pid, "sessionId": SID, "cwd": "/home/x/proj",
             "status": "idle", "tmux": "chat:@1.%1"}, indent=1))
        check("lookup: sessions file found, transcript missing -> None",
              T.fetch("local", "chat") is None)

        shutil.copy(FIXTURES / "transcript_turn.jsonl", project / f"{SID}.jsonl")
        reply = T.fetch("local", "chat")
        check("lookup: pane -> claude pid -> session id -> transcript -> reply",
              reply is not None and reply.session_id == SID
              and reply.text == REPLY, repr(reply))
        check("lookup: only the exact session name, never a prefix",
              T.fetch("local", "cha") is None)

        real = T.TAIL_BYTES
        try:
            T.TAIL_BYTES = 2400           # smaller than the file: a real tail
            reply = T.fetch("local", "chat")
            check("lookup: from a tail of a file bigger than the window",
                  reply is not None and reply.text == REPLY, repr(reply))
        finally:
            T.TAIL_BYTES = real

        # Sent to the background: the pane's file ends with continued-in.
        with open(project / f"{SID}.jsonl", "a") as out:
            out.write(json.dumps({"type": "continued-in", "sessionId": SID,
                                  "continuedInSessionId": NEXT}) + "\n")
        check("lookup: a hand-off to a file that is not there stays put",
              (T.fetch("local", "chat") or T.Reply("")).text == REPLY)
        other = home / ".claude" / "projects" / "-home-x"
        other.mkdir()
        shutil.copy(FIXTURES / "transcript_busy.jsonl", other / f"{NEXT}.jsonl")
        reply = T.fetch("local", "chat")
        check("lookup: a hand-off is followed to the file that took it",
              reply is not None and reply.text == "Four." and reply.moved
              and reply.session_id == NEXT, repr(reply))

        (sessions / f"{pid}.json").write_text(json.dumps(
            {"pid": pid, "sessionId": "not-a-uuid; rm -rf ~", "status": "idle"}))
        check("lookup: a session id that is not one is refused",
              T.fetch("local", "chat") is None)
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True, timeout=10)
        if old_home is not None:
            os.environ["HOME"] = old_home


def main() -> int:
    for key in ("TMUX", "TMUX_PANE"):
        os.environ.pop(key, None)
    tmp = Path(tempfile.mkdtemp(prefix="conn-transcript-"))
    sock = tmp / "tmux"
    sock.mkdir()
    os.environ["TMUX_TMPDIR"] = str(sock)
    os.environ["CONN_LOG"] = str(tmp / "log")
    import transcript as T
    try:
        test_pick(T)
        test_conversation(T)
        test_parse(T)
        test_fallback(T)
        test_lookup(T, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
