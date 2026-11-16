# Conn for iPhone and iPad (v1: M1 to M3)

A client of conn-agent's API (`agent_api.py`, SPEC §6). The app never SSHes
anywhere: the agent on the Mac mini watches every server, and the app reads
and acts through it.

What is here, by milestone:

- **M1:** pairing, the tab bar (Chats with a needs-you badge, Notes, Hosts),
  the chat list (fleet line, search, Needs you / Project / Server filters,
  Starred / Today / Yesterday / Earlier, swipe to star, pull to refresh,
  inline Allow once / Deny), and the read-only Hosts list.
- **M2:** the chat view: state pill, title, host and waiting time, the task as
  a bubble, Claude's latest reply as a card (line breaks kept, bold rendered,
  links as "Open preview"), permission options as buttons, the reply bar,
  Interrupt while working, star, Save recap to notes. Live from
  `chat.updated` events.
- **M3:** the New sheet (Chat or Note, project picker and recent chips, text,
  Project notes, "Start on <server>") and Notes (project chips with counts,
  cards, Start chat, swipe to mark done or delete, the jot box).

Not yet: push (M4), the terminal (M5; its button is there, disabled), QR
scanning in the app, Face ID, Geist and JetBrains Mono (system fonts stand
in; see `UI/Theme.swift`).

## How it reaches the agent

The agent listens on the local network, on the mini at port 8742,
with its own self-signed certificate, so the phone has to be on the
same network.

- **Pairing:** on the Mac mini, `python3.14 conn_agent.py pair` prints a code
  (works once, for 5 minutes), the port, and the certificate's sha256. Type
  the code into the app. Typing the first few characters of the sha256 too
  makes the first connection checked; leave it empty to trust on first use.
  The desktop's `POST /pair/codes` makes codes the same way, and a
  `conn://pair?url=https://<ip>:8742&code=<code>` link (a QR code scanned
  with the Camera app) fills the pairing screen in.
- **Pinning:** the certificate the agent showed at pairing is pinned by the
  SHA-256 of its DER. Any other certificate is refused in the handshake, and
  the app says "The Mac mini's certificate changed" with Pair again.
- **Keychain:** the token, device id, address and fingerprint are one
  Keychain item, `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`.
- **No connection-lost dialogs:** when the agent cannot be reached the
  screens show one inline line, "Can't reach the Mac mini", with Retry. On
  foreground the app refetches and resumes `/events` quietly.
- **Nothing on disk:** URLSession is ephemeral (no cache, no cookies), chat
  and note text lives only in memory, and nothing is logged.

## Build it

On the Mac mini:

```bash
brew install xcodegen
cd ios
xcodegen            # writes Conn.xcodeproj and Conn/Info.plist
open Conn.xcodeproj
```

`Conn.xcodeproj` and `Conn/Info.plist` are generated and gitignored; the
desktop half of conn is developed on Linux, so `project.yml` is the
project. There are no packages. Set your team under Signing, then run on a
device: the first request makes iOS ask for Local Network access, which the
app needs.

## Layout

```
Conn/
  ConnApp.swift          app entry, bar colours
  API/ConnAPI.swift      every route in SPEC §6, pinning, SSE
  API/Models.swift       Codable models matching agent_api.py's JSON
  API/PairingStore.swift the Keychain item
  Model/AppModel.swift   state, refresh, events, actions
  Support/Format.swift   times as the mockups write them
  UI/                    Theme (SPEC §8), Components, Pairing, Chats, Chat,
                         New, Notes, Hosts, Root
```

## Written without a compiler

This was written on Linux, with no Swift toolchain, against iOS 17 APIs only.
Expect the first Xcode build to want small fixes; none should touch the
API contract in `API/`.
