#!/usr/bin/env python3
"""The transport, checked without a network.

hosts.py talks to ssh, sshfs, tmux and the keyring. This file checks the
parts that decide things -- frame parsing, marker forgery, the mount table,
error routing -- without touching any of them: every process spawned here is
a fake on PATH or a pipe of our own. No display, no remote, and nothing of
yours is read or rewritten. The one real process is a tmux server of the
checks' own (tmux -L conn-test-<pid>), reached through a shim on PATH so the
probe, stream and option scripts run unmodified against it; it is killed on
the way out, and without tmux those checks skip.

    python3 tests/test_hosts.py

The notification and stream sections import gui, which needs the GTK stack
installed but no display; on a machine without it they skip rather than fail,
the way test_gui.py does without a display.
"""

from __future__ import annotations

import contextlib
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import hosts  # noqa: E402

# A per-run boundary token, the shape watch_screens mints.
TOKEN = "feedbeeffeedbeef"
RULE = "─" * 100


class Checks:
    def __init__(self):
        self.failures = 0

    def __call__(self, name: str, passed: bool, detail: str = "") -> None:
        self.failures += not passed
        print(f"{'ok  ' if passed else 'FAIL'} {name}"
              + (f"   {detail}" if detail and not passed else ""))


def frame_checks(check: Checks) -> None:
    lines = [
        f"@@@{TOKEN}:SESSION:deploy",
        "\x1b[2msuggestion\x1b[0m line",
        "@@@SESSION:evil",                  # forged: no token at all
        f"@@@{'0' * 16}:SESSION:evil",      # forged: the wrong token
        f"@@@{TOKEN}:PANES:deploy",
        "claude",
        "",
        f"@@@{TOKEN}:SESSION:myself",
        "at a prompt",
        f"@@@{TOKEN}:PANES:myself",
        "bash",
    ]
    # own_session() caches what tmux said at first ask; pinned here so the
    # checks do not depend on whether this test runs inside tmux.
    was, hosts._OWN_SESSION = hosts._OWN_SESSION, "myself"
    try:
        local = hosts.parse_frame(lines, "local", TOKEN)
        remote = hosts.parse_frame(lines, "web-1", TOKEN)
    finally:
        hosts._OWN_SESSION = was

    check("a frame parses into its sessions",
          set(remote) == {"deploy", "myself"}, f"got={sorted(remote)}")
    check("the raw screen keeps its escapes beside the stripped copy",
          "\x1b[2m" in remote["deploy"]["raw"]
          and "\x1b" not in remote["deploy"]["screen"])
    check("a printed marker without the token is content, not a boundary",
          "evil" not in remote
          and "@@@SESSION:evil" in remote["deploy"]["screen"],
          f"sessions={sorted(remote)}")
    check("and so is one carrying somebody else's token",
          "SESSION:evil" in remote["deploy"]["screen"])
    check("the pane commands ride along",
          remote["deploy"]["commands"] == ["claude"],
          f"commands={remote['deploy']['commands']}")
    check("our own session is dropped on the local host",
          set(local) == {"deploy"}, f"got={sorted(local)}")
    check("but a remote session with our name is a different session",
          "myself" in remote)
    check("no lines, no sessions", hosts.parse_frame([], "web-1", TOKEN) == {})


def capture_checks(check: Checks) -> None:
    text = (f"###{TOKEN}:CAPTURE\n"
            f"@@@{TOKEN}:SESSION:deploy\n"
            "  compiling\n"
            "###:END\n"                    # forged: an END with no token
            f"@@@{TOKEN}:PANES:deploy\n"
            "claude|/srv/app|logs\n"       # a path with '|' of its own
            f"###{TOKEN}:END\n")
    screens, panes = hosts._split_captures(text, TOKEN)
    check("the capture ends only at its own END marker",
          "###:END" in screens.get("deploy", ""),
          f"screen={screens.get('deploy')!r}")
    check("a pane path keeps its pipes -- only the first splits",
          panes.get("deploy") == [{"cmd": "claude", "path": "/srv/app|logs"}],
          f"panes={panes.get('deploy')}")
    check("no CAPTURE marker, no captures",
          hosts._split_captures("just output\n", TOKEN) == ({}, {}))

    parts = hosts._split_sections(
        f"###{TOKEN}:UPTIME\nup 3 days\n###:LOAD\n", TOKEN)
    check("a section header without the token is content, not a header",
          parts["UPTIME"] == ["up 3 days", "###:LOAD"] and parts["LOAD"] == [],
          f"parts={parts['UPTIME']}")


def meta_record(token: str, **fields) -> str:
    """One META_FORMAT record as tmux prints it, unset fields empty."""
    values = [str(fields.get(key, "")) for key, _fmt in hosts.META_FIELDS]
    return (f"@@@{token}:REC" + f"@@@{token}:F".join(values)
            + f"@@@{token}:EOR")


def meta_frame_checks(check: Checks) -> None:
    """The chat metadata riding a frame: free text in every field, so the
    token is the only thing allowed to delimit it."""
    # tmux passes newlines and tabs through, and a title can carry a marker
    # as text -- though never the real token, which nothing on the far side
    # knows, so the forgery a screen *can* print is a near miss.
    hostile_title = ("first line\n"
                     f"@@@{'0' * len(TOKEN)}:SESSION:ghost\n"
                     "|tab\there|")
    text = "\n".join([
        meta_record(TOKEN, name="deploy", created="1793800000",
                    project="acme", title=hostile_title,
                    task="~/.conn/tasks/csv.md", last_event="1793856000",
                    starred="1", source="desktop", conn_name="My CSV",
                    pane_title="✳ Telegram error reports",
                    path="/srv/app|logs"),
        # wrong token: a screen's attempt to speak for another session
        meta_record("0" * len(TOKEN), name="deploy", title="forged"),
        # cut off mid-record: dropped, not guessed at
        f"@@@{TOKEN}:REC" + "plain" + f"@@@{TOKEN}:F" + "1",
        # a session the metadata saw but the loop did not (gone between calls)
        meta_record(TOKEN, name="vanished", title="ghost"),
        meta_record(TOKEN, name="plain", created="17", last_event="soon",
                    starred="yes", pane_title="web-1"),
    ])
    lines = text.split("\n") + [
        f"@@@{TOKEN}:SESSION:deploy", "screen", f"@@@{TOKEN}:PANES:deploy",
        "claude",
        f"@@@{TOKEN}:SESSION:plain", "$ ", f"@@@{TOKEN}:PANES:plain", "bash",
        f"@@@{TOKEN}:SESSION:bare", "", f"@@@{TOKEN}:PANES:bare", "bash",
    ]
    frames = hosts.parse_frame(lines, "web-1", TOKEN)
    deploy, plain, bare = (frames.get(n, {}) for n in ("deploy", "plain", "bare"))

    check("the session set is the loop's, never the metadata's",
          set(frames) == {"deploy", "plain", "bare"}, f"got={sorted(frames)}")
    check("the options arrive typed",
          deploy.get("options") == {
              "project": "acme", "title": hostile_title,
              "task": "~/.conn/tasks/csv.md", "last_event": 1793856000,
              "starred": True, "source": "desktop", "name": "My CSV", "restored": 0, "autotitle": "", "autotitle_for": ""},
          f"options={deploy.get('options')}")
    check("a title keeps its newline, pipes, tab and fake marker verbatim",
          deploy["options"]["title"].count("\n") == 2
          and "ghost" not in frames)
    check("pane title, active path and creation time ride along",
          deploy.get("pane_title") == "✳ Telegram error reports"
          and deploy.get("path") == "/srv/app|logs"
          and deploy.get("created") == 1793800000,
          f"deploy={deploy.get('pane_title')!r} {deploy.get('path')!r}")
    check("the metadata block is not screen text",
          deploy["screen"] == "screen" and "REC" not in plain["screen"],
          f"screen={deploy['screen']!r}")
    check("a record under somebody else's token is ignored",
          deploy["options"]["title"] != "forged")
    check("unparseable numbers and stars read as unset",
          plain["options"]["last_event"] is None
          and plain["options"]["starred"] is False
          and plain["created"] == 17 and plain["pane_title"] == "web-1",
          f"plain={plain.get('options')}")
    check("a session with no record has every key, all unset",
          bare.get("options") == {"project": "", "title": "", "task": "",
                                  "last_event": None, "starred": False,
                                  "source": "", "name": "", "restored": 0, "autotitle": "", "autotitle_for": ""}
          and bare["pane_title"] == "" and bare["path"] == ""
          and bare["created"] == 0, f"bare={bare}")
    check("and the screen/commands contract is untouched",
          deploy["commands"] == ["claude"] and plain["commands"] == ["bash"])


def probe_parse_checks(check: Checks) -> None:
    """probe() over a canned transcript: the token is lifted from the script
    it sends, the way the far side would echo it."""
    import re

    sent: list[str] = []

    def fake_run(argv, input="", **_kw):
        sent.append(input)
        token = re.search(r"###([0-9a-f]{32}):UPTIME", input).group(1)
        b = lambda s: f"###{token}:{s}"
        out = "\n".join([
            b("HOME"), "/home/deploy", b("HOSTNAME"), "box-01",
            b("UPTIME"), "up 3 days", b("LOAD"), "0.1 0.2 0.3", b("CPUS"), "4",
            b("MEM"), "512 2048", b("DISK"), "5G 20G 25%",
            b("TMUX"), "chat|1|detached|1793856000",
            "odd|name|2|attached|1793856001",
            "shell|1|detached|1793856002",
            b("CAPTURE"),
            meta_record(token, name="chat", created="1793000000",
                        title="CSV\nexport", starred="1",
                        pane_title="⠋ Working on it", path="/home/deploy/app"),
            meta_record(token, name="odd|name", created="1793000001",
                        last_event="²"),
            f"@@@{token}:SESSION:chat", "hello",
            f"@@@{token}:PANES:chat", "claude|/home/deploy/app",
            f"@@@{token}:SESSION:odd|name", "",
            f"@@@{token}:PANES:odd|name", "bash|/tmp",
            f"@@@{token}:SESSION:shell", "$",
            f"@@@{token}:PANES:shell", "bash|/var/log",
            b("END"), ""])
        return subprocess.CompletedProcess(argv, 0, out, "")

    was_run, was_mine = hosts.subprocess.run, hosts._OWN_SESSION
    hosts.subprocess.run, hosts._OWN_SESSION = fake_run, ""
    try:
        row = hosts.probe("local")
    finally:
        hosts.subprocess.run, hosts._OWN_SESSION = was_run, was_mine

    by_name = {s["name"]: s for s in row["sessions"]}
    check("the probe sends one metadata call, before the session loop",
          sent and sent[0].count("list-sessions -F \"@@@") == 1
          and sent[0].index(":REC") < sent[0].index(":SESSION:"))
    check("the probe row carries the remote home",
          row.get("home") == "/home/deploy", f"home={row.get('home')!r}")
    check("probe sessions carry the options",
          by_name["chat"]["options"]["title"] == "CSV\nexport"
          and by_name["chat"]["options"]["starred"] is True
          and by_name["chat"]["created"] == 1793000000
          and by_name["chat"]["pane_title"] == "⠋ Working on it",
          f"chat={by_name.get('chat')}")
    check("a name with '|' still meets its record",
          by_name["odd|name"]["created"] == 1793000001)
    check("no record: the path falls back to the first pane's",
          by_name["shell"]["path"] == "/var/log"
          and by_name["shell"]["options"]["last_event"] is None,
          f"shell={by_name.get('shell')}")
    check("a blank row has a home key too", hosts.blank("x")["home"] == "")
    # tmux titles a pane with the hostname until something sets one; the
    # chat list needs it to tell that default from a real title.
    check("the probe row carries the machine's hostname",
          row.get("hostname") == "box-01" and hosts.blank("x")["hostname"] == "",
          f"hostname={row.get('hostname')!r}")
    # isdigit() is True for '²', which int() rejects: probe() raised, and
    # the host sat in-flight for the rest of the run.
    check("a non-ASCII digit stamp reads as unset instead of raising",
          by_name["odd|name"]["options"]["last_event"] is None
          and hosts._int_or("²", None) is None
          and hosts._int_or("١٢", None) is None
          and hosts._int_or(" 42 ", None) == 42)


def set_option_checks(check: Checks) -> None:
    """set_chat_option without a server: what it would send, and refusals."""
    sent: list[str] = []
    result = {"rc": 0, "err": b""}

    def fake_run(argv, input=b"", **_kw):
        sent.append(input.decode())
        return subprocess.CompletedProcess(argv, result["rc"], b"",
                                           result["err"])

    def raised(call) -> str:
        try:
            call()
        except hosts.HostError as exc:
            return str(exc)
        return ""

    was = hosts.subprocess.run
    hosts.subprocess.run = fake_run
    try:
        hosts.set_chat_option("web-1", "deploy", "starred", "1")
        plain = sent[-1]
        hosts.set_chat_option("web-1", "deploy", "starred", None)
        unset = sent[-1]
        hosts.set_chat_option("web-1", "x'y", "title", "$(reboot); `id` 'q'")
        hostile = sent[-1]
        hosts.set_chat_option("web-1", "deploy", "title", "ends;")
        semi = sent[-1]
        before = len(sent)
        bad_name = raised(lambda: hosts.set_chat_option(
            "web-1", "deploy", "status-left", "x"))
        bad_quote = raised(lambda: hosts.set_chat_option(
            "web-1", "deploy", "title; reboot", "x"))
        refused = len(sent) == before
        result.update(rc=1, err=b"no such session: =gone:\n")
        failed = raised(lambda: hosts.set_chat_option(
            "web-1", "gone", "title", "x"))
    finally:
        hosts.subprocess.run = was

    import shlex
    check("a set targets the exact session and the @conn_ option",
          plain == "tmux set-option -t =deploy: -- @conn_starred 1", plain)
    check("an unset is -u",
          unset == "tmux set-option -u -t =deploy: @conn_starred", unset)
    words = shlex.split(hostile)
    check("hostile names and values stay single shell words",
          words == ["tmux", "set-option", "-t", "=x'y:", "--", "@conn_title",
                    "$(reboot); `id` 'q'"], f"words={words}")
    check("a trailing ';' is escaped for tmux's own parser",
          shlex.split(semi)[-1] == r"ends\;", semi)
    check("only the SPEC's options are accepted, before any ssh",
          "not a chat option" in bad_name and "not a chat option" in bad_quote
          and refused)
    check("a tmux failure is a HostError with tmux's words",
          failed == "no such session: =gone:", failed)


@contextlib.contextmanager
def private_tmux():
    """A tmux server of our own, and a `tmux` on PATH that only talks to it.

    Everything hosts.py runs for the "local" host is plain bash calling plain
    `tmux`, so a shim is the whole redirect: the scripts under test are the
    real ones, byte for byte, and no session of yours is listed, touched or
    starred. TMUX is dropped too -- inside tmux it would name your server --
    though the shim's -L wins over it anyway.
    """
    real = shutil.which("tmux")
    if real is None:
        yield None
        return
    sock = f"conn-test-{os.getpid()}"
    with tempfile.TemporaryDirectory() as tmp:
        shim = Path(tmp, "tmux")
        shim.write_text(f'#!/bin/sh\nexec {real} -L {sock} -f /dev/null "$@"\n')
        shim.chmod(0o755)
        saved = {k: os.environ.get(k) for k in ("PATH", "TMUX")}
        os.environ["PATH"] = f"{tmp}:{saved['PATH']}"
        os.environ.pop("TMUX", None)
        was_mine, hosts._OWN_SESSION = hosts._OWN_SESSION, ""
        try:
            yield str(shim)
        finally:
            subprocess.run([str(shim), "kill-server"], capture_output=True)
            # tmux can leave the socket file behind a killed server.
            Path(os.environ.get("TMUX_TMPDIR") or "/tmp",
                 f"tmux-{os.getuid()}", sock).unlink(missing_ok=True)
            hosts._OWN_SESSION = was_mine
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def read_frame(proc, timeout: float = 10.0) -> dict | None:
    """The next whole frame off a watch_screens process, parsed."""
    import select
    deadline = time.monotonic() + timeout
    buffer: list[str] = []
    pending = b""
    poller = select.poll()
    poller.register(proc.stdout.fileno(), select.POLLIN)
    while time.monotonic() < deadline:
        if not poller.poll(200):
            continue
        chunk = os.read(proc.stdout.fileno(), 65536)
        if not chunk:
            return None
        pending += chunk
        while b"\n" in pending:
            raw, _, pending = pending.partition(b"\n")
            line = raw.decode(errors="replace")
            if line.startswith(proc.frame):
                return hosts.parse_frame(buffer, "local", proc.boundary)
            if not line.startswith(proc.alive):
                buffer.append(line)
    return None


def live_tmux_checks(check: Checks) -> None:
    """The real scripts against a real (private) tmux: the probe, the stream
    and set_chat_option must agree with each other and with tmux."""
    with private_tmux() as tmux:
        if tmux is None:
            print("skipped: live tmux checks need tmux")
            return
        for name in ("chat", "shell"):
            subprocess.run([tmux, "new-session", "-d", "-s", name,
                            "-x", "80", "-y", "20", "-c", "/tmp", "sh"],
                           check=True)
        hostile = "line one\nline two | ; $(x) `y` 'z' \"w\" ~ #{session_name};"
        hosts.set_chat_option("local", "chat", "title", hostile)
        hosts.set_chat_option("local", "chat", "starred", "1")
        hosts.set_chat_option("local", "chat", "last_event", "1793856000")
        hosts.set_chat_option("local", "shell", "project", "doomed")
        hosts.set_chat_option("local", "shell", "project", None)
        hosts.set_chat_option("local", "chat", "name", "My own name")
        stored = subprocess.run(
            [tmux, "show-options", "-v", "-t", "=chat:", "@conn_title"],
            capture_output=True, text=True).stdout.removesuffix("\n")
        check("tmux stores exactly the value we set, hostile parts and all",
              stored == hostile, f"stored={stored!r}")
        missing = ""
        try:
            hosts.set_chat_option("local", "cha", "title", "prefix?")
        except hosts.HostError as exc:
            missing = str(exc)
        check("the target is exact: a prefix does not reach 'chat'",
              "cha" in missing, f"error={missing!r}")

        row = hosts.probe("local")
        by_name = {s["name"]: s for s in row["sessions"]}
        check("the live probe sees both sessions and our home",
              set(by_name) == {"chat", "shell"}
              and row["home"] == os.environ.get("HOME")
              and row["hostname"] == os.uname().nodename,
              f"sessions={sorted(by_name)} home={row.get('home')!r}")
        chat = by_name.get("chat", {})
        check("and reads the options back through tmux formats",
              chat.get("options", {}).get("title") == hostile
              and chat["options"]["starred"] is True
              and chat["options"]["last_event"] == 1793856000
              and chat.get("created", 0) > 0 and chat.get("path") == "/tmp",
              f"chat={chat.get('options')} path={chat.get('path')!r}")
        check("an unset option reads as unset",
              by_name.get("shell", {}).get("options", {}).get("project") == "")
        check("the user's own name for a chat reads back as options['name']",
              chat.get("options", {}).get("name") == "My own name"
              and chat.get("name") == "chat",
              f"chat={chat.get('options')}")

        proc = hosts.watch_screens("local", 0.2)
        try:
            first = read_frame(proc)
            check("the live stream carries the same sessions as the probe",
                  first is not None and set(first) == set(by_name),
                  f"frame={sorted(first or {})}")
            check("and the same options",
                  first is not None
                  and first["chat"]["options"] == chat.get("options")
                  and first["chat"]["path"] == "/tmp"
                  and first["chat"]["created"] == chat.get("created"))
            hosts.set_chat_option("local", "shell", "starred", "1")
            second = read_frame(proc)
            check("an option changing is a change: a new frame goes out",
                  second is not None
                  and second["shell"]["options"]["starred"] is True,
                  f"frame={second and second['shell']['options']}")
        finally:
            proc.terminate()
            proc.wait(5)


def mounts_table_checks(check: Checks) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        table = Path(tmp) / "mounts"
        # A root with a space in it, because the kernel octal-escapes the
        # table and the comparison has to be made in its terms.
        root = Path(tmp) / "with space" / "mnt"
        was_root, hosts.MNT_ROOT = hosts.MNT_ROOT, root
        was_table, hosts.MOUNTS_TABLE = hosts.MOUNTS_TABLE, str(table)
        try:
            escaped = str(root / "web-1").replace(" ", "\\040")
            table.write_text(f"web-1: {escaped} fuse.sshfs rw 0 0\n"
                             "tmpfs /tmp tmpfs rw 0 0\n")
            check("a mount in the table is mounted, spaces escaped the "
                  "kernel's way", hosts.is_mounted("web-1"))
            check("one not in it is not", not hosts.is_mounted("web-2"))
            check("local is always mounted -- its files are already here",
                  hosts.is_mounted("local"))
            table.unlink()
            check("an unreadable table reads as not mounted",
                  not hosts.is_mounted("web-1"))
        finally:
            hosts.MNT_ROOT = was_root
            hosts.MOUNTS_TABLE = was_table


def mount_action_checks(check: Checks) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "bin" / "ssh-mount"
        fake.parent.mkdir()
        was_path = os.environ["PATH"]
        os.environ["PATH"] = f"{fake.parent}:{was_path}"

        def script(body: str) -> None:
            fake.write_text("#!/bin/bash\n" + body)
            fake.chmod(0o755)

        def raised(call) -> str:
            try:
                call()
            except hosts.HostError as exc:
                return str(exc)
            return ""

        try:
            script("echo mounted\n")
            check("a clean mount comes back as its path",
                  hosts.mount("web-1") == str(hosts.MNT_ROOT / "web-1"))
            script("echo noise >&2\n"
                   "echo 'could not mount web-1: connection refused' >&2\n"
                   "exit 1\n")
            said = raised(lambda: hosts.mount("web-1"))
            check("a refused mount surfaces the last line, readable",
                  said == "could not mount web-1: connection refused",
                  f"said={said!r}")
            script("exit 1\n")
            check("and one that failed silently still says something",
                  raised(lambda: hosts.mount("web-1")) == "sshfs failed")

            script("echo 'could not unmount: busy' >&2\nexit 1\n")
            check("a refused unmount comes back the same way",
                  raised(lambda: hosts.unmount("web-1"))
                  == "could not unmount: busy")
            script("exit 0\n")
            check("a clean unmount raises nothing",
                  hosts.unmount("web-1") is None)

            check("local mounts to the home directory",
                  hosts.mount("local") == str(Path.home()))
            check("and refuses to unmount this machine",
                  "this machine" in raised(lambda: hosts.unmount("local")))
        finally:
            os.environ["PATH"] = was_path


def ssh_mount_script_checks(check: Checks) -> None:
    """bin/ssh-mount itself, against a fake sshfs and mountpoint.

    The guards under test: sshfs exiting non-zero, and sshfs exiting 0 with
    nothing actually mounted. Both must fail out loud and take the just-made
    mountpoint with them, or the leftover directory reads as a mount that
    worked. The listed()-true branches -- already mounted, wedged by a dead
    sshfs -- need a real kernel mount and are left to use.
    """
    script = ROOT / "bin" / "ssh-mount"
    with tempfile.TemporaryDirectory() as tmp:
        fakebin = Path(tmp) / "bin"
        fakebin.mkdir()
        runtime = Path(tmp) / "run"
        runtime.mkdir()
        (fakebin / "sshfs").write_text(
            "#!/bin/bash\n"
            'if [ "$FAKE_SSHFS" = fail ]; then\n'
            '  echo "read: Connection refused" >&2\n'
            "  exit 1\n"
            "fi\n"
            "exit 0\n")
        (fakebin / "mountpoint").write_text(
            "#!/bin/bash\n"
            '[ "$FAKE_MOUNTED" = yes ]\n')
        for tool in fakebin.iterdir():
            tool.chmod(0o755)
        env = dict(os.environ, PATH=f"{fakebin}:{os.environ['PATH']}",
                   XDG_RUNTIME_DIR=str(runtime))
        mnt = runtime / "conn" / "mnt" / "web-1"

        def run(*args: str, **fakes: str):
            return subprocess.run(["bash", str(script), *args],
                                  env=dict(env, **fakes), capture_output=True,
                                  text=True, timeout=30)

        done = run("web-1", FAKE_SSHFS="fail", FAKE_MOUNTED="no")
        check("a refused sshfs fails out loud",
              done.returncode == 1 and "could not mount web-1" in done.stderr,
              f"rc={done.returncode} err={done.stderr!r}")
        check("and takes its empty mountpoint with it", not mnt.exists())

        done = run("web-1", FAKE_SSHFS="ok", FAKE_MOUNTED="no")
        check("sshfs exiting 0 with nothing mounted is still a failure",
              done.returncode == 1 and "nothing is mounted" in done.stderr,
              f"rc={done.returncode} err={done.stderr!r}")
        check("and leaves no mountpoint either", not mnt.exists())

        done = run("web-1", FAKE_SSHFS="ok", FAKE_MOUNTED="yes")
        check("a mount that verified reports where it is",
              done.returncode == 0 and str(mnt) in done.stdout,
              f"rc={done.returncode} out={done.stdout!r}")
        check("and the mountpoint stands", mnt.exists())

        done = run("-u", "web-1", FAKE_MOUNTED="no")
        check("unmounting what is not mounted says so and succeeds",
              done.returncode == 0 and "not mounted" in done.stdout,
              f"rc={done.returncode} out={done.stdout!r}")
        check("and tidies the leftover directory away", not mnt.exists())


def load_gui():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        gi.require_version("Vte", "3.91")
        import gui
        return gui
    except (ImportError, ValueError):
        return None


def notification_checks(check: Checks, gui) -> None:
    """The builder half of the notification round trip.

    Gio.Notification has no getters, so the builder runs with its setters
    observed. What matters most is the target it writes: the click only works
    because notification() and open_from_notification() both go through
    session_ref/split_ref, and the target here is checked against that seam.
    The click half -- the target landing back in a window -- needs a display
    and lives in test_gui.py.
    """
    import agent_state
    from gi.repository import Gio

    check("a ref comes back apart, host first",
          gui.split_ref(gui.session_ref("web-1", "deploy"))
          == ("web-1", "deploy"))
    check("however awkward the names",
          gui.split_ref(gui.session_ref("db.stage-2", "run_4"))
          == ("db.stage-2", "run_4"))
    check("what reaches the desktop is printable and bounded",
          gui.notify_text("a\x07b\x9bc" + "d" * 200) == "abc" + "d" * 117)

    built = {}
    watched = ("set_priority", "set_icon", "set_default_action_and_target")
    originals = {name: getattr(Gio.Notification, name) for name in watched}

    def observe(name):
        def observed(self, *args):
            built[name] = args
            return originals[name](self, *args)
        return observed

    for name in watched:
        setattr(Gio.Notification, name, observe(name))
    try:
        waiting = {"name": "deploy",
                   "agent": {"state": agent_state.NEEDS_YOU,
                             "label": "needs you",
                             "detail": "Do you want to?"}}
        gui.notification("web-1", waiting)
        action, target = built["set_default_action_and_target"]
        check("a click is aimed at the open-session action",
              action == "app.open-session", f"action={action}")
        check("and its target is the ref both sides share",
              target.get_string() == gui.session_ref("web-1", "deploy"),
              f"target={target.get_string()!r}")
        check("a session waiting on you interrupts",
              built["set_priority"][0] == Gio.NotificationPriority.HIGH)
        check("under the app's own icon",
              gui.APP_ID in built["set_icon"][0].get_names(),
              f"icon={built['set_icon'][0]}")
        gui.notification("web-1", dict(
            waiting, agent=dict(waiting["agent"],
                                state=agent_state.WORKING,
                                label="working", detail="")))
        check("and anything else does not",
              built["set_priority"][0] == Gio.NotificationPriority.NORMAL)
    finally:
        for name, orig in originals.items():
            setattr(Gio.Notification, name, orig)

    # The receiving action must take exactly the type the builder sends, or
    # the click dies inside GLib with nothing on screen to say so.
    app = gui.ConnApp()
    action = app.lookup_action("open-session")
    check("the app's action takes the type the builder sends",
          action is not None
          and action.get_parameter_type().equal(target.get_type()))


def stream_checks(check: Checks, gui) -> None:
    """The reader that feeds parse_frame, against a pipe of our own.

    ssh delivers the watch stream in arbitrary chunks, so a marker can arrive
    split across two reads; the reader's line buffer is what puts it back
    together. The fake proc here is a real pipe with the payload written in
    deliberately awkward pieces.
    """
    payload = (f"@@@{TOKEN}:SESSION:deploy\n"
               "line one\n"
               "@@@SESSION:forged\n"
               f"@@@{TOKEN}:PANES:deploy\n"
               "claude\n"
               f"###{TOKEN}:FRAME\n"
               f"###{TOKEN}:ALIVE\n").encode()
    read_end, write_end = os.pipe()
    cut = payload.find(b":FRAME")            # split inside a marker line
    for chunk in (payload[:9], payload[9:cut], payload[cut:]):
        os.write(write_end, chunk)

    class Proc:
        def __init__(self):
            self.stdout = os.fdopen(read_end, "rb", buffering=0)
            self.boundary = TOKEN
            self.frame = f"###{TOKEN}:FRAME"
            self.alive = f"###{TOKEN}:ALIVE"

        def terminate(self):
            pass

    class Panel:
        stopping = False

    stub = Panel()
    stub.watchers = {}
    stub.watch_lock = threading.Lock()
    stub.frames = queue.Queue()
    halt, poke = threading.Event(), threading.Event()
    spawned = []

    def fake_watch(host, interval):
        spawned.append(host)
        return Proc()

    was, hosts.watch_screens = hosts.watch_screens, fake_watch
    reader = threading.Thread(target=gui.Conn._stream,
                              args=(stub, "web-1", halt, poke), daemon=True)
    try:
        reader.start()
        try:
            frame = stub.frames.get(timeout=10)
        except queue.Empty:
            frame = None
    finally:
        halt.set()
        os.close(write_end)
        reader.join(timeout=10)
        hosts.watch_screens = was

    check("the reader hands parse_frame a whole frame", frame is not None
          and frame[0] == "web-1" and set(frame[1]) == {"deploy"},
          f"frame={frame}")
    if frame is not None:
        check("with markers split across reads put back together",
              frame[1]["deploy"]["screen"] == "line one\n@@@SESSION:forged"
              and frame[1]["deploy"]["commands"] == ["claude"],
              f"deploy={frame[1]['deploy']}")
    check("a heartbeat is not a frame", stub.frames.empty())
    check("and one connection was enough", spawned == ["web-1"],
          f"spawned={spawned}")
    check("the reader lets go of the stream when told",
          not reader.is_alive() and stub.watchers == {})


def drain_checks(check: Checks, gui) -> None:
    """drain_frames' two rules: same sessions reclassify in place, a changed
    set is a real probe -- the stream only carries screens."""
    import agent_state

    probed: list[str] = []
    landed = threading.Event()

    class Panel:
        stopping = False

        def render(self):
            self.rendered = True

        def _probe(self, host):
            probed.append(host)
            landed.set()

    stub = Panel()
    stub.rendered = False
    stub.frames = queue.Queue()
    stub.inflight = set()
    stub.streamed = {}
    stub.heard = {}
    stub.rows = {"web-1": {"sessions": [
        {"name": "deploy", "screen": "",
         "agent": {"state": agent_state.UNKNOWN, "label": "unknown",
                   "detail": ""}}]}}

    idle = (f"✻ Cooked for 12s\n{RULE}\n❯ Try \"fix the bug\"\n{RULE}\n"
            "  ⏵⏵ auto mode on")
    stub.frames.put(("web-1", {"deploy": {"screen": idle, "raw": "",
                                          "commands": ["claude"]}}))
    stub.frames.put(("gone-host", {}))      # a host the panel no longer lists
    gui.Conn.drain_frames(stub)
    session = stub.rows["web-1"]["sessions"][0]
    check("a frame over the same sessions reclassifies in place",
          session["agent"]["state"] == agent_state.READY
          and session["screen"] == idle,
          f"agent={session['agent']}")
    check("renders what changed", stub.rendered
          and stub.streamed.get("web-1", 0) > 0)
    check("and notes the wall-clock time it was heard from, for last seen",
          abs(stub.heard.get("web-1", 0) - time.time()) < 60, str(stub.heard))
    check("and probes nothing", probed == [], f"probed={probed}")

    stub.rendered = False
    stub.frames.put(("web-1", {"deploy": {"screen": idle, "raw": "",
                                          "commands": ["claude"]},
                               "fresh": {"screen": "", "raw": "",
                                         "commands": ["bash"]}}))
    gui.Conn.drain_frames(stub)
    check("a session appearing needs the probe, not the stream",
          landed.wait(5) and probed == ["web-1"]
          and "web-1" in stub.inflight, f"probed={probed}")
    check("so the rows wait for it", not stub.rendered
          and len(stub.rows["web-1"]["sessions"]) == 1)

    probed.clear()
    stub.frames.put(("web-1", {}))          # every session gone, probe busy
    gui.Conn.drain_frames(stub)
    check("but never two probes at once", probed == [], f"probed={probed}")


# ---------------------------------------------------------------------------
# Editing ~/.ssh/config: host_config, host_line_aliases, update_host,
# rename_host. Every edit is made to a config of our own -- hosts.SSH_CONFIG is
# pointed at a temp file and put back afterwards -- so nothing of yours is read
# or rewritten, and the suite never runs ssh itself.
# ---------------------------------------------------------------------------

# A config that carries every shape the editors have to survive: a plain block
# with all four directives, a block sharing its Host line with a second alias,
# a comment banner, a ProxyCommand, an Include and a Match block that must come
# through untouched, and the Host * defaults that must not fold into any block.
RICH = """\
# my servers

Host web-1
    HostName 10.0.0.1
    User deploy
    Port 2222
    # note about this host
    ProxyCommand corkscrew proxy 8080 %h %p

Host db shared-db
    HostName 10.0.0.2

Include ~/.ssh/extra

Match host bastion
    ForwardAgent yes

Host *
    ServerAliveInterval 30
    IdentityFile ~/.ssh/id_ed25519
"""

# The fake keyring: a secret-tool on PATH backed by a JSON file, so
# migrate_secrets is exercised end to end without ever touching the real
# keyring. store/lookup/clear/search are the four verbs hosts.py speaks.
FAKE_SECRET_TOOL = r'''#!/usr/bin/env python3
import json, os, sys
manifest = os.path.join(os.environ["FAKE_KEYRING"], "manifest.json")
args = sys.argv[1:]
cmd = args[0] if args else ""

def val(key):
    for i, a in enumerate(args):
        if a == key and i + 1 < len(args):
            return args[i + 1]
    return None

try:
    with open(manifest) as f:
        items = json.load(f)
except (OSError, ValueError):
    items = []

def save(rows):
    with open(manifest, "w") as f:
        json.dump(rows, f)

host, name = val("host"), val("name")
if cmd == "store":
    data = sys.stdin.read()
    items = [r for r in items if not (r["host"] == host and r["name"] == name)]
    items.append({"host": host, "name": name, "value": data})
    save(items)
elif cmd == "lookup":
    for r in items:
        if r["host"] == host and r["name"] == name:
            sys.stdout.write(r["value"])
            sys.exit(0)
    sys.exit(1)
elif cmd == "clear":
    save([r for r in items if not (r["host"] == host and r["name"] == name)])
elif cmd == "search":
    for r in items:
        if r["host"] == host:
            sys.stderr.write("attribute.name = %s\n" % r["name"])
sys.exit(0)
'''


@contextlib.contextmanager
def temp_config(text: str = ""):
    """Point hosts.SSH_CONFIG at a temp file holding `text`, and restore it."""
    was = hosts.SSH_CONFIG
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config"
        path.write_text(text)
        hosts.SSH_CONFIG = path
        try:
            yield path
        finally:
            hosts.SSH_CONFIG = was


def _raised(call) -> str:
    """The message a HostError carried, or "" if the call did not raise one."""
    try:
        call()
    except hosts.HostError as exc:
        return str(exc)
    return ""


def structurally_sound(text: str) -> bool:
    """A stand-in for "ssh can still parse it", without running ssh: every Host
    or Match header opens at the left margin, and no directive floats indented
    above the first block. Catches an edit that unindented a header or let a
    line escape its block -- the ways a rewrite breaks the file's structure."""
    seen_header = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indented = line[:1] in (" ", "\t")
        first = stripped.split()[0].lower()
        if first in ("host", "match"):
            if indented:
                return False
            seen_header = True
        elif indented and not seen_header:
            return False
    return True


def config_read_checks(check: Checks) -> None:
    with temp_config(RICH):
        web = hosts.host_config("web-1")
        check("a block's own directives come back as written",
              web == {"hostname": "10.0.0.1", "user": "deploy",
                      "port": "2222", "identityfile": "", "proxyjump": ""},
              f"got={web}")
        check("the Host * IdentityFile is not folded into a block that has "
              "none of its own", web["identityfile"] == "")
        db = hosts.host_config("shared-db")
        check("a shared Host line reads the same block from either alias",
              db == hosts.host_config("db")
              and db["hostname"] == "10.0.0.2", f"got={db}")
        check("a name the file does not carry is refused",
              _raised(lambda: hosts.host_config("ghost")))
        check("and so is the wildcard, which config_hosts never lists",
              _raised(lambda: hosts.host_config("*")))

        check("the other aliases on a shared line are surfaced",
              hosts.host_line_aliases("db") == ["shared-db"]
              and hosts.host_line_aliases("shared-db") == ["db"])
        check("a block that stands alone shares its line with nobody",
              hosts.host_line_aliases("web-1") == [])
        check("aliases of a name not present is refused",
              _raised(lambda: hosts.host_line_aliases("ghost")))

    # A probe that reaches ssh but never opens the session: three problems,
    # three states -- and a new server's untrusted key is its own first-contact
    # state, not a dead "down".
    state, why = hosts._probe_failure("Host key verification failed.", "box")
    check("an untrusted host key is 'unverified', not 'down'",
          state == "unverified" and "log in" in why, f"state={state} why={why!r}")
    state, _ = hosts._probe_failure("Permission denied (publickey).", "box")
    check("being reached and refused is 'nokey'", state == "nokey")
    state, _ = hosts._probe_failure("Permission denied (publickey).", "local")
    check("but a refusal from this machine is never a key problem",
          state == "down")
    state, _ = hosts._probe_failure("ssh: could not resolve hostname box", "box")
    check("and an unreachable box is plainly 'down'", state == "down")


def update_host_checks(check: Checks) -> None:
    with temp_config(RICH) as path:
        before = path.read_text()
        backup = hosts.update_host("web-1", {"hostname": "198.51.100.5",
                                             "user": "", "port": "",
                                             "identityfile": "~/.ssh/web1_key"})
        text = path.read_text()
        check("a replace, an insert and two removes land together",
              hosts.host_config("web-1") == {"hostname": "198.51.100.5",
                                             "user": "", "port": "",
                                             "identityfile": "~/.ssh/web1_key",
                                             "proxyjump": ""},
              f"got={hosts.host_config('web-1')}")
        check("the replaced directive keeps its indent",
              "\n    HostName 198.51.100.5\n" in text)
        check("the inserted directive is written like add_host would",
              "\n    IdentityFile ~/.ssh/web1_key\n" in text)
        check("the removed directives are gone",
              "User deploy" not in text and "Port 2222" not in text)
        check("comments and unknown directives in the block survive",
              "# note about this host" in text
              and "    ProxyCommand corkscrew proxy 8080 %h %p" in text)
        check("the Include and the Match block are untouched",
              "Include ~/.ssh/extra" in text
              and "Match host bastion" in text
              and "    ForwardAgent yes" in text)
        check("the Host * defaults are untouched",
              "Host *" in text and "    ServerAliveInterval 30" in text
              and "    IdentityFile ~/.ssh/id_ed25519" in text)
        check("the file still parses as ssh config",
              structurally_sound(text))
        check("every alias is still named",
              hosts.config_hosts() == ["web-1", "db", "shared-db"],
              f"got={hosts.config_hosts()}")
        check("the backup holds what was there before, and the file stays 0600",
              backup.read_text() == before
              and (path.stat().st_mode & 0o777) == 0o600)

    # Insert vs replace, in isolation.
    with temp_config("Host m\n    HostName x\n") as path:
        hosts.update_host("m", {"user": "bob"})
        check("setting a directive the block lacks inserts it",
              hosts.host_config("m")["user"] == "bob"
              and path.read_text().count("    User ") == 1)
        hosts.update_host("m", {"user": "carol"})
        check("setting one it already has replaces in place, not twice",
              hosts.host_config("m")["user"] == "carol"
              and path.read_text().count("    User ") == 1,
              f"text={path.read_text()!r}")

    # Several inserts at once arrive in a fixed order, under the Host line.
    with temp_config("Host m\n    HostName x\n") as path:
        hosts.update_host("m", {"port": "2200", "user": "bob",
                               "identityfile": "~/.ssh/mkey"})
        check("new directives are inserted in a stable order under the header",
              "Host m\n    User bob\n    Port 2200\n"
              "    IdentityFile ~/.ssh/mkey\n    HostName x\n"
              in path.read_text(), f"text={path.read_text()!r}")

    # Removing a directive with an empty value.
    with temp_config("Host m\n    HostName x\n    User bob\n") as path:
        hosts.update_host("m", {"user": ""})
        check("an empty value removes the directive",
              hosts.host_config("m")["user"] == ""
              and "User" not in path.read_text())

    # A hand-authored block can repeat a directive; ssh takes the first, so an
    # edit that touched only the first would leave a stale copy in charge.
    with temp_config("Host m\n    HostName x\n    HostName y\n    User bob\n") as path:
        hosts.update_host("m", {"hostname": ""})
        check("clearing a duplicated directive removes every copy",
              "HostName" not in path.read_text()
              and "User bob" in path.read_text(),
              f"text={path.read_text()!r}")
    with temp_config("Host m\n    IdentityFile ~/a\n    IdentityFile ~/b\n") as path:
        hosts.update_host("m", {"identityfile": "~/c"})
        check("setting a duplicated directive collapses it to one",
              path.read_text().count("IdentityFile") == 1
              and hosts.host_config("m")["identityfile"] == "~/c",
              f"text={path.read_text()!r}")

    # Port 22 and empty both mean "drop the Port line".
    with temp_config("Host m\n    HostName x\n    Port 2222\n") as path:
        hosts.update_host("m", {"port": "22"})
        check("Port 22 is the default and is removed, not written",
              "Port" not in path.read_text())
        check("removing a Port that is already absent is a quiet no-op",
              _raised(lambda: hosts.update_host("m", {"port": "22"})) == ""
              and "Port" not in path.read_text())

    # Editing through a shared alias edits the one shared block.
    with temp_config("Host db shared-db\n    HostName 10.0.0.2\n") as path:
        hosts.update_host("shared-db", {"user": "admin"})
        check("an edit through one alias reaches the block both share",
              hosts.host_config("db")["user"] == "admin"
              and hosts.host_config("shared-db")["user"] == "admin")

    # Bad values are refused before the file is opened.
    with temp_config(RICH) as path:
        before = path.read_text()
        for bak in path.parent.glob("config.bak.*"):
            bak.unlink()
        bad = {"a space in the host": {"hostname": "bad host"},
               "a hostname leading with a dash": {"hostname": "-nope"},
               "a shell metacharacter in the user": {"user": "we;rm"},
               "a non-numeric port": {"port": "22a"},
               "a newline in the identity file": {
                   "identityfile": "~/.ssh/k\nProxyCommand evil"},
               "a space in the jump": {"proxyjump": "db ProxyCommand evil"},
               "a jump leading with a dash": {"proxyjump": "-oProxyCommand=x"},
               "a jump through itself": {"proxyjump": "web-1"},
               "an empty hop in a chain": {"proxyjump": "db,,x"}}
        refused = all(_raised(lambda f=f: hosts.update_host("web-1", f))
                      for f in bad.values())
        check("every unsafe value is refused with HostError",
              refused, f"labels={list(bad)}")
        check("a refused edit leaves the file and its backups alone",
              path.read_text() == before
              and "ProxyCommand evil" not in path.read_text()
              and list(path.parent.glob("config.bak.*")) == [])
        check("editing a host that is not there is refused",
              _raised(lambda: hosts.update_host("ghost", {"user": "x"})))


def proxyjump_checks(check: Checks) -> None:
    with temp_config(RICH) as path:
        hosts.update_host("db", {"proxyjump": "web-1"})
        text = path.read_text()
        check("connecting through another server writes ProxyJump",
              hosts.host_config("db")["proxyjump"] == "web-1"
              and "Host db shared-db\n    ProxyJump web-1\n" in text,
              f"text={text!r}")
        hosts.update_host("db", {"proxyjump": "web-1"})
        check("saving the same jump again does not repeat the line",
              path.read_text().count("ProxyJump") == 1)
        hosts.update_host("db", {"proxyjump": "ops@gw.example:2200,web-1"})
        check("a hand-written chain of hops is accepted as is",
              hosts.host_config("db")["proxyjump"]
              == "ops@gw.example:2200,web-1")
        hosts.update_host("db", {"proxyjump": ""})
        check("Direct takes the ProxyJump line out again",
              "ProxyJump" not in path.read_text()
              and hosts.host_config("db")["hostname"] == "10.0.0.2")
        hosts.update_host("db", {"proxyjump": "none"})
        check("'none' is kept, to override a Host * jump",
              hosts.host_config("db")["proxyjump"] == "none")

    with temp_config("Host bastion\n    HostName 192.0.2.1\n\n"
                     "Host locked\n    HostName 192.0.2.2\n"
                     "    ProxyJump bastion\n\n"
                     "Host chained\n    HostName 192.0.2.3\n"
                     "    ProxyJump ops@gw:22,bastion\n\n"
                     "Host other\n    HostName 192.0.2.4\n"
                     "    ProxyJump bastions\n") as path:
        hosts.rename_host("bastion", "mini")
        text = path.read_text()
        check("renaming a jump host carries the servers behind it along",
              hosts.host_config("locked")["proxyjump"] == "mini"
              and hosts.host_config("chained")["proxyjump"] == "ops@gw:22,mini",
              f"text={text!r}")
        check("but a jump that only starts with the old name is left alone",
              hosts.host_config("other")["proxyjump"] == "bastions")

    with temp_config("Host bastion\n    HostName 192.0.2.1\n\n"
                     "Host * !bastion\n    ProxyJump bastion\n\n"
                     "Host *\n    ServerAliveInterval 30\n") as path:
        hosts.rename_host("bastion", "mini")
        text = path.read_text()
        check("renaming the default jump host keeps it out of its own default",
              "Host * !mini\n    ProxyJump mini\n" in text
              and "!bastion" not in text, f"text={text!r}")
        hosts.add_host("fresh", "192.0.2.7", "")
        text = path.read_text()
        check("a new server goes above the default jump, so it can override it",
              text.index("Host fresh") < text.index("Host * !mini"),
              f"text={text!r}")


DEFAULTS = """\
Host bastion
    HostName 192.0.2.1

Host home
    HostName 192.0.2.2

Host web
    HostName 192.0.2.3
    ProxyJump none

# ---- defaults, keep last ----
Host *
    ServerAliveInterval 30
"""


def default_route_checks(check: Checks) -> None:
    with temp_config(DEFAULTS) as path:
        check("no wildcard jump reads as direct", hosts.default_jump() == "")
        hosts.set_default_jump("bastion")
        text = path.read_text()
        check("a default route is one Host * !via block",
              hosts.default_jump() == "bastion"
              and "Host * !bastion\n" in text
              and "    ProxyJump bastion\n" in text, f"text={text!r}")
        check("and it sits above the defaults and their banner",
              text.index("Host * !bastion")
              < text.index("# ---- defaults")
              < text.index("Host *\n"), f"text={text!r}")
        check("a server's own block still comes first, so it can override",
              text.index("Host web") < text.index("Host * !bastion"))
        hosts.set_default_jump("home")
        text = path.read_text()
        check("switching moves the one block, never adds a second",
              hosts.default_jump() == "home" and text.count("ProxyJump home") == 1
              and "!bastion" not in text and "ProxyJump bastion" not in text,
              f"text={text!r}")
        hosts.set_default_jump("")
        check("Direct takes it out and leaves the file as it was",
              path.read_text() == DEFAULTS, f"text={path.read_text()!r}")
        check("a server that is not in the file is refused",
              _raised(lambda: hosts.set_default_jump("ghost")))
        hosts.update_host("web", {"user": "x"})
        check("and a server's own ProxyJump none is never touched by it",
              hosts.host_config("web")["proxyjump"] == "none")

    hand = DEFAULTS.replace("    ServerAliveInterval 30\n",
                            "    ServerAliveInterval 30\n    ProxyJump home\n")
    with temp_config(hand) as path:
        check("a jump written into the plain Host * defaults is read too",
              hosts.default_jump() == "home")
        hosts.set_default_jump("bastion")
        text = path.read_text()
        check("and replaced by conn's block, the other defaults kept",
              hosts.default_jump() == "bastion"
              and "ProxyJump home" not in text
              and "    ServerAliveInterval 30" in text, f"text={text!r}")


def rename_host_checks(check: Checks) -> None:
    with temp_config("Host db shared-db\n    HostName 10.0.0.2\n"
                     "    User admin\n\nHost web-1\n"
                     "    HostName 10.0.0.1\n") as path:
        backup = hosts.rename_host("shared-db", "replica")
        text = path.read_text()
        check("the old token is swapped and the other alias stays",
              "Host db replica\n" in text and "shared-db" not in text)
        check("the block's directives are left untouched",
              "    HostName 10.0.0.2" in text and "    User admin" in text)
        check("the config now names the new alias, not the old",
              "replica" in hosts.config_hosts()
              and "shared-db" not in hosts.config_hosts())
        check("the shared block is still reachable from the new name",
              hosts.host_line_aliases("replica") == ["db"])
        check("it still parses, and the backup and mode are as for any edit",
              structurally_sound(text) and backup.read_text()
              and (path.stat().st_mode & 0o777) == 0o600)

        check("renaming onto a name already in the config is refused",
              _raised(lambda: hosts.rename_host("db", "web-1")))
        check("renaming onto the reserved 'local' is refused too",
              _raised(lambda: hosts.rename_host("db", "local")))
        check("an invalid new name is refused",
              _raised(lambda: hosts.rename_host("db", "-bad"))
              and _raised(lambda: hosts.rename_host("db", "two words")))
        check("renaming a host that is not there is refused",
              _raised(lambda: hosts.rename_host("ghost", "fresh")))
        check("a refused rename changed nothing",
              path.read_text() == text)


def migrate_secrets_checks(check: Checks) -> None:
    """migrate_secrets end to end against a fake secret-tool on PATH -- the one
    function that talks to the keyring, never let near the real one."""
    with tempfile.TemporaryDirectory() as tmp:
        fakebin = Path(tmp) / "bin"
        fakebin.mkdir()
        store = Path(tmp) / "store"
        store.mkdir()
        tool = fakebin / "secret-tool"
        tool.write_text(FAKE_SECRET_TOOL)
        tool.chmod(0o755)
        was_path = os.environ["PATH"]
        was_kr = os.environ.get("FAKE_KEYRING")
        os.environ["PATH"] = f"{fakebin}:{was_path}"
        os.environ["FAKE_KEYRING"] = str(store)
        try:
            hosts.secret_store("old-box", "password", "s3cr3t")
            hosts.secret_store("old-box", "api-token", "tok-42")
            check("the fake keyring files and lists what it is given",
                  hosts.secret_names("old-box") == ["api-token", "password"],
                  f"got={hosts.secret_names('old-box')}")
            hosts.migrate_secrets("old-box", "new-box")
            check("migrate re-files every secret under the new host",
                  hosts.secret_names("new-box") == ["api-token", "password"]
                  and hosts.secret_value("new-box", "password") == "s3cr3t"
                  and hosts.secret_value("new-box", "api-token") == "tok-42",
                  f"got={hosts.secret_names('new-box')}")
            check("and clears them from the old host",
                  hosts.secret_names("old-box") == [])
            check("a host with nothing filed migrates without error",
                  _raised(lambda: hosts.migrate_secrets("empty", "other")) == ""
                  and hosts.secret_names("other") == [])
        finally:
            os.environ["PATH"] = was_path
            if was_kr is None:
                os.environ.pop("FAKE_KEYRING", None)
            else:
                os.environ["FAKE_KEYRING"] = was_kr


def host_key_checks(check: Checks) -> None:
    """A changed host key told apart from a new one, and the two terminals
    that fix them -- run here against a fake ssh and ssh-keygen on PATH, so
    nothing reaches a network and no known_hosts of yours is read or
    rewritten."""
    new = ("No ED25519 host key is known for box and you have requested "
           "strict checking.\nHost key verification failed.")
    changed = ("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
               "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n"
               "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
               "Offending ECDSA key in /home/u/.ssh/known_hosts:8\n"
               "Host key for box has changed and you have requested strict "
               "checking.\nHost key verification failed.")
    check("a new key is not a changed one", not hosts.key_changed(new))
    check("a changed key is told apart by ssh's warning",
          hosts.key_changed(changed))
    check("and by its strict-checking line alone, banner or not",
          hosts.key_changed("Host key for box has changed and you have "
                            "requested strict checking."))
    state, why = hosts._probe_failure("Host key verification failed.", "box",
                                      changed)
    check("a changed key stays 'unverified' but says it changed",
          state == "unverified" and "changed" in why and "log in" not in why,
          f"state={state} why={why!r}")
    state, why = hosts._probe_failure("Host key verification failed.", "box",
                                      new)
    check("a new key keeps the first-contact advice",
          state == "unverified" and "log in" in why)
    check("a fresh row knows of no changed key", hosts.blank("x")["keychanged"]
          is False)

    launched: list = []
    real = hosts.launch
    hosts.launch = lambda argv, **_k: launched.append(argv)
    try:
        hosts.trust_key("box; rm -rf ~")
        hosts.check_new_key("box; rm -rf ~")
        refused = [_raised(lambda: hosts.trust_key("local")),
                   _raised(lambda: hosts.check_new_key("local"))]
    finally:
        hosts.launch = real
    check("both terminals take the alias as an argument, never in the script",
          len(launched) == 2
          and all(argv[-1] == "box; rm -rf ~" and "rm -rf" not in argv[2]
                  for argv in launched), f"launched={launched}")
    check("and neither is offered for this machine", all(refused))

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        bin_dir = base / "bin"
        bin_dir.mkdir()
        log = base / "calls"
        (base / ".ssh").mkdir()
        (bin_dir / "ssh").write_text(
            "#!/bin/bash\n"
            f"echo \"ssh $*\" >> {log}\n"
            'if [ "$1" = -G ]; then\n'
            "  printf 'hostname box.example\\nport 2222\\n"
            "userknownhostsfile ~/.ssh/known_hosts ~/.ssh/known_hosts2\\n'\n"
            "  exit 0\nfi\n"
            'if [ "$1" = -v ]; then\n'
            "  echo 'debug1: Server host key: ssh-ed25519 SHA256:JUMP' >&2\n"
            "  echo 'debug1: Server host key: ssh-ed25519 SHA256:NEW' >&2\n"
            "  exit 255\nfi\n"
            "exit 0\n")
        (bin_dir / "ssh-keygen").write_text(
            "#!/bin/bash\n"
            f"echo \"ssh-keygen $*\" >> {log}\n"
            'if [ "$1" = -l ]; then\n'
            '  echo "# Host $3 found: line 1"\n'
            '  echo "$3 ED25519 SHA256:OLD"\nfi\n')
        for tool in ("ssh", "ssh-keygen"):
            (bin_dir / tool).chmod(0o755)
        env = dict(os.environ, HOME=str(base),
                   PATH=f"{bin_dir}:{os.environ['PATH']}")

        def run(answer: str) -> tuple[str, list[str]]:
            log.write_text("")
            done = subprocess.run(
                ["bash", "-c", hosts.CHECK_KEY_SCRIPT, "conn-check-key", "box"],
                input=answer, capture_output=True, text=True, env=env,
                timeout=20)
            return done.stdout, log.read_text().splitlines()

        out, calls = run("n\n")
        check("Check new key shows the key on file and the one offered now",
              "SHA256:OLD" in out and "SHA256:NEW" in out
              and "SHA256:JUMP" not in out, out)
        check("its look at the offered key logs in to nothing and pins nothing",
              any("-o BatchMode=yes" in c and "UserKnownHostsFile=/dev/null"
                  in c for c in calls), f"calls={calls}")
        check("a no leaves known_hosts alone",
              not any("-R" in c for c in calls)
              and not any("accept-new" in c for c in calls)
              and "Left as it was" in out, f"calls={calls}")
        out, calls = run("y\n")
        known = f"{base}/.ssh/known_hosts"
        removed = [c for c in calls if c.startswith("ssh-keygen -R")]
        check("a yes removes only the [host]:port entry, never the bare "
              "name another server on port 22 owns",
              removed == [f"ssh-keygen -R [box.example]:2222 -f {known}"],
              f"removed={removed}")
        check("and lists only that entry as the one on file",
              not any(c == f"ssh-keygen -l -F box.example -f {known}"
                      for c in calls), f"calls={calls}")
        check("then connects once with accept-new, after the removal",
              calls and "StrictHostKeyChecking=accept-new" in calls[-1]
              and calls.index(removed[-1]) < len(calls) - 1,
              f"calls={calls}")
        log.write_text("")
        subprocess.run(["bash", "-c", hosts.TRUST_SCRIPT, "conn-trust", "box"],
                       input="", capture_output=True, text=True, env=env,
                       timeout=20)
        calls = log.read_text().splitlines()
        check("Trust asks ssh to show the fingerprint prompt, nothing else",
              calls == ["ssh -o StrictHostKeyChecking=ask -o ControlPath=none "
                        "-- box true"], f"calls={calls}")


def main() -> int:
    check = Checks()
    frame_checks(check)
    capture_checks(check)
    meta_frame_checks(check)
    probe_parse_checks(check)
    set_option_checks(check)
    live_tmux_checks(check)
    mounts_table_checks(check)
    mount_action_checks(check)
    ssh_mount_script_checks(check)
    config_read_checks(check)
    update_host_checks(check)
    proxyjump_checks(check)
    default_route_checks(check)
    rename_host_checks(check)
    migrate_secrets_checks(check)
    host_key_checks(check)

    gui = load_gui()
    if gui is None:
        print("skipped: notification and stream checks need the GTK stack")
    else:
        notification_checks(check, gui)
        stream_checks(check, gui)
        drain_checks(check, gui)

    print(f"\n{'all good' if not check.failures else str(check.failures) + ' failed'}")
    return 1 if check.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
