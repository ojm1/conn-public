#!/usr/bin/env python3
"""Set conn-agent up on the Mac mini, from the laptop. Run each step on its
own; every step that changes something shows what it will do and does it
only with --apply.

  agent_setup.py plan                  what every step below would do
  agent_setup.py key                   make ~/.ssh/conn_agent on the mini
  agent_setup.py authorize [--apply]   add its public key to each server
  agent_setup.py config [--apply]      the mini's ssh config and known_hosts
  agent_setup.py install [--apply]     copy the agent over, start it in launchd

The key is the agent's own, never yours, and each server only accepts it
from the mini's network (from=...), with forwarding of every kind refused. To
take it back: delete the line ending "conn-agent@mini" from a server's
~/.ssh/authorized_keys.
"""

from __future__ import annotations

import functools
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import hosts

MINI = "mini"
# The site's addresses are not in this repo: they live in
# ~/.config/conn/agent.toml on the machine that runs the setup, e.g.
#
#   site_ip = "203.0.113.7"            # the public IP the mini is seen from
#   site_lan = "198.51.100.0/24"       # the mini's LAN
#   [on_lan]                           # servers on that same LAN
#   nas = "198.51.100.50:22"
#
# site_ip and site_lan go into the agent key's from= so the key only
# works from the mini's network. Servers listed under on_lan share that
# network: their public name is the site IP, and the mini reaching its own
# public IP comes back in through the router from a LAN address (or not at
# all), so the mini goes to them on the LAN. Their host key is still checked
# against the laptop's entry for the public name.
SITE = Path.home() / ".config" / "conn" / "agent.toml"
KEY = "~/.ssh/conn_agent"
COMMENT = "conn-agent@mini"
BEGIN, END = "# >>> conn-agent (written by agent_setup.py)", "# <<< conn-agent"
APP = "~/.local/share/conn-agent"
FILES = ("conn_agent.py", "agent_api.py", "hosts.py", "chats.py",
         "agent_state.py", "connlog.py", "notes.py", "starting.py")
LABEL = "co.ojm.conn-agent"
REPO = Path(__file__).resolve().parent


@functools.cache
def site() -> dict:
    """agent.toml, read once. Missing or incomplete stops the setup with
    what to write, rather than putting a key on servers with a from= that
    lets nothing (or everything) in."""
    try:
        data = tomllib.loads(SITE.read_text())
    except FileNotFoundError:
        sys.exit(f"{SITE} is missing: it needs site_ip and site_lan "
                 "(see the top of agent_setup.py)")
    except tomllib.TOMLDecodeError as exc:
        sys.exit(f"{SITE}: {exc}")
    for key in ("site_ip", "site_lan"):
        if not str(data.get(key, "")).strip():
            sys.exit(f"{SITE} has no {key}")
    return data


def options() -> str:
    """The authorized_keys options for the agent key: the mini's network only."""
    where = site()
    return (f'from="{where["site_ip"]},{where["site_lan"]}",'
            "no-agent-forwarding,no-port-forwarding,no-X11-forwarding")


def on_lan() -> dict[str, tuple[str, str]]:
    """{host: (LAN address, port)} for the servers on the mini's LAN."""
    found = {}
    for host, where in (site().get("on_lan") or {}).items():
        address, _, port = str(where).partition(":")
        found[host] = (address, port or "22")
    return found


def servers() -> list[str]:
    """Every server the laptop knows but the mini itself and this laptop."""
    return [h for h in hosts.list_hosts()
            if h != MINI and not hosts.is_local(h)]


def ssh(host: str, script: str, stdin: str | None = None,
        timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh", "-o", "BatchMode=yes",
                           "-o", "ConnectTimeout=10", host, script],
                          input=stdin, capture_output=True, text=True,
                          timeout=timeout)


def public_key() -> str:
    done = ssh(MINI, f"cat {KEY}.pub")
    key = done.stdout.strip()
    if done.returncode or not key.startswith("ssh-ed25519 "):
        raise SystemExit("no agent key on the mini yet: run "
                         "`agent_setup.py key` first")
    return key


def mini_config() -> str:
    """Host blocks for the mini: the same names, resolved by ssh -G here
    (so defaults and Match blocks are applied), with no jump -- the mini is
    on the site IP itself -- and the agent's key only."""
    lines = [BEGIN,
             "# The servers conn-agent watches. Regenerate from the laptop;",
             "# do not edit between the markers.", ""]
    for host in servers():
        info = hosts.resolve(host)
        if not info.get("hostname"):
            continue
        lines.append(f"Host {host}")
        port = info.get("port") or "22"
        if host in on_lan():
            lan, lan_port = on_lan()[host]
            alias = (info["hostname"] if port == "22"
                     else f"[{info['hostname']}]:{port}")
            lines.append(f"    HostName {lan}")
            lines.append(f"    HostKeyAlias {alias}")
            port = lan_port
        else:
            lines.append(f"    HostName {info['hostname']}")
        if info.get("user"):
            lines.append(f"    User {info['user']}")
        if port != "22":
            lines.append(f"    Port {port}")
        lines.append("")
    lines += ["Host " + " ".join(servers()),
              f"    IdentityFile {KEY}",
              "    IdentitiesOnly yes",
              # The servers take this key from the site's IPv4 address
              # only; over IPv6 the mini would arrive from somewhere else.
              "    AddressFamily inet",
              "    ControlMaster auto",
              "    ControlPath ~/.ssh/sockets/%C",
              "    ControlPersist 10m",
              "    ServerAliveInterval 15",
              "    ServerAliveCountMax 3",
              END]
    return "\n".join(lines) + "\n"


def known_hosts() -> str:
    """The laptop's already-trusted host keys for those servers, so the mini
    trusts exactly what you trust here rather than whatever answers."""
    out = []
    for host in servers():
        info = hosts.resolve(host)
        name = info.get("hostname", "")
        if not name:
            continue
        port = info.get("port") or "22"
        lookup = name if port == "22" else f"[{name}]:{port}"
        done = subprocess.run(["ssh-keygen", "-F", lookup],
                              capture_output=True, text=True)
        found = [line for line in done.stdout.splitlines()
                 if line and not line.startswith("#")]
        if not found:
            print(f"  note: no trusted host key here for {host} ({lookup}); "
                  "the agent will see it as down until you log in to it "
                  "from the mini once", file=sys.stderr)
        out += found
    return "\n".join(out) + ("\n" if out else "")


def step_key(apply: bool) -> None:
    script = (f"test -f {KEY} && echo exists || ssh-keygen -q -t ed25519 "
              f"-N '' -C {COMMENT} -f {KEY} && cat {KEY}.pub")
    print(f"on {MINI}: make {KEY} (ed25519, no passphrase: launchd has no "
          "one to type it) unless it exists")
    if apply:
        done = ssh(MINI, script)
        print(done.stdout.strip() or done.stderr.strip())


# Rewritten whole, never appended to: a file whose last key has no newline
# would have the new line glued onto it. An older conn-agent line (a key
# made again, other options) is replaced, everything else kept as it was,
# and the original is kept beside it.
AUTHORIZE = r"""
umask 077
mkdir -p ~/.ssh
f=~/.ssh/authorized_keys
touch "$f"
if grep -qxF "$1" "$f"; then echo already; exit 0; fi
cp -p "$f" "$f.bak-conn-agent" || exit 1
if grep -qF "$2" "$f"; then said=replaced; else said=added; fi
{ grep -vF "$2" "$f"; printf '%s
' "$1"; } > "$f.conn-agent" || exit 1
grep -qxF "$1" "$f.conn-agent" || { rm -f "$f.conn-agent"; exit 1; }
mv "$f.conn-agent" "$f" && echo "$said"
"""


def step_authorize(apply: bool) -> None:
    line = f"{options()} {public_key()}" if apply else f"{options()} <the key>"
    print(f"add to ~/.ssh/authorized_keys on each server, once:\n  {line}\n")
    for host in servers():
        if not apply:
            print(f"  {host}")
            continue
        try:
            done = ssh(host, "sh -c " + shlex.quote(AUTHORIZE) + " sh "
                       + shlex.quote(line) + " " + shlex.quote(COMMENT))
            print(f"  {host:22} {(done.stdout or done.stderr).strip()}")
        except subprocess.TimeoutExpired:
            print(f"  {host:22} no answer")


def step_config(apply: bool) -> None:
    block, keys = mini_config(), known_hosts()
    print(f"on {MINI}, appended to ~/.ssh/config (replacing an earlier "
          "conn-agent block; backed up first):\n")
    print(block)
    print(f"and {len(keys.splitlines())} host keys from this laptop's "
          "known_hosts appended to the mini's.")
    if not apply:
        return
    script = r"""
set -e
umask 077
mkdir -p ~/.ssh/sockets
touch ~/.ssh/config ~/.ssh/known_hosts
cat > ~/.ssh/conn-agent.in
cp ~/.ssh/config ~/.ssh/config.bak-conn-agent-$(date +%Y%m%d-%H%M%S)
awk -v b="$1" -v e="$2" '$0==b{skip=1} !skip{print} $0==e{skip=0}' ~/.ssh/config > ~/.ssh/config.new
awk '/^__KEYS__$/{exit} {print}' ~/.ssh/conn-agent.in >> ~/.ssh/config.new
awk 'f{print} /^__KEYS__$/{f=1}' ~/.ssh/conn-agent.in > ~/.ssh/known_hosts.add
mv ~/.ssh/config.new ~/.ssh/config
sort -u ~/.ssh/known_hosts ~/.ssh/known_hosts.add > ~/.ssh/known_hosts.new
mv ~/.ssh/known_hosts.new ~/.ssh/known_hosts
rm -f ~/.ssh/known_hosts.add ~/.ssh/conn-agent.in
echo done
"""
    command = ("bash -c " + shlex.quote(script) + " conn-agent "
               + shlex.quote(BEGIN) + " " + shlex.quote(END))
    done = ssh(MINI, command, stdin="\n" + block + "__KEYS__\n" + keys)
    print(done.stdout.strip() or done.stderr.strip())


def plist() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- conn-agent: phone alerts for Conn chats. Written by agent_setup.py. -->
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>__PYTHON__</string>
    <string>__HOME__/.local/share/conn-agent/conn_agent.py</string>
    <string>run</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>CONN_AGENT_HOSTS</key><string>__HOSTS__</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>__HOME__/Library/Logs/conn-agent.out</string>
  <key>StandardErrorPath</key><string>__HOME__/Library/Logs/conn-agent.out</string>
</dict>
</plist>
"""


def mini_python() -> str:
    """The mini's python3, if it is 3.11 or newer (tomllib): its absolute
    path, since launchd has no PATH worth the name."""
    done = ssh(MINI, "p=$(command -v python3) && \"$p\" -c 'import sys; "
               "print(sys.executable if sys.version_info >= (3, 11) else \"\")'")
    path = done.stdout.strip()
    if done.returncode or not path.startswith("/"):
        raise SystemExit(f"{MINI} has no python3 3.11 or newer on its PATH")
    return path


def step_install(apply: bool) -> None:
    print(f"copy {', '.join(FILES)} to {MINI}:{APP}, then load {LABEL} "
          "as a LaunchAgent (KeepAlive: starts at login -- the mini logs in "
          "by itself -- and restarts on a crash). Its log is "
          "~/Library/Logs/conn-agent.log.")
    if not apply:
        return
    python = mini_python()
    ssh(MINI, f"mkdir -p {APP} ~/Library/LaunchAgents")
    subprocess.run(["scp", "-q", *[str(REPO / f) for f in FILES],
                    f"{MINI}:{APP.replace('~/', '')}/"], check=True)
    # "local" is the mini itself: its own chats are watched too.
    # The config (and its topic) first, so the job never races us to it.
    topic = ssh(MINI, f"{python} {APP}/conn_agent.py topic")
    target = f"gui/$(id -u)/{LABEL}"
    plist_path = f"~/Library/LaunchAgents/{LABEL}.plist"
    hosts_csv = ",".join(["local", *servers()])
    # bootout returns before the job is gone, and a bootstrap straight after
    # fails with "Input/output error": wait for it, retry, then check.
    script = (f'sed "s|__HOME__|$HOME|g; s|__HOSTS__|{hosts_csv}|; '
              f's|__PYTHON__|{python}|" > {plist_path} || exit 1\n'
              f"launchctl bootout {target} 2>/dev/null\n"
              f"for i in 1 2 3 4 5 6 7 8 9 10; do launchctl print {target} "
              f">/dev/null 2>&1 || break; sleep 1; done\n"
              f"for i in 1 2 3 4 5; do launchctl bootstrap gui/$(id -u) "
              f"{plist_path} 2>/dev/null && break; sleep 2; done\n"
              f"sleep 2; launchctl print {target} | grep -q 'state = running'"
              f" && echo 'loaded, running' || echo 'NOT running: see "
              f"~/Library/Logs/conn-agent.out'\n")
    done = ssh(MINI, "bash -c " + shlex.quote(script), stdin=plist())
    print(done.stdout.strip() or done.stderr.strip())
    print(f"ntfy topic: {topic.stdout.strip() or topic.stderr.strip()}")


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("plan", "key", "authorize",
                                        "config", "install"):
        print(__doc__.strip())
        return 2
    apply = "--apply" in argv
    if argv[1] == "plan":
        for step in (step_key, step_authorize, step_config, step_install):
            print(f"== {step.__name__.removeprefix('step_')}")
            step(False)
            print()
        return 0
    if argv[1] == "key":
        step_key(True)      # makes a file on the mini, touches no server
        return 0
    {"authorize": step_authorize, "config": step_config,
     "install": step_install}[argv[1]](apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
