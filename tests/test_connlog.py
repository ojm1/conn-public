#!/usr/bin/env python3
"""The connection-event log: what goes in, what does not, and where.

    python3 tests/test_connlog.py

The log is pointed at a temp file (CONN_LOG) before anything is imported,
and the real ~/.local/state/conn/conn.log is checked to be untouched at the
end. No ssh runs: revive_masters takes its runner, network_ready's checks
and the window's hosts.* calls are replaced, and the window's methods run on
stand-in objects -- no display needed. The one real process is a local `sh`
playing a watch stream.
"""

from __future__ import annotations

import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="conn-log-test-"))
LOG = TMP / "state" / "conn.log"
os.environ["CONN_LOG"] = str(LOG)

REAL = (Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state")
        .expanduser() / "conn" / "conn.log")


def stat(path: Path):
    try:
        info = path.stat()
        return (info.st_size, info.st_mtime_ns)
    except OSError:
        return None


REAL_BEFORE = stat(REAL)

import connlog  # noqa: E402
import hosts  # noqa: E402

failures = total = 0


def check(label, passed, detail=""):
    global failures, total
    total += 1
    failures += not passed
    print(("ok   " if passed else "FAIL ") + label
          + ("" if passed or not detail else f"\n     {detail}"))


def lines() -> list[str]:
    try:
        return LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def since(mark: int) -> list[str]:
    return lines()[mark:]


def wait_for(pattern: str, mark: int, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(pattern in line for line in since(mark)):
            return True
        time.sleep(0.05)
    return False


LINE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d{4} "
                  r"\[[^\]]+\] .+$")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def test_setup():
    check("nothing is logged before setup", not connlog.active())
    connlog.event("before setup")
    check("an event before setup writes nothing", not LOG.exists())
    try:
        import gui  # noqa: F401
        imported = True
    except (ImportError, ValueError) as exc:
        imported = False
        print(f"skip importing gui: {exc}")
    check("importing the window module does not start the log",
          not connlog.active() and not LOG.exists())
    check("CONN_LOG moves it", connlog.path() == LOG, str(connlog.path()))
    saved = os.environ.pop("CONN_LOG")
    try:
        check("default is the state folder", connlog.path() == REAL,
              str(connlog.path()))
    finally:
        os.environ["CONN_LOG"] = saved
    bad = TMP / "not-a-dir"
    bad.write_text("")
    check("a log that cannot be opened is None, not an exception",
          connlog.setup(bad / "conn.log") is None and not connlog.active())
    where = connlog.setup()
    check("setup opens the overridden path", where == LOG and LOG.exists())
    check("the file is private", LOG.stat().st_mode & 0o077 == 0,
          oct(LOG.stat().st_mode))
    connlog.event("hello")
    got = lines()
    check("one line: ISO local time with ms, offset, thread, message",
          got and LINE.match(got[-1]) and got[-1].endswith("[MainThread] hello"),
          repr(got))
    return imported


def test_clean():
    nasty = ("ssh: \x1b]0;pwned title\x07Permission \x1b[31mdenied\x1b[0m\r\n"
             "\x1b]52;c;ZXZpbA==\x1b\\(publickey)\x00\x9b2J\x07 end")
    out = connlog.clean(nasty)
    check("escape sequences and control characters are gone",
          not CONTROL.search(out) and "pwned" not in out
          and "ZXZpbA" not in out, repr(out))
    check("the words survive", out == "ssh: Permission denied (publickey) end",
          repr(out))
    long = connlog.clean("x" * 5000)
    check("capped at 200 characters", len(long) == 200 and long.endswith("\u2026"))
    check("cap is adjustable", len(connlog.clean("y" * 99, 50)) == 50)


def test_limiter():
    now = [1000.0]
    real = connlog._clock
    connlog._clock = lambda: now[0]
    try:
        mark = len(lines())
        for i in range(5):
            connlog.limited(("watch", "flappy", "dropped"),
                            f"watch flappy: dropped (EOF) after {i}.0s")
            now[0] += 2
        connlog.limited(("watch", "steady", "dropped"),
                        "watch steady: dropped (EOF) after 300.0s")
        got = since(mark)
        check("five drops in ten seconds write one line; another host is "
              "not held back", len(got) == 2 and "after 0.0s" in got[0]
              and "steady" in got[1], repr(got))
        connlog.flush()
        check("a flush inside the window writes nothing", len(since(mark)) == 2)
        now[0] += 31
        connlog.flush()
        got = since(mark)
        check("after the window, one summary: the last line and the count",
              len(got) == 3 and "after 4.0s (repeated 4 times, last at "
              in got[2], repr(got[2:]))
        connlog.limited(("watch", "flappy", "dropped"), "watch flappy: again")
        check("after a summary the next line goes straight out",
              since(mark)[-1].endswith("watch flappy: again"))
        connlog.limited(("watch", "flappy", "dropped"), "watch flappy: x1")
        now[0] += 31
        connlog.limited(("watch", "flappy", "dropped"), "watch flappy: x2")
        got = since(mark)
        check("a line after the window carries the summary in front of it",
              got[-2].startswith(got[-2].split(" [")[0])
              and "watch flappy: x1 (repeated 1 time," in got[-2]
              and got[-1].endswith("watch flappy: x2"), repr(got[-2:]))
    finally:
        connlog._clock = real


def fake_run(calls):
    slow = []

    def fake(argv, **kw):
        calls.append(argv)
        sock = next(a.split("=", 1)[1] for a in argv
                    if a.startswith("ControlPath="))
        if argv[-1] == "true":
            if "dead" in sock or "wedged" in sock:
                raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
            if "gone" in sock:
                return subprocess.CompletedProcess(argv, 255)
            if "slow" in sock:
                slow.append(1)
                if len(slow) == 1:
                    raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
            return subprocess.CompletedProcess(argv, 0)
        if "wedged" in sock:        # even the local exit request hangs
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
        return subprocess.CompletedProcess(argv, 0)
    return fake


def test_revive_masters():
    socks = [Path("/x/live@a:22"), Path("/x/dead@mini:2222"),
             Path("/x/gone@b:22"), Path("/x/slow@c:22"),
             Path("/x/wedged@d:22"), Path("/x/evil\x1b]0;t\x07@e:22")]
    real_end = hosts._end_socket_owner
    hosts._end_socket_owner = lambda sock: [4242]
    try:
        said: list[str] = []
        dropped = hosts.revive_masters(socks, timeout=0.2, run=fake_run([]),
                                       log=said.append)
        check("revive_masters still returns what it closed",
              dropped == ["dead@mini:2222", "gone@b:22", "wedged@d:22"],
              repr(dropped))
        text = "\n".join(said)
        check("a log callback hears every socket",
              said[0] == "masters: checking 6 sockets"
              and "master live@a:22: alive (exit 0 in" in text
              and "master slow@c:22: alive (timed out after 0.2s, exit 0 in"
              in text, text)
        check("a dead one says why and how it was closed",
              "master dead@mini:2222: dead (timed out after 0.2s, timed out "
              "after 0.4s); ssh -O exit -> 0" in text
              and "master gone@b:22: dead (exit 255 in" in text, text)
        check("a wedged one falls back to SIGKILL and names the pid",
              "master wedged@d:22: dead (timed out after 0.2s, timed out "
              "after 0.4s); ssh -O exit failed (TimeoutExpired) -> SIGKILL "
              "4242" in text, text)
        check("socket names are cleaned",
              not any(CONTROL.search(s) for s in said), text)
        check("a total at the end",
              re.match(r"masters: 6 checked, 3 closed in \d+\.\ds$", said[-1]),
              said[-1])
        mark = len(lines())
        hosts.revive_masters(socks[:2], timeout=0.2, run=fake_run([]))
        got = since(mark)
        check("without a callback it goes to the log, from master-check "
              "threads", len(got) == 4 and any("[master-check] master "
                                               "dead@mini" in g for g in got)
              and "masters: 2 checked, 1 closed" in got[-1], repr(got))
        check("the old call shape still works (no log argument)",
              hosts.revive_masters([], run=fake_run([])) == [])
    finally:
        hosts._end_socket_owner = real_end


def test_network_ready():
    real_run, real_jump = hosts.subprocess.run, hosts._jump_target
    real_connect = socket.create_connection

    def route(up):
        def run(argv, **kw):
            if argv[:3] == ["ip", "route", "show"]:
                return subprocess.CompletedProcess(
                    argv, 0, "default via 10.0.0.1 dev wlan0\n" if up else "", "")
            raise AssertionError(f"unexpected command {argv}")
        return run

    def refused(*a, **kw):
        raise ConnectionRefusedError(111, "Connection refused")
    try:
        said: list[str] = []
        hosts._jump_target = lambda: None
        hosts.subprocess.run = route(True)
        check("ready with a route and no jump host",
              hosts.network_ready(wait=2, log=said.append) is True)
        check("it says so, with how long",
              said[0].startswith("network: waiting for a route and the jump "
                                 "host (none)")
              and re.match(r"network: ready after \d+\.\ds \(1 tries; route, "
                           r"no jump host\)$", said[-1]), repr(said))
        said.clear()
        hosts._jump_target = lambda: ("mini.example", 2222)
        socket.create_connection = refused
        ok = hosts.network_ready(wait=0.4, sleep=lambda s: time.sleep(0.1),
                                 log=said.append)
        check("jump host refusing: not ready, and why, naming host:port",
              ok is False and "(mini.example:2222)" in said[0]
              and said[-1].startswith("network: NOT ready after ")
              and "route up, mini.example:2222 refused TCP" in said[-1],
              repr(said))
        mark = len(lines())
        hosts._jump_target = lambda: None
        hosts.subprocess.run = route(False)
        hosts.network_ready(wait=0.3, sleep=lambda s: time.sleep(0.1))
        got = since(mark)
        check("default log: no route is said as such",
              got and "last: no default route" in got[-1], repr(got))
    finally:
        hosts.subprocess.run, hosts._jump_target = real_run, real_jump
        socket.create_connection = real_connect


def stub(**fields):
    return types.SimpleNamespace(**fields)


def bind(obj, gui, *names):
    for name in names:
        method = getattr(gui.Conn, name)
        setattr(obj, name, (lambda m: lambda *a, **k: m(obj, *a, **k))(method))


def test_wake(gui):
    real = hosts.slept_seconds, hosts.local_addresses
    revived = []
    win = stub(stopping=False, slept=100.0,
               addresses=frozenset({"10.0.0.2"}), reviving=False,
               revive_again=False, revive=revived.append)
    try:
        hosts.slept_seconds = lambda: 100.0
        hosts.local_addresses = lambda: frozenset({"10.0.0.2"})
        mark = len(lines())
        gui.Conn.wake_tick(win)
        check("a quiet tick writes nothing", since(mark) == [], repr(since(mark)))
        hosts.slept_seconds = lambda: 340.0
        hosts.local_addresses = lambda: frozenset({"198.51.100.5", "fd00::5"})
        gui.Conn.wake_tick(win)
        got = since(mark)
        check("a wake and a move are both said, old -> new",
              any(g.endswith("wake: slept 240s") for g in got)
              and any(g.endswith("addresses: {10.0.0.2} -> {198.51.100.5, "
                                 "fd00::5}") for g in got)
              and revived == ["woke from sleep"], repr(got))
        mark = len(lines())
        hosts.local_addresses = lambda: frozenset()
        gui.Conn.wake_tick(win)
        got = since(mark)
        check("losing every address is said, but is no new network yet",
              len(got) == 1 and got[0].endswith("-> {} (none: not a new "
                                                "network yet)")
              and revived == ["woke from sleep"], repr(got))
        mark = len(lines())
        win.reviving = True
        hosts.local_addresses = lambda: frozenset({"172.16.0.9"})
        gui.Conn.wake_tick(win)
        got = since(mark)
        check("a move during a revive queues another, and says so",
              win.revive_again and any("revive_again" in g for g in got)
              and revived == ["woke from sleep"], repr(got))
    finally:
        hosts.slept_seconds, hosts.local_addresses = real


def test_revive(gui):
    from gi.repository import GLib
    real = hosts.network_ready, hosts.revive_masters
    poke = threading.Event()
    swept = []
    win = stub(reviving=False, revive_again=False, notice=lambda text: None,
               streams={"mini": (None, threading.Event(), poke)},
               sweep=lambda force=False: swept.append(force))
    try:
        hosts.network_ready = lambda: True
        hosts.revive_masters = lambda: ["dead@mini:2222"]
        mark = len(lines())
        gui.Conn.revive(win, "woke from sleep")
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 5
        while not swept and time.monotonic() < deadline:
            context.iteration(False)
            time.sleep(0.02)
        got = since(mark)
        check("revive: start with the reason, done with the count and pokes",
              any(g.endswith("[MainThread] revive: start (woke from sleep)")
                  for g in got)
              and any(re.search(r"revive: done \(woke from sleep\) in "
                                r"\d+\.\ds, 1 master closed; poking 1 stream, "
                                r"sweep in 1\.5s$", g) for g in got)
              and poke.is_set() and swept == [True], repr(got))

        def boom():
            raise RuntimeError("bad\x1b]0;x\x07 thing")
        hosts.network_ready = boom
        swept.clear()
        mark = len(lines())
        gui.Conn.revive(win, "network changed")
        deadline = time.monotonic() + 5
        while not swept and time.monotonic() < deadline:
            context.iteration(False)
            time.sleep(0.02)
        got = since(mark)
        check("a revive that raises says so, cleaned, and still lands",
              any(g.endswith("[revive] revive: failed: RuntimeError: bad thing")
                  for g in got)
              and any("0 masters closed" in g for g in got), repr(got))
    finally:
        hosts.network_ready, hosts.revive_masters = real


def test_sweep(gui):
    win = stub(check_source=lambda: None, holding=False,
               order=["a", "b", "c"], inflight={"b"}, starred=set(),
               probed={}, _probe=lambda host: None, probe_report=set(),
               auto_live=set())
    win.watched = lambda host: gui.Conn.watched(win, host)
    mark = len(lines())
    gui.Conn.sweep(win, force=True)
    got = since(mark)
    check("a forced sweep says how many it asks, and which are in flight",
          len(got) == 1 and got[0].endswith(
              "sweep: forced, probing 2 hosts (1 still in flight: b)"),
          repr(got))
    check("the hosts a forced sweep asks have their answers reported, "
          "not the one already in flight", win.probe_report == {"a", "c"},
          repr(win.probe_report))
    win.probe_report.clear()
    win.inflight.clear()
    win.starred = {"a"}
    gui.Conn.sweep(win)
    check("an ordinary sweep writes nothing, and reports nothing",
          len(since(mark)) == 1 and not win.probe_report)


def test_transitions(gui):
    win = stub(inflight=set(), rows={"web": hosts.blank("web")}, streamed={},
               render=lambda: None, _host_back=lambda *a: None,
               _owed_open=lambda *a: None, probe_state={},
               probe_report=set())
    bind(win, gui, "_log_probe")

    def probed(state, error="", took=0.5):
        row = hosts.blank("web")
        row["state"], row["error"] = state, error
        gui.Conn._probed(win, "web", row, time.monotonic() - took)

    mark = len(lines())
    for _ in range(4):
        probed("up")
    got = since(mark)
    check("the first answer is a transition; the next three sweeps are not",
          len(got) == 1 and re.search(r"host web: unknown -> up "
                                      r"\(0\.\ds\)$", got[0]), repr(got))
    win.rows["web"]["state"] = "unknown"     # what recheck() does
    probed("up")
    check("a recheck's 'unknown' is not a transition", len(since(mark)) == 1)
    banner = ("\x1b]0;owned\x07Welcome to \x1b[1mprod\x1b[0m!\r\n" + "z" * 400)
    probed("down", banner)
    probed("down", banner)
    got = since(mark)
    reason = got[-1].split("): ", 1)[-1] if got else ""
    check("up -> down once, with the reason cleaned and capped",
          len(got) == 2 and "host web: up -> down (" in got[1]
          and not CONTROL.search(got[1]) and "owned" not in got[1]
          and reason.startswith("Welcome to prod! zzz")
          and len(reason) == 200, repr(got[-1:]))
    probed("down", "timed out", took=12.3)
    got = since(mark)
    check("still down for a new reason is said, with how long it took",
          len(got) == 3 and got[2].endswith(
              "host web: still down (12.3s), reason now: timed out"),
          repr(got))
    probed("down", "timed out", took=12.4)
    got = since(mark)
    check("a slow probe is said even without a change",
          len(got) == 4 and got[3].endswith(
              "host web: slow probe, 12.4s, still down: timed out"), repr(got))
    probed("down", "timed out")
    check("the same reason, quickly, again is not", len(since(mark)) == 4)
    connlog._seen.pop(("reason", "web"), None)
    probed("down", "Network is\x1b[31m unreachable\x07")
    got = since(mark)
    check("a reason change is cleaned too",
          len(got) == 5 and got[4].endswith(
              "host web: still down (0.5s), reason now: Network is "
              "unreachable"), repr(got[-1:]))
    probed("down", "timed out")
    probed("down", "Broken pipe")
    probed("down", "timed out")
    got = since(mark)
    check("a host flapping between reasons is rate-limited",
          len(got) == 5, repr(got[5:]))
    connlog.flush(force=True)
    got = since(mark)
    check("and summed up when the window is over",
          len(got) == 6 and got[5].endswith(
              "host web: still down (0.5s), reason now: timed out "
              "(repeated 3 times, last at " + got[5][-13:-1] + ")"),
          repr(got[5:]))
    probed("up")
    got = since(mark)
    check("and back up", got[-1].endswith(
        "host web: down -> up (0.5s)") and len(got) == 7, repr(got[-1:]))
    probed("up")
    check("up again with no reason says nothing", len(since(mark)) == 7)

    # The forced sweep after a revive: every answer goes in, changed or not,
    # and past the limiter -- once.
    win.probe_report.add("web")
    probed("up", took=1.2)
    got = since(mark)
    check("a forced sweep's unchanged answer is still said, once",
          len(got) == 8 and got[7].endswith(
              "host web: still up (1.2s) [forced sweep]")
          and "web" not in win.probe_report, repr(got[-1:]))
    probed("up")
    check("and the next ordinary sweep is quiet again", len(since(mark)) == 8)
    probed("down", "timed out")     # logged: the flush forgot every key
    probed("up")                    # held back: down -> up was just said
    win.probe_report.add("web")
    probed("down", "timed out")     # same transition, inside its window
    got = since(mark)
    check("a forced sweep's transition is not held back by the limiter",
          len(got) == 10 and got[9].endswith(
              "host web: up -> down (0.5s): timed out [forced sweep]"),
          repr(got[-3:]))
    win.probe_report.add("web")
    probed("down", "timed out")
    got = since(mark)
    check("a forced sweep's still-down carries its reason",
          len(got) == 11 and got[10].endswith(
              "host web: still down (0.5s): timed out [forced sweep]"),
          repr(got[-1:]))
    del win.rows["web"]
    probed("down")
    check("a forgotten host is not logged", len(since(mark)) == 11)


def test_stream(gui):
    """One real _stream loop: a local `sh` heartbeats then exits (EOF), a
    poke cuts the backoff, the next one sends a frame then goes silent
    (stall), and the third cannot spawn."""
    calls = []
    halt, poke = threading.Event(), threading.Event()
    token = "T0K"

    def proc(script):
        p = subprocess.Popen(["sh", "-c", script], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, bufsize=0)
        p.boundary = token
        p.frame = f"###{token}:FRAME"
        p.alive = f"###{token}:ALIVE"
        return p

    def watch(host, interval):
        calls.append(host)
        if len(calls) == 1:
            return proc(f"printf '###{token}:ALIVE\\n'; exit 3")
        if len(calls) == 2:
            return proc(f"printf '###{token}:FRAME\\n'; exec sleep 30")
        raise OSError(2, "No such file or directory: \x1b[2Jssh")

    win = stub(stopping=False, watch_lock=threading.Lock(), watchers={},
               frames=queue.Queue(maxsize=10))
    real_watch, real_stall = hosts.watch_screens, gui.WATCH_STALL
    now = [5000.0]
    real_clock = connlog._clock
    connlog._clock = lambda: now[0]     # every line inside one window
    hosts.watch_screens = watch
    gui.WATCH_STALL = 0.5
    thread = threading.Thread(target=gui.Conn._stream,
                              args=(win, "mini", halt, poke),
                              name="watch-mini", daemon=True)
    mark = len(lines())
    try:
        thread.start()
        dropped = wait_for("watch mini: dropped (EOF", mark)
        poke.set()
        poked = wait_for("watch mini: poked, retrying now", mark)
        stalled = wait_for("watch mini: dropped (stalled, silent 0.5s)", mark)
        poke.set()
        spawn = wait_for("watch mini: dropped (spawn error", mark)
        halt.set()
        thread.join(5)
        got = since(mark)
        text = "\n".join(got)
        check("connecting, connected and the first drop, from the watch thread",
              all("[watch-mini]" in g for g in got)
              and "watch mini: connecting (attempt 1)" in text
              and re.search(r"watch mini: connected in \d+\.\ds", text)
              and dropped, text)
        check("the drop says how long it lived and the backoff chosen",
              re.search(r"dropped \(EOF(, ssh exit 3)?\) after \d+\.\ds; "
                        r"retry in \d+\.\ds", text), text)
        check("a poke that cuts the backoff short is said", poked, text)
        check("a stall past WATCH_STALL is a drop of its own", stalled, text)
        check("a spawn error, cleaned, is never heard from",
              spawn and re.search(r"dropped \(spawn error: \[Errno 2\] No such "
                                  r"file or directory: ssh\) after \d+\.\ds, "
                                  r"never heard from", text), text)
        check("repeats inside the window are held back (one connecting, one "
              "connected)", text.count("connecting (attempt") == 1
              and text.count("connected in") == 1, text)
        check("a halt ends it, said", thread.is_alive() is False
              and got and "watch mini: stopped" in got[-1], text)
        mark = len(lines())
        connlog.flush(force=True)
        got = since(mark)
        check("the held-back lines come out as summaries",
              any("connecting (attempt 3) (repeated 2 times, last at " in g
                  for g in got)
              and any("connected in" in g and "(repeated 1 time," in g
                      for g in got), repr(got))
    finally:
        halt.set()
        hosts.watch_screens, gui.WATCH_STALL = real_watch, real_stall
        connlog._clock = real_clock
        for p in list(win.watchers.values()):
            p.kill()


def test_sessions(gui):
    class View:
        def __init__(self, name, spent=False):
            self.host, self.name, self.dead = "web", name, True
            self.died, self.respawn_spent = 0.0, spent
            self.respawned = time.monotonic() - 42
            self.respawns = 0

        def respawn(self):
            self.respawns += 1

    keep, loop, gone = View("work"), View("flaky", True), View("old")
    closed = []
    win = stub(rows={"web": {"state": "up", "sessions": [
                   {"name": "work"}, {"name": "flaky"}]}},
               open={("web", v.name): v for v in (keep, loop, gone)},
               notice=lambda text: None, close_session=closed.append,
               _retry=lambda host: None, paint_banners=lambda: None)
    real = gui.Session
    gui.Session = View
    try:
        mark = len(lines())
        gui.Conn._host_back(win, "web", time.monotonic())
        got = "\n".join(since(mark))
        check("server back: respawn, gone, and did-not-stay-connected each said",
              "session web/work: host up, respawning" in got
              and "session web/old: host up, session gone, closed" in got
              and "session web/flaky: did not stay connected (respawned 42s "
                  "ago), closed" in got
              and keep.respawns == 1
              and {id(v) for v in closed} == {id(loop), id(gone)}, got)
    finally:
        gui.Session = real

    import chats
    chat = types.SimpleNamespace(offline=True)
    win = stub(chat_index={chats.chat_id("web", "work"): chat},
               unheard=lambda host: False, _retry=lambda host: None,
               rows={"web": {"state": "down"}}, notice=lambda text: None)
    real_clock = connlog._clock
    connlog._clock = lambda: 9000.0
    try:
        mark = len(lines())
        gui.Conn.offline_refused(win, "web", "work")
        gui.Conn.offline_refused(win, "web", "work")
        got = since(mark)
        check("an offline chat refused is said once per window",
              len(got) == 1 and got[0].endswith(
                  "offline: web/work not attached, host down; asking it now"),
              repr(got))
    finally:
        connlog._clock = real_clock


def test_tail():
    rotated = TMP / "tail" / "conn.log"
    rotated.parent.mkdir()
    rotated.with_name("conn.log.1").write_text("old1\nold2\n")
    rotated.write_text("new1\nnew2\n")
    check("tail reaches into the rotated file when short",
          connlog.tail(3, rotated) == ["old2", "new1", "new2"])
    check("no log at all is None", connlog.tail(5, TMP / "nope.log") is None)
    env = dict(os.environ, CONN_LOG=str(rotated))
    out = subprocess.run([sys.executable, str(ROOT / "main.py"), "--log", "2"],
                         capture_output=True, text=True, env=env, timeout=30)
    check("conn --log N prints the last N lines",
          out.returncode == 0 and out.stdout.splitlines() == ["new1", "new2"],
          repr((out.returncode, out.stdout, out.stderr)))
    env["CONN_LOG"] = str(TMP / "nope.log")
    out = subprocess.run([sys.executable, str(ROOT / "main.py"), "--log"],
                         capture_output=True, text=True, env=env, timeout=30)
    check("conn --log with no log says where it would be",
          out.returncode == 1 and "no log yet at" in out.stderr, out.stderr)


def test_rotation():
    where = TMP / "rot" / "conn.log"
    real_max = connlog.MAX_BYTES
    connlog.MAX_BYTES = 2000
    umask = os.umask(0o022)     # the usual one: files 0644 unless told
    try:
        connlog.setup(where)
        for i in range(400):
            connlog.event(f"line {i:04d} " + "x" * 40)
        names = sorted(p.name for p in where.parent.iterdir())
        check("rotates, keeping three old files",
              names == ["conn.log", "conn.log.1", "conn.log.2", "conn.log.3"],
              repr(names))
        modes = {p.name: oct(p.stat().st_mode & 0o777)
                 for p in where.parent.iterdir()}
        check("every file stays owner-only across rotations",
              set(modes.values()) == {"0o600"}, repr(modes))
        old = TMP / "old" / "conn.log"
        old.parent.mkdir()
        old.write_text("from an older conn\n")
        old.chmod(0o644)
        connlog.setup(old)
        check("a log left 0644 by an older conn is narrowed on open",
              old.stat().st_mode & 0o777 == 0o600,
              oct(old.stat().st_mode))
    finally:
        os.umask(umask)
        connlog.MAX_BYTES = real_max
        connlog.setup(LOG)


def test_bad_paths():
    """A mistyped CONN_LOG or XDG_STATE_HOME ("~typo/...") makes expanduser
    raise; that must cost the log, never the app or `conn --log`."""
    saved = {k: os.environ.get(k) for k in ("CONN_LOG", "XDG_STATE_HOME")}
    xdg = TMP / "xdg"
    try:
        os.environ["CONN_LOG"] = "~nosuchuser-conn-test/conn.log"
        os.environ["XDG_STATE_HOME"] = str(xdg)
        try:
            found = connlog.path()
        except Exception as exc:    # noqa: BLE001
            found = exc
        check("an unexpandable CONN_LOG falls back, without raising",
              found == xdg / "conn" / "conn.log", repr(found))
        os.environ.pop("CONN_LOG")
        os.environ["XDG_STATE_HOME"] = "~nosuchuser-conn-test/state"
        try:
            found = connlog.path()
        except Exception as exc:    # noqa: BLE001
            found = exc
        check("so does an unexpandable XDG_STATE_HOME",
              found == REAL, repr(found))
        try:
            got = connlog.setup("~nosuchuser-conn-test/conn.log")
        except Exception as exc:    # noqa: BLE001
            got = exc
        check("setup of an unexpandable path is None, and keeps the log",
              got is None and connlog.active(), repr(got))
        here = Path.cwd()
        try:
            os.chdir(TMP)
            got = connlog.setup("relative/conn.log")
            made = (TMP / "relative").exists()
        finally:
            os.chdir(here)
        check("a relative path is refused, not made under the cwd",
              got is None and not made, repr((got, made)))
        env = dict(os.environ, CONN_LOG="~nosuchuser-conn-test/conn.log",
                   XDG_STATE_HOME=str(TMP / "xdg-empty"))
        out = subprocess.run([sys.executable, str(ROOT / "main.py"), "--log"],
                             capture_output=True, text=True, env=env,
                             timeout=30)
        check("conn --log with an unexpandable CONN_LOG does not crash",
              out.returncode == 1 and "no log yet at" in out.stderr
              and "Traceback" not in out.stderr,
              repr((out.returncode, out.stderr[-300:])))
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        connlog.setup(LOG)


def test_run_window(gui):
    """conn start/exit come from the primary instance only. GApplication
    runs startup only there; a second `conn` registers as a remote, hands
    over 'activate' and exits -- and must leave no start/exit pair."""
    import main
    real_run = gui.run
    saved = os.environ["CONN_LOG"]
    where = TMP / "main" / "conn.log"
    connlog.shutdown()
    os.environ["CONN_LOG"] = str(where)
    calls = []
    try:
        def remote(service=False, on_startup=None):
            calls.append(on_startup is not None)
            return 0                # startup never runs in a remote
        gui.run = remote
        status = main.run_window()
        check("a second conn (no startup) writes no start or exit",
              status == 0 and calls == [True] and not where.exists()
              and not connlog.active(), repr((status, calls)))

        def primary(service=False, on_startup=None):
            on_startup()
            connlog.event("working")
            return 3
        gui.run = primary
        status = main.run_window()
        got = where.read_text().splitlines() if where.exists() else []
        check("the primary writes start, then exit with its status",
              status == 3 and len(got) == 3
              and re.search(r"\] conn start: pid \d+, window, ", got[0])
              and got[1].endswith("] working")
              and got[2].endswith("] conn exit: status 3")
              and not connlog.active(), repr(got))

        def broken(service=False, on_startup=None):
            on_startup()
            raise RuntimeError("window failed")
        gui.run = broken
        try:
            main.run_window(service=True)
            raised = False
        except RuntimeError:
            raised = True
        got = where.read_text().splitlines()
        check("a primary that dies still writes its exit",
              raised and "D-Bus activation" in got[-2]
              and got[-1].endswith("conn exit: status 1"), repr(got[-2:]))
    finally:
        gui.run = real_run
        os.environ["CONN_LOG"] = saved
        connlog.setup(LOG)

    # The real thing, if a bus and the test display can be had: a primary
    # ConnApp holds the name on a private session bus, and a second
    # process goes through main.run_window() for real. Neither window is
    # built -- the primary never runs its loop -- so no ssh.
    if not shutil.which("dbus-run-session"):
        print("skip the two-instance check: no dbus-run-session")
        return
    helper = TMP / "two_instances.py"
    helper.write_text(TWO_INSTANCES)
    second = TMP / "second" / "conn.log"
    env = {k: v for k, v in os.environ.items()
           if k not in ("WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS")}
    env.update(DISPLAY=":7", GDK_BACKEND="broadway", BROADWAY_DISPLAY=":7",
               CONN_LOG=str(second), CONN_ROOT=str(ROOT))
    try:
        out = subprocess.run(["dbus-run-session", "--", sys.executable,
                              str(helper), "primary"],
                             capture_output=True, text=True, env=env,
                             timeout=60)
    except subprocess.TimeoutExpired:
        check("two-instance check finished", False, "timed out")
        return
    if "NO-DISPLAY" in out.stdout:
        print("skip the two-instance check: no broadway :7")
        return
    check("on a real bus: the primary starts up, the second is remote and "
          "logs nothing",
          "primary: PRIMARY STARTUP" in out.stdout
          and "second: status 0, log written False" in out.stdout
          and "primary saw: STARTUP ACTIVATED" in out.stdout,
          repr((out.returncode, out.stdout[-400:], out.stderr[-400:])))


TWO_INSTANCES = r'''
import os, subprocess, sys
sys.path.insert(0, os.environ["CONN_ROOT"])
mode = sys.argv[1]
try:
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    if not Gtk.init_check():
        raise RuntimeError("no display")
except Exception:
    print("NO-DISPLAY", flush=True)
    sys.exit(0)
import gui
if mode == "primary":
    import time
    from gi.repository import GLib
    seen = []

    class Primary(gui.ConnApp):
        def do_activate(self):      # no window: it would probe real hosts
            seen.append("ACTIVATED")

    app = Primary(on_startup=lambda: seen.append("STARTUP"))
    app.register(None)
    print("primary:", "REMOTE" if app.get_is_remote() else "PRIMARY",
          *seen, flush=True)
    # The second asks this one over the bus as it registers, so the loop
    # has to turn while it does.
    child = subprocess.Popen([sys.executable, __file__, "second"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 40
    while child.poll() is None and time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.01)
    for _ in range(50):             # let the forwarded activate land
        context.iteration(False)
    out, err = child.communicate(timeout=5)
    print(out, err, flush=True)
    print("primary saw:", *seen, flush=True)
else:
    import main
    status = main.run_window()
    print(f"second: status {status}, log written "
          f"{os.path.exists(os.environ['CONN_LOG'])}", flush=True)
'''


def main():
    imported = test_setup()
    test_clean()
    test_limiter()
    test_revive_masters()
    test_network_ready()
    if imported:
        import gui
        test_wake(gui)
        test_revive(gui)
        test_sweep(gui)
        test_transitions(gui)
        test_stream(gui)
        test_sessions(gui)
        test_run_window(gui)
    test_tail()
    test_rotation()
    test_bad_paths()
    connlog.shutdown()
    check("every line written is one clean line",
          all(LINE.match(g) and not CONTROL.search(g) for g in lines()),
          "\n".join(g for g in lines()
                    if not LINE.match(g) or CONTROL.search(g)))
    check("the real ~/.local/state/conn/conn.log was not touched",
          stat(REAL) == REAL_BEFORE, f"{REAL_BEFORE} -> {stat(REAL)}")
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        connlog.shutdown()
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
