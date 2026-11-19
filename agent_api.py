"""conn-agent's API for the iPhone and iPad app (SPEC §6, milestone A1).

The Mac mini's watcher (conn_agent.py) keeps the latest look at every
server here, in `Watch`; this module turns that into the JSON the phone
reads, and the phone's requests into the same ssh actions the desktop uses
(starting.py, hosts.set_chat_option, notes.Store).

Reachable on the local network only. HTTPS with the
agent's own certificate: the app pins it at pairing, so nobody else on the
wifi can read or forge a chat. Every request but POST /pair carries a
device token; tokens are stored hashed.

Never logged: chat text, note text, task text, tokens. Only ids and
transitions.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import shlex
import ssl
import subprocess
import threading
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import agent_state
import chats as chatmod
import connlog
import hosts
import notes as notes_mod
import starting
import transcript

CONFIG_DIR = Path("~/.config/conn").expanduser()
DEVICES = CONFIG_DIR / "devices.json"
PAIR_CODES = CONFIG_DIR / "pair-codes.json"
TLS_DIR = CONFIG_DIR / "agent-tls"
PAIR_TTL = 300.0
CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"     # nothing to misread
MAX_BODY = 256 * 1024
# An image from the phone, base64 in JSON: only on POST .../images, and only
# read once the device is known to be paired.
MAX_IMAGE_BODY = 16 * 1024 * 1024
IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_TYPES = {"jpg": b"\xff\xd8\xff", "png": b"\x89PNG\r\n\x1a\n"}
TASK_CAP = 64 * 1024
REPLY_CAP = transcript.REPLY_CAP     # whole replies, from the transcript
SSE_PING = 15.0
MAX_CONNECTIONS = 48
MAX_PER_CLIENT = 12
HANDSHAKE_TIMEOUT = 10.0
# The chat detail's ssh budget. The phone gives a request 20s
# (timeoutIntervalForRequest, ios/Conn/API/ConnAPI.swift) and the detail can
# need two round trips: the task (15s ceiling, once per chat) and the
# transcript. The transcript gets at most TRANSCRIPT_TIMEOUT, and only what
# is left of DETAIL_BUDGET after the task, so the two together stay under
# the phone's limit -- a slow server is the screen's reply, not an error.
DETAIL_BUDGET = 16.0
TRANSCRIPT_TIMEOUT = 5.0
TRANSCRIPT_FLOOR = 1.0      # less than this left is not worth an ssh
PAIR_FAILURES = 10          # wrong codes a minute, from anyone, before a pause

STATE_NAMES = {agent_state.NEEDS_YOU: "needs_you",
               agent_state.WORKING: "working",
               agent_state.READY: "idle", agent_state.DRAFT: "idle"}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------
# Devices and pairing
# --------------------------------------------------------------------------

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
    os.replace(temp, path)


class Devices:
    """Paired devices, their hashed tokens, and the pairing codes.

    devices.json is shared with `conn_agent.py token` and `pair`, which run
    as other processes: every change re-reads the file under an flock and
    writes it back whole, and a read notices when the file has changed, so
    neither side's change is lost to the other's copy.

    Codes come two ways: POST /pair/codes from the desktop, held here, and
    `conn_agent.py pair` on the mini, written to PAIR_CODES. Both are single
    use, five minutes, hashed at rest."""

    def __init__(self, path: Path = DEVICES, codes: Path = PAIR_CODES):
        self.path, self.codes_path = path, codes
        self.lock = threading.Lock()
        self.codes: dict[str, float] = {}      # hash -> expiry
        self.failures: list[float] = []        # times of wrong pairing codes
        self.data: dict = {"devices": {}}
        self._mtime = None
        self._reload()

    @contextmanager
    def _flock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path.with_name(self.path.name + ".lock"),
                     os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _reload(self, force: bool = False) -> None:
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            mtime = None
        if not force and mtime == self._mtime:
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict) or not isinstance(data.get("devices"), dict):
            data = {"devices": {}}
        self.data, self._mtime = data, mtime

    @contextmanager
    def _change(self):
        """Under both locks, from the file as it is now; saved after."""
        with self.lock, self._flock():
            self._reload(force=True)
            yield self.data["devices"]
            self._save()

    def _save(self) -> None:
        _write_private(self.path, self.data)
        try:
            self._mtime = self.path.stat().st_mtime_ns
        except OSError:
            self._mtime = None

    def create(self, name: str, kind: str = "phone") -> tuple[str, str]:
        """A new device: (device id, token). The token is shown once."""
        name = " ".join(str(name or "device").split())[:60] or "device"
        token = "cn_" + secrets.token_urlsafe(32)
        device_id = "d_" + secrets.token_hex(6)
        with self._change() as devices:
            devices[device_id] = {
                "name": name, "kind": kind, "hash": _hash(token),
                "created": int(time.time()), "last_seen": 0,
                "active": 0, "apns": ""}
        return device_id, token

    def check(self, token: str) -> str | None:
        """The device id a token belongs to, or None."""
        if not token:
            return None
        wanted = _hash(token)
        with self.lock:
            self._reload()
            found = next((device_id for device_id, device
                          in self.data["devices"].items()
                          if hmac.compare_digest(device.get("hash", ""), wanted)),
                         None)
            stale = found is not None and time.time() - self.data["devices"][
                found].get("last_seen", 0) > 60
        if stale:
            self.set(found, last_seen=int(time.time()))
        return found

    def get(self, device_id: str) -> dict:
        with self.lock:
            self._reload()
            return dict(self.data["devices"].get(device_id) or {})

    def set(self, device_id: str, **fields) -> None:
        with self._change() as devices:
            if device_id in devices:
                devices[device_id].update(fields)

    def remove(self, device_id: str) -> bool:
        with self._change() as devices:
            return devices.pop(device_id, None) is not None

    def listing(self) -> list[dict]:
        with self.lock:
            self._reload()
            return [{"id": k, "name": v.get("name", ""),
                     "kind": v.get("kind", ""),
                     "last_seen": v.get("last_seen", 0)}
                    for k, v in sorted(self.data["devices"].items())]

    def desk_active(self, within: float = 120.0) -> bool:
        """A desktop said it was in use lately (POST /devices/me/active)."""
        now = time.time()
        with self.lock:
            self._reload()
            return any(v.get("kind") == "desktop"
                       and now - v.get("active", 0) <= within
                       for v in self.data["devices"].values())

    # -- pairing codes --------------------------------------------------

    @staticmethod
    def _new_code() -> str:
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        return f"{raw[:4]}-{raw[4:]}"

    @staticmethod
    def _norm(code: str) -> str:
        return re.sub(r"[^0-9A-Z]", "", str(code or "").upper())

    def new_code(self, now: float | None = None) -> tuple[str, float]:
        now = time.time() if now is None else now
        code = self._new_code()
        with self.lock:
            self.codes = {h: e for h, e in self.codes.items() if e > now}
            self.codes[_hash(self._norm(code))] = now + PAIR_TTL
        return code, now + PAIR_TTL

    def new_code_file(self, now: float | None = None) -> tuple[str, float]:
        """A code for the running agent, from another process."""
        now = time.time() if now is None else now
        code = self._new_code()
        with self._flock():
            held = self._held_codes()
            held = {h: e for h, e in held.items() if e > now}
            held[_hash(self._norm(code))] = now + PAIR_TTL
            _write_private(self.codes_path, held)
        return code, now + PAIR_TTL

    def _held_codes(self) -> dict:
        try:
            held = json.loads(self.codes_path.read_text())
        except (OSError, ValueError):
            return {}
        return {h: e for h, e in held.items()
                if isinstance(h, str) and isinstance(e, (int, float))} \
            if isinstance(held, dict) else {}

    def redeem(self, code: str, now: float | None = None) -> bool:
        """Use a code up: True once, for a live one."""
        now = time.time() if now is None else now
        wanted = _hash(self._norm(code))
        with self.lock:
            # Six characters from 32 is a billion codes, but only with a
            # limit on guessing: past PAIR_FAILURES wrong ones a minute,
            # every attempt is refused until the minute is up.
            self.failures = [t for t in self.failures if now - t < 60]
            if len(self.failures) >= PAIR_FAILURES:
                raise ApiError(429, "too many wrong codes: wait a minute")
            expiry = self.codes.pop(wanted, None)
            if expiry is None:
                with self._flock():
                    held = self._held_codes()
                    expiry = held.pop(wanted, None)
                    if expiry is not None:
                        _write_private(self.codes_path, held)
            if expiry is None or expiry <= now:
                self.failures.append(now)
                return False
            return True


# --------------------------------------------------------------------------
# Reading a chat's screen
# --------------------------------------------------------------------------

OPTION = re.compile(r"^\s*(?:❯\s*)?(\d)\.\s+(.+?)\s*$")


def permission_of(screen: str) -> dict | None:
    """{kind, command, options} for the prompt still asking, else None.

    Only the live one -- the classifier's own test (a match with nothing
    settled under it), so an answered dialog higher up the transcript can
    never lend its options to the one on screen now. kind is "permission"
    for "Do you want to / Would you like to" dialogs, "question" for
    Claude's own picker. The options are read off the prompt as it stands,
    so the keys are exactly the ones this version of Claude Code takes."""
    claude = agent_state.AGENTS[0]
    screen = agent_state.strip_ansi(screen)
    live, kind = claude._plain_question(screen), "permission"
    if live is None:
        for match in claude.PICKER.finditer(screen):
            if not claude.SETTLED.search(screen, match.end()):
                live, kind = match, "question"
    if live is None:
        return None
    start = screen.count("\n", 0, live.start())
    lines = screen.splitlines()
    question = lines[start].strip().strip("│┃ ") if start < len(lines) else ""
    options: list[dict] = []
    for line in lines[start:]:
        text = line.strip().strip("│┃").strip()
        match = OPTION.match(text)
        if match:
            if options and int(match.group(1)) != int(options[-1]["key"]) + 1:
                break           # numbering restarts: not this prompt's list
            options.append({"key": match.group(1),
                            "label": match.group(2).strip()})
        elif options:
            if text and line.startswith((" " * 4, "│ " + " " * 3)) \
                    and not re.match(r"(Esc|Enter|Tab)\b", text):
                options[-1]["label"] += " " + text      # a wrapped label
                continue
            break
    if not options:
        return None
    preview = claude.permission_preview(screen) if kind == "permission" else ""
    command = re.sub(r"^Wants to (?:run )?", "", preview) if preview else ""
    if kind == "question":
        above = screen[:live.start()].splitlines()
        command = next((l.strip().strip("│┃ ") for l in reversed(above[-12:])
                        if l.strip().endswith("?")), question)
    return {"kind": kind, "command": command or question, "options": options}


# The screen's reading of the reply now lives in transcript.py, beside the
# transcript's, so the desktop can use it without importing an HTTPS server;
# the names stay here for the tests and anything else that reads them.
latest_reply = transcript.latest_reply


# --------------------------------------------------------------------------
# The watched state
# --------------------------------------------------------------------------

@dataclass
class Watch:
    """The latest look at every server, as the API serves it, and the
    events between one look and the next.

    The watcher thread calls update(); request threads read under the lock
    and subscribe for events.

    "Finished, not seen" is kept here by time, not by @conn_last_event: the
    desktop stamps too, and a stamp it writes on waking must not turn a
    chat you already read on the phone back into needs_you."""
    rows: dict = field(default_factory=dict)
    projects: list = field(default_factory=list)
    chats: dict = field(default_factory=dict)            # id -> Chat dict
    sessions: dict = field(default_factory=dict)         # id -> session dict
    finish: chatmod.FinishTracker = field(default_factory=chatmod.FinishTracker)
    finished_at: dict = field(default_factory=dict)      # id -> when it ended
    seen_at: dict = field(default_factory=dict)          # id -> when opened
    star_wish: dict = field(default_factory=dict)        # id -> starred
    tasks: dict = field(default_factory=dict)            # (id, path) -> text
    # id -> (what the chat looked like, reply text). Read from the transcript
    # once per change of the chat rather than once per request: the phone
    # asks again every time the chat is opened.
    replies: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    listeners: list = field(default_factory=list)
    started: float = field(default_factory=time.time)

    def update(self, rows: dict, projects: list, live: list,
               now: float | None = None) -> None:
        now = time.time() if now is None else now
        for cid in self.finish.update(live):
            self.finished_at[cid] = now
            connlog.event(f"api {cid}: finished")
        sessions = {}
        for host, row in rows.items():
            if row.get("state") != "up":
                continue
            for session in row.get("sessions") or []:
                sessions[chatmod.chat_id(host, session.get("name", ""))] = session
        events = []
        with self.lock:
            fresh = {}
            for chat in live:
                data = self._chat_json(chat, sessions.get(chat.id) or {})
                old = self.chats.get(chat.id)
                if chat.state == agent_state.UNKNOWN and old is not None \
                        and not old.get("offline"):
                    # A torn capture says nothing: keep what was known
                    # rather than flicker to idle and drop a prompt.
                    data = dict(data, state=old["state"],
                                permission=old["permission"],
                                preview=old["preview"])
                wish = self.star_wish.get(chat.id)
                if wish is not None:
                    if chat.starred == wish:
                        del self.star_wish[chat.id]    # the server has it
                    data["starred"] = wish
                fresh[chat.id] = data
            # A dark server's chats stay listed, as they were, until it
            # answers again and says what is really there.
            up = {h for h, r in rows.items() if r.get("state") == "up"}
            for cid, old in self.chats.items():
                if cid not in fresh and old["host"] not in up \
                        and old["host"] in rows:
                    fresh[cid] = dict(old, state="idle", offline=True,
                                      permission=None)
            for cid, data in fresh.items():
                if self.chats.get(cid) != data:
                    events.append(("chat.updated", data))
            for cid in self.chats:
                if cid not in fresh:
                    events.append(("chat.removed", {"id": cid}))
            old_hosts = {h: self._host_json(h, r) for h, r in self.rows.items()}
            self.rows, self.projects = rows, projects
            self.chats, self.sessions = fresh, sessions
            for host, row in rows.items():
                data = self._host_json(host, row)
                if old_hosts.get(host) != data:
                    events.append(("host.updated", data))
            self.finish.retain(set(fresh))
            for book in (self.finished_at, self.seen_at, self.star_wish):
                for gone in [k for k in book if k not in fresh]:
                    del book[gone]
            self.tasks = {k: v for k, v in self.tasks.items() if k[0] in fresh}
            self.replies = {k: v for k, v in self.replies.items() if k in fresh}
            for name, data in events:
                self._publish(name, data)

    def _needs(self, chat) -> bool:
        if chat.state == agent_state.NEEDS_YOU:
            return True
        ended = self.finished_at.get(chat.id)
        return (chat.state == agent_state.READY and ended is not None
                and self.finish.finished(chat.id)
                and ended > self.seen_at.get(chat.id, 0.0))

    def _chat_json(self, chat, session: dict) -> dict:
        state = "needs_you" if self._needs(chat) else \
            STATE_NAMES.get(chat.state, "idle")
        screen = session.get("screen") or ""
        return {
            "id": chat.id, "host": chat.host, "session": chat.session,
            "project": chat.project, "title": chat.title, "state": state,
            "last_event": chat.last_event, "preview": chat.preview,
            # What it is on now, under the name that holds still; "" if none.
            "doing": chatmod.doing(chat),
            "starred": chat.starred, "offline": False,
            "permission": (permission_of(screen)
                           if chat.state == agent_state.NEEDS_YOU else None),
        }

    @staticmethod
    def _host_json(host: str, row: dict) -> dict:
        state = row.get("state", "unknown")
        status = {"up": "up", "nokey": "needs_login",
                  "unverified": "needs_login"}.get(state, "dark")
        return {"host": host, "status": status,
                "detail": connlog.clean(row.get("error") or "", 200),
                "chats": sum(1 for s in row.get("sessions") or []
                             if (s.get("agent") or {}).get("state")
                             not in (agent_state.SHELL, None))}

    def mark_seen(self, cid: str, now: float | None = None) -> None:
        """A device opened it: a finished turn is no longer news."""
        with self.lock:
            self.seen_at[cid] = time.time() if now is None else now
            chat = self.chats.get(cid)
            if chat is not None and chat["state"] == "needs_you" \
                    and chat["permission"] is None \
                    and (self.sessions.get(cid, {}).get("agent") or {}).get(
                        "state") == agent_state.READY:
                chat = dict(chat, state="idle")
                self.chats[cid] = chat
                self._publish("chat.updated", chat)

    def set_star(self, cid: str, starred: bool) -> None:
        """Starred now, and kept so until the server's option agrees."""
        with self.lock:
            self.star_wish[cid] = starred
            chat = self.chats.get(cid)
            if chat is not None:
                chat = dict(chat, starred=starred)
                self.chats[cid] = chat
                self._publish("chat.updated", chat)

    # -- events ---------------------------------------------------------

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        q.overflowed = False
        with self.lock:
            self.listeners.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.listeners:
                self.listeners.remove(q)

    def _publish(self, name: str, data: dict) -> None:
        for q in list(self.listeners):
            try:
                q.put_nowait((name, data))
            except queue.Full:
                # A stuck client loses events, not the agent -- and is told
                # to start over, rather than left quietly out of date.
                q.overflowed = True

    def publish(self, name: str, data: dict) -> None:
        with self.lock:
            self._publish(name, data)


# --------------------------------------------------------------------------
# Actions -- the desktop's own, over the agent's ssh
# --------------------------------------------------------------------------

@dataclass
class Actions:
    """What the API does on a server. A class so tests can swap it out."""
    store: notes_mod.Store | None = None

    def notes(self) -> notes_mod.Store:
        if self.store is None:
            self.store = notes_mod.Store()
        return self.store

    def start(self, host, text, folder, *, project, source, mode,
              resume, notes_block) -> str:
        started = starting.start_chat(host, text, folder, project=project,
                                      source=source, now=int(time.time()),
                                      mode=mode, resume=resume,
                                      notes_block=notes_block)
        return started.session

    def reply(self, host, session, text) -> None:
        starting.send_text(host, session, text)

    def upload_image(self, host, data: bytes, ext: str) -> str:
        """Put an image from the phone on `host`, in ~/.conn/uploads, and
        return its full path there -- what goes in the reply, since Claude
        Code reads an image named by its path. One ssh, the file inside the
        script as base64 (the script is already the ssh's stdin)."""
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.{ext}"
        encoded = base64.encodebytes(data).decode("ascii")
        script = (
            'd="$HOME/.conn/uploads"\n'
            'mkdir -p "$d" && chmod 700 "$d" || exit 1\n'
            f'f="$d/{name}"\n'
            "cat > \"$f.b64\" <<'CONN_IMAGE_END'\n"
            f"{encoded}"
            "CONN_IMAGE_END\n"
            # GNU and current macOS take -d; older macOS only -D.
            '{ base64 -d < "$f.b64" > "$f" 2>/dev/null || '
            'base64 -D < "$f.b64" > "$f"; } || { rm -f "$f" "$f.b64"; exit 1; }\n'
            'rm -f "$f.b64"\n'
            'printf "%s\\n" "$f"\n')
        try:
            done = subprocess.run(hosts.run_argv(host), input=script.encode(),
                                  capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            raise ApiError(502, f"{host} did not answer")
        path = done.stdout.decode("utf-8", "replace").strip().splitlines()
        if done.returncode or not path or not path[-1].endswith(name):
            raise ApiError(502, f"could not save the image on {host}")
        return path[-1]

    def interrupt(self, host, session) -> None:
        starting.interrupt(host, session)

    def key(self, host, session, key) -> None:
        if not re.fullmatch(r"[0-9]", key):
            raise ApiError(400, "not an option key")
        where = shlex.quote(f"={session}:")
        try:
            done = subprocess.run(
                hosts.run_argv(host),
                input=f"tmux send-keys -t {where} {key}\n".encode(),
                capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            raise ApiError(502, f"{host} did not answer")
        if done.returncode:
            raise ApiError(502, "the server did not take the key")

    def star(self, host, session, starred) -> None:
        hosts.set_chat_option(host, session, "starred",
                              "1" if starred else None)

    def task(self, host, path) -> str:
        if not re.fullmatch(r"~/\.conn/tasks/[A-Za-z0-9._-]+\.md", path or ""):
            return ""
        script = f'head -c {TASK_CAP} "$HOME/{shlex.quote(path[2:])}"\n'
        try:
            done = subprocess.run(hosts.run_argv(host), input=script.encode(),
                                  capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return ""
        return done.stdout.decode(errors="replace") if not done.returncode else ""

    def transcript(self, host, session,
                   timeout: float = TRANSCRIPT_TIMEOUT):
        """Claude's last reply from its transcript (a transcript.Reply, the
        conversation's end with it), or "" for the screen's turn. One ssh
        round trip, at most `timeout`; see transcript.py."""
        reply = transcript.fetch(host, session, timeout=timeout)
        return reply if reply is not None and reply.text else ""


# --------------------------------------------------------------------------
# The API
# --------------------------------------------------------------------------

ROUTES: list[tuple[str, re.Pattern, str]] = []


def route(method: str, pattern: str):
    def wrap(fn):
        ROUTES.append((method, re.compile(f"^{pattern}$"), fn.__name__))
        return fn
    return wrap


SEGMENT = r"([^/]+)"


class Api:
    """Requests in, JSON out. No HTTP here, so it is testable directly:
    call(method, path, query, body, device) -> (status, json-able)."""

    def __init__(self, watch: Watch, devices: Devices, actions: Actions,
                 version: str = "A1"):
        self.watch, self.devices, self.actions = watch, devices, actions
        self.version = version

    def call(self, method: str, path: str, query: dict, body,
             device: str | None):
        for verb, pattern, name in ROUTES:
            match = pattern.match(path)
            if match and verb == method:
                if name != "pair" and device is None:
                    raise ApiError(401, "not paired")
                args = [unquote(g) for g in match.groups()]
                return getattr(self, name)(*args, query=query, body=body,
                                           device=device)
        raise ApiError(404, "no such endpoint")

    # -- helpers ----------------------------------------------------------

    def _chat(self, host: str, session: str) -> dict:
        cid = chatmod.chat_id(host, session)
        with self.watch.lock:
            chat = self.watch.chats.get(cid)
        if chat is None:
            raise ApiError(404, "no such chat")
        return chat

    def _live(self, host: str, session: str) -> dict:
        chat = self._chat(host, session)
        if chat.get("offline"):
            raise ApiError(409, f"{host} is not answering")
        return chat

    def _project(self, pid: str):
        with self.watch.lock:
            projects = list(self.watch.projects)
        for project in projects:
            if project.id == pid:
                return project
        raise ApiError(400, "no such project")

    @staticmethod
    def _text(body, key: str = "text", cap: int = 64 * 1024) -> str:
        text = (body or {}).get(key)
        if not isinstance(text, str) or not text.strip():
            raise ApiError(400, f"{key} is required")
        if len(text) > cap:
            raise ApiError(413, f"{key} is too long")
        return text

    def _start(self, project_id: str, host: str | None, text: str,
               source: str, resume: bool = False,
               include_notes: bool = True) -> dict:
        if not isinstance(project_id, str) or (host is not None
                                               and not isinstance(host, str)):
            raise ApiError(400, "project and host are names")
        project = self._project(project_id)
        servers = list(project.servers)
        host = host or (servers[0] if servers else "")
        if host not in project.servers:
            raise ApiError(400, f"{host or 'no server'} is not in "
                                f"{project.name}")
        with self.watch.lock:
            row = self.watch.rows.get(host) or {}
        if row.get("state") != "up":
            raise ApiError(409, f"{host} is not answering")
        block = ""
        if include_notes:
            block = notes_mod.notes_block(self.actions.notes().list(project.id))
        try:
            session = self.actions.start(
                host, text, project.servers[host], project=project.id,
                source=source, mode=project.mode, resume=resume,
                notes_block=block)
        except starting.StartError as exc:
            raise ApiError(502, str(exc))
        connlog.event(f"api: started {host}/{session} ({source})")
        title = text.strip().splitlines()[0][:40]
        chat = {"id": chatmod.chat_id(host, session), "host": host,
                "session": session, "project": project.id, "title": title,
                "state": "working", "last_event": int(time.time()),
                "preview": "", "starred": False, "offline": False,
                "permission": None}
        return chat

    # -- endpoints --------------------------------------------------------

    @route("GET", "/health")
    def health(self, **_):
        with self.watch.lock:
            states = [r.get("state") for r in self.watch.rows.values()]
        return 200, {"ok": True, "version": self.version,
                     "hosts_up": states.count("up"),
                     "hosts_dark": len(states) - states.count("up")}

    @route("GET", "/projects")
    def projects(self, **_):
        with self.watch.lock:
            projects = list(self.watch.projects)
            rows = dict(self.watch.rows)
        out = []
        for p in projects:
            out.append({"id": p.id, "name": p.name, "letter": p.letter,
                        "color": p.color, "bg": p.bg, "servers": p.servers,
                        # JSON objects lose their order in most decoders;
                        # the first server is the default one.
                        "server_order": list(p.servers),
                        "agent": p.agent, "mode": p.mode,
                        "implicit": p.implicit,
                        "server_status": {
                            h: Watch._host_json(h, rows.get(h) or {})["status"]
                            for h in p.servers}})
        return 200, out

    @route("GET", "/hosts")
    def hosts_list(self, **_):
        with self.watch.lock:
            rows = dict(self.watch.rows)
        return 200, [Watch._host_json(h, r) for h, r in rows.items()]

    @route("GET", "/chats")
    def chat_list(self, query, **_):
        with self.watch.lock:
            listed = list(self.watch.chats.values())
        project = (query.get("project") or [""])[0]
        server = (query.get("server") or [""])[0]
        needs = (query.get("needs_you") or [""])[0] == "1"
        listed = [c for c in listed
                  if (not project or c["project"] == project)
                  and (not server or c["host"] == server)
                  and (not needs or c["state"] == "needs_you")]
        listed.sort(key=lambda c: (-c["last_event"], c["id"]))
        return 200, listed

    @route("GET", f"/chats/{SEGMENT}/{SEGMENT}")
    def chat_detail(self, host, session, **_):
        began = time.monotonic()
        chat = self._chat(host, session)
        cid = chat["id"]
        with self.watch.lock:
            sess = dict(self.watch.sessions.get(cid) or {})
        screen = sess.get("screen") or ""
        options = sess.get("options") or {}
        agent = sess.get("agent") or {}
        detail = dict(chat)
        path = options.get("task") or ""
        with self.watch.lock:
            task = self.watch.tasks.get((cid, path))
        if task is None and path and not chat.get("offline"):
            task = self.actions.task(host, path)      # once per chat, then kept
            with self.watch.lock:
                self.watch.tasks[(cid, path)] = task
        reply, history = self._reply(
            chat, host, session, screen,
            DETAIL_BUDGET - (time.monotonic() - began), with_history=True)
        detail.update({
            "task": task or "",
            "latest_reply": reply,
            # The conversation's end, from the transcript: [] when there is
            # none, and the phone shows task + latest_reply as before.
            "history": history,
            "recap": agent.get("recap", ""),
            "started": int(sess.get("created") or 0),
            "windows": [],
        })
        self.watch.mark_seen(cid)
        return 200, detail

    def _reply(self, chat, host, session, screen,
               left: float = DETAIL_BUDGET, with_history: bool = False):
        """The reply's exact text from the transcript when there is one, the
        screen's reading of it when not. Kept per chat until the chat's
        stamp or state moves -- a new reply moves both -- so opening the
        same chat twice is one ssh, not two. A miss is kept too: a server
        without a transcript does not get asked again until something
        changes. In the request thread, like task: never the watcher's."""
        cid = chat["id"]
        seen = (chat.get("last_event"), chat.get("state"))
        with self.watch.lock:
            kept = self.watch.replies.get(cid)
        history: list = []
        if kept is not None and kept[0] == seen:
            text, history = kept[1], kept[2]
        elif chat.get("offline"):
            text = ""
        elif left < TRANSCRIPT_FLOOR:
            # The task ssh used the time up: the screen now, and nothing
            # kept, so the next open (task cached by then) asks.
            text = ""
        else:
            got = self.actions.transcript(
                host, session, timeout=min(TRANSCRIPT_TIMEOUT, left))
            if isinstance(got, str):
                text = got
            else:
                text, history = got.text, list(got.history)
            with self.watch.lock:
                self.watch.replies[cid] = (seen, text, history)
            connlog.event(f"api {cid}: reply from "
                          + ("the transcript" if text else "the screen"))
        text = text or latest_reply(screen)
        return (text, history) if with_history else text

    @route("POST", "/chats")
    def chat_start(self, body, **_):
        body = body or {}
        from_note = body.get("from_note")
        if from_note:
            if not isinstance(from_note, str):
                raise ApiError(400, "from_note is a note id")
            return self.note_start(from_note, body=body)
        text = self._text(body)
        chat = self._start(body.get("project") or "",
                           body.get("host") or None, text, "phone",
                           resume=bool(body.get("continue")),
                           include_notes=body.get("include_notes", True)
                           is not False)
        return 201, chat

    @route("POST", f"/chats/{SEGMENT}/{SEGMENT}/reply")
    def chat_reply(self, host, session, body, **_):
        self._live(host, session)
        text = self._text(body)
        try:
            self.actions.reply(host, session, text)
        except starting.StartError as exc:
            raise ApiError(502, str(exc))
        connlog.event(f"api: reply to {host}/{session}")
        return 204, None

    @route("POST", f"/chats/{SEGMENT}/{SEGMENT}/images")
    def chat_image(self, host, session, body, **_):
        """{"data": base64, "type": "jpg" | "png"} -> {"path": ...}: the image
        saved on the chat's server, for the phone to put in its reply."""
        self._live(host, session)
        body = body or {}
        ext = body.get("type")
        if ext not in IMAGE_TYPES:
            raise ApiError(400, "type is jpg or png")
        raw = body.get("data")
        if not isinstance(raw, str) or not raw:
            raise ApiError(400, "data is the image, base64")
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise ApiError(400, "data is not base64")
        if len(data) > IMAGE_BYTES:
            raise ApiError(413, "image too large")
        if not data.startswith(IMAGE_TYPES[ext]):
            raise ApiError(400, f"that is not a {ext}")
        path = self.actions.upload_image(host, data, ext)
        connlog.event(f"api: image to {host}/{session} ({len(data) // 1024} KB)")
        return 200, {"path": path}

    @route("POST", f"/chats/{SEGMENT}/{SEGMENT}/approve")
    def chat_approve(self, host, session, body, **_):
        chat = self._live(host, session)
        key = (body or {}).get("key")
        if not isinstance(key, str):
            raise ApiError(400, "key is the option's number")
        offered = {o["key"] for o in (chat.get("permission") or {}).get(
            "options", [])}
        if key not in offered:
            raise ApiError(409, "that option is not on screen")
        self.actions.key(host, session, key)
        connlog.event(f"api: answered {host}/{session} with {key}")
        return 204, None

    @route("POST", f"/chats/{SEGMENT}/{SEGMENT}/interrupt")
    def chat_interrupt(self, host, session, **_):
        self._live(host, session)
        try:
            self.actions.interrupt(host, session)
        except starting.StartError as exc:
            raise ApiError(502, str(exc))
        return 204, None

    @route("PUT", f"/chats/{SEGMENT}/{SEGMENT}/star")
    def chat_star(self, host, session, body, **_):
        self._live(host, session)
        starred = (body or {}).get("starred")
        if not isinstance(starred, bool):
            raise ApiError(400, "starred must be true or false")
        try:
            self.actions.star(host, session, starred)
        except hosts.HostError as exc:
            raise ApiError(502, str(exc))
        self.watch.set_star(chatmod.chat_id(host, session), starred)
        return 204, None

    @route("POST", f"/chats/{SEGMENT}/{SEGMENT}/recap-to-notes")
    def chat_recap(self, host, session, **_):
        chat = self._chat(host, session)
        with self.watch.lock:
            sess = dict(self.watch.sessions.get(chat["id"]) or {})
        text = (sess.get("agent") or {}).get("recap") or \
            self._reply(chat, host, session,
                        sess.get("screen") or "").split("\n\n")[0]
        if not text.strip():
            raise ApiError(409, "nothing to save yet")
        try:
            note = self.actions.notes().create(
                chat["project"], text, source="recap", kind="recap",
                chat=chat["id"])
        except (notes_mod.NoteError, OSError) as exc:
            raise ApiError(400, str(exc))
        self.watch.publish("note.updated", note)
        return 201, note

    @route("GET", "/notes")
    def note_list(self, query, **_):
        project = (query.get("project") or [""])[0]
        status = (query.get("status") or [""])[0]
        return 200, self.actions.notes().list(project, status)

    @route("POST", "/notes")
    def note_create(self, body, **_):
        body = body or {}
        text = self._text(body)
        source = str(body.get("source") or "phone")
        if source not in ("phone", "desktop", "quick-add"):
            source = "phone"
        try:
            note = self.actions.notes().create(str(body.get("project") or ""),
                                               text, source=source)
        except (notes_mod.NoteError, OSError, TypeError, ValueError) as exc:
            raise ApiError(400, str(exc))
        self.watch.publish("note.updated", note)
        return 201, note

    @route("PATCH", f"/notes/{SEGMENT}")
    def note_patch(self, note_id, body, **_):
        body = body or {}
        changes = {}
        if "text" in body:
            changes["text"] = self._text(body)
        if "status" in body:
            if body["status"] not in ("open", "started", "done"):
                raise ApiError(400, "status is open, started or done")
            changes["status"] = body["status"]
        try:
            note = self.actions.notes().update(note_id, **changes)
        except (notes_mod.NoteError, OSError) as exc:
            raise ApiError(404, str(exc))
        self.watch.publish("note.updated", note)
        return 200, note

    @route("DELETE", f"/notes/{SEGMENT}")
    def note_delete(self, note_id, **_):
        try:
            self.actions.notes().delete(note_id)
        except (notes_mod.NoteError, OSError) as exc:
            raise ApiError(404, str(exc))
        self.watch.publish("note.updated", {"id": note_id, "deleted": True})
        return 204, None

    @route("POST", f"/notes/{SEGMENT}/start")
    def note_start(self, note_id, body, **_):
        body = body or {}
        try:
            note = self.actions.notes().get(note_id)
        except (notes_mod.NoteError, OSError) as exc:
            raise ApiError(404, str(exc))
        if body.get("host") is not None and not isinstance(body["host"], str):
            raise ApiError(400, "host is a name")
        chat = self._start(note["project"], body.get("host") or None,
                           note.get("text", ""), f"note:{note_id}",
                           include_notes=body.get("include_notes", True)
                           is not False)
        try:
            note = self.actions.notes().update(note_id, status="started",
                                               chat=chat["id"])
            self.watch.publish("note.updated", note)
        except (notes_mod.NoteError, OSError, TypeError):
            pass
        return 201, chat

    @route("POST", "/devices/me/active")
    def device_active(self, device, **_):
        self.devices.set(device, active=int(time.time()))
        return 204, None

    @route("PUT", "/devices/me/apns")
    def device_apns(self, device, body, **_):
        token = str((body or {}).get("token") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{32,200}", token):
            raise ApiError(400, "not an APNs token")
        self.devices.set(device, apns=token)
        return 204, None

    @route("POST", "/pair/codes")
    def pair_code(self, device, **_):
        if self.devices.get(device).get("kind") != "desktop":
            raise ApiError(403, "only the desktop makes pairing codes")
        code, expires = self.devices.new_code()
        return 201, {"code": code, "expires": int(expires)}

    @route("POST", "/pair")
    def pair(self, body, **_):
        body = body or {}
        if not self.devices.redeem(str(body.get("code") or "")):
            raise ApiError(403, "that code is wrong or has expired")
        device_id, token = self.devices.create(
            str(body.get("device_name") or "iPhone"), kind="phone")
        connlog.event(f"api: paired {device_id}")
        return 201, {"token": token, "device_id": device_id}

    @route("GET", "/devices")
    def device_list(self, **_):
        return 200, self.devices.listing()

    @route("DELETE", f"/devices/{SEGMENT}")
    def device_delete(self, device_id, **_):
        if not self.devices.remove(device_id):
            raise ApiError(404, "no such device")
        connlog.event(f"api: revoked {device_id}")
        return 204, None


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    api: Api                                # set on the class by serve()
    server_version = "conn-agent"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 120.0                         # a client that goes quiet is let go

    def setup(self) -> None:
        # The TLS handshake was left to this thread (see serve()); do it
        # now, against a short clock, so a client that opens a socket and
        # says nothing gives its slot back in seconds.
        self.request.settimeout(HANDSHAKE_TIMEOUT)
        if isinstance(self.request, ssl.SSLSocket):
            self.request.do_handshake()
        super().setup()

    def log_message(self, *args) -> None:   # no request lines in the log
        pass

    def _device(self) -> str | None:
        header = self.headers.get("Authorization", "")
        token = header[7:].strip() if header.startswith("Bearer ") else ""
        return self.api.devices.check(token)

    def _send(self, status: int, data=None) -> None:
        body = b"" if data is None else json.dumps(data).encode()
        self.send_response(status)
        if data is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _body(self, limit: int = MAX_BODY):
        """The JSON object sent, or None. Only a plain Content-Length is
        taken: anything else could have us read without end."""
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(501, "send a Content-Length body")
        raw_length = (self.headers.get("Content-Length") or "0").strip()
        if not raw_length.isdigit():
            raise ApiError(400, "bad Content-Length")
        length = int(raw_length)
        if length > limit:
            raise ApiError(413, "too large")
        raw = self.rfile.read(length) if length else b""
        if len(raw) != length:
            raise ApiError(400, "the body was cut short")
        self._read = True
        body = json.loads(raw) if raw else None
        if body is not None and not isinstance(body, dict):
            raise ApiError(400, "send a JSON object")
        return body

    def _handle(self, method: str) -> None:
        parts = urlsplit(self.path)
        self.request.settimeout(self.timeout)
        self._read = False
        try:
            device = self._device()
            if device is None and not (method == "POST" and parts.path == "/pair"):
                raise ApiError(401, "not paired")
            image = (device is not None and method == "POST"
                     and parts.path.endswith("/images"))
            body = self._body(MAX_IMAGE_BODY if image else MAX_BODY)
            if method == "GET" and parts.path == "/events":
                self._events(device)
                return
            status, data = self.api.call(method, parts.path,
                                         parse_qs(parts.query), body, device)
            self._send(status, data)
        except ApiError as exc:
            # Whatever was not read must not be taken for the next request.
            if not self._read:
                self.close_connection = True
            self._send(exc.status, {"error": str(exc)})
        except (ValueError, UnicodeDecodeError, RecursionError):
            self._send(400, {"error": "not JSON"})
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError,
                TimeoutError):
            self.close_connection = True
        except Exception as exc:            # noqa: BLE001
            connlog.event(f"api: {method} {parts.path} failed: "
                          f"{type(exc).__name__}")
            self.close_connection = True
            try:
                self._send(500, {"error": "the agent hit a problem"})
            except OSError:
                pass

    def _events(self, device: str) -> None:
        """Server-sent events until the client goes, or is revoked."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        q = self.api.watch.subscribe()
        try:
            self.wfile.write(b": hello\n\n")
            self.wfile.flush()
            while self.api.devices.get(device):
                if q.overflowed:
                    # Fell behind: say so and end, and the app refetches.
                    self.wfile.write(b"event: resync\ndata: {}\n\n")
                    self.wfile.flush()
                    return
                try:
                    name, data = q.get(timeout=SSE_PING)
                    chunk = f"event: {name}\ndata: {json.dumps(data)}\n\n"
                except queue.Empty:
                    chunk = ": ping\n\n"
                if not self.api.devices.get(device):
                    return          # revoked while we waited
                self.wfile.write(chunk.encode())
                self.wfile.flush()
        except (OSError, ssl.SSLError):
            pass
        finally:
            self.api.watch.unsubscribe(q)

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_PUT(self): self._handle("PUT")
    def do_PATCH(self): self._handle("PATCH")
    def do_DELETE(self): self._handle("DELETE")


class Server(ThreadingHTTPServer):
    """One thread a connection, but only so many: MAX_CONNECTIONS in all
    and MAX_PER_CLIENT from any one address, before a word of TLS. A LAN
    host opening sockets by the hundred gets them closed, and the agent
    keeps its file descriptors for its own ssh."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        self.slots = threading.Semaphore(MAX_CONNECTIONS)
        self.per_client: dict[str, int] = {}
        self.count_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        address = client_address[0]
        with self.count_lock:
            busy = self.per_client.get(address, 0)
            if busy >= MAX_PER_CLIENT or not self.slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            self.per_client[address] = busy + 1
        try:
            super().process_request(request, client_address)
        except Exception:
            self._release(address)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(client_address[0])

    def _release(self, address: str) -> None:
        with self.count_lock:
            left = self.per_client.get(address, 1) - 1
            if left > 0:
                self.per_client[address] = left
            else:
                self.per_client.pop(address, None)
        self.slots.release()

    def handle_error(self, request, client_address) -> None:
        # A failed handshake or a dropped socket is a fact of networks, not
        # a traceback on stderr.
        # The kind of failure, never what was sent: an SSL alert from the
        # client (it refused the certificate) reads very differently from a
        # handler that raised.
        error = sys.exc_info()[1]
        why = type(error).__name__ if error is not None else "unknown"
        if isinstance(error, ssl.SSLError):
            why += f" {getattr(error, 'reason', '') or ''}".rstrip()
        elif error is not None and not isinstance(error, OSError):
            frame = traceback.extract_tb(error.__traceback__)[-1:]
            if frame:
                why += f" at {Path(frame[0].filename).name}:{frame[0].lineno}"
        connlog.limited(("api", "conn-error", client_address[0], why),
                        f"api: a connection from {client_address[0]} failed "
                        f"({why})")


def tls_context(directory: Path = TLS_DIR) -> tuple[ssl.SSLContext, str]:
    """The agent's own certificate, made on first use: (context, sha256
    fingerprint). The app pins the fingerprint when it pairs."""
    cert, key = directory / "cert.pem", directory / "key.pem"
    if not (cert.exists() and key.exists()):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-days", "3650", "-subj", "/CN=conn-agent",
             "-keyout", str(key), "-out", str(cert)],
            check=True, capture_output=True)
        os.chmod(key, 0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return context, fingerprint(cert)


def fingerprint(cert: Path) -> str:
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    return hashlib.sha256(der).hexdigest()


def serve(api: Api, host: str, port: int,
          context: ssl.SSLContext | None) -> Server:
    """Start the API on a thread; returns the server (shutdown() to stop)."""
    handler = type("BoundHandler", (Handler,), {"api": api})
    try:                            # launchd's default of 256 is tight
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = min(4096, hard if hard != resource.RLIM_INFINITY else 4096)
        if soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except (ImportError, ValueError, OSError):
        pass
    server = Server((host, port), handler)
    if context is not None:
        # The handshake waits for the handler's thread (first read), not
        # accept(): one slow client must not hold the door for everyone.
        server.socket = context.wrap_socket(server.socket, server_side=True,
                                            do_handshake_on_connect=False)
    threading.Thread(target=server.serve_forever, name="api",
                     daemon=True).start()
    return server
