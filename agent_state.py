"""Reading a coding agent's screen and deciding whether it needs you.

The panel exists to answer one question from a distance: is that chat working,
or is it waiting on me? Neither agent publishes that anywhere, but both say so
plainly on screen -- so this reads the screen.

Three rules shape everything below:

  * Patterns are loose. The wording shifts between releases, and this runs
    against whatever version happens to be on the far box.
  * Anything unrecognised is UNKNOWN, never READY. Telling you a chat is idle
    when it is actually blocked on a permission prompt is the one failure that
    would make the panel worse than not looking.
  * The states are the agent's, but the *ordering* of the checks is not: busy
    beats blocked beats draft beats idle, whichever agent is on screen. Each
    agent supplies patterns; `classify` owns the decision.

Supported agents are the classes in AGENTS. Adding a third means writing its
patterns, not touching the caller.
"""

from __future__ import annotations

import re

WORKING = "working"      # actively thinking or running tools
NEEDS_YOU = "needs-you"  # blocked on a prompt only you can answer
DRAFT = "draft"          # text sitting in the box, never submitted
READY = "ready"          # idle at an empty prompt
SHELL = "shell"          # not an agent session at all
UNKNOWN = "unknown"

# What a dialog is allowed to draw in front of its own text: box bars, carets,
# bullets, indentation. Quote marks are deliberately not in it -- see BLOCKED.
CHROME = r"""^[^\w\n"“”‘’'`]*"""

LABELS = {
    WORKING: "working",
    NEEDS_YOU: "needs you",
    DRAFT: "unsent draft",
    READY: "idle",
    SHELL: "shell",
    UNKNOWN: "unknown",
}


class Agent:
    """What one agent's screen looks like.

    Subclasses supply patterns and, where the layout demands it, their own
    input-box parser. Everything else is shared.
    """

    key = ""
    name = ""
    COMMANDS: tuple[str, ...] = ()
    # A pane command that is this agent though its name does not say so.
    COMMAND_PATTERN: re.Pattern | None = None

    def runs(self, command: str) -> bool:
        """Whether a pane running `command` is running this agent."""
        return (any(name in command for name in self.COMMANDS)
                or bool(self.COMMAND_PATTERN
                        and self.COMMAND_PATTERN.match(command)))

    BUSY: re.Pattern | None = None
    BLOCKED: tuple[re.Pattern, ...] = ()
    SETTLED: re.Pattern | None = None
    TOKENS: re.Pattern | None = None

    def input_box(self, screen: str,
                  ghosts: set[int] | None = None) -> str | None:
        """Contents of the input box: None if it cannot be found, "" if empty,
        otherwise the unsent text."""
        raise NotImplementedError

    def busy(self, screen: str) -> bool:
        """Whether the agent's own busy marker is on screen."""
        return bool(self.BUSY and self.BUSY.search(screen))

    def busy_detail(self, screen: str) -> str:
        return "working"

    def idle_detail(self, screen: str) -> str:
        return ""

    def background(self, screen: str) -> str:
        """Work running outside the main turn, or "" when there is none."""
        return ""

    def step(self, screen: str) -> str:
        """What a busy turn is doing right now, in words fit for a preview."""
        return self.busy_detail(screen)

    def permission_preview(self, screen: str) -> str:
        """What a permission prompt wants, in words for a chat list ("Wants
        to run rm -rf build"), or "" to fall back on the question."""
        return ""

    def recap(self, screen: str) -> tuple[int, str]:
        """The newest recap on screen as (line index, text), or (-1, "").

        The index is there so the caller can tell whether a reply arrived
        after it: a recap describes the turn it was written for, not the one
        that came next."""
        return -1, ""

    def reply(self, screen: str) -> tuple[int, str]:
        """The first line of the agent's newest reply as (line index, text),
        or (-1, "") when no reply can be told apart from everything else."""
        return -1, ""

    def tokens(self, screen: str) -> str:
        """The last count on screen: a transcript can carry older ones from a
        /context or a compaction notice, and the footer is below all of them."""
        return last_match(self.TOKENS, screen) if self.TOKENS else ""

    def blocked(self, screen: str) -> bool:
        """Whether a prompt is still asking, not merely still visible.

        An answered prompt stays in the transcript, and 200 captured lines
        keep it on screen long after the turn moved on -- so a match alone
        does not mean blocked. What marks it answered is what the agent
        drew underneath: SETTLED names that shape, and a match with any of
        it below is history, not a question.
        """
        for pattern in self.BLOCKED:
            for match in pattern.finditer(screen):
                if self.SETTLED is None or not self.SETTLED.search(
                        screen, match.end()):
                    return True
        return False

    def blocked_detail(self, screen: str) -> str:
        """The newest live match of the most telling pattern.

        Pattern order is a preference -- the question a prompt asks says
        more than the "1. Yes" underneath it -- so that is kept. But only
        matches still asking count, whichever pattern they fall under: a
        prompt you already answered stays in the transcript above the one
        still waiting, and quoting the old one is how the panel described
        the wrong prompt -- even across patterns, when the answered
        question outranked the live one.
        """
        for pattern in self.BLOCKED:
            newest = None
            for match in pattern.finditer(screen):
                if self.SETTLED is None or not self.SETTLED.search(
                        screen, match.end()):
                    newest = match
            if newest is not None:
                line = screen[newest.start():].splitlines()[0]
                return line.strip().strip("│┃ ")[:60]
        return "waiting for you"


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------

class ClaudeCode(Agent):
    key = "claude"
    name = "Claude Code"
    COMMANDS = ("claude",)
    # The native install runs ~/.local/share/claude/versions/2.1.322 through
    # a ~/.local/bin/claude symlink. Linux names the process after the link,
    # macOS after the file it points at -- so on a Mac tmux reports the
    # command as the bare version number.
    COMMAND_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")

    # Printed next to the spinner the entire time it is busy. The single most
    # reliable "still going" signal on the screen.
    BUSY = re.compile(r"esc to interrupt", re.I)

    # Blocked-on-you prompts. Several shapes across versions, so match
    # generously.
    # Anchored to the start of a line, past whatever chrome is drawn in front
    # of it -- box bars, carets, bullets, all of which are non-word characters.
    # A prompt Claude Code is actually asking begins its line. The same words
    # inside a sentence are someone *talking* about a prompt, and reading those
    # as one is how a session discussing this file reported itself blocked.
    #
    # Quote marks are excluded from that chrome even though they are not word
    # characters: a dialog never draws one in front of its question, and prose
    # quoting a prompt lands one there every time -- including halfway down a
    # sentence, where the terminal wrapped the line and left the quote at the
    # front of the next one.
    # Claude asking you to pick (AskUserQuestion): a caret on a numbered
    # option, and the picker's own footer underneath. Both, so a numbered
    # list in prose with a stray caret cannot read as one; an answered
    # picker is replaced by its ⎿ result, which SETTLED already covers.
    PICKER = re.compile(CHROME + r"❯\s*\d+\.\s+\S[\s\S]*?"
                        r"Enter to select\b[^\n]*Esc to cancel", re.I | re.M)

    BLOCKED = (
        re.compile(CHROME + r"Do you want to\b", re.I | re.M),
        re.compile(CHROME + r"Would you like to\b", re.I | re.M),
        # The selection caret is what makes an option list a live dialog.
        # Without it, "1. Yes, keep the current design" in a transcript --
        # Claude answering a question with a numbered list -- reads as a
        # prompt; and an answered prompt keeps its " 1. Yes" but loses the
        # caret.
        re.compile(CHROME + r"❯\s*1\.\s*Yes\b", re.M),
        re.compile(r"\(y/n\)\s*$", re.I | re.M),
        re.compile(CHROME + r"Press enter to continue\b", re.I | re.M),
        re.compile(CHROME + r"waiting for your input\b", re.I | re.M),
        PICKER,
    )

    # What the screen shows once a prompt has been answered: the turn moves
    # on underneath it -- a tool result, a finished-turn stamp. A prompt
    # still asking is the last thing drawn above its own options and the
    # box.
    SETTLED = re.compile(
        r"^\s*⎿|[✻✽✢⋆*]\s*[A-Z][^\W\d_]+ed\s+for\s+(?:\d+h\s*)?(?:\d+m\s*)?\d+s",
        re.M)

    # "275.1k tokens"
    TOKENS = re.compile(r"([0-9.]+k)\s+tokens", re.I)

    # The input box shows this greyed-out hint only while it is empty.
    PLACEHOLDER = re.compile(r'Try\s+"')

    # The footer strip that sits under the input box.
    FOOTER = re.compile(r"⏵⏵|auto mode on|shift\+tab to cycle", re.I)

    # The live spinner, e.g. "✶ Coalescing…". The trailing ellipsis is what
    # separates it from the finished form ("✻ Cooked for 1s").
    SPINNER = re.compile(r"^\s*[✶✻✽✢✳✱⋆*]\s*(\S[^\n]*?[….]{1,3})\s*$", re.M)

    # "✻ Cogitated for 21m 47s". Past tense and a real duration, so it cannot
    # swallow a live line like "Waiting for 1 workflow".
    LAST_ACTION = re.compile(
        r"[✻✽✢⋆*]\s*([A-Z][^\W\d_]+ed\s+for\s+(?:\d+h\s*)?(?:\d+m\s*)?\d+s)")

    # Work running *outside* the main turn: background workflows and agent
    # fleets. The main loop sits at an empty prompt while these run, so without
    # this the panel would call a busy session idle.
    AGENTS = re.compile(r"(\d+)\s*/\s*(\d+)\s+agents?\s+done", re.I)
    WORKFLOW = re.compile(r"Waiting for\s+(\d+)\s+(?:dynamic\s+)?workflow", re.I)
    ELAPSED = re.compile(r"·\s*((?:\d+h\s*)?(?:\d+m\s*)?\d+s)\s*·")
    # The footer's status line for a running workflow:
    # "◯ conn-review ▰▰▰▱▱ 2/6 · 8m29s · ↓ 550.2k tokens".
    WORKFLOW_LINE = re.compile(
        r"^\s*[◯◉●○]\s+([\w.-]+)\s+[▰▱]+\s+(\d+/\d+)\s*·\s*"
        r"((?:\d+h)?(?:\d+m)?\d+s)", re.M)

    # A horizontal rule -- the input box is fenced by two of them. The top rule
    # can carry a short label baked into it ("──── ultracode ─"), so a rule is
    # "mostly dashes", not "only dashes".
    RULE_LABEL_MAX = 24

    def _is_rule(self, line: str) -> bool:
        stripped = line.strip()
        dashes = sum(1 for char in stripped if char in "─━-")
        if dashes < 20:
            return False
        label = [char for char in stripped if char not in "─━- "]
        return len(label) <= self.RULE_LABEL_MAX

    def input_box(self, screen: str,
                  ghosts: set[int] | None = None) -> str | None:
        """This has to be exact. Submitted messages stay on screen in the
        transcript behind the very same "❯" that marks the input line, so
        matching "❯" anywhere reports every finished chat as having unsent
        text. What actually identifies the box is its fencing: it is the
        region between the last two horizontal rules, however far apart --
        the box grows with what you type into it. A wide box-drawing border
        like "╭────╮" counts as a rule too, and that is fine: the input box
        sits below any banner drawn that way, so the last two rules are
        still its own.
        """
        lines = screen.splitlines()
        rules = [index for index, line in enumerate(lines) if self._is_rule(line)]
        if len(rules) < 2:
            # The capture arrives through tail, so a trim cuts the *top* of
            # the screen: a lone rule is the box's closing fence with the
            # opening one lost, and below it there is only footer. Nothing
            # left to read the box from.
            return None

        segment = lines[rules[-2] + 1:rules[-1]]

        # Whatever the box holds, if it was drawn dim then Claude suggested
        # it and you did not leave it there. An empty box is idle.
        if ghosts:
            first = rules[-2] + 1
            segment = [line for offset, line in enumerate(segment)
                       if first + offset not in ghosts]

        segment = [line for line in segment if not self.FOOTER.search(line)]
        if not segment:
            return ""

        text = " ".join(part.strip() for part in segment).strip()
        text = re.sub(r"^(?:❯|>)\s*", "", text).strip()

        if not text or self.PLACEHOLDER.search(text):
            return ""
        return text[:60]

    # A spinner line, whatever follows the glyph: old versions put the
    # marker there, "(12s · esc to interrupt)".
    SPINNER_LINE = re.compile(r"^\s*[✶✻✽✢✳✱⋆*]\s*\S")

    def busy(self, screen: str) -> bool:
        """The marker where Claude Code draws it -- the strip under the
        input box, or the spinner line -- not anywhere at all. Anywhere
        includes the transcript, and a chat that merely *quotes* "esc to
        interrupt" (one debugging this very file, say) read as working long
        after its turn had finished."""
        lines = screen.splitlines()
        rules = [i for i, line in enumerate(lines) if self._is_rule(line)]
        if rules:
            if self.BUSY.search("\n".join(lines[rules[-1] + 1:])):
                return True
            above = lines[:rules[-2] if len(rules) >= 2 else rules[-1]]
        else:
            # No box to anchor on: only the last few lines can be chrome.
            tail = [line for line in lines if line.strip()][-3:]
            if any(self.BUSY.search(line) for line in tail):
                return True
            above = lines
        # The spinner sits just above the box, after a blank line or two.
        near = [line for line in above if line.strip()][-3:]
        return any(self.SPINNER_LINE.match(line) and self.BUSY.search(line)
                   for line in near)

    def busy_detail(self, screen: str) -> str:
        """The "esc to interrupt" marker lives in the footer strip, which says
        nothing useful, so the detail comes from the spinner line instead."""
        spins = self.SPINNER.findall(screen)
        if spins:
            return spins[-1].strip()[:60]
        return last_match(self.LAST_ACTION, screen)[:60] or "working"

    def idle_detail(self, screen: str) -> str:
        return last_match(self.LAST_ACTION, screen)

    def background(self, screen: str) -> str:
        # Last match, as everywhere: a finished run's "8/8 agents done" can
        # sit in the transcript above the fleet that is still going.
        agents = None
        for match in self.AGENTS.finditer(screen):
            agents = match
        if agents:
            done, total = int(agents.group(1)), int(agents.group(2))
            if done < total:
                line = screen[agents.start():].splitlines()[0]
                elapsed = self.ELAPSED.search(line)
                suffix = f" - {elapsed.group(1)}" if elapsed else ""
                return f"{done}/{total} agents{suffix}"

        workflow = None
        for match in self.WORKFLOW.finditer(screen):
            workflow = match
        if workflow:
            count = workflow.group(1)
            if count == "1":
                # One workflow: the footer names it and how far along it is,
                # which says more than the count does.
                line = None
                for match in self.WORKFLOW_LINE.finditer(screen):
                    line = match
                if line:
                    return " · ".join(line.groups())
            return f"{count} workflow{'s' if count != '1' else ''} running"
        return ""

    # The spinner line with whatever Claude Code has appended to it since
    # SPINNER was written -- "✶ Running the tests… (1m 2s · ↓ 3.4k tokens)".
    # SPINNER wants the ellipsis at the end of the line, so on that shape the
    # detail falls back to the last finished turn; a preview saying "Cooked
    # for 12s" about a turn still running is worse than a plain "Working".
    STEP = re.compile(
        r"^\s*[✶✻✽✢✳✱⋆*·]\s*(\S[^\n]*?)(?:…|\.\.\.)(?:\s*\([^\n]*\))?\s*$",
        re.M)

    # "※ recap: We're refreshing the homepage ..." -- written after a turn
    # when you have been away. It wraps onto lines indented by exactly two
    # spaces; anything indented further is the right-aligned footer hint
    # ("new task? /clear to save ...") that sits directly under it.
    RECAP = re.compile(r"^\s*※\s*recap:\s*(.*)$", re.I)
    WRAPPED = re.compile(r"^  (?![\s⎿●⏺❯>│])\S")

    # Claude's own words start at column 0 behind a bullet. Tool calls and
    # status notices use the same bullet, so a bullet is a reply only when it
    # is none of those: a call is "Name(args)" or a collapsed "Read 3 files
    # (ctrl+o to expand)" with its result hanging underneath on a ⎿ line;
    # the notices are the few Claude Code prints on its own account.
    BULLET = re.compile(r"^(?:●|⏺)\s+(\S.*)$")
    # A call's name is one capitalised token ("Bash(", "Update("), two for
    # the few that have a space ("Web Search("), an mcp__ name, or an MCP
    # tool's "(MCP)" tag. Not any two words before a "(": Claude draws
    # inline code without its backticks, so a reply opening "Updated
    # parse_frame() so ..." would be skipped and an older turn shown.
    TOOL_CALL = re.compile(
        r"^(?:[A-Z][\w.:-]*(?: [A-Z][\w.:-]*)?|mcp__[\w.:-]+)\("
        r"|\(MCP\)|ctrl\+o to expand")
    NOTICE = re.compile(
        r"^(?:Dynamic workflow\b.*\bcompleted|Compacted\b|Interrupted\b)",
        re.I)

    # Claude's spinner word when it has no todo to name: one capitalised -ing
    # verb ("Transmogrifying", "Schlepping") picked at random each turn. It
    # says nothing about the work, so it is not a step. A todo's activeForm
    # ("Running the tests") is several words and comes through as it is.
    VERB = re.compile(r"^[A-Z][^\W\d_]*ing$")
    PROMPT = re.compile(r"^\s*❯\s")
    TODO_ACTIVE = re.compile(r"^\s*(?:⎿\s*)?◼\s+(\S.*?)\s*$")
    CALL = re.compile(r"^([A-Z][\w.:-]*(?: [A-Z][\w.:-]*)?|mcp__[\w.:-]+)"
                      r"\((.*?)\)?\s*$")
    COLLAPSED = {"Read": "Reading", "Searched for": "Searching for",
                 "Listed": "Listing", "Wrote": "Writing",
                 "Updated": "Updating", "Fetched": "Fetching"}

    def step(self, screen: str) -> str:
        found = last_match(self.STEP, screen)
        if found and not self.VERB.match(found.strip()):
            return found[:80]
        return self.turn_step(screen)[:80] or "Working"

    def _turn(self, screen: str) -> list[str]:
        """The lines of the turn now running: below the last prompt you sent,
        above the input box."""
        lines = [strip_ansi(line) for line in screen.splitlines()]
        rules = [i for i, line in enumerate(lines) if self._is_rule(line)]
        if len(rules) >= 2:
            lines = lines[:rules[-2]]
        start = 0
        for index in range(len(lines) - 1, -1, -1):
            if self.PROMPT.match(lines[index]):
                start = index + 1
                break
        return lines[start:]

    @staticmethod
    def _arg(args: str) -> str:
        """The first argument of a call as drawn, quotes and "name:" off."""
        first = args.split(", ")[0].strip()
        if re.match(r"^\w+:\s", first):
            first = first.split(":", 1)[1].strip()
        return first.strip("\"'`")

    def call_words(self, text: str) -> str:
        """A tool-call bullet's text as what it is doing, or ""."""
        collapsed = re.match(r"^(Read|Searched for|Listed|Wrote|Updated|"
                             r"Fetched)\s+(\d+\s+\S+?)\s*(?:\(|,|$)", text)
        if collapsed:
            return f"{self.COLLAPSED[collapsed.group(1)]} {collapsed.group(2)}"
        if "(MCP)" in text:
            name = text.split("(MCP)")[0].split(" - ")[-1].strip()
            return f"Using {name}" if name else ""
        call = self.CALL.match(text)
        if not call:
            return ""
        name, args = call.group(1), call.group(2) or ""
        arg = self._arg(args)
        base = arg.rstrip("/").rsplit("/", 1)[-1] or arg
        if name.startswith("mcp__"):
            return f"Using {name.rsplit('__', 1)[-1]}"
        if name == "Bash":
            return f"Running {arg[:40]}".strip()
        if name == "Read":
            return f"Reading {base}".strip()
        if name in ("Edit", "Update", "MultiEdit", "NotebookEdit"):
            return f"Editing {base}".strip()
        if name in ("Write", "Create"):
            return f"Writing {base}".strip()
        if name in ("Grep", "Search", "Glob"):
            return f"Searching for {arg}".strip()
        if name in ("WebFetch", "Fetch"):
            host = re.sub(r"^\w+://", "", arg).split("/")[0]
            return f"Fetching {host}".strip()
        if name == "Web Search":
            return f"Searching the web for {arg}".strip()
        if name in ("Task", "Agent"):
            return arg
        return f"Using {name}"

    def turn_step(self, screen: str) -> str:
        """What the running turn is doing, read off the turn itself: the todo
        in progress, else the newest tool call, else the newest thing Claude
        said in it. "" when the turn has shown none of them yet."""
        turn = self._turn(screen)
        for line in reversed(turn):
            todo = self.TODO_ACTIVE.match(line)
            if todo:
                return todo.group(1)
        for line in reversed(turn):
            bullet = self.BULLET.match(line)
            if bullet and self.TOOL_CALL.search(bullet.group(1).strip()):
                words = self.call_words(bullet.group(1).strip())
                if words:
                    return words
        for line in reversed(turn):
            bullet = self.BULLET.match(line)
            if (bullet and not self.TOOL_CALL.search(bullet.group(1))
                    and not self.NOTICE.search(bullet.group(1))):
                return bullet.group(1).strip()
        return ""

    # A permission dialog's header, and what it says the request is.
    DIALOG = {"Bash command": "run", "Edit file": "edit",
              "Create file": "create", "Write file": "write",
              "Write": "write", "Read file": "read", "Fetch": "fetch",
              "Web Search": "search the web for"}

    def blocked_detail(self, screen: str) -> str:
        """The question a picker asks, rather than its first option."""
        picker = None
        for match in self.PICKER.finditer(screen):
            if not self.SETTLED.search(screen, match.end()):
                picker = match
        if picker is None or self._plain_question(screen) is not None:
            return super().blocked_detail(screen)
        above = screen[:picker.start()].splitlines()
        for line in reversed(above[-12:]):
            text = line.strip().strip("│┃ ")
            if text.endswith("?"):
                return text[:60]
        return super().blocked_detail(screen)

    def _plain_question(self, screen: str):
        """A live match of any blocked pattern but the picker, or None."""
        for pattern in self.BLOCKED:
            if pattern is self.PICKER:
                continue
            for match in pattern.finditer(screen):
                if not self.SETTLED.search(screen, match.end()):
                    return match
        return None

    def permission_preview(self, screen: str) -> str:
        """ "Wants to run rm -rf build" from the dialog's header and body.

        The question ("Do you want to proceed?") is the same on every
        permission prompt; the header and the first line under it are what
        the request actually is."""
        live = self._plain_question(screen)
        if live is None:
            return ""
        above = screen[:live.start()].splitlines()[-16:]
        cleaned = [line.strip().strip("│┃╭╮╰╯ ").strip() for line in above]
        for index in range(len(cleaned) - 1, -1, -1):
            verb = self.DIALOG.get(cleaned[index])
            if verb is None:
                continue
            body = next((line for line in cleaned[index + 1:]
                         if line and not self._is_rule(line)
                         and not set(line) <= set("─━-│┃╭╮╰╯ ")), "")
            if not body:
                return ""
            if verb in ("edit", "create", "write", "read"):
                body = body.rstrip("/").rsplit("/", 1)[-1]
            if verb == "fetch":
                body = re.sub(r"^\w+://", "", body).split("/")[0]
            return f"Wants to {verb} {body}"[:80]
        return ""

    # Claude Code's own hint, wrapped into the recap's last line.
    RECAP_HINT = re.compile(r"\s*\(disable recaps in /config\)\s*$", re.I)

    def recap(self, screen: str) -> tuple[int, str]:
        lines = [strip_ansi(line) for line in screen.splitlines()]
        for index in range(len(lines) - 1, -1, -1):
            match = self.RECAP.match(lines[index])
            if not match:
                continue
            parts = [match.group(1).strip()]
            for line in lines[index + 1:]:
                if not self.WRAPPED.match(line):
                    break
                parts.append(line.strip())
            text = " ".join(part for part in parts if part)
            return index, self.RECAP_HINT.sub("", text)
        return -1, ""

    def reply(self, screen: str) -> tuple[int, str]:
        lines = [strip_ansi(line) for line in screen.splitlines()]
        for index in range(len(lines) - 1, -1, -1):
            match = self.BULLET.match(lines[index])
            if not match:
                continue
            text = match.group(1).strip()
            if self.TOOL_CALL.search(text) or self.NOTICE.search(text):
                continue
            following = next((line for line in lines[index + 1:]
                              if line.strip()), "")
            if following.lstrip().startswith("⎿"):
                continue
            return index, text
        return -1, ""


# --------------------------------------------------------------------------
# opencode
# --------------------------------------------------------------------------

class OpenCode(Agent):
    """opencode draws nothing like Claude Code.

    Its input box is fenced down the *left* by a heavy vertical bar and closed
    underneath by a single rule of upper-half blocks -- no top rule at all. The
    transcript uses the same left bar for the messages you have already sent,
    so the bar alone identifies nothing; the bottom rule is what anchors the
    box. Captured from opencode 1.18.19.
    """

    key = "opencode"
    name = "opencode"
    COMMANDS = ("opencode",)

    # The footer hint shown only while a turn is running. It becomes "esc again
    # to interrupt" once esc has been pressed a first time.
    BUSY = re.compile(r"\besc\s+(?:again\s+to\s+)?interrupt\b", re.I)

    # Same rule as Claude Code's: the dialog's own lines, not a sentence that
    # happens to contain the words.
    BLOCKED = (
        re.compile(CHROME + r"Permission required", re.I | re.M),
        re.compile(CHROME + r"Allow once\b", re.I | re.M),
        re.compile(CHROME + r"Allow always\b.*\bReject\b", re.I | re.M),
    )

    # The idle footer carries the context used, e.g. "7.4K (3%) · $0.00".
    TOKENS = re.compile(r"([0-9.]+[KM])\s*\(\s*\d+\s*%\s*\)")

    BAR = "┃"                      # U+2503, fences the box and the transcript
    RULE = re.compile(r"^\s*╹▀+\s*$")   # U+2579 corner, then upper-half blocks

    PLACEHOLDER = re.compile(r"Ask anything", re.I)

    # The last line inside the box is opencode's own status ("Build · Kimi K2.7
    # Code OpenCode Go"), not something you typed.
    STATUS = re.compile(r"·")

    # A tool running inside the box, e.g. "⠋ rm -f demo.py".
    SPINNER = re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s*(\S[^\n]*)")

    # A finished turn stamps the elapsed time onto the model line:
    # "▣  Build · Kimi K2.7 Code · 4.7s".
    LAST_ACTION = re.compile(
        r"▣[^\n]*·\s*((?:\d+h\s*)?(?:\d+m\s*)?[\d.]+s)\s*$", re.M)

    def input_box(self, screen: str,
                  ghosts: set[int] | None = None) -> str | None:
        lines = screen.splitlines()
        rules = [index for index, line in enumerate(lines)
                 if self.RULE.match(line)]
        if not rules:
            # No bottom rule: either not opencode, or a dialog has taken the
            # box over. Either way this must not guess.
            return None

        end = rules[-1]
        start = end
        while start > 0 and self.BAR in lines[start - 1]:
            start -= 1
        segment = lines[start:end]
        if not segment:
            return ""

        # Drop opencode's own status line, which always sits at the bottom of
        # the box and would otherwise read as unsent text on every session.
        if segment and self.STATUS.search(segment[-1]):
            segment = segment[:-1]

        parts = []
        for line in segment:
            _, _, rest = line.partition(self.BAR)
            parts.append(rest.strip())
        text = " ".join(part for part in parts if part).strip()

        if not text or self.PLACEHOLDER.search(text):
            return ""
        return text[:60]

    def busy_detail(self, screen: str) -> str:
        spins = self.SPINNER.findall(screen)
        if spins:
            return spins[-1].strip().strip("│┃ ")[:60]
        return "working"

    def idle_detail(self, screen: str) -> str:
        return last_match(self.LAST_ACTION, screen)


AGENTS: tuple[Agent, ...] = (ClaudeCode(), OpenCode())


def detect(commands: list[str]) -> Agent | None:
    """Which agent is running in these panes, if any.

    `commands` is what the panes are running, which settles the shell case
    without having to infer it from pixels.
    """
    for agent in AGENTS:
        for command in commands:
            if agent.runs(command):
                return agent
    return None


SGR = re.compile(r"\x1b\[([0-9;]*)m")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# Operating-system commands, terminated by BEL or ST. Claude Code wraps every
# URL in an OSC 8 hyperlink, and left in, "\x1b]8;id=...;https://..." reads
# as text: in the preview, and to every pattern above. Never across a line
# break: ghost_lines() numbers raw lines against stripped ones.
OSC = re.compile(r"\x1b\][^\x07\x1b\n]*(?:\x07|\x1b\\)")


def last_match(pattern: re.Pattern, screen: str) -> str:
    """The last time a pattern appears on the screen, which is the most recent
    one. A terminal scrolls: the *first* match is the oldest turn still
    visible, so searching forwards reported a session's timing from something
    it finished several turns ago -- "Cogitated for 26s" while the screen
    plainly said "Cooked for 1m 5s" underneath it.
    """
    found = ""
    for match in pattern.finditer(screen):
        found = match.group(1).strip()
    return found


def strip_ansi(text: str) -> str:
    return ANSI.sub("", OSC.sub("", text))


def ghost_lines(raw: str) -> set[int]:
    """Line numbers whose text is mostly dim.

    Claude Code writes its suggested next prompt into the input box in dim
    grey -- SGR 2 -- exactly where a half-typed message would sit. Stripped of
    colour the two are the same characters, so every suggestion read as an
    unsent draft and the panel said you were the holdup when you were not.

    Dimness is the only thing separating them, so it has to survive the
    capture. Judged per line and by weight, because the "\u276f" that opens the box
    is drawn at normal brightness even when what follows is a ghost.
    """
    ghosts = set()
    for index, line in enumerate(raw.splitlines()):
        dim = False
        faint = solid = 0

        position = 0
        for match in SGR.finditer(line):
            for char in line[position:match.start()]:
                if not char.isspace() and char != "\xa0":
                    if dim:
                        faint += 1
                    else:
                        solid += 1
            position = match.end()
            for code in (match.group(1) or "0").split(";"):
                if code == "2":
                    dim = True
                elif code in ("", "0", "22"):
                    dim = False
        for char in line[position:]:
            if not char.isspace() and char != "\xa0":
                if dim:
                    faint += 1
                else:
                    solid += 1
        if faint and faint > solid:
            ghosts.add(index)
    return ghosts


def latest_words(agent: Agent, screen: str, recap: tuple[int, str]) -> str:
    """What the agent last said, for a chat list's second line.

    The recap when there is one, because it is written to be read cold --
    but only while it is still the newest thing said: a reply drawn below it
    belongs to a later turn the recap knows nothing about.
    """
    at, text = recap
    reply_at, reply = agent.reply(screen)
    if text and at > reply_at:
        return text
    return reply


def classify(screen: str, commands: list[str], raw: str = "") -> dict:
    """Return {state, label, detail, draft, tokens, agent, preview, recap} for
    one session's screen.

    preview is the one line a chat list shows under the title: the current
    step while working, the question while blocked, and otherwise what the
    agent last said (its recap if that is still current). recap is the
    newest "※ recap:" text whatever the state, "" when there is none. Both
    are "" where nothing can be read -- never a guess.
    """
    result = {"state": UNKNOWN, "label": LABELS[UNKNOWN], "detail": "",
              "draft": "", "tokens": "", "agent": "", "preview": "",
              "recap": ""}

    agent = detect(commands)
    if commands and agent is None and all(_transport(c) for c in commands):
        # ssh (or mosh) to another machine: what runs there is only on the
        # screen. Claude on the far side is still a chat.
        agent = _guess(screen)
    if commands and agent is None:
        result["state"] = SHELL
        result["label"] = LABELS[SHELL]
        result["detail"] = ", ".join(dict.fromkeys(commands))
        return result

    if not screen.strip():
        return result

    if agent is None:
        # No pane list came back, so fall back to whichever agent's chrome the
        # screen actually shows. Guessing wrong here is better than reporting
        # UNKNOWN for every session on a host where the pane probe failed.
        agent = _guess(screen)
        if agent is None:
            return result

    result["agent"] = agent.key
    result["tokens"] = agent.tokens(screen)
    recap = agent.recap(screen)
    result["recap"] = recap[1]

    # Order matters. Busy beats everything: a permission prompt from a previous
    # turn can still be on screen above a spinner that has moved on.
    if agent.busy(screen):
        result["state"] = WORKING
        result["label"] = LABELS[WORKING]
        result["detail"] = agent.busy_detail(screen)
        result["preview"] = agent.step(screen)
        return result

    if agent.blocked(screen):
        result["state"] = NEEDS_YOU
        result["label"] = LABELS[NEEDS_YOU]
        result["detail"] = agent.blocked_detail(screen)
        # What it is asking, not the last reply: the reply above a permission
        # prompt is usually the tool call it is asking about, or older still.
        # The request itself when the dialog says it, since the question is
        # the same "Do you want to proceed?" on every one.
        result["preview"] = (agent.permission_preview(screen)
                             or result["detail"])
        return result

    box = agent.input_box(screen, ghost_lines(raw) if raw else set())

    # An unsent draft still wins: that one needs a human, background work does
    # not. But anything running in the background beats "idle".
    if box is not None and not box:
        background = agent.background(screen)
        if background:
            result["state"] = WORKING
            result["label"] = LABELS[WORKING]
            result["detail"] = background
            result["preview"] = background
            return result
    if box is None:
        # The state is unreadable, the transcript may not be: what was last
        # said is still true, and a blank line under a chat says less.
        result["preview"] = latest_words(agent, screen, recap)
        return result

    if box:
        # A draft still means "a human has to do something", but if work is
        # also running in the background, say so -- otherwise you walk into a
        # session expecting it to be idle and find it mid-flight.
        result["state"] = DRAFT
        result["label"] = LABELS[DRAFT]
        # The box alone, apart from the detail: the background suffix ticks
        # on its own, and the draft-settled clock must not restart with it.
        result["draft"] = box
        background = agent.background(screen)
        result["detail"] = f"{box}  ({background})" if background else box
        # What the agent said, not what you are typing: the draft is yours,
        # and it is already in "draft".
        result["preview"] = latest_words(agent, screen, recap)
        return result

    result["state"] = READY
    result["label"] = LABELS[READY]
    result["detail"] = agent.idle_detail(screen)
    result["preview"] = latest_words(agent, screen, recap)
    return result


# Commands that only carry a terminal to another machine.
TRANSPORTS = {"ssh", "mosh", "mosh-client", "autossh", "et", "telnet"}


def _transport(command: str) -> bool:
    return command.rsplit("/", 1)[-1] in TRANSPORTS


# Claude Code's own footer, idle or not: the mode line under the box, or the
# shortcuts hint when no mode is set. Only ever in the last lines.
CLAUDE_FOOTER = re.compile(r"\(shift\+tab to cycle\)|^\s*\? for shortcuts",
                           re.M)


def _guess(screen: str) -> Agent | None:
    """Identify the agent from its chrome, for when the pane list is missing
    or only says ssh."""
    if OpenCode.RULE.search(screen) or "Ask anything" in screen:
        return AGENTS[1]
    for agent in AGENTS:
        if agent.BUSY and agent.BUSY.search(screen):
            return agent
    tail = "\n".join([l for l in screen.splitlines() if l.strip()][-4:])
    if CLAUDE_FOOTER.search(tail):
        return AGENTS[0]
    return None


def needs_attention(state: str) -> bool:
    """The states where a human has to do something before anything moves."""
    return state in (NEEDS_YOU, DRAFT)
