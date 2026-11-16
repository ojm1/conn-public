import Foundation
import SwiftUI
import UIKit

enum AppTab: Hashable {
    case chats, notes, hosts
}

/// How the app is getting on with the agent. Never a dialog: the screens
/// show one quiet line for anything but `.ok`.
enum LinkState: Equatable {
    case connecting
    case ok
    case unreachable
    case unpaired
    case certificateChanged
}

enum ComposeKind: String, Hashable {
    case chat, note
}

/// A request to show the New sheet.
struct ComposeRequest: Identifiable, Equatable {
    let id = UUID()
    var kind: ComposeKind
    var project: String?
}

/// Everything the screens show, kept current by GET requests on foreground
/// and by /events while the app is open.
///
/// Chat and note text lives only in memory here: nothing is written to
/// disk, and nothing is logged.
@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var pairing: Pairing?
    @Published private(set) var chats: [Chat] = []
    @Published private(set) var hosts: [HostStatus] = []
    @Published private(set) var projects: [Project] = []
    @Published private(set) var notes: [Note] = []
    @Published private(set) var loaded = false
    @Published var link: LinkState = .connecting
    @Published var tab: AppTab = .chats
    /// A chat to open in the Chats tab (from New, Notes or a link).
    @Published var openRequest: ChatRef?
    @Published var compose: ComposeRequest?
    /// The Notes tab's project chip; nil is All.
    @Published var notesProject: String?
    /// Pairing details from a conn://pair link, for the pairing screen.
    @Published var pairPrefill: PairPrefill?

    private(set) var api: ConnAPI?
    private var eventsTask: Task<Void, Never>?
    private var lastRefresh: Date = .distantPast
    private var refreshing = false

    struct PairPrefill: Equatable {
        var host: String
        var port: Int
        var code: String
    }

    init() {
        if let saved = PairingStore.load() {
            pairing = saved
            api = ConnAPI(pairing: saved)
        }
    }

    // MARK: Derived

    var needsYouCount: Int { chats.filter { $0.state == .needsYou }.count }
    var hostsUp: Int { hosts.filter { $0.state == .up }.count }
    var hostsDark: Int { hosts.count - hostsUp }

    func project(_ id: String) -> Project? { projects.first(where: { $0.id == id }) }
    func chat(_ id: String) -> Chat? { chats.first(where: { $0.id == id }) }
    func host(_ name: String) -> HostStatus? { hosts.first(where: { $0.host == name }) }

    /// Projects with the most recently active first, then the rest in the
    /// agent's order.
    var recentProjects: [Project] {
        var latest: [String: TimeInterval] = [:]
        for chat in chats {
            latest[chat.project] = max(latest[chat.project] ?? 0, chat.lastEvent)
        }
        let order = Dictionary(projects.enumerated().map { ($0.element.id, $0.offset) },
                               uniquingKeysWith: { first, _ in first })
        return projects.sorted { a, b in
            let la = latest[a.id] ?? 0
            let lb = latest[b.id] ?? 0
            if la != lb { return la > lb }
            return (order[a.id] ?? 0) < (order[b.id] ?? 0)
        }
    }

    // MARK: Pairing

    func paired(_ new: Pairing) {
        PairingStore.save(new)
        pairing = new
        api = ConnAPI(pairing: new)
        link = .connecting
        pairPrefill = nil
        lastRefresh = .distantPast
        Task {
            await refresh()
            startEvents()
        }
    }

    /// Forget the pairing on this phone; with `revoke`, ask the agent to
    /// drop this device's token first (best effort).
    func unpair(revoke: Bool) async {
        if revoke, let api = api, let pairing = pairing {
            try? await api.deleteDevice(id: pairing.deviceId)
        }
        stopEvents()
        PairingStore.clear()
        api = nil
        pairing = nil
        chats = []
        hosts = []
        projects = []
        notes = []
        loaded = false
        link = .connecting
        openRequest = nil
        compose = nil
    }

    /// conn://pair?url=https://mini.local:8742&code=7K4Q-2M
    /// (also accepts host= and port=).
    func handle(url: URL) {
        guard url.scheme == "conn", url.host == "pair",
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return }
        var host = AgentAddress.defaultHost
        var port = AgentAddress.defaultPort
        var code = ""
        for item in components.queryItems ?? [] {
            let value = item.value ?? ""
            switch item.name {
            case "url":
                if let agent = URLComponents(string: value) {
                    if let h = agent.host, !h.isEmpty { host = h }
                    if let p = agent.port { port = p }
                }
            case "host": if !value.isEmpty { host = value }
            case "port": if let p = Int(value) { port = p }
            case "code": code = value
            default: break
            }
        }
        let prefill = PairPrefill(host: host, port: port, code: code)
        if pairing != nil {
            // Already paired and working: a stray link changes nothing.
            guard link == .unpaired || link == .certificateChanged else { return }
            Task {
                await unpair(revoke: false)
                pairPrefill = prefill
            }
        } else {
            pairPrefill = prefill
        }
    }

    // MARK: Lifecycle

    func becameActive() {
        guard api != nil else { return }
        Task {
            await refresh()
            startEvents()
        }
    }

    func becameInactive() {
        stopEvents()
    }

    /// Retry from the inline "Can't reach the Mac mini" line.
    func retry() {
        stopEvents()
        lastRefresh = .distantPast
        link = .connecting
        becameActive()
    }

    // MARK: Fetching

    func refresh(force: Bool = false) async {
        guard let api = api else { return }
        if refreshing { return }
        if !force && Date().timeIntervalSince(lastRefresh) < 2 { return }
        refreshing = true
        defer { refreshing = false }
        do {
            async let chatList: [Chat] = api.chats()
            async let hostList: [HostStatus] = api.hosts()
            async let projectList: [Project] = api.projects()
            async let noteList: [Note] = api.notes()
            let fetched: ([Chat], [HostStatus], [Project], [Note]) =
                try await (chatList, hostList, projectList, noteList)
            chats = AppModel.sortedChats(fetched.0)
            hosts = fetched.1.sorted { $0.host < $1.host }
            projects = fetched.2
            notes = AppModel.sortedNotes(fetched.3)
            loaded = true
            lastRefresh = Date()
            link = .ok
        } catch {
            note(error)
        }
    }

    /// Record what an error says about the link. Returns the message to show
    /// next to the thing that failed.
    @discardableResult
    func note(_ error: Error) -> String {
        guard let apiError = error as? APIError else {
            if error is CancellationError { return "" }
            return error.localizedDescription
        }
        switch apiError {
        case .unreachable: link = .unreachable
        case .unauthorized: link = .unpaired; stopEvents()
        case .certificateChanged: link = .certificateChanged; stopEvents()
        case .server, .badResponse: break
        }
        return apiError.message
    }

    // MARK: Events

    private func startEvents() {
        guard eventsTask == nil, let api = api else { return }
        // The model lives as long as the app, so the loop holds it strongly;
        // stopEvents() cancels it.
        eventsTask = Task {
            var delay: UInt64 = 1
            var first = true
            while !Task.isCancelled {
                do {
                    for try await event in api.events() {
                        if case .connected = event {
                            delay = 1
                            // Anything that happened while the stream was
                            // down is caught up by one fetch.
                            if !first { await self.refresh(force: true) }
                            first = false
                        }
                        self.apply(event)
                    }
                } catch is CancellationError {
                    return
                } catch {
                    if Task.isCancelled { return }
                    self.note(error)
                    if self.link == .unpaired || self.link == .certificateChanged { return }
                    first = false
                }
                if Task.isCancelled { return }
                try? await Task.sleep(nanoseconds: delay * 1_000_000_000)
                delay = min(delay * 2, 30)
                if Task.isCancelled { return }
                await self.refresh(force: true)
            }
        }
    }

    private func stopEvents() {
        eventsTask?.cancel()
        eventsTask = nil
    }

    private func apply(_ event: ServerEvent) {
        switch event {
        case .connected:
            link = .ok
        case .chatUpdated(let chat):
            upsert(chat)
        case .chatRemoved(let id):
            chats.removeAll { $0.id == id }
        case .noteUpdated(let note):
            upsert(note)
        case .noteDeleted(let id):
            notes.removeAll { $0.id == id }
        case .hostUpdated(let host):
            if let index = hosts.firstIndex(where: { $0.host == host.host }) {
                hosts[index] = host
            } else {
                hosts.append(host)
                hosts.sort { $0.host < $1.host }
            }
        }
    }

    func upsert(_ chat: Chat) {
        var list = chats.filter { $0.id != chat.id }
        list.append(chat)
        chats = AppModel.sortedChats(list)
    }

    func upsert(_ note: Note) {
        var list = notes.filter { $0.id != note.id }
        list.append(note)
        notes = AppModel.sortedNotes(list)
    }

    private static func sortedChats(_ list: [Chat]) -> [Chat] {
        list.sorted { a, b in
            if a.lastEvent != b.lastEvent { return a.lastEvent > b.lastEvent }
            return a.id < b.id
        }
    }

    private static func sortedNotes(_ list: [Note]) -> [Note] {
        list.sorted { a, b in
            let da = a.createdDate ?? .distantPast
            let db = b.createdDate ?? .distantPast
            if da != db { return da > db }
            return a.id > b.id
        }
    }

    // MARK: Actions. Each returns nil, or a short message to show inline.

    private func run(_ action: @MainActor (ConnAPI) async throws -> Void) async -> String? {
        guard let api = api else { return "Not paired" }
        do {
            try await action(api)
            return nil
        } catch {
            let message = note(error)
            return message.isEmpty ? nil : message
        }
    }

    func open(_ ref: ChatRef) {
        tab = .chats
        openRequest = ref
    }

    func setStar(_ chat: Chat, _ starred: Bool) async -> String? {
        let before = self.chat(chat.id)
        upsert(chat.with(starred: starred))
        let failure = await run { api in try await api.star(chat.ref, starred: starred) }
        if failure != nil, let before = before {
            upsert(before)
        }
        return failure
    }

    func approve(_ chat: Chat, option: PermissionOption) async -> String? {
        let failure = await run { api in try await api.approve(chat.ref, key: option.key) }
        if failure == nil {
            // The prompt is answered: take the buttons away now, so a second
            // tap cannot type a stray key into Claude's input before the
            // agent's next look replaces this chat.
            if let current = self.chat(chat.id) {
                upsert(Chat(id: current.id, host: current.host, session: current.session,
                            project: current.project, title: current.title,
                            state: option.isDeny ? .idle : .working,
                            lastEvent: current.lastEvent, preview: current.preview,
                            starred: current.starred, offline: current.offline,
                            permission: nil))
            }
            UINotificationFeedbackGenerator().notificationOccurred(.success)
        } else {
            UINotificationFeedbackGenerator().notificationOccurred(.error)
        }
        return failure
    }

    func reply(_ ref: ChatRef, text: String) async -> String? {
        await run { api in try await api.reply(ref, text: text) }
    }

    func interrupt(_ ref: ChatRef) async -> String? {
        await run { api in try await api.interrupt(ref) }
    }

    func saveRecap(_ ref: ChatRef) async -> String? {
        await run { api in
            let note = try await api.recapToNotes(ref)
            self.upsert(note)
        }
    }

    /// Start a chat; on success it is listed and opened.
    func startChat(project: String, host: String?, text: String, includeNotes: Bool) async -> String? {
        await run { api in
            let request = StartChatRequest(project: project, host: host, text: text,
                                           resume: nil, includeNotes: includeNotes, fromNote: nil)
            let chat = try await api.startChat(request)
            self.upsert(chat)
            self.open(chat.ref)
        }
    }

    func createNote(project: String, text: String) async -> String? {
        await run { api in
            let note = try await api.createNote(project: project, text: text, source: "phone")
            self.upsert(note)
        }
    }

    func setNoteStatus(_ note: Note, _ status: String) async -> String? {
        await run { api in
            let updated = try await api.updateNote(id: note.id, status: status)
            self.upsert(updated)
        }
    }

    func deleteNote(_ note: Note) async -> String? {
        let before = notes
        notes.removeAll { $0.id == note.id }
        let failure = await run { api in try await api.deleteNote(id: note.id) }
        if failure != nil { notes = before }
        return failure
    }

    /// Start a chat from a note on its project's default server, then open it.
    func startChat(from note: Note) async -> String? {
        await run { api in
            let chat = try await api.startNote(id: note.id, host: nil, includeNotes: true)
            self.upsert(chat)
            self.open(chat.ref)
        }
    }
}
