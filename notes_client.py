"""The desktop's end of notes: notes.py run on the notes host over ssh.

Before conn-agent exists the desktop is the only client, so this stands in
for the agent's /notes endpoints (SPEC §6) with the same behaviour: the
files are on the notes host -- the mini, the spec's home for them -- and
this sends notes.py's own source to `python3 -` there with the request set
at the top. Nothing is installed on that machine to make it work.

When the notes host cannot be reached, a new note goes to the outbox
(SPEC §5.2, ~/.config/conn/outbox/) and is sent with the next call that
gets through; the last list fetched is kept, so the Notes view still shows
something on a train.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import threading
import time
from pathlib import Path

import hosts
import notes

SOURCE = Path(notes.__file__).read_text(encoding="utf-8")
OUTBOX = Path("~/.config/conn/outbox").expanduser()
CACHE = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser() \
    / "conn" / "notes.json"
TIMEOUT = 25
# One flush at a time across every thread, or two could send the same
# outbox files when the host comes back.
FLUSH_LOCK = threading.Lock()


class Unreachable(RuntimeError):
    """The notes host did not answer; the caller may queue or show stale."""


def notes_host(known: list[str] | None = None) -> str:
    """CONN_NOTES_HOST, else the mini when it is one of your hosts,
    else this laptop."""
    wanted = os.environ.get("CONN_NOTES_HOST", "").strip()
    if wanted:
        return wanted
    known = hosts.list_hosts() if known is None else known
    return "mini" if "mini" in known else hosts.LOCAL


def program(request: dict) -> str:
    blob = base64.b64encode(json.dumps(request).encode()).decode()
    return f"REQUEST = {blob!r}\n" + SOURCE


def _script(request: dict) -> str:
    code = program(request)
    mark = "CONN_NOTES_" + secrets.token_hex(8).upper()
    return f"exec python3 - <<'{mark}'\n{code}\n{mark}\n"


class NotesClient:
    def __init__(self, host: str, outbox: Path = OUTBOX, cache: Path = CACHE):
        self.host = host
        self.outbox = outbox
        self.cache = cache

    # -- transport ----------------------------------------------------------

    def call(self, op: str, **args) -> dict:
        """One request. Raises Unreachable, or notes.NoteError for a
        request the store refused."""
        script = _script({"op": op, "args": args})
        try:
            done = subprocess.run(hosts.run_argv(self.host),
                                  input=script.encode(), capture_output=True,
                                  timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            raise Unreachable(f"{self.host} did not answer")
        except (OSError, subprocess.SubprocessError) as exc:
            raise Unreachable(str(exc))
        for line in done.stdout.decode(errors="replace").splitlines():
            if line.startswith("CONN_NOTES "):
                reply = json.loads(line[len("CONN_NOTES "):])
                if not reply.get("ok"):
                    raise notes.NoteError(reply.get("error") or "refused")
                return reply
        err = done.stderr.decode(errors="replace").strip()
        raise Unreachable(err.splitlines()[-1] if err
                          else f"no answer from {self.host}")

    # -- the API the GUI uses ------------------------------------------------

    def list(self) -> list[dict]:
        """Every note, newest first. Sends the outbox first, so a note
        jotted offline shows up the moment the host answers."""
        self.flush()
        found = self.call("list")["notes"]
        self._save_cache(found)
        return found

    def cached(self) -> list[dict]:
        try:
            return json.loads(self.cache.read_text())
        except (OSError, ValueError):
            return []

    def queued(self) -> list[dict]:
        out = []
        for path in sorted(self.outbox.glob("*.json")):
            try:
                item = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            item.setdefault("id", "")
            item["queued"] = path.name
            out.append(item)
        return out

    def create(self, project: str, text: str, source: str = "desktop",
               kind: str = "note", chat: str = "",
               queue: bool = True) -> dict:
        """The new note, or -- the host unreachable and queue on -- the
        queued copy with "queued" set. A refusal is never queued."""
        args = dict(project=project, text=text, source=source, kind=kind,
                    chat=chat)
        notes.check_new(project, text, kind)
        try:
            self.flush()
            return self.call("create", **args)["note"]
        except Unreachable:
            if not queue:
                raise
            return self._enqueue(args)

    def update(self, note_id: str, **changes) -> dict:
        return self.call("update", id=note_id, **changes)["note"]

    def delete(self, note_id: str) -> None:
        self.call("delete", id=note_id)

    def unqueue(self, name: str) -> None:
        try:
            (self.outbox / Path(name).name).unlink()
        except OSError:
            pass

    # -- outbox and cache ----------------------------------------------------

    def _enqueue(self, args: dict) -> dict:
        self.outbox.mkdir(parents=True, exist_ok=True)
        args = dict(args, created=notes._now_iso())
        name = f"{int(time.time() * 1000)}-{secrets.token_hex(3)}.json"
        path = self.outbox / name
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(args))
        os.replace(tmp, path)
        return dict(args, id="", status="open", queued=name)

    def flush(self) -> int:
        """Send what the outbox holds, oldest first; the number sent.
        Raises Unreachable like any call -- the files stay for next time.
        An item the store refuses is moved to outbox/rejected/ rather than
        blocking every later call."""
        with FLUSH_LOCK:
            return self._flush()

    def _flush(self) -> int:
        files = sorted(self.outbox.glob("*.json")) if self.outbox.is_dir() \
            else []
        if not files:
            return 0
        batch, sent = [], []
        for path in files:
            try:
                item = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            batch.append({k: item[k] for k in
                          ("project", "text", "source", "kind", "chat",
                           "created") if k in item})
            sent.append(path)
        if not batch:
            return 0
        reply = self.call("create_many", notes=batch)
        results = reply.get("results") or []
        done = 0
        for path, result in zip(sent, results):
            try:
                if result.get("ok"):
                    path.unlink()
                    done += 1
                else:
                    rejected = self.outbox / "rejected"
                    rejected.mkdir(exist_ok=True)
                    os.replace(path, rejected / path.name)
            except OSError:
                pass
        return done

    def _save_cache(self, found: list[dict]) -> None:
        try:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache.with_suffix(".tmp")
            tmp.write_text(json.dumps(found))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.cache)
        except OSError:
            pass
