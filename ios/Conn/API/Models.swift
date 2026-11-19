import Foundation

// The JSON conn-agent serves (agent_api.py), field for field. Decoding is
// forgiving about missing keys -- an older or newer agent should still show
// a list, not an error -- but every name here is the agent's own.
//
// Times are unix seconds. The agent sends them as integers; they decode as
// Double so a float never breaks a list.

// MARK: - Chats

struct PermissionOption: Codable, Hashable, Identifiable {
    let key: String
    let label: String

    var id: String { key }

    /// "No" options deny. Claude Code labels them "No", "No, and tell Claude
    /// what to do differently" and so on.
    var isDeny: Bool { label.lowercased().hasPrefix("no") }
}

struct Permission: Codable, Hashable {
    let command: String
    let options: [PermissionOption]

    /// The first option that is not a "No": "Yes" in Claude Code's prompts.
    var allowOnce: PermissionOption? { options.first(where: { !$0.isDeny }) }
    /// The option whose label starts with "No".
    var deny: PermissionOption? { options.first(where: { $0.isDeny }) }

    init(command: String, options: [PermissionOption]) {
        self.command = command
        self.options = options
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        command = try c.decodeIfPresent(String.self, forKey: .command) ?? ""
        options = try c.decodeIfPresent([PermissionOption].self, forKey: .options) ?? []
    }
}

enum ChatState: String, Codable, Hashable {
    case needsYou = "needs_you"
    case working
    case idle

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = ChatState(rawValue: raw) ?? .idle
    }
}

/// `Chat` in SPEC §6, plus the agent's `offline` (a dark server's chats stay
/// listed, as they were, until it answers again).
struct Chat: Codable, Hashable, Identifiable {
    let id: String
    let host: String
    let session: String
    let project: String
    let title: String
    let state: ChatState
    let lastEvent: TimeInterval
    let preview: String
    /// What it is on now (Conn's auto-title), under the name that holds
    /// still; "" when there is none.
    let doing: String
    let starred: Bool
    let offline: Bool
    let permission: Permission?

    var ref: ChatRef { ChatRef(host: host, session: session) }

    enum CodingKeys: String, CodingKey {
        case id, host, session, project, title, state
        case lastEvent = "last_event"
        case preview, doing, starred, offline, permission
    }

    init(id: String, host: String, session: String, project: String,
         title: String, state: ChatState, lastEvent: TimeInterval,
         preview: String, doing: String = "", starred: Bool, offline: Bool,
         permission: Permission?) {
        self.id = id
        self.host = host
        self.session = session
        self.project = project
        self.title = title
        self.state = state
        self.lastEvent = lastEvent
        self.preview = preview
        self.doing = doing
        self.starred = starred
        self.offline = offline
        self.permission = permission
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let host = try c.decodeIfPresent(String.self, forKey: .host) ?? ""
        let session = try c.decodeIfPresent(String.self, forKey: .session) ?? ""
        self.host = host
        self.session = session
        id = try c.decodeIfPresent(String.self, forKey: .id) ?? "\(host)/\(session)"
        project = try c.decodeIfPresent(String.self, forKey: .project) ?? host
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? session
        state = try c.decodeIfPresent(ChatState.self, forKey: .state) ?? .idle
        lastEvent = try c.decodeIfPresent(Double.self, forKey: .lastEvent) ?? 0
        preview = try c.decodeIfPresent(String.self, forKey: .preview) ?? ""
        doing = try c.decodeIfPresent(String.self, forKey: .doing) ?? ""
        starred = try c.decodeIfPresent(Bool.self, forKey: .starred) ?? false
        offline = try c.decodeIfPresent(Bool.self, forKey: .offline) ?? false
        permission = try c.decodeIfPresent(Permission.self, forKey: .permission)
    }

    func with(starred: Bool) -> Chat {
        Chat(id: id, host: host, session: session, project: project, title: title,
             state: state, lastEvent: lastEvent, preview: preview, doing: doing,
             starred: starred, offline: offline, permission: permission)
    }
}

/// One message of the conversation, from Claude's transcript: yours or
/// Claude's answer, `at` in unix seconds (0 when unknown).
struct HistoryItem: Decodable, Hashable {
    let role: String
    let text: String
    let at: TimeInterval

    var isYou: Bool { role == "you" }

    enum CodingKeys: String, CodingKey { case role, text, at }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        role = try c.decodeIfPresent(String.self, forKey: .role) ?? "claude"
        text = try c.decodeIfPresent(String.self, forKey: .text) ?? ""
        at = try c.decodeIfPresent(Double.self, forKey: .at) ?? 0
    }
}

/// `ChatDetail` = Chat plus task, latest_reply, recap, started, windows,
/// and history (the conversation's end; empty from an older agent or a chat
/// with no transcript).
struct ChatDetail: Decodable, Hashable {
    let chat: Chat
    let task: String
    let latestReply: String
    let recap: String
    let started: TimeInterval
    let windows: [String]
    let history: [HistoryItem]

    enum CodingKeys: String, CodingKey {
        case task
        case latestReply = "latest_reply"
        case recap, started, windows, history
    }

    init(from decoder: Decoder) throws {
        chat = try Chat(from: decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        task = try c.decodeIfPresent(String.self, forKey: .task) ?? ""
        latestReply = try c.decodeIfPresent(String.self, forKey: .latestReply) ?? ""
        recap = try c.decodeIfPresent(String.self, forKey: .recap) ?? ""
        started = try c.decodeIfPresent(Double.self, forKey: .started) ?? 0
        windows = try c.decodeIfPresent([String].self, forKey: .windows) ?? []
        history = try c.decodeIfPresent([HistoryItem].self, forKey: .history) ?? []
    }
}

/// A chat's address: `<host>/<session>`.
struct ChatRef: Hashable, Codable {
    let host: String
    let session: String

    var id: String { "\(host)/\(session)" }

    init(host: String, session: String) {
        self.host = host
        self.session = session
    }

    init?(id: String) {
        guard let slash = id.firstIndex(of: "/") else { return nil }
        let host = String(id[id.startIndex..<slash])
        let session = String(id[id.index(after: slash)...])
        guard !host.isEmpty, !session.isEmpty else { return nil }
        self.host = host
        self.session = session
    }
}

// MARK: - Projects and hosts

/// GET /projects: SPEC §1 plus `bg`, `implicit` and `server_status`.
///
/// `servers` is a JSON object, so its order does not survive decoding;
/// `server_order` carries it (projects.toml order, the first is the default).
struct Project: Decodable, Hashable, Identifiable {
    let id: String
    let name: String
    let letter: String
    let color: String
    let bg: String
    let servers: [String: String]
    let agent: String
    let mode: String
    let implicit: Bool
    let serverStatus: [String: String]
    let serverOrder: [String]

    enum CodingKeys: String, CodingKey {
        case id, name, letter, color, bg, servers, agent, mode, implicit
        case serverStatus = "server_status"
        case serverOrder = "server_order"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        name = try c.decodeIfPresent(String.self, forKey: .name) ?? id
        let letter = try c.decodeIfPresent(String.self, forKey: .letter) ?? ""
        self.letter = letter.isEmpty ? String(name.prefix(1)).uppercased() : letter
        color = try c.decodeIfPresent(String.self, forKey: .color) ?? ""
        bg = try c.decodeIfPresent(String.self, forKey: .bg) ?? ""
        servers = try c.decodeIfPresent([String: String].self, forKey: .servers) ?? [:]
        agent = try c.decodeIfPresent(String.self, forKey: .agent) ?? ""
        mode = try c.decodeIfPresent(String.self, forKey: .mode) ?? ""
        implicit = try c.decodeIfPresent(Bool.self, forKey: .implicit) ?? false
        serverStatus = try c.decodeIfPresent([String: String].self, forKey: .serverStatus) ?? [:]
        serverOrder = try c.decodeIfPresent([String].self, forKey: .serverOrder) ?? []
    }

    /// The project's servers in projects.toml order (sorted if the agent did
    /// not say), only those it actually lists.
    var serverNames: [String] {
        let ordered = serverOrder.filter { servers[$0] != nil }
        return ordered.count == servers.count ? ordered : servers.keys.sorted()
    }

    /// The server a new chat goes to unless you pick one: the first that is
    /// up, else the first.
    var defaultServer: String? {
        let names = serverNames
        return names.first(where: { serverStatus[$0] == "up" }) ?? names.first
    }
}

enum HostState: String, Hashable {
    case up
    case dark
    case needsLogin = "needs_login"
}

/// GET /hosts row and the `host.updated` event.
struct HostStatus: Decodable, Hashable, Identifiable {
    let host: String
    let status: String
    let detail: String
    let chats: Int

    var id: String { host }
    var state: HostState { HostState(rawValue: status) ?? .dark }

    enum CodingKeys: String, CodingKey { case host, status, detail, chats }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        host = try c.decode(String.self, forKey: .host)
        status = try c.decodeIfPresent(String.self, forKey: .status) ?? "dark"
        detail = try c.decodeIfPresent(String.self, forKey: .detail) ?? ""
        chats = try c.decodeIfPresent(Int.self, forKey: .chats) ?? 0
    }
}

struct Health: Decodable, Hashable {
    let ok: Bool
    let version: String
    let hostsUp: Int
    let hostsDark: Int

    enum CodingKeys: String, CodingKey {
        case ok, version
        case hostsUp = "hosts_up"
        case hostsDark = "hosts_dark"
    }
}

// MARK: - Notes

/// `Note` = the front matter fields plus "text". Every value is a string;
/// `created` and `updated` are ISO 8601 with an offset.
struct Note: Decodable, Hashable, Identifiable {
    let id: String
    let project: String
    let created: String
    let updated: String
    let source: String
    let chat: String
    let status: String
    let kind: String
    let text: String

    enum CodingKeys: String, CodingKey {
        case id, project, created, updated, source, chat, status, kind, text
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        project = try c.decodeIfPresent(String.self, forKey: .project) ?? ""
        created = try c.decodeIfPresent(String.self, forKey: .created) ?? ""
        updated = try c.decodeIfPresent(String.self, forKey: .updated) ?? ""
        source = try c.decodeIfPresent(String.self, forKey: .source) ?? ""
        chat = try c.decodeIfPresent(String.self, forKey: .chat) ?? ""
        status = try c.decodeIfPresent(String.self, forKey: .status) ?? "open"
        kind = try c.decodeIfPresent(String.self, forKey: .kind) ?? "note"
        text = try c.decodeIfPresent(String.self, forKey: .text) ?? ""
    }

    var isRecap: Bool { kind == "recap" }
    var isOpen: Bool { status == "open" || status.isEmpty }
    var isDone: Bool { status == "done" }
    var createdDate: Date? { Format.isoDate(created) }
}

/// The `note.updated` event carries a Note, or `{ id, deleted: true }`.
struct NoteDeletion: Decodable {
    let id: String
    let deleted: Bool?
}

// MARK: - Devices and pairing

struct PairResponse: Decodable {
    let token: String
    let deviceId: String

    enum CodingKeys: String, CodingKey {
        case token
        case deviceId = "device_id"
    }
}

struct PairCode: Decodable {
    let code: String
    let expires: TimeInterval
}

struct Device: Decodable, Hashable, Identifiable {
    let id: String
    let name: String
    let kind: String
    let lastSeen: TimeInterval

    enum CodingKeys: String, CodingKey {
        case id, name, kind
        case lastSeen = "last_seen"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        name = try c.decodeIfPresent(String.self, forKey: .name) ?? ""
        kind = try c.decodeIfPresent(String.self, forKey: .kind) ?? ""
        lastSeen = try c.decodeIfPresent(Double.self, forKey: .lastSeen) ?? 0
    }
}

/// `{ "error": "..." }`, the body of every 4xx and 5xx.
struct ErrorBody: Decodable {
    let error: String
}

// MARK: - Events

/// One server-sent event from GET /events.
enum ServerEvent {
    /// The stream is open (the agent's ": hello" arrived).
    case connected
    case chatUpdated(Chat)
    case chatRemoved(id: String)
    case noteUpdated(Note)
    case noteDeleted(id: String)
    case hostUpdated(HostStatus)
}
