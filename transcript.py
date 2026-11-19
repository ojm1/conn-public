"""Claude's last reply, word for word, from its own transcript.

The screen is a picture of the reply: wrapped at the terminal's width,
indented, cut off where it scrolled away, with tmux's idea of a box drawn
round it. Claude Code also writes the conversation down --
~/.claude/projects/<cwd-slug>/<sessionId>.jsonl, one JSON object per line --
and that copy is the text Claude actually sent. So "Copy last reply" on the
desktop, and the phone's latest_reply, read it from there, and fall back to
the screen (latest_reply below) whenever anything on the way is missing.

Finding the file, for one tmux session on one host, in a single sh script
run the way every other probe runs (hosts.run_argv: bash here, `exec sh`
over ssh, so no python is needed on the far side):

  pane pids  -> tmux list-panes, the active pane of the active window first
  claude pid -> a descendant of that pane whose command is claude (a bare
                version number on a Mac), with ~/.claude/sessions/<pid>.json.
                The process tree rather than the file's "tmux" field: a file
                left behind by a crashed claude can name a pane id that a
                later server has handed to something else, but a live child
                of the pane cannot be anyone else's.
  session id -> that file's "sessionId"
  transcript -> ~/.claude/projects/*/<id>.jsonl, by glob: the slug cannot be
                worked out again from a cwd with hyphens in it
  hand-off   -> a chat sent to the background ends its file with a
                "continued-in" line; followed once, to the file that took it.

The files run to a hundred MB, so only the tail is read (TAIL_BYTES), and
only the assistant lines in it come back (grep, on the far side), capped
again (OUT_BYTES). One pass, big enough that a long busy turn or a run of
huge tool results after the reply still leaves it in reach; a second, bigger
ssh round trip would cost more than reading a few MB off a disk.

Never logged, never cached on disk: this is chat text. Stdlib only -- it
goes to the Mac mini with the agent (agent_setup.FILES).
"""

from __future__ import annotations

import json
import re
import secrets
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime

import agent_state
import hosts

TAIL_BYTES = 4 * 1024 * 1024
OUT_BYTES = 1024 * 1024
OUT_LINES = 400
# A whole reply, not a preview: the longest single text block seen in real
# transcripts is ~80 KB. The phone gets this much too (agent_api re-exports
# it), so it stays bounded.
REPLY_CAP = 128 * 1024
# The conversation the phone shows: the last HISTORY_ITEMS messages, yours and
# Claude's answers, and no more than HISTORY_CAP characters of them in all.
HISTORY_ITEMS = 30
HISTORY_CAP = 256 * 1024
TIMEOUT = 15
UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                  r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# %(...)s are filled in by script(); everything else is the far side's sh.
# Plain POSIX -- dash on Ubuntu, the BSD tools on a Mac.
SCRIPT = r"""s=%(session)s
m='###%(token)s:'
dir="$HOME/.claude/sessions"
found=
for pp in $(tmux list-panes -s -t "=$s:" -F '#{window_active}#{pane_active} #{pane_pid}' 2>/dev/null | sort -r | cut -d' ' -f2); do
  for pid in $(ps -A -o pid= -o ppid= -o comm= 2>/dev/null | awk -v root="$pp" '
      { par[$1] = $2; c = $3; sub(".*/", "", c); comm[$1] = c }
      END { for (p in par) { q = p; n = 0
              while (q != "" && q != root && n < 32) { q = par[q]; n++ }
              if (q == root && (comm[p] == "claude" || comm[p] == "node" ||
                  comm[p] ~ /^[0-9]+\.[0-9]+\.[0-9]+$/)) print p } }'); do
    if [ -f "$dir/$pid.json" ]; then found="$dir/$pid.json"; break 2; fi
  done
done
[ -n "$found" ] || { echo "${m}NONE no claude"; exit 0; }
info=$(tr -d '\n' < "$found")
id=$(printf '%%s' "$info" | sed -n 's/.*"sessionId" *: *"\([0-9a-fA-F-]*\)".*/\1/p')
status=$(printf '%%s' "$info" | sed -n 's/.*"status" *: *"\([a-z]*\)".*/\1/p')
case "$id" in ""|*[!0-9a-fA-F-]*) echo "${m}NONE no session id"; exit 0;; esac
find_file() {
  for c in "$HOME"/.claude/projects/*/"$1".jsonl; do
    [ -f "$c" ] && { printf '%%s' "$c"; return; }
  done
}
f=$(find_file "$id")
[ -n "$f" ] || { echo "${m}NONE no transcript"; exit 0; }
moved=$(tail -c 262144 "$f" | grep -a -e '"type": *"assistant"' -e '"type": *"continued-in"' | tail -n 1 | grep -a '"type": *"continued-in"' | sed -n 's/.*"continuedInSessionId" *: *"\([0-9a-fA-F-]*\)".*/\1/p')
if [ -n "$moved" ]; then
  g=$(find_file "$moved")
  if [ -n "$g" ]; then f=$g; id=$moved; else moved=; fi
fi
size=$(wc -c < "$f" | tr -d ' ')
echo "${m}SESSION $id"
echo "${m}STATUS $status"
echo "${m}MOVED $moved"
echo "${m}SIZE $size"
echo "${m}TAIL"
if [ "$size" -gt %(tail)d ]; then tail -c %(tail)d "$f" | sed 1d; else cat "$f"; fi \
  | awk '/"type": *"assistant"/ || (/"type": *"user"/ && !/"tool_use_id"/)' \
  | tail -n %(lines)d | tail -c %(out)d
"""


@dataclass
class Reply:
    text: str
    session_id: str = ""
    uuid: str = ""          # the reply's last line: changes when a new one lands
    busy: bool = False      # Claude is past it, on a turn not finished yet
    moved: bool = False     # read from the background job the chat went to
    # The conversation's end: {"role": "you" | "claude", "text", "at"}, oldest
    # first, "at" in unix seconds (0 when the line had no timestamp).
    history: list = field(default_factory=list)


def script(session: str, token: str) -> str:
    return SCRIPT % {"session": shlex.quote(session), "token": token,
                     "tail": TAIL_BYTES, "lines": OUT_LINES, "out": OUT_BYTES}


def records(lines) -> list[dict]:
    """The lines that parse. The first line of a tail is usually cut in
    half, and the last can be mid-append; a JSON object missing either end
    does not parse, so dropping what fails is the whole of that handling."""
    out = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def pick(entries: list[dict]) -> Reply | None:
    """The last finished reply among transcript records, or None.

    One API response is written as several lines, one content block each,
    sharing message.id -- typically a thinking line then a text line. So
    lines are grouped by message.id and a group's text blocks joined in
    order. A turn is many messages: the ones that end in a tool call
    (stop_reason "tool_use", or holding a tool_use block) are narration on
    the way, and the reply is the last message that is not one of those.
    Mid-turn, that is the previous turn's answer -- the last thing Claude
    finished saying -- and busy says there is more coming.

    Only the fields every version writes are read: type, isSidechain,
    uuid, message.id/model/stop_reason/content. Skipped: subagents'
    sidechains, API errors and interruptions (model "<synthetic>").
    """
    groups: dict[str, dict] = {}
    order: list[str] = []
    for entry in entries:
        if entry.get("type") != "assistant" or entry.get("isSidechain") \
                or entry.get("isApiErrorMessage"):
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("model") == "<synthetic>":
            continue
        key = str(message.get("id") or entry.get("uuid") or len(order))
        group = groups.get(key)
        if group is None:
            group = groups[key] = {"texts": [], "tool": False, "stop": None,
                                   "uuid": "", "seen": set()}
            order.append(key)
        uuid = str(entry.get("uuid") or "")
        if uuid and uuid in group["seen"]:
            continue                # the same line written twice
        group["seen"].add(uuid)
        group["uuid"] = uuid or group["uuid"]
        group["stop"] = message.get("stop_reason") or group["stop"]
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                group["tool"] = True
            elif block.get("type") == "text" and isinstance(block.get("text"), str):
                group["texts"].append(block["text"])

    def finished(group):
        return not group["tool"] and group["stop"] != "tool_use"

    busy = False
    for key in reversed(order):
        group = groups[key]
        if not finished(group):
            busy = True
            continue
        text = "\n\n".join(t.strip("\n") for t in group["texts"]
                           if t.strip()).strip()
        if text:
            return Reply(text=text[:REPLY_CAP], uuid=group["uuid"], busy=busy)
    return None


def _when(entry: dict) -> float:
    stamp = entry.get("timestamp")
    if not isinstance(stamp, str) or not stamp:
        return 0.0
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _typed(entry: dict) -> str:
    """What you typed, if this user record is a message from you: not a
    tool's result, a system notice or a subagent's prompt. Newer versions
    say so (origin.kind "human"); older ones are judged by their content."""
    if entry.get("type") != "user" or entry.get("isSidechain") \
            or entry.get("isMeta"):
        return ""
    origin = entry.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return ""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result"
               for b in content):
            return ""
        texts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
    else:
        return ""
    text = "\n\n".join(t for t in texts if isinstance(t, str)).strip()
    # Command echoes and injected context, written as the user's turn.
    if text.startswith("<") or text.startswith("Caveat:"):
        return ""
    return text


def conversation(entries: list[dict]) -> list[dict]:
    """Your messages and Claude's answers among transcript records, oldest
    first: each answer the way pick() finds the reply (a message's text
    blocks joined, the narration before tool calls left out), each of yours
    as typed. Capped to the last HISTORY_ITEMS, and HISTORY_CAP characters."""
    items: list[dict] = []
    groups: dict[str, dict] = {}
    for entry in entries:
        typed = _typed(entry)
        if typed:
            items.append({"role": "you", "text": typed[:REPLY_CAP],
                          "at": _when(entry)})
            continue
        if entry.get("type") != "assistant" or entry.get("isSidechain") \
                or entry.get("isApiErrorMessage"):
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("model") == "<synthetic>":
            continue
        key = str(message.get("id") or entry.get("uuid") or len(items))
        group = groups.get(key)
        if group is None:
            group = groups[key] = {"role": "claude", "texts": [], "tool": False,
                                   "stop": None, "at": 0.0, "seen": set()}
            items.append(group)
        uuid = str(entry.get("uuid") or "")
        if uuid and uuid in group["seen"]:
            continue
        group["seen"].add(uuid)
        group["stop"] = message.get("stop_reason") or group["stop"]
        group["at"] = _when(entry) or group["at"]
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                group["tool"] = True
            elif block.get("type") == "text" and isinstance(block.get("text"), str):
                group["texts"].append(block["text"])

    out: list[dict] = []
    for item in items:
        if item["role"] == "you":
            out.append(item)
            continue
        if item["tool"] or item["stop"] == "tool_use":
            continue                # narration on the way to a tool call
        text = "\n\n".join(t.strip("\n") for t in item["texts"]
                           if t.strip()).strip()
        if text:
            out.append({"role": "claude", "text": text[:REPLY_CAP],
                        "at": item["at"]})
    out = out[-HISTORY_ITEMS:]
    total = 0
    for index in range(len(out) - 1, -1, -1):
        total += len(out[index]["text"])
        if total > HISTORY_CAP:
            out = out[index + 1:]
            break
    return out


def parse(out: str, token: str) -> Reply | None:
    """The script's output -> Reply, or None for "not found"."""
    marker = f"###{token}:"
    meta: dict[str, str] = {}
    head, sep, tail = out.partition(marker + "TAIL\n")
    for line in head.splitlines():
        if line.startswith(marker):
            key, _, value = line[len(marker):].partition(" ")
            meta[key] = value.strip()
    if not sep or "NONE" in meta:
        return None
    session_id = meta.get("SESSION", "")
    if not UUID.match(session_id):
        return None
    entries = records(tail.splitlines())
    reply = pick(entries)
    if reply is None:
        return None
    reply.history = conversation(entries)
    reply.session_id = session_id
    reply.moved = bool(meta.get("MOVED"))
    reply.busy = reply.busy or meta.get("STATUS") == "busy"
    return reply


def fetch(host: str, session: str, timeout: float = TIMEOUT) -> Reply | None:
    """The last reply of the claude in `session` on `host`, or None.
    Blocking -- one ssh round trip -- so never on the main loop. Never
    raises: every failure is the screen's turn."""
    token = secrets.token_hex(8)
    try:
        done = subprocess.run(hosts.run_argv(host),
                              input=script(session, token).encode(),
                              capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if done.returncode:
        return None
    return parse(done.stdout.decode("utf-8", errors="replace"), token)


def latest_reply(screen: str) -> str:
    """Claude's last reply as the screen shows it: the "●" block's lines,
    de-indented, line breaks kept, until the next block, prompt box or
    status line. The fallback for when there is no transcript to read."""
    claude = agent_state.AGENTS[0]
    index, first = claude.reply(screen)
    if index < 0:
        return ""
    lines = agent_state.strip_ansi(screen).splitlines()
    out = [first]
    for line in lines[index + 1:]:
        stripped = line.strip()
        # A new block, the box or a rule start at the left edge; the
        # reply's own lines are indented, so "> quote" or "- item" in it
        # do not end it.
        if re.match(r"^[●⏺✻✶✽✢✳✱⋆*❯>─━╭╰]", line) or claude._is_rule(line):
            break
        if stripped and not line.startswith(("  ", "\t")):
            break
        out.append(line[2:] if line.startswith("  ") else line)
    text = "\n".join(out).strip("\n")
    return text[:REPLY_CAP]


def last_reply(host: str, session: str, screen: str = "",
               fetcher=None) -> tuple[str, str]:
    """(text, where it came from): "transcript", "screen", or ("", "") when
    neither has one -- or "transcript-busy" when Claude is mid-turn, the
    text then being the turn before's finished answer, which whoever copies
    it should be told. Blocking, like fetch. fetch is looked up per call, so
    a test that replaces transcript.fetch replaces it here too."""
    reply = (fetcher or fetch)(host, session)
    if reply is not None and reply.text:
        return reply.text, "transcript-busy" if reply.busy else "transcript"
    text = latest_reply(screen)
    return (text, "screen") if text else ("", "")
