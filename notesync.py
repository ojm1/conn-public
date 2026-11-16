"""Two-way sync between the notes repo and a Nextcloud folder, over WebDAV.

Runs on the notes host every minute (on a Mac, from a LaunchAgent), next
to notes.py, which it imports. Standard library only, Python 3.9, for the
same reason notes.py is: a stock Mac has /usr/bin/python3 and nothing else.

    python3 notesync.py --url https://cloud.example/remote.php/dav/files/me/Notes/Conn/ \
                        --user me [--root ~/conn-notes] [--dry-run] [--force]

The password is an app password in the login Keychain, service
"conn-nextcloud", account = --user; never on a command line or in a file.

What syncs: <project>/<anything>.md, one level down, both sides. Anything
else on either side (top-level files, deeper folders, other types) is left
alone. A project folder must be a valid project id (notes.PROJECT_ID).

How changes are told apart: the last sync's state, kept in
<root>/.git/conn-nextcloud-sync.json (inside .git, so never committed),
holds each file's sha256 here and ETag there. Against it:

  changed here only     -> PUT (If-Match the ETag we last saw) or DELETE
  changed there only    -> GET and write, or delete here; committed to git
  changed on both sides -> equal: nothing to do. One deleted, one edited:
                           the edit wins. Both edited: this side's version
                           stays at the path on both sides, and theirs is
                           kept beside it as <id>-nextcloud.md -- a note of
                           its own, so nothing is lost and you merge by hand.

A file made in Nextcloud with a name that is not a note id ("Shopping.md")
becomes a note: it gets an id from its date and words and a front matter
block on top (everything it already held is kept, byte for byte), and is
renamed to <id>.md on both sides, so Conn can edit and delete it. Only once
it has sat unchanged and unlocked for SETTLE_SECONDS, and is not empty: New
> New text file makes an empty file and opens it in Text, and renaming it
then would pull it out from under the editor. A plain-named file already
known to the sync (or present here) is synced as it is, never adopted.

Safety:
  - any listing error aborts the whole run before anything is changed
  - a run that would delete more than a third of either side (and more than
    two files) stops and says so; --force goes ahead
  - a local file is re-read just before it is overwritten, and left for the
    next run if it changed in between (Conn writes here too)
  - PUT and DELETE carry If-Match, so a remote edit made mid-run is never
    clobbered: a 412 just leaves the file for the next run
  - Conn's notes.py and this take the same lock (Store.locked) around each
    read-write-commit, so neither writes over the other's change
  - a file locked in Nextcloud (open in Text) is left for a later run; a
    failed PUT or DELETE never makes a second copy or a second note
  - one run at a time (flock), and every change is a git commit, so a bad
    run is one `git revert` away

Logs name paths and actions only, never note text.
"""

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notes  # noqa: E402

KEYCHAIN_SERVICE = "conn-nextcloud"
STATE_NAME = "conn-nextcloud-sync.json"
TIMEOUT = 30
DAV = "{DAV:}"
NC = "{http://nextcloud.org/ns}"
MAX_DELETE_SHARE = 1 / 3
MAX_DELETE_FREE = 2


class SyncError(Exception):
    pass


def log(message: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}", flush=True)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def keychain_password(user: str) -> str:
    try:
        done = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", user, "-w"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SyncError(f"keychain: {exc}")
    if done.returncode != 0 or not done.stdout.strip():
        raise SyncError(f"no keychain item {KEYCHAIN_SERVICE!r} for {user!r}")
    return done.stdout.strip()


def wanted(rel: str) -> bool:
    """<project>/<name>.md, with a project id for a folder."""
    parts = rel.split("/")
    return (len(parts) == 2 and parts[1].endswith(".md")
            and not parts[1].startswith(".")
            and bool(notes.PROJECT_ID.match(parts[0])))


# -- the Nextcloud side ------------------------------------------------------

class Remote:
    """A WebDAV folder. Paths are relative ("personal/x.md")."""

    def __init__(self, url: str, user: str, password: str, opener=None):
        self.base = url if url.endswith("/") else url + "/"
        self.base_path = urllib.parse.urlsplit(self.base).path
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.auth = "Basic " + token
        self.opener = opener or urllib.request.build_opener()

    def _url(self, rel: str) -> str:
        return self.base + urllib.parse.quote(rel)

    def _send(self, method: str, rel: str, data: Optional[bytes] = None,
              headers: Optional[Dict[str, str]] = None):
        req = urllib.request.Request(self._url(rel), data=data, method=method)
        req.add_header("Authorization", self.auth)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with self.opener.open(req, timeout=TIMEOUT) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read() or b"", dict(exc.headers or {})

    def listing(self) -> Dict[str, Dict]:
        """{rel: {"etag", "locked"}} for every wanted file. Raises on any
        failure, so a run never acts on half a listing."""
        found: Dict[str, Dict] = {}
        for rel, _etag, folder, _lk in self._propfind(""):
            if folder and notes.PROJECT_ID.match(rel.rstrip("/")):
                for sub, etag, sub_folder, locked in self._propfind(rel):
                    if not sub_folder and wanted(sub):
                        found[sub] = {"etag": etag, "locked": locked}
        return found

    def _propfind(self, rel: str) -> List[Tuple[str, str, bool, bool]]:
        body = (b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:" '
                b'xmlns:nc="http://nextcloud.org/ns"><d:prop>'
                b"<d:getetag/><d:resourcetype/><nc:lock/>"
                b"<d:lockdiscovery/></d:prop></d:propfind>")
        status, data, _ = self._send(
            "PROPFIND", rel, body,
            {"Depth": "1", "Content-Type": "application/xml"})
        if status != 207:
            raise SyncError(f"PROPFIND {rel or '/'}: HTTP {status}")
        try:
            tree = ET.fromstring(data)
        except ET.ParseError as exc:
            raise SyncError(f"PROPFIND {rel or '/'}: bad XML ({exc})")
        out = []
        for resp in tree.findall(f"{DAV}response"):
            href = urllib.parse.unquote(resp.findtext(f"{DAV}href") or "")
            if not href.startswith(self.base_path):
                continue
            path = href[len(self.base_path):]
            if path.rstrip("/") == rel.rstrip("/"):
                continue            # the folder itself
            etag, folder, locked = "", False, False
            for stat in resp.findall(f"{DAV}propstat"):
                if "200" not in (stat.findtext(f"{DAV}status") or ""):
                    continue
                prop = stat.find(f"{DAV}prop")
                if prop is None:
                    continue
                etag = (prop.findtext(f"{DAV}getetag") or etag).strip('"')
                kind = prop.find(f"{DAV}resourcetype")
                if kind is not None and kind.find(f"{DAV}collection") is not None:
                    folder = True
                # files_lock's nc:lock is "1" while Text (or a person) holds
                # it; a WebDAV lock shows as an activelock.
                if (prop.findtext(f"{NC}lock") or "").strip() not in ("", "0"):
                    locked = True
                disc = prop.find(f"{DAV}lockdiscovery")
                if disc is not None and disc.find(f"{DAV}activelock") is not None:
                    locked = True
            out.append((path.rstrip("/") if folder else path, etag, folder,
                        locked))
        return out

    def get(self, rel: str) -> Tuple[bytes, str]:
        status, data, headers = self._send("GET", rel)
        if status != 200:
            raise SyncError(f"GET {rel}: HTTP {status}")
        return data, _etag(headers)

    def put(self, rel: str, data: bytes, etag: Optional[str]) -> Optional[str]:
        """The new ETag, or None when the file changed there meanwhile."""
        headers = {"Content-Type": "text/markdown; charset=utf-8"}
        if etag:
            headers["If-Match"] = f'"{etag}"'
        else:
            headers["If-None-Match"] = "*"
        status, _, got = self._send("PUT", rel, data, headers)
        if status in (412, 423):    # changed there meanwhile, or locked
            return None
        if status in (404, 409):    # the project folder is not there yet
            self.mkcol(rel.split("/")[0])
            status, _, got = self._send("PUT", rel, data, headers)
        if status in (412, 423):
            return None
        if status not in (200, 201, 204):
            raise SyncError(f"PUT {rel}: HTTP {status}")
        return _etag(got) or self.etag_of(rel)

    def delete(self, rel: str, etag: Optional[str]) -> bool:
        headers = {"If-Match": f'"{etag}"'} if etag else {}
        status, _, _ = self._send("DELETE", rel, None, headers)
        if status in (412, 423):
            return False
        if status not in (200, 204, 404):
            raise SyncError(f"DELETE {rel}: HTTP {status}")
        return True

    def mkcol(self, rel: str) -> None:
        status, _, _ = self._send("MKCOL", rel)
        if status not in (201, 405):        # 405: already there
            raise SyncError(f"MKCOL {rel}: HTTP {status}")

    def etag_of(self, rel: str) -> str:
        folder, _, name = rel.rpartition("/")
        for path, etag, _f, _lk in self._propfind(folder):
            if path == rel:
                return etag
        return ""


def _etag(headers: Dict[str, str]) -> str:
    low = {k.lower(): v for k, v in headers.items()}
    return (low.get("oc-etag") or low.get("etag") or "").strip('"')


# -- this side ---------------------------------------------------------------

class Local:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.store = notes.Store(self.root)

    def listing(self) -> Dict[str, str]:
        found = {}
        if not self.root.is_dir():
            return found
        for path in self.root.glob("*/*.md"):
            rel = path.relative_to(self.root).as_posix()
            if wanted(rel) and path.is_file():
                found[rel] = sha(path.read_bytes())
        return found

    def read(self, rel: str) -> bytes:
        return (self.root / rel).read_bytes()

    def write(self, rel: str, data: bytes, message: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.sync")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        self.store._ready()
        self.store._commit(path, message)

    def remove(self, rel: str, message: str) -> None:
        path = self.root / rel
        try:
            path.unlink()
        except FileNotFoundError:
            return
        self.store._commit(path, message)


# -- the sync ------------------------------------------------------------------

def with_fields(data: bytes, fields: Dict[str, str]) -> bytes:
    """data with these front matter fields set, every other byte kept.

    A file that already opens with a block of our fields (a Conn note,
    perhaps renamed) has those lines replaced or added in place, other lines
    left verbatim. Anything else -- no block, or a '---' block of the user's
    own (YAML with their keys, a rule, a heading) -- is kept whole as the
    body, under a new block of ours."""
    text = data.decode("utf-8", "replace")
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            lines = text[4:end].split("\n")
            keys = [l.partition(":")[0].strip() for l in lines]
            if any(k in notes.FIELDS for k in keys):
                for name, value in fields.items():
                    line = f"{name}: {notes._clean(value)}"
                    if name in keys:
                        lines[keys.index(name)] = line
                    else:
                        lines.append(line)
                return ("---\n" + "\n".join(lines) + text[end:]).encode("utf-8")
    head = ["---"] + [f"{k}: {notes._clean(v)}" for k, v in fields.items()
                      if v] + ["---"]
    return ("\n".join(head) + "\n" + text).encode("utf-8")


def _free_id(base: str, taken: set, root: Path) -> str:
    """base, or base-2, -3...: unused in any project on either side --
    note ids are global (Store._path looks in every folder)."""
    ids = {_id(r) for r in taken}
    note_id, n = base, 1
    while note_id in ids or any(root.glob(f"*/{note_id}.md")):
        n += 1
        note_id = f"{base}-{n}"
    return note_id


def adopt(rel: str, data: bytes, when: float, taken: set,
          root: Path) -> Tuple[str, bytes]:
    """A file made in Nextcloud under a name that is not a note id, as a
    note: (its new rel path, its bytes under our front matter)."""
    project, name = rel.split("/")
    note = notes.parse(data.decode("utf-8", "replace"))
    words = note["text"].strip() or name[:-3]
    stamp = datetime.fromtimestamp(when).astimezone()
    note_id = _free_id(f"{stamp:%Y%m%d-%H%M}-{notes._slug(words)}", taken, root)
    iso = stamp.isoformat(timespec="seconds")
    fields = {"id": note_id, "project": project}
    if not note["created"]:
        fields.update({"created": iso, "updated": iso})
    for key, default in (("source", "nextcloud"), ("status", "open"),
                         ("kind", "note")):
        if not note[key]:
            fields[key] = default
    return f"{project}/{note_id}.md", with_fields(data, fields)


def conflict_copy(rel: str, data: bytes, taken: set,
                  root: Path) -> Tuple[str, bytes]:
    project, name = rel.split("/")
    new_id = _free_id(f"{name[:-3]}-nextcloud", taken, root)
    return f"{project}/{new_id}.md", with_fields(
        data, {"id": new_id, "project": project})


def _id(rel: str) -> str:
    return rel.rsplit("/", 1)[-1][:-3]


# A file made in Nextcloud is adopted only once it has sat unchanged this
# long, and is not locked: New > New text file makes an empty file and
# opens it, and adopting (renaming) it then would pull the file out from
# under the editor.
SETTLE_SECONDS = 5 * 60

FAILS = (SyncError, OSError)        # URLError and socket timeouts are OSErrors


class Sync:
    def __init__(self, local: Local, remote: Remote, state_path: Path,
                 dry_run: bool = False, force: bool = False,
                 now: Optional[float] = None):
        self.local = local
        self.remote = remote
        self.state_path = state_path
        self.dry = dry_run
        self.force = force
        self.now = time.time() if now is None else now
        self.actions: List[str] = []
        self.waiting: Dict[str, Dict] = {}

    def load(self) -> Dict[str, Dict[str, str]]:
        try:
            data = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        self.waiting = data.get("waiting", {}) or {}
        return data.get("files", {}) or {}

    def save(self, files: Dict[str, Dict[str, str]]) -> None:
        if self.dry:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"synced": int(self.now), "files": files,
                                   "waiting": self.waiting},
                                  indent=1, sort_keys=True))
        os.replace(tmp, self.state_path)

    def did(self, what: str) -> None:
        self.actions.append(what)
        log(("would " if self.dry else "") + what)

    def plan(self, here: Dict[str, str], there: Dict[str, str],
             state: Dict[str, Dict[str, str]]) -> List[Tuple[str, str]]:
        """[(rel, action)] with action one of push, pull, del_there,
        del_here, both, forget."""
        out = []
        for rel in sorted(set(here) | set(there) | set(state)):
            L, R, S = here.get(rel), there.get(rel), state.get(rel)
            l_changed = (L != S.get("sha")) if S else L is not None
            r_changed = (R != S.get("etag")) if S else R is not None
            if not l_changed and not r_changed:
                if L is None and R is None and S:
                    out.append((rel, "forget"))
                continue
            if l_changed and not r_changed:
                out.append((rel, "push" if L is not None else "del_there"))
            elif r_changed and not l_changed:
                out.append((rel, "pull" if R is not None else "del_here"))
            elif L is None and R is None:
                out.append((rel, "forget"))
            else:
                out.append((rel, "both"))
        return out

    def guard(self, steps, here, there) -> None:
        for side, action, count in (("here", "del_here", len(here)),
                                    ("Nextcloud", "del_there", len(there))):
            gone = sum(1 for _r, a in steps if a == action)
            if gone > MAX_DELETE_FREE and gone > count * MAX_DELETE_SHARE \
                    and not self.force:
                raise SyncError(f"would delete {gone} of {count} notes {side}; "
                                "not doing that without --force")

    def run(self) -> List[str]:
        state = self.load()
        listing = self.remote.listing()         # raises before any change
        there = {rel: info["etag"] for rel, info in listing.items()}
        self.info = listing
        here = self.local.listing()
        steps = self.plan(here, there, state)
        self.guard(steps, here, there)
        taken = set(here) | set(there)
        self.waiting = {r: w for r, w in self.waiting.items() if r in there}
        try:
            for rel, action in steps:
                try:
                    getattr(self, "_" + action)(rel, here, there, state, taken)
                except FAILS as exc:
                    log(f"skipped {rel}: {exc}")
        finally:
            self.save(state)
        return self.actions

    # -- one file each -----------------------------------------------------

    def _record(self, state, rel, data: bytes, etag: str) -> None:
        state[rel] = {"sha": sha(data), "etag": etag}

    def _unchanged_here(self, rel, here) -> bool:
        """Still what the listing saw (Conn may have written since)."""
        path = self.local.root / rel
        now = sha(path.read_bytes()) if path.exists() else None
        return now == here.get(rel)

    def _forget(self, rel, here, there, state, taken) -> None:
        state.pop(rel, None)

    def _push(self, rel, here, there, state, taken) -> None:
        data = self.local.read(rel)
        self.did(f"push {rel}")
        if self.dry:
            return
        etag = self.remote.put(rel, data, there.get(rel))
        if etag is None:
            log(f"{rel} changed or locked in Nextcloud; next run")
            return
        self._record(state, rel, data, etag)

    def _del_there(self, rel, here, there, state, taken) -> None:
        self.did(f"delete in Nextcloud {rel}")
        if self.dry:
            return
        if self.remote.delete(rel, there.get(rel)):
            state.pop(rel, None)

    def _del_here(self, rel, here, there, state, taken) -> None:
        with self.local.store.locked():
            if not self._unchanged_here(rel, here):
                return              # edited here a moment ago: next run
            self.did(f"delete here {rel}")
            if self.dry:
                return
            self.local.remove(rel, f"nextcloud: delete {_id(rel)}")
            state.pop(rel, None)

    def _settled(self, rel, there) -> bool:
        """A new file there is left alone until it has sat unchanged and
        unlocked for SETTLE_SECONDS, so an editor still has it open."""
        info = self.info.get(rel, {})
        seen = self.waiting.get(rel)
        if not seen or seen.get("etag") != there[rel]:
            self.waiting[rel] = {"etag": there[rel], "since": self.now}
            return False
        return not info.get("locked") and \
            self.now - seen.get("since", self.now) >= SETTLE_SECONDS

    def _pull(self, rel, here, there, state, taken) -> None:
        adopting = not notes.NOTE_ID.match(_id(rel)) and rel not in here \
            and rel not in state
        if adopting and not self._settled(rel, there):
            return
        data, etag = self.remote.get(rel)
        etag = etag or there[rel]
        if adopting:
            if not data.strip():
                return              # an empty new file: wait for words
            return self._adopt(rel, data, etag, state, taken)
        with self.local.store.locked():
            if here.get(rel) == sha(data):
                self._record(state, rel, data, etag)
                return
            if not self._unchanged_here(rel, here):
                return              # edited here a moment ago: next run
            self.did(f"pull {rel}")
            if self.dry:
                return
            verb = "update" if rel in here else "create"
            self.local.write(rel, data, f"nextcloud: {verb} {_id(rel)}")
            self._record(state, rel, data, etag)

    def _adopt(self, rel, data, etag, state, taken) -> None:
        with self.local.store.locked():
            new_rel, new_data = adopt(rel, data, self.now, taken,
                                      self.local.root)
            taken.add(new_rel)
            self.did(f"adopt {rel} as {new_rel}")
            if self.dry:
                return
            new_etag = self.remote.put(new_rel, new_data, None)
            if new_etag is None:
                return
            self.local.write(new_rel, new_data,
                             f"nextcloud: create {_id(new_rel)}")
            self._record(state, new_rel, new_data, new_etag)
            # The old name, as if deleted here: if the DELETE below fails
            # (locked, offline), the next run retries just the delete --
            # and adopts nothing twice. If it is edited there meanwhile,
            # the edit wins and is adopted as a note of its own.
            state[rel] = {"sha": sha(data), "etag": etag,
                          "adopted_as": new_rel}
            self.waiting.pop(rel, None)
        if self.remote.delete(rel, etag):
            state.pop(rel, None)

    def _both(self, rel, here, there, state, taken) -> None:
        L, R = here.get(rel), there.get(rel)
        if R is None:               # deleted there, edited here: edit wins
            return self._push(rel, here, there, state, taken)
        data, etag = self.remote.get(rel)
        etag = etag or R
        if L is None and state.get(rel, {}).get("adopted_as"):
            # Adopted, the old name's delete failed, and it was edited
            # there since: the edit is new words, so it becomes a note too.
            if self._settled(rel, there) and data.strip():
                state.pop(rel, None)
                self._adopt(rel, data, etag, state, taken)
            return
        with self.local.store.locked():
            if not self._unchanged_here(rel, here):
                return              # edited here a moment ago: next run
            if L is None:           # deleted here, edited there: edit wins
                self.did(f"pull {rel} (deleted here, edited in Nextcloud)")
                if not self.dry:
                    self.local.write(rel, data, f"nextcloud: restore {_id(rel)}")
                    self._record(state, rel, data, etag)
                return
            mine = self.local.read(rel)
            if sha(data) == L:
                self._record(state, rel, data, etag)
                return
            copy_rel, copy_data = conflict_copy(rel, data, taken,
                                                self.local.root)
            taken.add(copy_rel)
            self.did(f"conflict {rel}: kept both, theirs as {copy_rel}")
            if self.dry:
                return
            self.local.write(copy_rel, copy_data,
                             f"nextcloud: conflict copy {_id(copy_rel)}")
            # Theirs is safe in the copy now, so record theirs as seen: if a
            # PUT below fails, the next run sees only a change here and
            # retries a plain push -- it never makes a second copy.
            self._record(state, rel, data, etag)
        try:
            copy_etag = self.remote.put(copy_rel, copy_data, None)
            if copy_etag:
                self._record(state, copy_rel, copy_data, copy_etag)
        except FAILS as exc:
            log(f"copy {copy_rel} goes up next run: {exc}")
        new_etag = self.remote.put(rel, mine, etag)
        if new_etag:
            self._record(state, rel, mine, new_etag)


def login(url: str, user: str, opener=None, wait: float = 600) -> int:
    """Get an app password by Nextcloud's Login Flow v2 and put it straight
    in the Keychain: prints only the link to open and approve. The password
    is never printed, never on a command line (it goes to `security -i` on
    stdin), and never in a file."""
    parts = urllib.parse.urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    opener = opener or urllib.request.build_opener()
    req = urllib.request.Request(origin + "/index.php/login/v2", data=b"",
                                 method="POST",
                                 headers={"User-Agent": "conn-notesync"})
    with opener.open(req, timeout=TIMEOUT) as resp:
        start = json.loads(resp.read())
    print("open and approve: " + start["login"], flush=True)
    poll = start["poll"]
    body = urllib.parse.urlencode({"token": poll["token"]}).encode()
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(2)
        try:
            with opener.open(urllib.request.Request(
                    poll["endpoint"], data=body, method="POST"),
                    timeout=TIMEOUT) as resp:
                got = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:     # not approved yet
                continue
            raise
        name, secret = got.get("loginName", ""), got.get("appPassword", "")
        if name != user:
            print(f"signed in as {name!r}, not {user!r}; nothing stored",
                  flush=True)
            return 1
        if not secret or '"' in secret or "\\" in secret or "\n" in secret:
            print("no usable app password came back; nothing stored")
            return 1
        cmd = (f'add-generic-password -U -a "{user}" -s "{KEYCHAIN_SERVICE}" '
               f'-l "Conn notes sync (Nextcloud)" -w "{secret}"\n')
        done = subprocess.run(["security", "-i"], input=cmd, text=True,
                              capture_output=True, timeout=30)
        try:
            keychain_password(user)
        except SyncError as exc:
            print(f"could not store it: {exc} {done.stderr.strip()[:200]}")
            return 1
        print(f"stored in the Keychain for {user}", flush=True)
        return 0
    print("timed out waiting for approval")
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--root", default=str(notes.ROOT))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--login", action="store_true",
                        help="get an app password by Login Flow v2")
    args = parser.parse_args(argv)
    if args.login:
        return login(args.url, args.user)
    root = Path(args.root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    notes.Store(root)._ready()
    lock_path = root / ".git" / "conn-nextcloud-sync.lock"
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0                # the last run is still going
        try:
            remote = Remote(args.url, args.user, keychain_password(args.user))
            Sync(Local(root), remote, root / ".git" / STATE_NAME,
                 dry_run=args.dry_run, force=args.force).run()
        except SyncError as exc:
            log(f"sync stopped: {exc}")
            return 1
        except (OSError, urllib.error.URLError) as exc:
            log(f"sync stopped: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
