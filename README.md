# conn

Conn is a GTK4 desktop app for keeping track of coding-agent chats -- Claude Code, opencode --
running in `tmux` on many servers, reached over `ssh`. It lists every chat across the hosts in your
`~/.ssh/config`, shows which are working and which are waiting on you, and opens any of them in a
terminal beside the list. There is also an optional iPhone app (`ios/`) and a small agent
(`conn_agent.py`) that runs on an always-on Mac and lets the phone see and answer the same chats.

**Requirements:** Linux, Python 3.11+, GTK4 with PyGObject, VTE (GTK4 build), `tmux` on the
servers, and `ssh`. See [Install](#install) below.

**Status:** a personal project, built for the author's own setup (Omarchy / Hyprland). It works
day to day there, but expect rough edges anywhere else. Source:
<https://github.com/ojm1/conn-public>. MIT licensed (see `LICENSE`).

A window for the servers you keep coding-agent sessions on.
Reads **Claude Code** and **opencode**.

*You have the conn.*

Named for the bridge station: on a ship, the conn is the watch -- whoever has it is answerable for
where everything is and what it is doing. In *The Original Series* that console was the helm; by
*The Next Generation* the same station is Conn.

Installed as `conn` -- the command is `conn`. It was called helm until September 2026, and the
rename is exactly that collision: `helm` is the Kubernetes package manager, it is on the PATH of
everyone who would want this, and the two do entirely unrelated jobs.

It lists every coding-agent chat in `tmux` across every host in your `~/.ssh/config`, newest
first, tells you **which ones are working and which are waiting on you**, says so out loud when one
starts waiting, and opens any of them in a terminal beside the list.

```
  ▌ conn         10 up · 3 dark │ [web-01/claude]
  [ New chat            ctrl-n ]│
  [ Search chats               ]│ ● Restructured the Jobs column header into two rows:
  Project v  Server v  Needs 2  │ ✻ Cooked for 24m 14s
  ★ STARRED                     │ ❯ commit and push both
  1 [W]• Jobs header        3m  │
       web-01 · Draft is up     │
  TODAY                         │
  2 [W]◌ CSV export         1m  │
       web-01 · Running tests   │
  > SERVERS                  4  │
  ?  10 chats · 4 projects      │
```

Requires GTK4 and VTE. Nothing from pip.

## Why

If you run coding agents on remote boxes, the question is never "which server" — it's *"is that chat
still working, or has it been sitting waiting for me for an hour?"*. Nothing answers that from a
distance, so this reads it off the screen.

## Chat states

| Chat row | Mark in the Servers tab | State | Means |
|---|---|---|---|
| blue spinner, blue time | `*` | working | Busy, or running background agents. Leave it. |
| amber dot, amber time | `!` | needs you | Blocked on a permission prompt. |
| pencil | `!` | unsent draft | Text left in the box, never submitted — looks done, isn't. |
| no badge, dimmed | `o` | idle | Empty prompt, waiting. |
| grey `?` | `?` | unknown | The screen could not be read -- never assumed idle. |
| -- | `.` | shell | Not an agent session -- a plain shell. Not a chat; listed in the Servers tab. |

The colours are the theme's, not fixed ones: amber is your Omarchy theme's yellow, working its blue,
and red is kept for hosts that are dark.

**How it works, and its limit.** There is no API for this. `agent_state.py` reads the session's
visible screen from `tmux capture-pane` and matches markers. The states above are shared; the
markers are per-agent, because the two draw nothing alike:

| | Claude Code | opencode |
|---|---|---|
| busy | `esc to interrupt` | `esc interrupt` in the footer |
| blocked | `Do you want to…`, `1. Yes` | `Permission required`, `Allow once / Reject` |
| the input box | between the last two horizontal rules | the `┃` run above the `╹▀▀▀` rule |
| empty box | the `Try "…"` placeholder | the `Ask anything…` placeholder |
| context used | `275.1k tokens` | `7.4K (3%)` in the footer |

Which agent a session is running comes from the pane's command, so a shell is settled without
guessing from pixels. Background agent fleets (`N/M agents done`) are read for Claude Code only —
opencode's equivalent has not been captured yet, so a session of its own is never reported as busy
on that basis.

Anything unrecognised reports **unknown**, never idle: claiming a blocked chat is idle is the one
failure that would make the tool worse than not looking.

Markers are matched **at the start of a line**, past whatever chrome the dialog draws in front of
them -- box bars, carets, indentation. A prompt Claude Code is asking begins its line; the same
words inside a sentence are someone *talking* about a prompt. Quote marks are excluded from that
chrome on purpose: prose puts one in front of the words every time, including at the start of a
wrapped line, and a dialog never does. (A session working on conn reporting itself blocked is how
this was found.)

Everything read off a screen is read **backwards** -- the last match, not the first. A terminal
scrolls, so the earliest "Cogitated for 26s", the earliest prompt and the earliest token count are
all things that happened several turns ago and are merely still visible.

A redesign on either side can break these markers. `tests/test_agent_state.py` runs the reader
against real captured screens — `python3 tests/test_agent_state.py`, no test dependencies — so a
break shows up as a failure rather than as a quietly wrong dashboard. `tests/test_hosts.py` does
the same for the transport: frame parsing, marker forgery, the mount table and `bin/ssh-mount`'s
guards, all against fakes — no display, no network. The window itself is checked by
`tests/test_gui.py`, which needs a display and a local tmux; it points the run at an ssh config
and state files of its own (`CONN_SSH_CONFIG` overrides where `hosts.py` reads `~/.ssh/config` —
ssh itself never sees it), so nothing of yours is probed or rewritten. `tests/test_theming.py` holds the
colours to their contrast floors on every Omarchy theme installed, and checks the terminal palette
against the one Omarchy gives foot — no display.

Adding a third agent means writing its patterns as a class in `agent_state.py`; the callers do not
change.

## Live, not polled

One held-open `ssh` channel per host runs a loop that re-dumps each session's screen and only sends
anything when it changed. Polling pays the network round trip on every check; this pays it once and
then streams. An idle host costs a heartbeat line a second -- which is also how the
panel tells a quiet connection from one that has silently died, and reconnects. A 45-second sweep
covers what the stream doesn't: uptime, disk, mounts, and the session list.

`CONN_NO_WATCH=1` falls back to plain polling.

The far side needs only `tmux` and a POSIX `sh`: the background scripts are piped to `sh`
explicitly rather than handed to the login shell to parse, and the attach command is a single
`exec sh -c` line built to read identically under sh, fish and tcsh -- so a fish or tcsh user on
the remote is fine.

## This machine, too

The first host in the list is `local` -- the laptop the panel is running on. It is not ssh to
yourself: the probe, the watcher and the replies are the same POSIX shell scripts handed straight
to `bash` instead of to `ssh`, so a local row needs no sshd, no key and no round trip. One function
in `hosts.py` knows the difference; nothing else does.

**It reads tmux, not terminals.** A `claude` running in a bare terminal window cannot be read by
anything without attaching to it, and it dies when you close the window -- so it is not listed.
Start local agents the way `ssh-connect` starts remote ones:

```
tmux new -A -s claude
```

and they show up here, survive the window closing, and can be replied to from the panel.

`CONN_NO_LOCAL=1` hides the row. A `Host local` already in `~/.ssh/config` wins: an alias you can
really ssh to beats one the panel invented.

**If conn is itself running inside tmux**, a local session opens in its own window instead of taking
this one. It has to: tmux gives one terminal to one client, so attaching in place detaches the
session conn is running in -- conn disappears, and F12 then closes the window rather than bringing
it back, because the client it detaches is the outer one. Remote sessions are a different tmux
server and still take the terminal as before.

For the same reason the session conn is running in is left out of its own list: attaching to it is
the one attach that cannot go anywhere.

## In the window

The sidebar is a list of **chats**, recent first -- a chat being a tmux session with an agent
running in it. At the top: the name, and the fleet (`10 up · 3 dark`); the **New chat** button;
search; and three filter chips.

- **Sections.** Starred, Today, Yesterday, Earlier this week, then a folded **Older**. Within each,
  newest first. A starred chat is only under Starred.
- **A row** is the project's tile with the state as a badge on its corner, the title and how long
  ago, then the server and what the chat last said: the step it is on while working, the question
  while it waits on you, otherwise the first line of Claude's latest reply (its `※ recap:` when
  that is still current). The star on the right pins it. Idle rows are dimmer. The times tick.
  While Claude works with no todo to name, the preview is the tool it is running ("Running npm
  test", "Reading app.py") or what it last said in the turn -- never its random spinner word. A
  permission prompt previews what it wants ("Wants to run rm -rf build"), a question picker its
  question.
- **Finished while you were elsewhere** is needs you too: a chat conn watched go from working to
  idle wears the amber badge and time, counts in Needs you and says so in a notification, until you
  open it (it becomes the visible session, or you press Enter or click its row). What has been seen
  is kept per machine in `~/.local/state/conn/seen.json`. A chat already idle when conn starts is
  not news.
- **A server that stops answering keeps its chats on the list**, greyed, in their usual place,
  reading "unreachable · last seen 3h ago" (or "connecting" before it has answered this run), so
  a chat does not vanish into a red host you forget to look at. They come from
  `~/.local/state/conn/known-chats.json`: what each server last listed while it answered (titles,
  options, folders -- no screens, previews or task text), so they are there after a restart too.
  They never count as needs you. Opening one does not attach: the right-hand side shows the chat's
  offline page -- its title, "<server> is unreachable", when it was last seen, "Trying again…"
  while conn asks, and a Check again button -- and conn tries it again at once; the menu offers
  only Open and Check again. A server that answers and turns you away says so instead, as its row
  does: "no key" (is the ssh agent unlocked?) or "log in" (ssh does not trust its key yet), where
  the page also offers Log in…, a New session on it to answer ssh's prompt in. If the server comes
  back with the chat while its page is showing, the chat opens by itself -- taking the keyboard and
  the list's highlight only if you left them on the page; back without it, the page says the chat
  has ended, also when you come back to the page from Notes or New chat. When the server
  answers, its live sessions replace the remembered ones, and a chat that ended while it was away
  is gone. Forgetting a server drops what was remembered of it.
- **A terminal already open on a server that goes dark stays**, under a banner: "Offline ·
  <server> is unreachable · last seen … · reconnects when it's back". If its ssh loses the
  connection (ssh's own exit 255, or a signal), the view is kept, scrollback and all, as
  "Disconnected", and attaches again in the same terminal when the server is back and the chat is
  still there -- once per return: one that dies again before the server has been seen dark again
  is let go with a notice, and one that never started (ssh-connect's "press any key to close")
  closes, with its reason. Any other end -- a detach or exit (0), the chat ending -- closes as
  before, even while the server is marked dark: a probe timing out says "down" while the
  terminal's own ssh is fine.
- **Recency is not tmux's activity time**, which Claude's spinner bumps constantly. It is
  `@conn_last_event` on the session: conn stamps it when it first sees a chat (with the newer of
  when the session was created and its last output -- not now, so opening conn does not make every
  old chat look new, and not the creation time alone, so a month-old session used an hour ago is
  under Today) and whenever the chat
  goes idle -> working -> needs you, and back. A spinner turning, a new step, a draft being typed:
  none of those move a chat. The row moves in the same frame; the write goes over ssh behind it.
- **Stars** are `@conn_starred` on the tmux session itself, so they survive a restart and show the
  same on every client. A star that cannot be written goes back, and the footer says why.
- **Filters.** **Project** (from `~/.config/conn/projects.toml`, SPEC §1 -- a server in no project
  is a project of its own, so without the file the list is one project per server), **Server**
  (only the picked project's, each with its chat count or "dark"), and **Needs you**, with a count.
  Picking a project resets the server. While any filter is on, the list says "Only Acme, acme-app"
  with **Clear**, the footer says "3 of 10 chats", and New chat says where it will go -- "New chat
  in Acme", "New chat on acme-app". Filters are remembered between launches
  (`~/.local/state/conn/filters.json`).
- **Title** is your own name for the chat if you gave it one (`@conn_name`: right-click
  **Rename…**, or `F2` on the selected chat; empty puts it back), else the title Claude gives the
  terminal with its status glyph taken off, else `@conn_title`, else the session name.
  **Rename session…** in the same menu still renames the tmux session itself.
- **Projects.** **Move to project ›** on a chat's right-click menu pins it to a project
  (`@conn_project`), or back to **Automatic**, where the folder decides. **Edit projects…** (the
  Project chip's menu, the footer menu, or **Projects…** on a host) opens the Projects window: each
  project's name, letter and colour, and a tick and folder for every server, suggesting the folders
  chats there actually run in. Save writes `projects.toml` whole, keeps the old file as
  `projects.toml.bak` (comments are not kept), leaves keys it does not show -- `agent`, `mode`,
  anything newer -- as they were, and the sidebar picks the change up at once.
- **Plain shells are not chats.** They, and every server, are in the **Servers** tab (`ctrl-2`,
  beside Chats and Notes): each server with a dot, its state in plain words ("up", "no key",
  "new host key", "host key changed", "can't reach", "checking"), its uptime and disk when it is
  up, and how many chats it has, with all its sessions under it. Click a server for a terminal on
  it -- its session called `shell`, made if it is not there. A broken one has its fix on a button:
  **Install my key** (ssh-copy-id), **Trust…** (a terminal where ssh shows the new key's
  fingerprint and you answer), **Check new key…** (a terminal showing the key on file beside the
  one offered now; only a `y` typed there runs `ssh-keygen -R` and connects once to pin the new
  one -- Conn never edits `known_hosts` itself), or **Check again**. Right-click a server for
  everything its menu always offered; **Add server** at the bottom opens the Site Manager. The
  search box searches servers and sessions by name while the tab is showing.

New chat in this phase opens the new-session prompt on the server the filter picks (the first
server of the picked project that answers, or the selected row's server). The full New chat screen
is the next phase.

The bar beside the name is the fleet: green for the servers that answer, red for the ones that are
dark, grey for any not heard from yet -- a fleet out of sight is not a fleet with nothing to do.
What is waiting on you is the Needs you count, and the bar's tooltip.

The foot of the list is the count, the **Hosts** button (the Site Manager), and a menu: Add
server…, Edit projects…, Refresh now, Keys and marks… (`F1`), and Close Conn. The sidebar is a fixed
320px whatever the filters or the footer say, so the terminal beside it never reflows.

## Keys and actions

| | Does |
|---|---|
| `alt-1`..`alt-9` | Open the chat (or session) with that number -- hold `alt` to see the numbers |
| `F2` | Rename the selected chat (empty: back to Claude's title) -- only while the list has the keyboard |
| `ctrl-n` | The New chat screen, on the project and server the filter (or the selected chat) picks; `ctrl-enter` starts, `esc` goes back |
| `ctrl-k` | The palette: jump to any chat, plain shell, note or server, or run an action. `tab` peeks at a session's screen, `ctrl-enter` opens it in a window of its own |
| `ctrl-f` | Search the chat list; `down` moves into the list, `enter` opens the first match, `esc` clears |
| `ctrl-1` / `ctrl-2` / `ctrl-3` | The chat list / the servers / the notes |
| `Super+N` (anywhere) | Quick add (`conn --quick-add`): one line, `tab` changes project, `ctrl-tab` switches Chat / Note, `enter` starts in the background, `ctrl-enter` starts and opens |
| `up` / `down`, `enter` | Move through the list, open the chat |
| `s` | Star or unstar the selected chat -- only while the list has the keyboard |
| `ctrl-tab` / `ctrl-shift-tab` | Next / previous open session |
| `F12` | Back to the list -- arrows move, enter opens |
| `ctrl-shift-w` | Close the view; the session behind it keeps running |
| `F11` | Fullscreen, and back |
| `ctrl-q` | Quit |
| `ctrl-shift-c` / `ctrl-shift-v` | Copy / paste. Plain `ctrl-c` stays the interrupt |
| `ctrl-shift-s` | Copy the whole screen -- no selection needed |
| `ctrl-shift-y` | Copy Claude's last reply, word for word, from its transcript (also in the right-click and ⋯ menus) |
| `ctrl-+` / `ctrl--` / `ctrl-0` | Text bigger, smaller, back to the terminal's own size |
| `ctrl-shift-a` | Select everything on the screen |
| `ctrl-click` a URL | Open it |
| right-click in a session | Copy, paste, the whole screen, Claude's last reply, and every link on it -- including ones tmux wrapped |
| hover a session | A bin appears over its time: kill it, asked first -- there is no undo |
| `ctrl-shift-k` | Kill the selected session |
| `ctrl-shift-r` | Rename its tmux session -- local or remote, nothing running in it is interrupted |
| `ctrl-shift-up` / `ctrl-shift-down` | Move the selected session's server up or down the list |
| right-click a row | Open, star, rename, move to a project, rename the session, kill -- and on a host, watch it live or move it, its projects, new session, files, passwords, edit, forget |
| New chat | The New chat screen (as `ctrl-n`) |
| right-click a note | Start chat, edit, mark done or reopen, copy, delete (it stays in the notes' git history) |
| server icon | Hosts -- the Site Manager: edit a server's settings, rename it, add or forget one, Watch live, and keep its passwords |
| menu icon | Add a server to `~/.ssh/config`, edit projects, refresh every host now (and re-read `projects.toml`), this guide, close |
| `F1` or `?` | The guide: what the marks mean, every key, and who wrote it |

There is no title bar. GTK hides it fullscreen, so nothing that matters could live there anyway --
the actions and Close Conn are in the footer, on screen whatever the window is doing.

Every chat has the number that opens it, counted down the chat list and then, in the Servers
tab, on through the sessions there, so the shortcut is never counted out. The numbers show on the tiles while
`alt` is held and are hidden otherwise, so the list stays quiet. Past nine there is no number
rather than one promising a key that does not exist.

`ctrl-f`, `ctrl-1`, `ctrl-2` and `ctrl-3` are conn's wherever the keyboard is. `ctrl-n` and `ctrl-k` are
conn's only outside a terminal: in a session they stay readline's next-history and kill-line,
nano's next-line and cut, and Claude's kill-to-end. From a terminal use `ctrl-shift-n` and
`ctrl-shift-p` instead (or `F12` first).

## Starting chats, and notes

New chat and quick add both follow SPEC §4: the task goes to `~/.conn/tasks/<slug>.md` on the
server over stdin, and `tmux new-session` runs `claude` on it under your login shell (that is what
finds `~/.local/bin/claude`), with `@conn_project`, `@conn_title`, `@conn_task`, `@conn_source`
and `@conn_last_event` set. When claude exits the session drops to a shell rather than vanishing.
"Continue" runs `claude --continue` in that folder instead.

Every note belongs to a project. `ctrl-3` (or Notes over the list) puts the projects in the
sidebar, with how many open notes each has, and the notes themselves where the terminal usually
goes: a writing box at the top (Enter is a new line, `ctrl-enter` saves), then the notes as cards.
Click a note's text or its pencil to edit it in place; its `⋯` menu (or a right-click) has Start
chat, Mark done, Copy and Delete. `ctrl-1`, or opening any chat, puts back what was showing.

Notes live in `~/conn-notes/<project>/` on the mini, one markdown file each, committed to git
after every change. Nothing is installed there: conn sends `notes.py` to the mini's `python3`
over ssh. Offline, a new note waits in `~/.config/conn/outbox/` and goes with the next call that
gets through. Set `CONN_NOTES_HOST` to keep them somewhere else (`local` for this laptop).

Quick add needs a window rule so it floats over everything: match the title `conn quick add` in
your Hyprland window rules, and bind a key to it in your keybindings.

## After a reboot

tmux keeps sessions in a server process, so a reboot takes every local session with it. Two
pieces bring them back:

- `conn-snapshot.timer` (a user timer, in `systemd/`) runs `conn --snapshot-sessions` every minute:
  each local session's windows and folders, and the Claude conversation in each pane, read from
  Claude Code's own `~/.claude/sessions/<pid>.json`. The list is
  `~/.local/state/conn/local-sessions.json`; it is never written while no tmux server runs, so the
  one from just before shutdown survives.
- Hyprland's autostart runs `conn --restore-sessions`: once per boot, from a list taken in an
  earlier boot, it recreates each missing session under the same name and folder. A Claude pane
  runs `claude --resume <id>` (the exact conversation -- `--continue` would reopen the newest one in
  `~` five times over) with its permission mode and model; anything else comes back as a shell.

What Claude was in the middle of is lost: it comes back idle with its history, and Conn's header
says "restored after reboot" until there is something newer. A session killed by hand drops out of
the next snapshot and stays gone. `conn --restore-sessions --force` redoes it in the same boot.

    cp systemd/conn-snapshot.* ~/.config/systemd/user/
    systemctl --user enable --now conn-snapshot.timer
    # and in ~/.config/hypr/autostart.lua:
    o.launch_on_start("conn --restore-sessions")

## It tells you

The panel is honest about who is waiting on you, but only to someone looking at it -- and the thing
worth knowing is exactly that a chat has been sitting there while you did something else. So it
says so: a desktop notification, once, and not again until the session has been something else in
between. Clicking it opens that session -- starting conn first if it has exited in the meantime,
which is what the D-Bus service file in the install section is for.

**Needs you** is announced the moment it happens -- nothing moves in that chat until you answer.
**An unsent draft is not**, because typing is a draft too: the box holds text from the first
keystroke, so announcing the state itself interrupted whoever was at the keyboard, about the
sentence they were in the middle of. A draft is announced once it has sat **unchanged for two
minutes**; every edit puts the clock back to the start.

States that were already true when conn started are on screen already, and are not announced.

### Selecting text an agent is sitting on

Claude Code and opencode both turn on mouse reporting, which means the drag never reaches the
terminal -- the agent gets it. **Hold shift while dragging** and VTE takes the mouse back; that
override is hardcoded in VTE and there is no setting that turns an application's mouse reporting
off. With nothing selected, copy is a no-op, so the footer says **nothing selected -- hold shift
while you drag** rather than leaving you to wonder whether the key is bound. A copy that works says
so too, with the number of characters, because the alternative is finding out at the other end.

`ctrl-shift-s` copies the **whole screen** and needs no selection at all -- conn already holds that
text, it is the same capture the state is read from.

`ctrl-shift-y` (or **Copy Claude's last reply** in the right-click menu and the chat's ⋯ menu)
copies Claude's **last finished reply exactly as it wrote it** -- not as the screen wrapped it. Claude
Code writes every conversation to `~/.claude/projects/*/<session id>.jsonl`; conn finds the claude
running in the session's pane, reads the tail of that file on the server (plain `sh`, `tail` and
`grep` -- nothing to install there) and takes the text of the last message that was not on its way
to a tool call. While Claude is still working, that is the previous answer. When there is no
transcript to read the copy comes from the screen instead, and the footer says so, since that one
may be cut short. The phone's chat view gets the same text from the agent.

## When it has been replaced underneath it

`cp *.py ~/.local/share/conn/app/` does not touch the window already running: it goes on running
the code it started with, and a feature added an hour ago is simply not there. So the footer says
**restart to update** once the files on disk differ from the ones in memory, and clicking it
replaces the process with the installed one. The sessions are tmux and outlive it -- the views
close and come straight back.

## What it did about the network

After a wake or a change of network the ssh connections from before are dead without knowing it,
and conn reconnects on its own (closing stale ControlMasters, then asking every host again). When
that goes wrong it should not take a guess to find out why, so the window keeps a record:

    ~/.local/state/conn/conn.log        conn --log      # the last 200 lines (conn --log 1000)

One line per event, with a millisecond timestamp and the thread it came from: conn starting and
exiting, a detected sleep and how long, the local addresses changing, each reconnect (whether the
route and the jump host's ssh port came back, and how long that took; every ControlMaster socket
checked and what became of it -- alive, or dead and closed with `ssh -O exit` or killed), each host
changing state with the probe's time and a short reason, a host that stays down for a new reason,
every host's answer to the sweep that follows a reconnect, probes slower than 10s, every stream
connecting, connected, dropping (end of stream, silent past 45s, or not starting) with how long it
lived and the wait before the next try, and terminals disconnected, reattached or let go. A host
flapping writes one line of each kind per 30 seconds and a "(repeated N times)" summary.

It never holds chat text, screens, previews, titles, notes or secrets -- host and session names,
states, exit codes, addresses and times only -- and the ssh error text in it is stripped of control
characters and cut at 200. It rotates at about 1 MB, keeping three old files, every one of them
readable by you alone (0600). Only the running window writes it: `conn --check` and the other
one-shot commands do not, a second `conn` that just hands over to the open window does not, and
neither does the test suite.
`CONN_LOG=<path>` puts it somewhere else.

## The one password

conn never sees it. Your ssh key is unlocked once -- by the desktop keyring at login -- and every
`ssh` it spawns rides the agent, which is why no host prompts you. If the agent is holding nothing,
the foot of the list says so and offers to run `ssh-add` in a terminal of its own: the passphrase
goes from your keyboard to `ssh-add`, never through conn.

Right-click a host for **passwords and keys** -- whatever else you keep for it: a database password,
an API key. They live in the desktop keyring, which the same login password already unlocks, so
"one password for all of it" is the arrangement that exists rather than a thing to build. conn
stores nothing itself: a value is fetched when you press Show, masked again on Hide, and a copy is
wiped off the clipboard after thirty seconds.

## The Site Manager

The server icon at the foot of the list -- or **Edit...** on a right-clicked host -- opens the Site Manager:
the servers `~/.ssh/config` names down one side, and everything you can change about the selected
one down the other. Its HostName, User, Port and IdentityFile, prefilled from the block and showing
what ssh falls back to where the block is silent; **Connect**, which is Direct or *through* another
of your servers (ssh's `ProxyJump`) -- for a server that only lets in a whitelisted IP, pick the
machine that sits on that IP and conn hops through it. Default follows
**All servers connect**, at the top of the manager's list: Direct, or through one of your servers
for all of them at once (a `Host * !via` block above the `Host *` defaults), so moving everything
to a different jump host is one pick; the aliases it shares a `Host` line with, because
an edit reaches all of them; and the same passwords and keys. **Save** writes the fields back --
refusing an unsafe value rather than quoting it, since a config directive can run a command --
**Rename...** relabels the server and carries its keyring secrets, Watch live and place in the list across
with it, and **Forget...** takes it out. **Move up** / **Move down** reorder the servers here too --
the same order the Servers tab reads. **Watch live** decides how the server is watched: ticked
keeps a live stream open to it, so its chats update within a second or two; unticked checks it
every five minutes instead; the Servers tab lists it either way. Its chats are in the
chat list and notify you either way -- just later. (It was the host star, and is still stored in
`~/.local/state/conn/starred`; a star on a *chat* is a different thing, kept on the session.) Every write keeps the old config beside the new one. It
is not modal: the list behind it updates as you go. **Close** or **Escape** shuts it.

## First contact

The background probe never accepts a host key, and it never prompts -- it runs ssh with
`BatchMode=yes` and `StrictHostKeyChecking=yes`. So a server you just added, whose key is not yet in
`known_hosts`, does not pop a login or a fingerprint box at you from the background; it shows
**log in** in the accent colour -- not a red **down**, because it is new, not broken -- with the fix
under the pointer. Open a session on it once (right-click the host -> **New session**): ssh itself
shows the fingerprint and asks, then takes your password or key. Every probe after that rides what
you accepted, and the row goes green. An unattended loop that accepted whatever key the network
offered would pin a man-in-the-middle before you had consciously connected at all; the one trust
decision stays yours, in a real terminal, where it belongs.

## Replying

You type into the session. It is a real terminal on a real pty -- not a text box that builds a
command out of what you wrote -- so quotes, `$VAR`, backticks and semicolons are just characters,
and there is no layer left for them to be a command in.

The panel it grew out of could not do that: attaching took the whole screen, so it sent replies as
tmux buffers to avoid interpolating them into a command line. Opening a session next to the list
made the safest version of that feature the same thing as not having it. (`hosts.send_text` is in
the history if a reply-without-opening is ever wanted again.)

## Install

Requires Python 3.11+, GTK4 and VTE locally, and `tmux` on the hosts you connect to.

```bash
sudo pacman -S gtk4 vte4 python-gobject
mkdir -p ~/.local/share/conn/app
cp *.py ~/.local/share/conn/app/
cp bin/* ~/.local/bin/
conn --check      # probe every host once and print what it found
```

Nothing from pip, and no virtualenv: GTK, VTE and PyGObject are system packages, and a venv sealed
off from them cannot see GTK at all.

For a launcher entry with its own icon:

```bash
cp org.omarchy.conn.desktop ~/.local/share/applications/
install -Dm644 icons/org.omarchy.conn.svg ~/.local/share/icons/hicolor/scalable/apps/org.omarchy.conn.svg
for s in 48 64 128 256; do
  rsvg-convert -w $s -h $s icons/org.omarchy.conn.svg \
    -o ~/.local/share/icons/hicolor/${s}x${s}/apps/org.omarchy.conn.png
done
gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor
```

The icon is named for the app id, so the window picks it up for the taskbar too. It is a
window now, so starting it through a TUI wrapper (`omarchy-launch-tui conn`, or a terminal binding) leaves an empty
terminal sitting beside the real one -- the terminal is hosting a process that no longer draws
anything in it.

And the D-Bus service file, which is what lets the session bus *start* conn: a notification
clicked after conn has exited, and `conn --notify` with no conn running, both land on the bus name
-- `DBusActivatable=true` in the desktop entry only names it. The `Exec` line has to be an
absolute path, because the bus does no PATH lookup, hence the sed:

```bash
mkdir -p ~/.local/share/dbus-1/services
sed "s|/home/you|$HOME|" org.omarchy.conn.service \
  > ~/.local/share/dbus-1/services/org.omarchy.conn.service
```

### A dev copy beside it

To try a change without giving up the conn you are working in, install the checkout as a second,
separate copy and run `conn-dev`:

```bash
mkdir -p ~/.local/share/conn-dev/app && cp *.py ~/.local/share/conn-dev/app/ && install -m755 bin/conn-dev ~/.local/bin/ && cp org.omarchy.conn.dev.desktop ~/.local/share/applications/
```

The `.desktop` file puts "Conn dev" in the launcher (Super+Space) beside Conn, and names the
`org.omarchy.conn.dev` window in the bar and switcher, with conn's icon.

`conn-dev` is conn with `CONN_CHANNEL=dev` (all of it is in `paths.py`):

- its own application id, `org.omarchy.conn.dev`, so it opens a second window rather than handing
  over to the running conn, and Hyprland's launch-or-focus for conn never grabs it;
- its own state in `~/.local/state/conn-dev` -- started, the first time, from a copy of conn's
  stars, order, filters and seen chats, then separate -- and its own `conn.log` there;
- "dev" in yellow beside the name, and "conn dev" as the window title;
- no desktop notifications, since the stable conn is already sending them for the same chats
  (`CONN_DEV_NOTIFY=1 conn-dev` turns them on);
- while the stable conn is running, it leaves the turn stamps (`@conn_last_event`) and the Haiku
  chat titles to it, so the two never write the same option twice. It does not even queue titles
  meanwhile, and takes over only after the stable conn has been gone 15 seconds -- a restart to
  update is a blink, not a quit. What you do in it -- a star, a rename, a project, a reply -- it
  writes, as the stable one does;
- it keeps `CONN_CHANNEL` to itself: nothing it starts (terminals, ssh, a tmux server) inherits it,
  and `bin/conn` clears it anyway, so a `conn` typed anywhere is always the stable one;
- restart-to-update re-runs the dev install, never the stable one.

Shared on purpose: `~/.config/conn` (projects, the notes outbox -- a flush lock keeps the two
from sending a queued note twice) and the reboot snapshot that `--restore-sessions` reads. Leave
`--snapshot-sessions`/`--restore-sessions` to the stable `conn`. Updating the dev copy is the same
`cp` again, then restart it from its window.

Colours follow the [Omarchy](https://omarchy.org) desktop theme when present, and fall back to a
built-in palette otherwise. `CONN_THEME=light|dark` forces it. Read once, at startup: a theme
changed while conn is running shows up on the next start -- the sessions are tmux, so restarting
costs nothing but the views.

A theme's colours are used as it has them wherever they can be read. Secondary text is Omarchy's
own recipe for it (the foreground mixed a third of the way to the background) rather than the
theme's `muted`, which is a border shade; any text colour that still falls short of 4.5:1 on the
surfaces it lands on -- 3:1 for the red, yellow and green state marks -- is made lighter or darker
in its own hue until it clears. The session terminals get the theme's sixteen ANSI colours, the
same ones foot does, with bright black lifted the same way, since that is what dim text is written
in.

## The font is the one you already chose

A session inside conn should be the size of the terminals beside it, so the font is not conn's to
pick: it is read out of the config of the terminal this machine actually has -- foot, ghostty,
alacritty or kitty, first one whose config is readable **and** whose binary is installed. Only if
none is does it fall back to `monospace 11`.

`CONN_FONT="JetBrainsMono Nerd Font 11"` overrides that outright.

`ctrl-+`, `ctrl--` and `ctrl-0` zoom, the way a terminal does -- a multiplier on that font rather
than a second opinion about it, applied to every session at once and remembered in
`~/.local/state/conn/zoom`. Worth knowing if it still looks off: your terminal may scale points by
the monitor's DPI where GTK uses the desktop's text-scaling factor, so the same "9" can land a
little different. That is what the zoom is for.

## One window

The list is a sidebar and the session opens beside it, in a real terminal widget -- VTE, the one
GNOME Terminal and Ptyxis are built on. Opening a chat costs you nothing you were already looking
at.

It used to throw a new window at the compositor per session, which is not a layout: on Hyprland
those landed on whichever workspace happened to be active, at whatever size dwindle felt like,
while the panel sat on another workspace entirely.

tmux is still doing the real work on the far side -- it is what makes a session survive the window
closing. You just stop seeing it as a window of its own. Each session names itself in tmux's status
bar (`[web-01/claude]`), which is how you know which one you are typing into.

conn itself has no title bar: GTK hides it when a window goes fullscreen, so nothing that matters
can live there.

## Licence

MIT.

## Notes in Nextcloud

`notesync.py` keeps `~/conn-notes` on the notes host and a folder in
Nextcloud in step, both ways, every minute (on a Mac, run it from a LaunchAgent, so it can read the app password
from the login Keychain). Edits come back as git commits.
A note edited on both sides keeps both: yours at the path, theirs beside it
as `<id>-nextcloud`. A file made in Nextcloud with a plain name is turned
into a note once it has sat for five minutes. Add `--dry-run` to
see what a run would do; `notesync.py --login` gets a new app
password by Nextcloud's login flow, straight into the Keychain.
