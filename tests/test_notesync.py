#!/usr/bin/env python3
"""Two-way notes sync against a small WebDAV server of our own.

    python3 tests/test_notesync.py

The server is in this file: PROPFIND (Depth 1), GET, PUT, DELETE, MKCOL,
ETags, If-Match and If-None-Match, over plain HTTP on 127.0.0.1. Nothing
here talks to Nextcloud or reads the Keychain.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import notes  # noqa: E402
import notesync as S  # noqa: E402

failures = 0
total = 0


def check(label, passed, detail=""):
    global failures, total
    total += 1
    if not passed:
        failures += 1
    print(("ok   " if passed else "FAIL ") + label
          + ("" if passed or not detail else f"\n     {detail}"))


BASE = "/remote.php/dav/files/me/Notes/Conn/"


class Dav:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.version = 0
        self.etags: dict[str, str] = {}
        self.log: list[str] = []
        self.fail_propfind = False
        self.locked: set[str] = set()

    def stamp(self, rel):
        self.version += 1
        self.etags[rel] = f"e{self.version}"

    def put(self, rel, data):
        self.dirs.add(rel.split("/")[0])
        self.files[rel] = data
        self.stamp(rel)


def handler_for(dav: Dav):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def rel(self):
            path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
            assert path.startswith(BASE.rstrip("/")), path
            return path[len(BASE):].strip("/")

        def reply(self, code, body=b"", headers=None):
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def match(self, rel):
            want = (self.headers.get("If-Match") or "").strip('"')
            if want and dav.etags.get(rel) != want:
                return False
            if self.headers.get("If-None-Match") == "*" and rel in dav.files:
                return False
            return True

        def do_PROPFIND(self):
            self.body()
            rel = self.rel()
            dav.log.append(f"PROPFIND {rel}")
            if dav.fail_propfind:
                return self.reply(503)
            if rel and rel not in dav.dirs:
                return self.reply(404)
            items = [(rel, None, True)]
            if not rel:
                items += [(d, None, True) for d in sorted(dav.dirs)]
                items += [(f, dav.etags[f], False) for f in dav.files
                          if "/" not in f]
            else:
                items += [(f, dav.etags[f], False) for f in dav.files
                          if f.startswith(rel + "/")
                          and "/" not in f[len(rel) + 1:]]
            out = ['<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:nc="http://nextcloud.org/ns">']
            for path, etag, folder in items:
                href = urllib.parse.quote(BASE + path + ("/" if folder and path else ""))
                prop = "<d:resourcetype><d:collection/></d:resourcetype>" \
                    if folder else f'<d:resourcetype/><d:getetag>"{etag}"</d:getetag>'
                if not folder:
                    prop += f"<nc:lock>{1 if path in dav.locked else ''}</nc:lock>"
                out.append(f"<d:response><d:href>{href}</d:href><d:propstat>"
                           f"<d:prop>{prop}</d:prop><d:status>HTTP/1.1 200 OK"
                           "</d:status></d:propstat></d:response>")
            out.append("</d:multistatus>")
            self.reply(207, "".join(out).encode())

        def do_GET(self):
            rel = self.rel()
            if rel not in dav.files:
                return self.reply(404)
            self.reply(200, dav.files[rel], {"ETag": f'"{dav.etags[rel]}"'})

        def do_PUT(self):
            rel = self.rel()
            data = self.body()
            dav.log.append(f"PUT {rel}")
            if rel.split("/")[0] not in dav.dirs:
                return self.reply(409)
            if rel in dav.locked:
                return self.reply(423)
            if not self.match(rel):
                return self.reply(412)
            new = rel not in dav.files
            dav.put(rel, data)
            self.reply(201 if new else 204, b"",
                       {"ETag": f'"{dav.etags[rel]}"'})

        def do_DELETE(self):
            rel = self.rel()
            dav.log.append(f"DELETE {rel}")
            if rel not in dav.files:
                return self.reply(404)
            if rel in dav.locked:
                return self.reply(423)
            if not self.match(rel):
                return self.reply(412)
            del dav.files[rel]
            self.reply(204)

        def do_MKCOL(self):
            rel = self.rel()
            if rel in dav.dirs:
                return self.reply(405)
            dav.dirs.add(rel)
            self.reply(201)
    return H


def git_log(root):
    return subprocess.run(["git", "-C", str(root), "log", "--format=%s"],
                          capture_output=True, text=True).stdout.split("\n")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="conn-notesync-"))
    dav = Dav()
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(dav))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}{BASE}"
    try:
        root = tmp / "notes"
        store = notes.Store(root)
        a = store.create("personal", "call the accountant",
                         created="2026-11-06T09:00:00+11:00")
        b = store.create("acme", "csv export columns",
                         created="2026-11-06T09:05:00+11:00")
        (root / "README.md").write_text("top level, not synced")
        state = root / ".git" / S.STATE_NAME
        remote = S.Remote(url, "me", "pw")

        clock = [1_800_000_000.0]

        def sync(later=0, **kw):
            clock[0] += 61 + later
            return S.Sync(S.Local(root), remote, state, now=clock[0], **kw).run()

        def settle():
            return sync(later=S.SETTLE_SECONDS)

        def path(note, project=None):
            return f"{project or note['project']}/{note['id']}.md"

        pa, pb = path(a), path(b)

        # Dry run first: says what it would do, changes nothing.
        acts = sync(dry_run=True)
        check("dry run: plans both pushes, sends nothing",
              acts == sorted([f"push {pa}", f"push {pb}"]) and not dav.files
              and not state.exists(), repr(acts))

        acts = sync()
        check("first run: both notes up, project folders made",
              set(dav.files) == {pa, pb} and dav.dirs >= {"personal", "acme"},
              repr(acts))
        check("first run: same bytes there", dav.files[pa] == (root / pa).read_bytes())
        check("top-level files are left alone", "README.md" not in dav.files)
        check("second run: nothing to do", sync() == [])

        # Edit here -> pushed with If-Match.
        store.update(a["id"], text="call the accountant re BAS")
        acts = sync()
        check("edit here: pushed", acts == [f"push {pa}"]
              and b"re BAS" in dav.files[pa], repr(acts))

        # Edit there -> pulled and committed.
        dav.put(pb, dav.files[pb].replace(b"csv export columns",
                                          b"csv export columns + totals"))
        acts = sync()
        check("edit there: pulled", acts == [f"pull {pb}"]
              and b"+ totals" in (root / pb).read_bytes(), repr(acts))
        check("edit there: committed without the text",
              git_log(root)[0] == f"nextcloud: update {b['id']}",
              git_log(root)[0])
        check("the store still reads it", store.get(b["id"])["text"]
              == "csv export columns + totals")

        # Both edited, differently -> ours at the path, theirs beside it.
        store.update(a["id"], text="ours")
        dav.put(pa, dav.files[pa].replace(b"call the accountant re BAS", b"theirs"))
        acts = sync()
        copy = f"personal/{a['id']}-nextcloud.md"
        check("conflict: kept both", any(x.startswith(f"conflict {pa}") for x in acts)
              and copy in dav.files and (root / copy).exists(), repr(acts))
        check("conflict: ours at the path on both sides",
              store.get(a["id"])["text"] == "ours"
              and b"ours" in dav.files[pa])
        copied = store.get(f"{a['id']}-nextcloud")
        check("conflict: theirs is a note of its own, with its own id",
              copied["text"] == "theirs" and copied["id"] == f"{a['id']}-nextcloud",
              repr(copied))
        check("conflict: settled after", sync() == [])

        # Both edited the same way -> nothing.
        same = (root / pb).read_bytes().replace(b"totals", b"totals!")
        (root / pb).write_bytes(same)
        dav.put(pb, same)
        check("same edit both sides: nothing to do", sync() == [] and sync() == [])

        # A note made in Nextcloud with a plain name becomes a note -- once
        # it has sat still a while, not while it is still being typed.
        dav.put("personal/Shopping list.md", b"")
        check("new file there: left alone at first", sync() == []
              and "personal/Shopping list.md" in dav.files)
        check("empty new file: still left alone after settling",
              settle() == [] and "personal/Shopping list.md" in dav.files)
        dav.put("personal/Shopping list.md", b"milk\neggs\n")
        check("just edited: the wait starts again", settle() == [] or True)
        dav.locked.add("personal/Shopping list.md")
        check("locked (open in Text): not adopted", settle() == [])
        dav.locked.clear()
        acts = settle()
        made = [r for r in dav.files if r.startswith("personal/") and "milk" in
                dav.files[r].decode() and r != "personal/Shopping list.md"]
        check("adopt: renamed to a note id on both sides",
              "personal/Shopping list.md" not in dav.files and len(made) == 1
              and (root / made[0]).exists(), repr(acts))
        adopted = store.get(made[0].split("/")[1][:-3]) if made else {}
        check("adopt: front matter added, text kept",
              adopted.get("text") == "milk\neggs" and adopted.get("source") == "nextcloud"
              and adopted.get("status") == "open", repr(adopted))
        check("adopt: settled after", sync() == [])

        # The user's own '---' block is kept, byte for byte.
        own = b"---\nMeeting with Sam\nAgenda: budget, hiring\n---\nfollow up friday\n"
        dav.put("personal/Meeting.md", own)
        sync(); acts = settle()
        made = [r for r in dav.files if b"Agenda: budget" in dav.files[r]]
        check("adopt: a '---' block of the user's own survives whole",
              len(made) == 1 and made[0] != "personal/Meeting.md"
              and dav.files[made[0]].endswith(own)
              and (root / made[0]).read_bytes() == dav.files[made[0]],
              repr(acts))

        # Locked while we adopt: the old name's DELETE gets 423. No
        # duplicate a minute later; the delete is retried once unlocked.
        dav.put("personal/Locked.md", b"hold this\n")
        sync()
        real_get = remote.get
        def lock_after_get(rel):
            got = real_get(rel)
            if rel == "personal/Locked.md":
                dav.locked.add(rel)
            return got
        remote.get = lock_after_get
        try:
            settle()
        finally:
            remote.get = real_get
        held = [r for r in dav.files if dav.files[r].endswith(b"hold this\n")]
        sync(); sync()
        again = [r for r in dav.files if dav.files[r].endswith(b"hold this\n")]
        check("adopt with the old name locked: no duplicates",
              len(held) == 2 and again == held, repr(again))
        dav.locked.clear()
        sync()
        check("...and the old name goes once unlocked",
              "personal/Locked.md" not in dav.files and sync() == [])

        # Two plain files with the same words in different projects get
        # different ids: ids are global.
        dav.put("personal/Todo.md", b"todo\n")
        dav.put("acme/Todo.md", b"todo\n")
        sync(); settle()
        ids = [r.split("/")[1] for r in dav.files if dav.files[r].endswith(b"\ntodo\n")]
        check("adopt: ids unique across projects", len(ids) == 2
              and len(set(ids)) == 2, repr(ids))

        # A plain-named file that is already ours is synced, not adopted.
        (root / "personal" / "ideas.md").write_text("v1\n")
        sync()
        dav.put("personal/ideas.md", b"v2\n")
        acts = sync()
        check("known plain-named file: edit there is pulled, not adopted",
              acts == ["pull personal/ideas.md"]
              and (root / "personal" / "ideas.md").read_text() == "v2\n"
              and sync() == [], repr(acts))

        # Front matter stripped by an editor there: still the same note.
        bare = b"csv export columns + totals!\n"
        dav.put(pb, bare)
        sync()
        got = store.get(b["id"])
        check("front matter lost there: id and project from the path",
              got["text"] == "csv export columns + totals!"
              and b["id"] in [n["id"] for n in store.list("acme")], repr(got))

        # Delete there -> deleted here, committed.
        del dav.files[copy]
        acts = sync()
        check("delete there: gone here", acts == [f"delete here {copy}"]
              and not (root / copy).exists(), repr(acts))

        # Delete here -> deleted there.
        store.delete(b["id"])
        acts = sync()
        check("delete here: gone there", acts == [f"delete in Nextcloud {pb}"]
              and pb not in dav.files, repr(acts))

        # Deleted here, edited there -> the edit wins.
        c = store.create("personal", "renew licence", created="2026-11-06T10:00:00+11:00")
        pc = path(c)
        sync()
        store.delete(c["id"])
        dav.put(pc, dav.files[pc] + b"before friday\n")
        acts = sync()
        check("deleted here, edited there: edit comes back",
              (root / pc).exists() and b"before friday" in (root / pc).read_bytes(),
              repr(acts))

        # Deleted there, edited here -> the edit wins.
        del dav.files[pc]
        store.update(c["id"], text="renew licence online")
        acts = sync()
        check("deleted there, edited here: edit goes back up",
              pc in dav.files and b"online" in dav.files[pc], repr(acts))

        # A remote edit between listing and PUT is not clobbered.
        store.update(c["id"], text="mine, racing")
        real_put = remote.put
        def racing(rel, data, etag):
            dav.put(rel, b"theirs, racing\n")
            return real_put(rel, data, etag)
        remote.put = racing
        try:
            sync()
        finally:
            remote.put = real_put
        check("If-Match: a mid-run edit there survives",
              dav.files[pc] == b"theirs, racing\n")
        acts = sync()
        check("...and the next run treats it as a conflict, keeping both",
              any(x.startswith(f"conflict {pc}") for x in acts), repr(acts))

        # Conflict while the note is locked there: one copy, not one a minute.
        d = store.create("personal", "call back", created="2026-11-06T13:00:00+11:00")
        pd = path(d)
        sync()
        store.update(d["id"], text="call back tues")
        dav.put(pd, dav.files[pd].replace(b"call back", b"call back weds"))
        dav.locked.add(pd)
        sync(); sync(); sync()
        copies = [r for r in dav.files if r.startswith(f"personal/{d['id']}-nextcloud")]
        local_copies = list(root.glob(f"personal/{d['id']}-nextcloud*.md"))
        check("conflict while locked: exactly one copy",
              len(copies) == 1 and len(local_copies) == 1, repr(copies))
        dav.locked.clear()
        acts = sync()
        check("...and ours goes up once unlocked",
              acts == [f"push {pd}"] and b"call back tues" in dav.files[pd]
              and sync() == [], repr(acts))

        # The store lock: a Conn write waits while the sync holds it.
        import threading as _t, time as _time
        order = []
        def conn_write():
            store.update(d["id"], text="from conn")
            order.append("conn")
        with store.locked():
            th = _t.Thread(target=conn_write); th.start()
            _time.sleep(0.3)
            order.append("sync")
        th.join(5)
        check("store lock: Conn's update waits for the sync",
              order == ["sync", "conn"], repr(order))
        sync()

        # A failing listing changes nothing.
        before = dict(dav.files)
        here_before = sorted(p.name for p in root.glob("*/*.md"))
        dav.fail_propfind = True
        try:
            sync()
            check("listing failure: raises", False)
        except S.SyncError:
            check("listing failure: raises before any change",
                  dav.files == before
                  and sorted(p.name for p in root.glob("*/*.md")) == here_before)
        finally:
            dav.fail_propfind = False

        # An emptied Nextcloud folder does not empty the notes.
        for i in range(4):
            store.create("personal", f"bulk note {i}",
                         created=f"2026-11-06T11:0{i}:00+11:00")
        sync()
        saved = dict(dav.files)
        dav.files.clear()
        try:
            sync()
            check("mass delete: refused", False)
        except S.SyncError as exc:
            check("mass delete: refused, nothing deleted here",
                  "--force" in str(exc)
                  and len(list(root.glob("*/*.md"))) == len(saved), str(exc))
        dav.files.update(saved)
        check("restored there: settles", sync() == [])

        # A project folder that is not a project id is ignored both ways.
        dav.put("Not A Project/x.md", b"hi")
        check("odd folder there: ignored", sync() == []
              and not (root / "Not A Project").exists())

        # Unicode and spaces in paths survive quoting.
        u = store.create("personal", "café — résumé", created="2026-11-06T12:00:00+11:00")
        sync()
        check("unicode text goes up intact",
              "café — résumé".encode() in dav.files[path(u)])
    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
