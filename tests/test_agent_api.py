#!/usr/bin/env python3
"""conn-agent's phone API, checked with fake servers and a real HTTPS
socket on localhost. No ssh, no network beyond 127.0.0.1.

    python3 tests/test_agent_api.py
"""

from __future__ import annotations

import http.client
import json
import os
import ssl
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_api as API  # noqa: E402
import agent_state as A  # noqa: E402
import chats as C  # noqa: E402
import notes as N  # noqa: E402
import starting  # noqa: E402
from test_agent_state import CLAUDE, RULE  # noqa: E402

failures = 0
total = 0


def check(label: str, passed: bool, detail: str = "") -> None:
    global failures, total
    total += 1
    failures += not passed
    print(f"{'ok  ' if passed else 'FAIL'} {label}"
          + (f"\n     {detail}" if detail and not passed else ""))


REPLY_SCREEN = (
    "❯ fix the export\n\n"
    "● Bash(pytest)\n  ⎿  41 passed\n\n"
    "● Fixed the CSV export. Two things changed:\n\n"
    "  - the header row is written once\n"
    "  - dates use ISO format\n\n"
    "  All 41 tests pass.\n\n"
    "✻ Worked for 12s\n\n"
    f"{RULE}\n❯ \n{RULE}\n")


class FakeActions(API.Actions):
    def __init__(self, store):
        super().__init__(store)
        self.calls = []

    def start(self, host, text, folder, **kw):
        self.calls.append(("start", host, text, folder, kw))
        return "fix-export"

    def reply(self, host, session, text):
        self.calls.append(("reply", host, session, text))

    def interrupt(self, host, session):
        self.calls.append(("interrupt", host, session))

    def key(self, host, session, key):
        self.calls.append(("key", host, session, key))

    def star(self, host, session, starred):
        self.calls.append(("star", host, session, starred))

    def task(self, host, path):
        return "fix the export" if path else ""


def rows_for(screen_b: str, state_b: str, up: bool = True) -> dict:
    return {
        "web": {"state": "up" if up else "down", "error": "" if up else "timed out",
                "home": "/home/u", "hostname": "web", "sessions": [
            {"name": "export", "created": 100, "activity": 200,
             "options": {"last_event": "300", "task": "~/.conn/tasks/export.md",
                         "project": "shop"},
             "screen": REPLY_SCREEN,
             "agent": A.classify(REPLY_SCREEN, ["claude"])},
            {"name": "deploy", "created": 100, "activity": 200,
             "options": {"last_event": "400", "project": "shop"},
             "screen": screen_b, "agent": {"state": state_b,
                                           "preview": "Wants to run rm"}},
            {"name": "sh", "created": 1, "options": {}, "screen": "$ ",
             "agent": {"state": A.SHELL}},
        ] if up else []},
    }


def projects(tmp: Path) -> list:
    path = tmp / "projects.toml"
    path.write_text('[shop]\nname = "Shop"\nservers = { "web" = "~/shop" }\n'
                    'mode = "auto"\n')
    return C.load_projects(path, hosts=["web"])


def feed(watch: API.Watch, rows: dict, projs: list) -> None:
    view = {h: dict(r, hostnames=(r.get("hostname", ""),))
            for h, r in rows.items() if r.get("state") == "up"}
    watch.update(rows, projs, C.build_chats(view, projs))


def test_screen_reading() -> None:
    perm = API.permission_of(CLAUDE["boxed prompt"])
    check("a permission prompt's options are read off the screen",
          perm is not None and [o["key"] for o in perm["options"]][:1] == ["1"]
          and perm["options"][0]["label"].startswith("Yes"), repr(perm))
    check("and what it wants to run", perm and "rm -rf build" in perm["command"],
          repr(perm))
    check("no prompt, no permission", API.permission_of(REPLY_SCREEN) is None)
    reply = API.latest_reply(REPLY_SCREEN)
    check("the latest reply comes whole, line breaks kept",
          reply.startswith("Fixed the CSV export.")
          and "- dates use ISO format" in reply and "All 41 tests pass." in reply
          and "Worked for" not in reply and "pytest" not in reply, repr(reply))


def test_live_prompt_only() -> None:
    answered = (CLAUDE["boxed prompt"]
                + "\n  ⎿  Removed build/\n\n✻ Worked for 3s\n\n"
                + f"{RULE}\n❯ \n{RULE}\n")
    check("an answered prompt higher up lends no options",
          API.permission_of(answered) is None)
    plan = ("● Here is the plan.\n\n"
            " Would you like to proceed?\n\n"
            " ❯ 1. Yes, and auto-accept edits\n"
            "   2. Yes, and manually approve edits\n"
            "   3. No, keep planning\n")
    perm = API.permission_of(plan)
    check("a plan-mode 'Would you like to' dialog has its options",
          perm is not None and [o["key"] for o in perm["options"]]
          == ["1", "2", "3"] and perm["kind"] == "permission", repr(perm))
    reply = API.latest_reply("● Two notes:\n\n  > a quote\n  - an item\n\n"
                             f"{RULE}\n❯ \n{RULE}\n")
    check("a quote or list item inside a reply does not cut it",
          "a quote" in reply and "an item" in reply, repr(reply))


def test_devices(tmp: Path) -> None:
    d = API.Devices(tmp / "devices.json", tmp / "codes.json")
    device_id, token = d.create("Desk", kind="desktop")
    check("a token checks out, and is stored only hashed",
          d.check(token) == device_id
          and token not in (tmp / "devices.json").read_text())
    check("a wrong token does not", d.check("cn_nope") is None
          and d.check("") is None)
    check("the file is yours only",
          oct((tmp / "devices.json").stat().st_mode & 0o777) == "0o600")
    code, expires = d.new_code(now=1000)
    check("a code redeems once", d.redeem(code.lower(), now=1001)
          and not d.redeem(code, now=1002))
    code, _ = d.new_code(now=1000)
    check("and not after five minutes", not d.redeem(code, now=1000 + 301))
    code, _ = d.new_code_file(now=2000)
    check("a code made by `conn_agent.py pair` redeems in the agent",
          API.Devices(tmp / "devices.json", tmp / "codes.json").redeem(
              code, now=2001) and not d.redeem(code, now=2002))
    check("revoking a device ends its token",
          d.remove(device_id) and d.check(token) is None)
    other = API.Devices(tmp / "devices.json", tmp / "codes.json")
    late_id, late = other.create("Desk 2", kind="desktop")
    check("a token made by another process (conn_agent.py token) works at once",
          d.check(late) == late_id)
    d.set(late_id, active=5)
    check("and survives this process saving its own change",
          other.check(late) == late_id and d.get(late_id)["active"] == 5)
    g = API.Devices(tmp / "g.json", tmp / "gc.json")
    for _ in range(API.PAIR_FAILURES):
        g.redeem("WRONG-1", now=5000)
    good, _ = g.new_code(now=5000)
    try:
        g.redeem(good, now=5001)
        limited = False
    except API.ApiError as exc:
        limited = exc.status == 429
    check("guessing codes is cut off after a few wrong ones", limited)
    check("for a minute", g.redeem(good, now=5000 + 61) is True)


def test_api(tmp: Path) -> None:
    store = N.Store(tmp / "notes")
    actions = FakeActions(store)
    watch = API.Watch()
    devices = API.Devices(tmp / "d.json", tmp / "c.json")
    api = API.Api(watch, devices, actions)
    phone, _ = devices.create("iPhone")
    desk, _ = devices.create("Desk", kind="desktop")
    projs = projects(tmp)
    q = watch.subscribe()
    feed(watch, rows_for(CLAUDE["boxed prompt"], A.NEEDS_YOU), projs)

    def call(method, path, body=None, query=None, device=phone):
        try:
            return api.call(method, path, query or {}, body, device)
        except API.ApiError as exc:
            return exc.status, str(exc)

    check("unpaired requests are refused",
          call("GET", "/chats", device=None)[0] == 401)
    status, listed = call("GET", "/chats")
    check("chats: newest first, plain shells left out",
          status == 200 and [c["id"] for c in listed]
          == ["web/deploy", "web/export"], repr(listed))
    deploy = listed[0]
    check("a permission prompt is needs_you, with its options",
          deploy["state"] == "needs_you" and deploy["permission"]
          and deploy["permission"]["options"][0]["key"] == "1", repr(deploy))
    check("filters: needs_you=1",
          [c["id"] for c in call("GET", "/chats", query={"needs_you": ["1"]})[1]]
          == ["web/deploy"])
    status, detail = call("GET", "/chats/web/export")
    check("chat detail: task, whole latest reply, started",
          status == 200 and detail["task"] == "fix the export"
          and "ISO format" in detail["latest_reply"]
          and detail["started"] == 100, repr(detail))
    check("an unknown chat is 404", call("GET", "/chats/web/nope")[0] == 404)

    check("reply goes to the session",
          call("POST", "/chats/web/export/reply", {"text": "thanks"})[0] == 204
          and actions.calls[-1] == ("reply", "web", "export", "thanks"))
    check("an empty reply is refused",
          call("POST", "/chats/web/export/reply", {"text": "  "})[0] == 400)
    check("approve sends an option that is on screen",
          call("POST", "/chats/web/deploy/approve", {"key": "1"})[0] == 204
          and actions.calls[-1] == ("key", "web", "deploy", "1"))
    check("and refuses one that is not",
          call("POST", "/chats/web/deploy/approve", {"key": "7"})[0] == 409)
    check("interrupt", call("POST", "/chats/web/export/interrupt")[0] == 204)
    check("star needs a real boolean",
          call("PUT", "/chats/web/export/star", {"starred": "yes"})[0] == 400
          and call("PUT", "/chats/web/export/star", {"starred": True})[0] == 204
          and call("GET", "/chats/web/export")[1]["starred"] is True)

    store.create("shop", "Use ISO dates everywhere")
    status, chat = call("POST", "/chats", {"project": "shop", "text":
                                           "Add a CSV export\nwith filters"})
    start = [c for c in actions.calls if c[0] == "start"][-1]
    check("starting a chat: first server, its folder, mode, notes appended",
          status == 201 and chat["id"] == "web/fix-export"
          and chat["title"] == "Add a CSV export"
          and start[1:4] == ("web", "Add a CSV export\nwith filters", "~/shop")
          and start[4]["mode"] == "auto" and start[4]["source"] == "phone"
          and "Use ISO dates" in start[4]["notes_block"], repr(start))
    check("a server outside the project is refused",
          call("POST", "/chats", {"project": "shop", "host": "other",
                                  "text": "x"})[0] == 400)
    check("an unknown project is refused",
          call("POST", "/chats", {"project": "nope", "text": "x"})[0] == 400)

    status, note = call("POST", "/notes", {"project": "shop",
                                           "text": "Check the totals"})
    check("notes: create", status == 201 and note["source"] == "phone")
    check("notes: list by project",
          note["id"] in [n["id"] for n in call("GET", "/notes",
                                               query={"project": ["shop"]})[1]])
    status, started = call("POST", f"/notes/{note['id']}/start", {})
    check("start a chat from a note marks it started, linked",
          status == 201 and store.get(note["id"])["status"] == "started"
          and store.get(note["id"])["chat"] == started["id"]
          and [c for c in actions.calls if c[0] == "start"][-1][4]["source"]
          == f"note:{note['id']}")
    check("notes: patch status",
          call("PATCH", f"/notes/{note['id']}", {"status": "done"})[1]["status"]
          == "done")
    check("notes: a bad status is refused",
          call("PATCH", f"/notes/{note['id']}", {"status": "gone"})[0] == 400)
    status, recap = call("POST", "/chats/web/export/recap-to-notes")
    check("recap to notes: a recap note linked to the chat",
          status == 201 and recap["kind"] == "recap"
          and recap["chat"] == "web/export"
          and recap["text"].startswith("Fixed the CSV export."), repr(recap))
    check("notes: delete", call("DELETE", f"/notes/{note['id']}")[0] == 204)

    check("only the desktop makes pairing codes",
          call("POST", "/pair/codes")[0] == 403
          and call("POST", "/pair/codes", device=desk)[0] == 201)
    code = call("POST", "/pair/codes", device=desk)[1]["code"]
    status, paired = call("POST", "/pair", {"code": code,
                                            "device_name": "iPad"}, device=None)
    check("pairing with a code needs no token and gives one",
          status == 201 and devices.check(paired["token"]) == paired["device_id"])
    check("the code is spent",
          call("POST", "/pair", {"code": code}, device=None)[0] == 403)
    check("devices list and revoke",
          paired["device_id"] in [d["id"] for d in call("GET", "/devices")[1]]
          and call("DELETE", f"/devices/{paired['device_id']}")[0] == 204
          and devices.check(paired["token"]) is None)
    check("health counts servers",
          call("GET", "/health")[1]["hosts_up"] == 1)
    check("projects carry each server's status",
          call("GET", "/projects")[1][0]["server_status"] == {"web": "up"})

    events = []
    while not q.empty():
        events.append(q.get_nowait()[0])
    check("events were published for chats and hosts",
          "chat.updated" in events and "host.updated" in events
          and "note.updated" in events, repr(events))

    # The server goes dark: its chats stay listed, offline; actions refused.
    feed(watch, rows_for("", A.READY, up=False), projs)
    status, listed = call("GET", "/chats")
    check("a dark server's chats stay, offline",
          [c["id"] for c in listed] == ["web/deploy", "web/export"]
          and all(c["offline"] for c in listed), repr(listed))
    check("and refuse a reply",
          call("POST", "/chats/web/export/reply", {"text": "hi"})[0] == 409)
    check("hosts say why",
          call("GET", "/hosts")[1][0]["status"] == "dark")
    check("starting on it is refused",
          call("POST", "/chats", {"project": "shop", "text": "x"})[0] == 409)


def test_flicker_and_overlays(tmp: Path) -> None:
    watch = API.Watch()
    projs = projects(tmp)
    feed(watch, rows_for(CLAUDE["boxed prompt"], A.NEEDS_YOU), projs)
    torn = rows_for("", A.UNKNOWN)
    feed(watch, torn, projs)
    check("a torn capture keeps the prompt rather than flicker to idle",
          watch.chats["web/deploy"]["state"] == "needs_you"
          and watch.chats["web/deploy"]["permission"] is not None)
    watch.set_star("web/export", True)
    feed(watch, rows_for(CLAUDE["boxed prompt"], A.NEEDS_YOU), projs)
    check("a star stays until the server's option says so",
          watch.chats["web/export"]["starred"] is True)
    starred = rows_for(CLAUDE["boxed prompt"], A.NEEDS_YOU)
    starred["web"]["sessions"][0]["options"]["starred"] = "1"
    feed(watch, starred, projs)
    check("then the wish is done with", "web/export" not in watch.star_wish)


def test_finished(tmp: Path) -> None:
    watch = API.Watch()
    projs = projects(tmp)
    working = rows_for("", A.WORKING)
    working["web"]["sessions"][0]["agent"] = {"state": A.WORKING}
    feed(watch, working, projs)
    feed(watch, rows_for("", A.READY), projs)
    check("a turn the agent watched end is needs_you",
          watch.chats["web/export"]["state"] == "needs_you")
    watch.mark_seen("web/export")
    check("until a device opens it", watch.chats["web/export"]["state"] == "idle")
    later = rows_for("", A.READY)
    later["web"]["sessions"][0]["options"]["last_event"] = "999999"
    feed(watch, later, projs)
    check("a stamp the desktop writes later does not make it news again",
          watch.chats["web/export"]["state"] == "idle")


def test_https(tmp: Path) -> None:
    context, fp = API.tls_context(tmp / "tls")
    check("a certificate is made, its key private",
          len(fp) == 64 and oct((tmp / "tls" / "key.pem").stat().st_mode
                                & 0o777) == "0o600")
    watch = API.Watch()
    devices = API.Devices(tmp / "h.json", tmp / "hc.json")
    _, token = devices.create("iPhone")
    api = API.Api(watch, devices, FakeActions(N.Store(tmp / "hn")))
    server = API.serve(api, "127.0.0.1", 0, context)
    port = server.server_address[1]
    client = ssl.create_default_context()
    client.check_hostname = False
    client.verify_mode = ssl.CERT_NONE      # the app pins by fingerprint

    def request(method, path, body=None, auth=True):
        conn = http.client.HTTPSConnection("127.0.0.1", port, context=client,
                                           timeout=10)
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {token}"
        conn.request(method, path, json.dumps(body) if body else None, headers)
        seen_fp = __import__("hashlib").sha256(
            conn.sock.getpeercert(binary_form=True)).hexdigest()
        answer = conn.getresponse()
        data = answer.read()
        conn.close()
        return answer.status, (json.loads(data) if data else None), seen_fp

    try:
        status, data, seen = request("GET", "/health")
        check("HTTPS: health answers", status == 200 and data["ok"], repr(data))
        check("HTTPS: the certificate is the one pinned", seen == fp)
        check("HTTPS: no token, 401",
              request("GET", "/chats", auth=False)[0] == 401)
        check("HTTPS: not JSON is a 400, not a crash",
              request("POST", "/notes", None)[0] in (400,))

        import socket
        raw = socket.create_connection(("127.0.0.1", port), timeout=10)
        tls = client.wrap_socket(raw, server_hostname="x")
        tls.sendall(b"POST /pair HTTP/1.1\r\nHost: x\r\n"
                    b"Content-Length: -1\r\n\r\n")
        check("HTTPS: a negative Content-Length is refused at once",
              tls.recv(200).startswith(b"HTTP/1.1 400"))
        tls.close()
        raw = socket.create_connection(("127.0.0.1", port), timeout=10)
        tls = client.wrap_socket(raw, server_hostname="x")
        tls.sendall(b"POST /pair HTTP/1.1\r\nHost: x\r\n"
                    b"Transfer-Encoding: chunked\r\n\r\n")
        check("HTTPS: chunked bodies are refused",
              tls.recv(200).startswith(b"HTTP/1.1 501"))
        tls.close()
        idle = [socket.create_connection(("127.0.0.1", port), timeout=10)
                for _ in range(API.MAX_PER_CLIENT + 3)]
        time.sleep(0.3)
        shut = 0
        for sock in idle:
            sock.settimeout(0.5)
            try:
                if sock.recv(1) == b"":
                    shut += 1
            except (TimeoutError, OSError):
                pass
            sock.close()
        check("HTTPS: one address gets only so many connections at once",
              shut >= 3, f"closed {shut}")
        time.sleep(0.3)

        got = []

        def listen():
            conn = http.client.HTTPSConnection("127.0.0.1", port,
                                               context=client, timeout=10)
            conn.request("GET", "/events",
                         headers={"Authorization": f"Bearer {token}"})
            answer = conn.getresponse()
            got.append(answer.status)
            for line in answer:
                if line.startswith(b"event:"):
                    got.append(line.decode().strip())
                    break
            conn.close()

        thread = threading.Thread(target=listen, daemon=True)
        thread.start()
        for _ in range(50):
            if watch.listeners:
                break
            time.sleep(0.05)
        feed(watch, rows_for("", A.READY), projects(tmp))
        thread.join(5)
        check("HTTPS: events stream as server-sent events",
              got[:1] == [200] and any("event: " in g for g in got[1:]
                                       if isinstance(g, str)), repr(got))

        ended = []

        def revoked():
            conn = http.client.HTTPSConnection("127.0.0.1", port,
                                               context=client, timeout=30)
            conn.request("GET", "/events",
                         headers={"Authorization": f"Bearer {token}"})
            answer = conn.getresponse()
            answer.read()           # returns when the server ends the stream
            ended.append(True)

        listeners = len(watch.listeners)
        thread = threading.Thread(target=revoked, daemon=True)
        thread.start()
        for _ in range(50):
            if len(watch.listeners) > listeners:
                break
            time.sleep(0.05)
        devices.remove(devices.check(token))
        watch.publish("chat.updated", {"id": "x"})
        thread.join(10)
        check("HTTPS: revoking a phone ends its open event stream", ended == [True])
    finally:
        server.shutdown()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        os.environ["CONN_LOG"] = str(tmp / "log")
        test_screen_reading()
        test_devices(tmp)
        test_api(tmp)
        test_finished(tmp)
        test_flicker_and_overlays(tmp)
        test_live_prompt_only()
        test_https(tmp)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
