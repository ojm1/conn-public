#!/usr/bin/env python3
"""conn-agent: Conn's always-on half, on the Mac mini.

It looks at every server every few seconds with the desktop's own probe and
classifier (hosts.py, chats.py, agent_state.py -- the same files, not a copy
of their logic) and:

  - serves the API the iPhone and iPad app use (agent_api.py, SPEC §6), on
    the local network, HTTPS with its own pinned certificate;
  - stamps @conn_last_event on state changes (SPEC §2), so the chat list is
    in the right order with the laptop shut;
  - optionally sends ntfy alerts (SPEC §7's A0 stopgap), off unless
    agent.toml names an ntfy_topic.

Alerts, when on: one per transition into needing you -- Claude finished a
turn, or is asking -- never repeated while the chat stays put, held while you
are at a keyboard (anyone typed on any server in the last two minutes, or a
desktop said it is in use) and sent once you are not. Titles and a fixed
phrase only: ntfy.sh is a public relay.

The log has chat ids and transitions, never chat, note or task text.

  conn_agent.py run               the agent (what launchd runs)
  conn_agent.py once              one look at every server, printed
  conn_agent.py pair              a one-time code to pair a phone with
  conn_agent.py token <name>      a token for a desktop (shown once)
  conn_agent.py test-ping         send one ntfy test message
"""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import agent_state
import chats
import connlog
import hosts

VERSION = "A1"
CONFIG = Path("~/.config/conn/agent.toml").expanduser()
DEFAULTS = {"ntfy_server": "https://ntfy.sh", "ntfy_topic": "",
            "interval": 4.0, "quiet_after_typing": 120,
            "listen": "0.0.0.0", "port": 8742}
# A probe that has not come back in this long is skipped for this round,
# not waited on: one hung server must not hold up everyone else's alerts.
PROBE_BUDGET = 25.0

# A server that refuses the agent's key (or whose host key ssh will not
# trust) is asked again after a minute, then two, doubling to half an hour:
# a refused login every 4 s is ~20,000 a day, enough for a server's
# fail2ban to ban the address it comes from -- and everything else there too.
REFUSED = ("nokey", "unverified")
BACKOFF_FIRST = 60.0
BACKOFF_MAX = 1800.0
WORKERS = 16
SEND_TIMEOUT = 10.0
TITLE_CAP = 80

FINISHED = "Finished, waiting on you"
ASKING = "Waiting on you"
PERMISSION = "Wants to run a command"
PERMISSION_QUESTION = re.compile(r"\s*Do you want to\b", re.I)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def _toml_string(value: str) -> str:
    return json.dumps(value)        # a JSON string is a valid TOML basic string


def load_config(path: Path = CONFIG, create: bool = True) -> dict:
    """agent.toml, made on first use with the defaults written out. 0600:
    an ntfy topic, if you add one, is a secret -- anyone who knows it can
    read and post."""
    config = dict(DEFAULTS)
    try:
        with path.open("rb") as handle:
            config.update(tomllib.load(handle))
    except FileNotFoundError:
        if not create:
            raise
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        text = ("# conn-agent\n"
                f"interval = {DEFAULTS['interval']}\n"
                "# The API for the phone app: every address on this Mac, "
                "local network only.\n"
                f"listen = {_toml_string(DEFAULTS['listen'])}\n"
                f"port = {DEFAULTS['port']}\n"
                "# ntfy alerts are off. To turn them on, set a long random "
                "topic and subscribe to it.\n"
                '# ntfy_topic = ""\n'
                f"ntfy_server = {_toml_string(DEFAULTS['ntfy_server'])}\n"
                "# No alerts while you typed into a terminal this recently "
                "(seconds; 0: always alert).\n"
                f"quiet_after_typing = {DEFAULTS['quiet_after_typing']}\n")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(text)
        except FileExistsError:
            pass            # made by the other process a moment ago
    topic = str(config.get("ntfy_topic") or "")
    if topic and (len(topic) < 20
                  or not all(c.isalnum() or c in "-_" for c in topic)):
        raise SystemExit(f"conn-agent: {path}: ntfy_topic must be a long "
                         "random string of letters, digits, - and _")
    config["ntfy_topic"] = topic
    try:
        config["port"] = int(config["port"])
    except (TypeError, ValueError):
        config["port"] = DEFAULTS["port"]
    server = str(config.get("ntfy_server") or "")
    if not server.startswith(("https://", "http://")):
        raise SystemExit(f"conn-agent: {path}: ntfy_server must start with "
                         "https:// (or http://)")
    for key, floor in (("interval", 2.0), ("quiet_after_typing", 0.0)):
        try:
            config[key] = max(floor, float(config[key]))
        except (TypeError, ValueError):
            config[key] = float(DEFAULTS[key])
    return config


# --------------------------------------------------------------------------
# Deciding what is worth a ping
# --------------------------------------------------------------------------

@dataclass
class Ping:
    chat_id: str
    title: str
    message: str
    reason: str         # "finished" | "asking" | "permission", for the log


@dataclass
class Alerter:
    """Turns successive looks at the chats into the pings now due.

    Pure: feed it build_chats() output, the permission prompts, how long ago
    anyone typed (None: nobody at a terminal) and the time, get back the
    pings. Nothing here touches the network, so the rules can be tested on
    their own.

    A ping due while you are at the keyboard is held, not dropped: you may
    have typed the prompt and shut the lid, and the laptop's frozen client
    then reads as "typed 90s ago" for as long as it sleeps. It goes out once
    nobody has typed for quiet_after_typing -- if the chat is still where it
    was. Answered, working again or gone, it is dropped; so is one held past
    HOLD_MAX, since you were plainly at the desk to see it.
    """
    quiet_after_typing: float = 120.0
    finish: chats.FinishTracker = field(default_factory=chats.FinishTracker)
    _blocked: dict[str, bool] = field(default_factory=dict)
    _held: dict[str, tuple[Ping, float]] = field(default_factory=dict)
    # Hosts seen answering at least once. A chat that turns up on one of
    # them later is new, not old news: if it is already waiting, say so.
    _hosts: set[str] = field(default_factory=set)

    HOLD_MAX = 600.0

    def update(self, live: list, permission: set[str],
               typed_ago: float | None = None, now: float = 0.0,
               up: set[str] | None = None) -> list[Ping]:
        quiet = bool(self.quiet_after_typing and typed_ago is not None
                     and typed_ago <= self.quiet_after_typing)
        due = []
        finished = self.finish.update(live)
        for chat in live:
            state = chat.state
            if state == agent_state.UNKNOWN:
                continue        # no information: keep what was known
            blocked = state == agent_state.NEEDS_YOU
            before = self._blocked.get(chat.id)
            if before is None and chat.host in self._hosts:
                before = False  # new since the agent started: news
            self._blocked[chat.id] = blocked
            if blocked and before is False:
                reason = "permission" if chat.id in permission else "asking"
            elif chat.id in finished:
                reason = "finished"
            else:
                continue
            message = {"finished": FINISHED, "asking": ASKING,
                       "permission": PERMISSION}[reason]
            due.append(Ping(chat.id, _title(chat.title), message, reason))
        self._hosts |= up if up is not None else {c.host for c in live}

        # What was held: still standing, or overtaken?
        states = {chat.id: chat.state for chat in live}
        for cid, (ping, since) in list(self._held.items()):
            state = states.get(cid)
            standing = (state == agent_state.UNKNOWN
                        or (ping.reason == "finished"
                            and state in (agent_state.READY,
                                          agent_state.DRAFT))
                        or (ping.reason != "finished"
                            and state == agent_state.NEEDS_YOU))
            if cid not in states and cid.partition("/")[0] in self._hosts \
                    and (up is None or cid.partition("/")[0] in up):
                standing = False            # the session went
            if not standing or now - since > self.HOLD_MAX:
                del self._held[cid]
                connlog.event(f"alert {cid}: {ping.reason}, dropped "
                              + ("(held too long)" if standing
                                 else "(it moved on)"))
        for ping in due:
            self._held[ping.chat_id] = (ping, now)  # a newer one replaces it
        if quiet:
            for ping in due:
                connlog.event(f"alert {ping.chat_id}: {ping.reason}, held "
                              f"(typed {typed_ago:.0f}s ago)")
            return []
        out = [ping for ping, _ in self._held.values()]
        self._held.clear()
        return out

    def retain(self, chat_ids) -> None:
        keep = set(chat_ids)
        self._blocked = {k: v for k, v in self._blocked.items() if k in keep}
        self._held = {k: v for k, v in self._held.items() if k in keep}
        self.finish.retain(keep)


def _title(text: str) -> str:
    """A chat title fit for a notification: one line, no control
    characters, capped."""
    text = " ".join(connlog.clean(text or "", 400).split())
    return (text[:TITLE_CAP - 1] + "…") if len(text) > TITLE_CAP else text


# --------------------------------------------------------------------------
# Looking at the servers
# --------------------------------------------------------------------------

def watched_hosts() -> list[str]:
    """CONN_AGENT_HOSTS (comma-separated, as agent_setup.py writes into the
    launchd job) when set -- the mini's own ssh config has entries of its
    own, and the same box under two names would ping twice -- else every
    host in ~/.ssh/config."""
    named = [h.strip() for h in os.environ.get("CONN_AGENT_HOSTS", "").split(",")]
    return [h for h in named if h] or hosts.list_hosts()


def look(host_list: list[str], pool: ThreadPoolExecutor) -> dict:
    """{host: probe row}, every host at once; a host whose probe overran
    the budget comes back as unknown for this round."""
    futures = {host: pool.submit(hosts.probe, host) for host in host_list}
    rows = {}
    deadline = time.monotonic() + PROBE_BUDGET
    for host, future in futures.items():
        try:
            rows[host] = future.result(max(0.0, deadline - time.monotonic()))
        except Exception as exc:        # noqa: BLE001 -- timeout or a bug
            row = hosts.blank(host)
            row["state"], row["error"] = "unknown", connlog.clean(exc, 120)
            rows[host] = row
    return rows


def digest(rows: dict, projects: list
           ) -> tuple[list, set[str], float | None, set[str]]:
    """(live chats, permission-prompt chat ids, seconds since anyone typed
    on any server -- None when nobody is at a terminal -- and up hosts)."""
    up = {host for host, row in rows.items() if row.get("state") == "up"}
    view = {host: dict(row, hostnames=(row.get("hostname") or "",))
            for host, row in rows.items() if host in up}
    live = chats.build_chats(view, projects)
    permission: set[str] = set()
    typed = [rows[host]["typed_ago"] for host in up
             if rows[host].get("typed_ago") is not None]
    claude = agent_state.AGENTS[0]
    for host in up:
        for session in rows[host].get("sessions") or []:
            cid = chats.chat_id(host, session.get("name", ""))
            agent = session.get("agent") or {}
            if agent.get("state") != agent_state.NEEDS_YOU:
                continue
            # Claude's permission dialogs all ask "Do you want to ...?" (run
            # this, make this edit, proceed); a question of its own does not.
            if (claude.permission_preview(session.get("screen") or "")
                    or PERMISSION_QUESTION.match(agent.get("detail") or "")):
                permission.add(cid)
    return live, permission, (min(typed) if typed else None), up


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------

def send(config: dict, ping: Ping) -> None:
    """One ntfy message. JSON to the server root, so a title in any script
    survives (headers are Latin-1). Raises OSError on failure."""
    body = json.dumps({"topic": config["ntfy_topic"], "title": ping.title,
                       "message": ping.message, "priority": 4,
                       "tags": ["hourglass" if ping.reason != "finished"
                                else "white_check_mark"]}).encode()
    request = urllib.request.Request(
        config["ntfy_server"].rstrip("/") + "/", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": f"conn-agent/{VERSION}"})
    with urllib.request.urlopen(request, timeout=SEND_TIMEOUT) as answer:
        if answer.status >= 300:
            raise OSError(f"ntfy answered {answer.status}")


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------

class Stamper:
    """Writes @conn_last_event behind the loop, one ssh at a time, newest
    value per chat only (SPEC §2: once the agent runs, it writes the
    state-change times). A slow server delays its stamps, never the loop."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending: dict[tuple[str, str], str] = {}
        self.wake = threading.Event()
        threading.Thread(target=self._run, name="stamps", daemon=True).start()

    def put(self, host: str, session: str, stamp: int) -> None:
        with self.lock:
            self.pending[(host, session)] = str(stamp)
        self.wake.set()

    def _run(self) -> None:
        while True:
            self.wake.wait()
            with self.lock:
                if not self.pending:
                    self.wake.clear()
                    continue
                key = next(iter(self.pending))
                value = self.pending.pop(key)
            try:
                hosts.set_chat_option(*key, "last_event", value)
            except (hosts.HostError, OSError) as exc:
                connlog.limited(("stamp", key[0]),
                                f"stamp {key[0]}/{key[1]}: not written "
                                f"({connlog.clean(exc, 120)})")


@dataclass
class Agent:
    """Everything the loop carries from one round to the next."""
    config: dict
    alerter: Alerter
    watch: object = None            # agent_api.Watch, when the API runs
    devices: object = None          # agent_api.Devices
    stamper: Stamper | None = None
    recency: chats.RecencyTracker = field(default_factory=chats.RecencyTracker)
    state: dict = field(default_factory=dict)
    known: set = field(default_factory=set)
    # Stamps written but not yet read back: laid over each round until the
    # server agrees, as the desktop does, so a chat does not jump back.
    stamps: dict = field(default_factory=dict)
    shells: set = field(default_factory=set)
    # host -> (monotonic time of the next try, the wait after that one)
    backoff: dict = field(default_factory=dict)
    rows: dict = field(default_factory=dict)    # each host's last probe row

    def due(self, host_list: list[str], now: float) -> list[str]:
        """The hosts to probe this round: all but those backing off."""
        return [h for h in host_list
                if h not in self.backoff or self.backoff[h][0] <= now]

    def settle(self, rows: dict, now: float) -> None:
        """Start, double or clear each probed host's backoff."""
        for host, row in rows.items():
            if row.get("state") in REFUSED:
                wait = self.backoff.get(host, (0.0, BACKOFF_FIRST))[1]
                self.backoff[host] = (now + wait, min(wait * 2, BACKOFF_MAX))
            elif row.get("state") != "unknown":
                self.backoff.pop(host, None)


def run(config: dict) -> int:
    log = connlog.setup(_log_path())
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    agent = Agent(config, Alerter(quiet_after_typing=config["quiet_after_typing"]))
    lines = [f"conn-agent {VERSION}; log {log}"]
    if config.get("port"):
        import agent_api
        agent.watch, agent.devices = agent_api.Watch(), agent_api.Devices()
        agent.stamper = Stamper()
        context, print_ = agent_api.tls_context()
        api = agent_api.Api(agent.watch, agent.devices, agent_api.Actions(),
                            VERSION)
        agent_api.serve(api, config["listen"], config["port"], context)
        lines.append(f"API on https://{config['listen']}:{config['port']}, "
                     f"certificate sha256 {print_}")
    lines.append(f"ntfy alerts to {config['ntfy_topic']}" if config.get(
        "ntfy_topic") else "ntfy alerts off")
    print("\n".join(lines), flush=True)
    connlog.event(f"agent start: pid {os.getpid()}, every "
                  f"{config['interval']:g}s")
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        while not stop.is_set():
            started = time.monotonic()
            try:
                one_round(agent, pool)
            except Exception as exc:        # noqa: BLE001
                # A bug or a bad answer must not end the loop: launchd would
                # restart it with blank trackers, and every chat mid-turn
                # would then be first sight, its alert lost.
                connlog.event(f"round failed: {type(exc).__name__}: "
                              f"{connlog.clean(exc, 160)}")
            connlog.flush()
            stop.wait(max(0.5, config["interval"]
                          - (time.monotonic() - started)))
    connlog.event("agent exit")
    connlog.shutdown()
    return 0


def one_round(agent: Agent, pool: ThreadPoolExecutor) -> None:
    """One look at every server: the API's state, the stamps and the
    alerts it makes due."""
    host_list = watched_hosts()
    now_mono = time.monotonic()
    fresh = look(agent.due(host_list, now_mono), pool)
    agent.settle(fresh, now_mono)
    agent.rows.update(fresh)
    # A host backing off keeps its last row, so it stays "refused" rather
    # than flickering to unknown between tries.
    rows = {h: agent.rows[h] for h in host_list if h in agent.rows}
    for host, row in rows.items():
        now = row.get("state", "unknown")
        if agent.state.get(host) != now:
            connlog.event(f"host {host}: {agent.state.get(host, 'unknown')} "
                          f"-> {now}" + (f" ({row.get('error')})"
                                         if row.get("error") else ""))
            agent.state[host] = now
    projects = chats.load_projects(hosts=host_list)
    live, permission, typed_ago, up = digest(rows, projects)

    if agent.stamper is not None:
        for chat in live:
            mine = agent.stamps.get(chat.id)
            if mine is None:
                continue
            if chat.stamped and chat.last_event >= mine:
                del agent.stamps[chat.id]       # the server has caught up
            else:
                chat.last_event, chat.stamped = mine, True
        # A session seen as a plain shell that then runs Claude is a chat
        # starting now, not a first sighting (chats.RecencyTracker).
        shells = {chats.chat_id(host, session.get("name", ""))
                  for host in up for session in rows[host].get("sessions") or []
                  if (session.get("agent") or {}).get("state")
                  == agent_state.SHELL}
        agent.recency.saw_shells(shells)
        due = agent.recency.update_chats(live, time.time())
        for chat in live:
            stamp = due.get(chat.id)
            if stamp is not None:
                chat.last_event, chat.stamped = stamp, True
                agent.stamps[chat.id] = stamp
                agent.stamper.put(chat.host, chat.session, stamp)
        agent.shells = shells
    if agent.watch is not None:
        agent.watch.update(rows, projects, live)

    if agent.config.get("ntfy_topic"):
        if agent.devices is not None and agent.devices.desk_active():
            typed_ago = 0.0         # a desktop says it is in use
        for ping in agent.alerter.update(live, permission, typed_ago,
                                         time.time(), up):
            try:
                send(agent.config, ping)
                connlog.event(f"alert {ping.chat_id}: {ping.reason}, sent")
            except Exception as exc:        # noqa: BLE001 -- any failure
                connlog.event(f"alert {ping.chat_id}: {ping.reason}, failed "
                              f"({type(exc).__name__}: "
                              f"{connlog.clean(exc, 120)})")
    # Forget chats that have gone, but not those on a server that is only
    # dark for now: when it answers, a change is a change.
    agent.known = {chat.id for chat in live} | {
        cid for cid in agent.known if cid.partition("/")[0] not in up}
    agent.alerter.retain(agent.known)
    agent.recency.retain(agent.known | agent.shells)
    agent.stamps = {k: v for k, v in agent.stamps.items() if k in agent.known}


def _log_path() -> Path:
    if sys.platform == "darwin":
        return Path("~/Library/Logs/conn-agent.log").expanduser()
    return Path("~/.local/state/conn/agent.log").expanduser()


def once() -> int:
    """One look, printed: the states the agent would act on."""
    host_list = watched_hosts()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        rows = look(host_list, pool)
    live, permission, typed_ago, up = digest(
        rows, chats.load_projects(hosts=host_list))
    print("last typed into a terminal: "
          + ("nobody attached" if typed_ago is None else f"{typed_ago:.0f}s ago"))
    for host in host_list:
        row = rows[host]
        print(f"{host:22} {row.get('state', '?'):8} {row.get('error') or ''}")
    for chat in live:
        flags = "permission" if chat.id in permission else ""
        print(f"  {chat.id:34} {chat.state:10} {flags}")
    return 0


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "run"
    if command == "once":
        return once()
    config = load_config()
    if command == "pair":
        import agent_api
        code, expires = agent_api.Devices().new_code_file()
        _, print_ = agent_api.tls_context()
        print(f"code {code}  (works once, for 5 minutes)\n"
              f"port {config['port']}\n"
              f"certificate sha256 {print_}")
        return 0
    if command == "token" and len(argv) > 2:
        import agent_api
        device_id, token = agent_api.Devices().create(argv[2], kind="desktop")
        print(f"{device_id}\n{token}\n(shown once: keep it)")
        return 0
    if command == "topic":
        print(config["ntfy_topic"] or "ntfy alerts are off")
        return 0
    if command == "test-ping":
        if not config["ntfy_topic"]:
            raise SystemExit("ntfy alerts are off: set ntfy_topic first")
        send(config, Ping("test", "Conn", "Test alert from conn-agent", "finished"))
        print("sent")
        return 0
    if command == "run":
        return run(config)
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
