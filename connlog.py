"""The connection-event log: what conn did about the network, and when.

After a wake or a network change every server has sat "unreachable" for
minutes with nothing on record of what conn tried -- whether it noticed the
wake, whether the jump host answered, which ssh masters it closed, how long
each stream lived. This keeps that record: state transitions and timings,
one line each, in

    ~/.local/state/conn/conn.log      (CONN_LOG=<path> puts it elsewhere)

rotated at about 1 MB with three old files kept. `conn --log` prints the end.

Nothing is written until setup() runs, and only the window does that
(main.py): importing this, the CLI modes and every test log into a
NullHandler. A log that cannot be opened or written is dropped silently --
never a reason for the app to fail.

What goes in: host names, tmux session names, states, exit codes, socket
names, addresses, durations. What never does: chat text, screens, previews,
task or note text, titles, secrets. Text a remote can influence -- an ssh
error line carries the server's banner, a tmux name is whatever the far side
called it -- goes through clean() first.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import threading
import time
from pathlib import Path

MAX_BYTES = 1_000_000
BACKUPS = 3
# At most one line per key per window; the rest are counted and summed up.
WINDOW = 30.0
ERROR_CAP = 200

log = logging.getLogger("conn")
log.propagate = False           # never into a root handler someone else set
log.addHandler(logging.NullHandler())

_handler: logging.Handler | None = None
_lock = threading.Lock()
# key -> [monotonic of the line written, suppressed count, last suppressed
# message, wall time of it]
_seen: dict = {}
_clock = time.monotonic


DEFAULT = "~/.local/state/conn/conn.log"


def _expand(where) -> Path | None:
    """`where` with ~ expanded, or None if it cannot be: "~typo/x" raises
    RuntimeError, and a mistyped variable is no reason for conn to fail."""
    try:
        return Path(where).expanduser()
    except (RuntimeError, KeyError, ValueError, TypeError):
        return None


def path() -> Path:
    """Where the log lives: $CONN_LOG, or conn's state folder. Never raises:
    a $CONN_LOG or $XDG_STATE_HOME that cannot be expanded is passed over
    for the default, and if even that cannot be (no home at all) the result
    is relative -- which setup() refuses to open."""
    override = os.environ.get("CONN_LOG")
    if override:
        found = _expand(override)
        if found is not None:
            return found
    state = os.environ.get("XDG_STATE_HOME")
    if state:
        found = _expand(state)
        if found is not None:
            return found / "conn" / "conn.log"
    return _expand(DEFAULT) or Path(DEFAULT)


class _Handler(logging.handlers.RotatingFileHandler):
    def handleError(self, record):      # noqa: N802 -- logging's name
        pass    # disk full, file gone: lose the line, keep the app

    def _open(self):
        """Owner-only, every time the file is opened -- not just the first.

        A rollover opens a fresh conn.log, and logging's own _open would
        make it under the umask (0644 here) in a folder anyone can list:
        host names, socket names, addresses and the jump host readable by
        every local account from the first 1 MB on. The renamed backups
        keep the mode they were made with, so this one place covers them.
        A file left 0644 by an older conn is narrowed on open too."""
        fd = os.open(self.baseFilename,
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        try:
            return open(fd, self.mode, encoding=self.encoding,
                        errors=self.errors)
        except Exception:
            os.close(fd)
            raise


class _Format(logging.Formatter):
    def formatTime(self, record, datefmt=None):     # noqa: N802
        t = time.localtime(record.created)
        return (time.strftime("%Y-%m-%dT%H:%M:%S", t)
                + f".{int(record.msecs):03d}" + time.strftime("%z", t))


FORMAT = "%(asctime)s [%(threadName)s] %(message)s"


def setup(where: Path | str | None = None) -> Path | None:
    """Start writing the log; return its path, or None if it cannot be
    opened. Once per process -- a second call moves it (tests do)."""
    global _handler
    try:
        target = _expand(where) if where is not None else path()
        if target is None or not target.is_absolute():
            return None     # unexpandable, or no home to put it in
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handler = _Handler(target, maxBytes=MAX_BYTES, backupCount=BACKUPS,
                           encoding="utf-8")
        handler.setFormatter(_Format(FORMAT))
    except Exception:       # noqa: BLE001 -- a log is never worth a crash
        return None
    shutdown()
    _handler = handler
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    return target


def shutdown() -> None:
    """Write any pending summaries and close the file."""
    global _handler
    if _handler is None:
        return
    flush(force=True)
    log.removeHandler(_handler)
    try:
        _handler.close()
    except Exception:       # noqa: BLE001
        pass
    _handler = None
    with _lock:
        _seen.clear()


def active() -> bool:
    return _handler is not None


def event(message: str) -> None:
    """One line, as is. Callers clean() anything remote-controlled."""
    try:
        log.info(message)
    except Exception:       # noqa: BLE001
        pass


def _hms(wall: float) -> str:
    t = time.localtime(wall)
    return time.strftime("%H:%M:%S", t) + f".{int(wall % 1 * 1000):03d}"


def _summary(entry: list) -> str:
    return (f"{entry[2]} (repeated {entry[1]} time"
            f"{'s' if entry[1] != 1 else ''}, last at {_hms(entry[3])})")


def limited(key, message: str) -> bool:
    """event(), at most once per `key` per WINDOW seconds.

    A flapping host would otherwise write a connect and a drop every few
    seconds for as long as it flaps. Within the window a line under the same
    key is counted, not written; the count goes out as one summary line --
    the last suppressed message, "(repeated N times, last at HH:MM:SS)" --
    when the key next writes after the window, or when flush() finds the
    window over. Keys are shapes ("watch", host, "dropped"), not texts: the
    lines differ by their durations. True if this one was written."""
    if not active():
        return False
    now = _clock()
    with _lock:
        entry = _seen.get(key)
        if entry is not None and now - entry[0] < WINDOW:
            entry[1] += 1
            entry[2] = message
            entry[3] = time.time()
            return False
        owed = _summary(entry) if entry is not None and entry[1] else None
        _seen[key] = [now, 0, "", 0.0]
    if owed:
        event(owed)
    event(message)
    return True


def flush(force: bool = False) -> None:
    """Write the summaries whose window is over (all of them, forced), and
    forget keys that have been quiet a whole window."""
    now = _clock()
    owed = []
    with _lock:
        for key, entry in list(_seen.items()):
            if not force and now - entry[0] < WINDOW:
                continue
            if entry[1]:
                owed.append(_summary(entry))
            del _seen[key]
    for line in owed:
        event(line)


# CSI (ESC [ ... final), OSC (ESC ] ... BEL or ST), other two-byte escapes,
# and the C1 forms of CSI/OSC; then anything left that is a control char.
_ESCAPES = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?"
    r"|\x9b[0-?]*[ -/]*[@-~]"
    r"|\x9d[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?"
    r"|\x1b[@-Z\\-_]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def clean(text, cap: int = ERROR_CAP) -> str:
    """Remote-controlled text made safe for one log line: escape sequences
    gone, every other control character a space, runs of space one, at most
    `cap` characters."""
    text = _ESCAPES.sub("", str(text))
    text = " ".join(_CONTROL.sub(" ", text).split())
    if len(text) > cap:
        text = text[:cap - 1] + "…"
    return text


def addresses(found) -> str:
    return "{" + ", ".join(sorted(found)) + "}"


def tail(lines: int = 200, where: Path | None = None) -> list[str] | None:
    """The last `lines` lines, reaching into the newest rotated file when the
    current one is short. None if there is no log at all."""
    target = where or path()
    if where is None and not target.is_absolute():
        return None     # nowhere setup() would have written either
    files = [target.with_name(target.name + ".1"), target]
    found = False
    kept: list[str] = []
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                kept.extend(fh.read().splitlines())
            found = True
        except OSError:
            continue
    if not found:
        return None
    return kept[-lines:] if lines > 0 else kept
