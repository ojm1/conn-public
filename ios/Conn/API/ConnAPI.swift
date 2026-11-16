import CryptoKit
import Foundation
import Security

// conn-agent's API (SPEC §6, agent_api.py), one method per route.
//
// The agent listens on the local network with its own self-signed certificate.
// The app learns that certificate's SHA-256 (of its DER) when it pairs and,
// from then on, talks to nothing else: any other certificate is refused in
// the TLS handshake, before a token or a byte of chat leaves the phone.
//
// The sessions are ephemeral: no URL cache, no cookies, nothing about a chat
// written to disk by URLSession.

// MARK: - Address

struct AgentAddress: Codable, Hashable {
    var host: String
    var port: Int

    /// None built in: the pairing link or you fill it in.
    static let defaultHost = ""
    static let defaultPort = 8742

    var label: String { "\(host):\(port)" }

    func url(path: String, query: [URLQueryItem]) -> URL? {
        var components = URLComponents()
        components.scheme = "https"
        if host.contains(":") && !host.hasPrefix("[") {
            components.percentEncodedHost = "[\(host)]"     // IPv6 literal
        } else {
            components.host = host
        }
        components.port = port
        components.percentEncodedPath = path
        if !query.isEmpty {
            components.queryItems = query
        }
        return components.url
    }
}

// MARK: - Errors

enum APIError: Error, Equatable {
    /// No answer: wrong network, the mini asleep, the agent not running.
    case unreachable(String)
    /// 401: this device's token is unknown (revoked, or the agent was reset).
    case unauthorized
    /// The agent presented a certificate other than the pinned one.
    case certificateChanged
    /// Any other non-2xx, with the agent's `error` text.
    case server(status: Int, message: String)
    /// A 2xx whose body did not decode.
    case badResponse

    var message: String {
        switch self {
        case .unreachable:
            return "Can't reach the Mac mini"
        case .unauthorized:
            return "This device is no longer paired"
        case .certificateChanged:
            return "The Mac mini's certificate changed"
        case .server(_, let message):
            return message.isEmpty ? "The Mac mini said no" : message
        case .badResponse:
            return "The Mac mini sent something unexpected"
        }
    }
}

// MARK: - Certificate pinning

/// Accepts the agent's certificate by SHA-256 of its DER and nothing else.
///
/// `pinned == nil` is trust-on-first-use, used only for POST /pair: the
/// certificate seen is recorded (and, if you typed the start of the
/// fingerprint `conn_agent.py pair` printed, checked against it).
final class PinningDelegate: NSObject, URLSessionDelegate, @unchecked Sendable {
    private let lock = NSLock()
    private let pinned: String?
    private let expectedPrefix: String?
    private var seen: String?
    private var refused = false

    init(pinned: String?, expectedPrefix: String?) {
        self.pinned = pinned.map(PinningDelegate.normalise)
        let prefix = expectedPrefix.map(PinningDelegate.normalise) ?? ""
        self.expectedPrefix = prefix.isEmpty ? nil : prefix
    }

    var seenFingerprint: String? {
        lock.lock()
        defer { lock.unlock() }
        return seen
    }

    var refusedCertificate: Bool {
        lock.lock()
        defer { lock.unlock() }
        return refused
    }

    // The async form of the session-level challenge: no completion-handler
    // signature to get subtly wrong (a near miss is silently not called,
    // and every connection would then fail ordinary trust evaluation).
    func urlSession(_ session: URLSession,
                    didReceive challenge: URLAuthenticationChallenge) async
        -> (URLSession.AuthChallengeDisposition, URLCredential?) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust else {
            return (.performDefaultHandling, nil)
        }
        guard let print = PinningDelegate.leafFingerprint(trust) else {
            markRefused()
            return (.cancelAuthenticationChallenge, nil)
        }
        let accepted: Bool
        if let pinned = pinned {
            accepted = (print == pinned)
        } else if let prefix = expectedPrefix {
            accepted = print.hasPrefix(prefix)
        } else {
            accepted = true
        }
        record(print, accepted: accepted)
        if accepted {
            return (.useCredential, URLCredential(trust: trust))
        }
        return (.cancelAuthenticationChallenge, nil)
    }

    private func record(_ print: String, accepted: Bool) {
        lock.lock()
        seen = print
        if !accepted { refused = true }
        lock.unlock()
    }

    private func markRefused() {
        lock.lock()
        refused = true
        lock.unlock()
    }

    /// Lowercase hex SHA-256 of the leaf certificate's DER, the same string
    /// agent_api.fingerprint() makes.
    static func leafFingerprint(_ trust: SecTrust) -> String? {
        guard let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate],
              let leaf = chain.first else {
            return nil
        }
        let der = SecCertificateCopyData(leaf) as Data
        let digest = SHA256.hash(data: der)
        return digest.map { String(format: "%02x", $0) }.joined()
    }

    /// "AB:CD ef01" -> "abcdef01".
    static func normalise(_ text: String) -> String {
        let hex = Set("0123456789abcdef")
        return String(text.lowercased().filter { hex.contains($0) })
    }
}

// MARK: - Request bodies (snake_case, exactly as agent_api.py reads them)

private struct TextBody: Encodable { let text: String }
private struct KeyBody: Encodable { let key: String }
private struct StarBody: Encodable { let starred: Bool }
private struct TokenBody: Encodable { let token: String }

private struct PairBody: Encodable {
    let code: String
    let deviceName: String
    enum CodingKeys: String, CodingKey {
        case code
        case deviceName = "device_name"
    }
}

/// POST /chats `{ project, host?, text, continue?, include_notes?, from_note? }`.
/// Optionals left nil are omitted from the JSON.
struct StartChatRequest: Encodable {
    var project: String
    var host: String?
    var text: String
    var resume: Bool?
    var includeNotes: Bool?
    var fromNote: String?

    enum CodingKeys: String, CodingKey {
        case project, host, text
        case resume = "continue"
        case includeNotes = "include_notes"
        case fromNote = "from_note"
    }
}

private struct NewNoteBody: Encodable {
    let project: String
    let text: String
    let source: String
}

private struct NotePatchBody: Encodable {
    let text: String?
    let status: String?
}

private struct NoteStartBody: Encodable {
    let host: String?
    let includeNotes: Bool?
    enum CodingKeys: String, CodingKey {
        case host
        case includeNotes = "include_notes"
    }
}

// MARK: - The client

final class ConnAPI: @unchecked Sendable {
    let address: AgentAddress
    let pinning: PinningDelegate
    private let token: String?
    private let session: URLSession
    private let streamSession: URLSession
    private let decoder = JSONDecoder()
    private let encoder = JSONEncoder()

    /// A client for a paired agent: every request carries the token and
    /// every connection must present the pinned certificate.
    convenience init(pairing: Pairing) {
        self.init(address: pairing.address, token: pairing.token,
                  pinned: pairing.fingerprint, expectedPrefix: nil)
    }

    private init(address: AgentAddress, token: String?, pinned: String?, expectedPrefix: String?) {
        self.address = address
        self.token = token
        self.pinning = PinningDelegate(pinned: pinned, expectedPrefix: expectedPrefix)

        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 20
        config.timeoutIntervalForResource = 90
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.urlCache = nil
        config.httpCookieStorage = nil
        config.waitsForConnectivity = false
        session = URLSession(configuration: config, delegate: pinning, delegateQueue: nil)

        let stream = URLSessionConfiguration.ephemeral
        // The agent pings every 15 s; three missed pings means it is gone.
        stream.timeoutIntervalForRequest = 45
        stream.timeoutIntervalForResource = 7 * 24 * 3600
        stream.requestCachePolicy = .reloadIgnoringLocalCacheData
        stream.urlCache = nil
        stream.httpCookieStorage = nil
        stream.waitsForConnectivity = false
        streamSession = URLSession(configuration: stream, delegate: pinning, delegateQueue: nil)
    }

    deinit {
        session.invalidateAndCancel()
        streamSession.invalidateAndCancel()
    }

    // MARK: Routes, in SPEC §6 order

    /// GET /health
    func health() async throws -> Health {
        try await call("GET", ["health"])
    }

    /// GET /projects
    func projects() async throws -> [Project] {
        try await call("GET", ["projects"])
    }

    /// GET /hosts
    func hosts() async throws -> [HostStatus] {
        try await call("GET", ["hosts"])
    }

    /// GET /chats?project=&server=&needs_you=1
    func chats(project: String? = nil, server: String? = nil, needsYou: Bool = false) async throws -> [Chat] {
        var query: [URLQueryItem] = []
        if let project = project, !project.isEmpty { query.append(URLQueryItem(name: "project", value: project)) }
        if let server = server, !server.isEmpty { query.append(URLQueryItem(name: "server", value: server)) }
        if needsYou { query.append(URLQueryItem(name: "needs_you", value: "1")) }
        return try await call("GET", ["chats"], query: query)
    }

    /// GET /chats/{host}/{session}. Also tells the agent the chat was seen.
    func chat(_ ref: ChatRef) async throws -> ChatDetail {
        try await call("GET", ["chats", ref.host, ref.session])
    }

    /// POST /chats -> 201 Chat
    func startChat(_ request: StartChatRequest) async throws -> Chat {
        try await call("POST", ["chats"], body: try encoder.encode(request))
    }

    /// POST /chats/{host}/{session}/reply -> 204
    func reply(_ ref: ChatRef, text: String) async throws {
        try await callEmpty("POST", ["chats", ref.host, ref.session, "reply"],
                            body: try encoder.encode(TextBody(text: text)))
    }

    /// POST /chats/{host}/{session}/approve -> 204. 409 if that option is no
    /// longer on screen.
    func approve(_ ref: ChatRef, key: String) async throws {
        try await callEmpty("POST", ["chats", ref.host, ref.session, "approve"],
                            body: try encoder.encode(KeyBody(key: key)))
    }

    /// POST /chats/{host}/{session}/interrupt -> 204
    func interrupt(_ ref: ChatRef) async throws {
        try await callEmpty("POST", ["chats", ref.host, ref.session, "interrupt"])
    }

    /// PUT /chats/{host}/{session}/star -> 204
    func star(_ ref: ChatRef, starred: Bool) async throws {
        try await callEmpty("PUT", ["chats", ref.host, ref.session, "star"],
                            body: try encoder.encode(StarBody(starred: starred)))
    }

    /// POST /chats/{host}/{session}/recap-to-notes -> 201 Note
    func recapToNotes(_ ref: ChatRef) async throws -> Note {
        try await call("POST", ["chats", ref.host, ref.session, "recap-to-notes"])
    }

    /// GET /notes?project=&status=
    func notes(project: String? = nil, status: String? = nil) async throws -> [Note] {
        var query: [URLQueryItem] = []
        if let project = project, !project.isEmpty { query.append(URLQueryItem(name: "project", value: project)) }
        if let status = status, !status.isEmpty { query.append(URLQueryItem(name: "status", value: status)) }
        return try await call("GET", ["notes"], query: query)
    }

    /// POST /notes -> 201 Note
    func createNote(project: String, text: String, source: String = "phone") async throws -> Note {
        try await call("POST", ["notes"],
                       body: try encoder.encode(NewNoteBody(project: project, text: text, source: source)))
    }

    /// PATCH /notes/{id} -> Note. `status` is open, started or done.
    func updateNote(id: String, text: String? = nil, status: String? = nil) async throws -> Note {
        try await call("PATCH", ["notes", id],
                       body: try encoder.encode(NotePatchBody(text: text, status: status)))
    }

    /// DELETE /notes/{id} -> 204
    func deleteNote(id: String) async throws {
        try await callEmpty("DELETE", ["notes", id])
    }

    /// POST /notes/{id}/start -> 201 Chat
    func startNote(id: String, host: String? = nil, includeNotes: Bool? = nil) async throws -> Chat {
        try await call("POST", ["notes", id, "start"],
                       body: try encoder.encode(NoteStartBody(host: host, includeNotes: includeNotes)))
    }

    /// POST /devices/me/active -> 204 (the desktop's; mirrored for completeness)
    func markActive() async throws {
        try await callEmpty("POST", ["devices", "me", "active"])
    }

    /// PUT /devices/me/apns -> 204 (M4)
    func setAPNsToken(_ hex: String) async throws {
        try await callEmpty("PUT", ["devices", "me", "apns"],
                            body: try encoder.encode(TokenBody(token: hex)))
    }

    /// POST /pair/codes -> 201 (desktop tokens only; a phone gets 403)
    func pairCode() async throws -> PairCode {
        try await call("POST", ["pair", "codes"])
    }

    /// POST /pair -> 201 `{ token, device_id }`, no auth. Trusts the
    /// certificate it meets (checked against `fingerprintPrefix` if one was
    /// typed) and returns it, to be pinned from then on.
    static func pair(address: AgentAddress, code: String, deviceName: String,
                     fingerprintPrefix: String?) async throws -> Pairing {
        let api = ConnAPI(address: address, token: nil, pinned: nil, expectedPrefix: fingerprintPrefix)
        let body = try api.encoder.encode(PairBody(code: code, deviceName: deviceName))
        let response: PairResponse = try await api.call("POST", ["pair"], body: body)
        guard let print = api.pinning.seenFingerprint, !print.isEmpty else {
            throw APIError.badResponse
        }
        return Pairing(address: address, token: response.token, deviceId: response.deviceId,
                       fingerprint: print, deviceName: deviceName, paired: Date().timeIntervalSince1970)
    }

    /// GET /devices
    func devices() async throws -> [Device] {
        try await call("GET", ["devices"])
    }

    /// DELETE /devices/{id} -> 204
    func deleteDevice(id: String) async throws {
        try await callEmpty("DELETE", ["devices", id])
    }

    /// GET /events: server-sent events until the connection drops, which
    /// ends the stream (with an error, or without one when the agent closed
    /// it). The caller reconnects.
    func events() -> AsyncThrowingStream<ServerEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    var request = try self.makeRequest("GET", ["events"], query: [], body: nil)
                    request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
                    request.timeoutInterval = 45
                    let result: (URLSession.AsyncBytes, URLResponse)
                    do {
                        result = try await self.streamSession.bytes(for: request)
                    } catch {
                        throw self.mapTransport(error)
                    }
                    let status = (result.1 as? HTTPURLResponse)?.statusCode ?? 0
                    if status == 401 { throw APIError.unauthorized }
                    guard status == 200 else {
                        throw APIError.server(status: status, message: "events refused")
                    }
                    continuation.yield(.connected)
                    var parser = EventStreamParser()
                    var line: [UInt8] = []
                    do {
                        for try await byte in result.0 {
                            if byte == 0x0A {
                                if line.last == 0x0D { line.removeLast() }
                                let text = String(decoding: line, as: UTF8.self)
                                line.removeAll(keepingCapacity: true)
                                if let message = parser.feed(text),
                                   let event = self.decodeEvent(name: message.name, data: message.data) {
                                    continuation.yield(event)
                                }
                            } else {
                                line.append(byte)
                            }
                        }
                    } catch {
                        throw self.mapTransport(error)
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    // MARK: Plumbing

    private static let segmentAllowed: CharacterSet = {
        var set = CharacterSet.urlPathAllowed
        set.remove(charactersIn: "/%;?#")
        return set
    }()

    private static func segment(_ text: String) -> String {
        text.addingPercentEncoding(withAllowedCharacters: segmentAllowed) ?? text
    }

    private func makeRequest(_ method: String, _ path: [String], query: [URLQueryItem], body: Data?) throws -> URLRequest {
        let encoded = "/" + path.map { ConnAPI.segment($0) }.joined(separator: "/")
        guard let url = address.url(path: encoded, query: query) else {
            throw APIError.unreachable("bad address")
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let token = token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let body = body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return request
    }

    private func mapTransport(_ error: Error) -> Error {
        if error is APIError { return error }
        if error is CancellationError { return error }
        if pinning.refusedCertificate { return APIError.certificateChanged }
        if let urlError = error as? URLError {
            if urlError.code == .cancelled && Task.isCancelled { return CancellationError() }
            return APIError.unreachable(urlError.localizedDescription)
        }
        return APIError.unreachable(error.localizedDescription)
    }

    private func perform(_ method: String, _ path: [String], query: [URLQueryItem], body: Data?) async throws -> Data {
        let request = try makeRequest(method, path, query: query, body: body)
        let result: (Data, URLResponse)
        do {
            result = try await session.data(for: request)
        } catch {
            throw mapTransport(error)
        }
        let data: Data = result.0
        guard let http = result.1 as? HTTPURLResponse else { throw APIError.badResponse }
        switch http.statusCode {
        case 200..<300:
            return data
        case 401:
            throw APIError.unauthorized
        default:
            let message = (try? decoder.decode(ErrorBody.self, from: data))?.error ?? ""
            throw APIError.server(status: http.statusCode, message: message)
        }
    }

    private func call<T: Decodable>(_ method: String, _ path: [String],
                                    query: [URLQueryItem] = [], body: Data? = nil) async throws -> T {
        let data = try await perform(method, path, query: query, body: body)
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw APIError.badResponse
        }
    }

    private func callEmpty(_ method: String, _ path: [String],
                           query: [URLQueryItem] = [], body: Data? = nil) async throws {
        _ = try await perform(method, path, query: query, body: body)
    }

    private func decodeEvent(name: String, data: String) -> ServerEvent? {
        let raw = Data(data.utf8)
        switch name {
        case "chat.updated":
            guard let chat = try? decoder.decode(Chat.self, from: raw) else { return nil }
            return .chatUpdated(chat)
        case "chat.removed":
            guard let gone = try? decoder.decode(NoteDeletion.self, from: raw) else { return nil }
            return .chatRemoved(id: gone.id)
        case "note.updated":
            if let gone = try? decoder.decode(NoteDeletion.self, from: raw), gone.deleted == true {
                return .noteDeleted(id: gone.id)
            }
            guard let note = try? decoder.decode(Note.self, from: raw) else { return nil }
            return .noteUpdated(note)
        case "host.updated":
            guard let host = try? decoder.decode(HostStatus.self, from: raw) else { return nil }
            return .hostUpdated(host)
        default:
            return nil
        }
    }
}

/// The text/event-stream framing: `event:` and `data:` lines, a blank line
/// ends a message, lines starting with ":" are comments (the agent's hello
/// and pings).
struct EventStreamParser {
    private var name = ""
    private var data: [String] = []

    mutating func feed(_ line: String) -> (name: String, data: String)? {
        if line.isEmpty {
            defer {
                name = ""
                data = []
            }
            guard !data.isEmpty else { return nil }
            return (name.isEmpty ? "message" : name, data.joined(separator: "\n"))
        }
        if line.hasPrefix(":") { return nil }
        let field: Substring
        var value: Substring
        if let colon = line.firstIndex(of: ":") {
            field = line[line.startIndex..<colon]
            value = line[line.index(after: colon)...]
            if value.hasPrefix(" ") { value = value.dropFirst() }
        } else {
            field = Substring(line)
            value = ""
        }
        switch field {
        case "event": name = String(value)
        case "data": data.append(String(value))
        default: break
        }
        return nil
    }
}
