import SwiftUI
import PhotosUI
import UIKit

/// One chat (iOSChat mockup): state, title, host and waiting time; the user's
/// task as a bubble; Claude's latest reply as a card; permission buttons and
/// a reply bar at the bottom. Live from `chat.updated` events.
@MainActor
struct ChatView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.openURL) private var openURL
    let ref: ChatRef

    @State private var detail: ChatDetail?
    @State private var loadError: String?
    @State private var replyText = ""
    @State private var picked: [PhotosPickerItem] = []
    @State private var attachments: [Attachment] = []
    @State private var sending = false
    @State private var answering = false
    @State private var actionError: String?
    @State private var notice: String?
    @FocusState private var replyFocused: Bool

    /// The live summary from the list (events), else what the detail said.
    private var chat: Chat? { model.chat(ref.id) ?? detail?.chat }

    /// What makes the detail worth fetching again. Not the preview: it moves
    /// every few seconds while Claude works, and the working line shows it
    /// straight from the event.
    private var signature: String {
        guard let chat = chat else { return ref.id }
        return "\(chat.state.rawValue)|\(Int(chat.lastEvent))|\(chat.permission?.command ?? "")|\(chat.offline)"
    }

    var body: some View {
        TimelineView(.periodic(from: .now, by: 30)) { context in
            content(now: context.date)
        }
        .background(Theme.bg)
        .navigationBarTitleDisplayMode(.inline)
        // The list hides its bar for its own title row; this one keeps it,
        // for the back button.
        .toolbar(.visible, for: .navigationBar)
        .toolbarBackground(Theme.header, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
        .toolbar {
            ToolbarItemGroup(placement: .topBarTrailing) {
                Button {
                    toggleStar()
                } label: {
                    Image(systemName: (chat?.starred ?? false) ? "star.fill" : "star")
                        .foregroundStyle(Color(hex: 0xE3E7ED))
                        .frame(width: 44, height: 44)
                }
                .disabled(chat == nil || chat?.offline == true)
                .accessibilityLabel((chat?.starred ?? false) ? "Unstar chat" : "Star chat")
            }
        }
        .task(id: signature) {
            if detail != nil {
                // Let a burst of events settle before asking again.
                try? await Task.sleep(nanoseconds: 400_000_000)
                if Task.isCancelled { return }
            }
            await load()
        }
    }

    // MARK: Layout

    private func content(now: Date) -> some View {
        VStack(spacing: 0) {
            header(now: now)
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        LinkLine()
                        if let detail = detail, !detail.history.isEmpty {
                            // The conversation, from Claude's transcript.
                            let items = detail.history
                            let lastClaude = items.lastIndex(where: { !$0.isYou })
                            ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                                if item.isYou {
                                    youBubble(item.text, at: item.at, now: now)
                                } else {
                                    claudeCard(item.text,
                                               meta: index == lastClaude ? replyMeta(now: now)
                                                   : claudeStamp(item.at, now: now))
                                }
                            }
                        } else {
                            if let detail = detail, !detail.task.isEmpty {
                                taskBubble(detail, now: now)
                            }
                            replyCard(now: now)
                        }
                        if chat?.state == .working {
                            workingLine
                        }
                        Color.clear.frame(height: 1).id("end")
                    }
                    .padding(.horizontal, 16)
                    .padding(.top, 18)
                    .padding(.bottom, 12)
                    // A tap on the conversation puts the keyboard away.
                    .contentShape(Rectangle())
                    .onTapGesture { replyFocused = false }
                }
                .scrollDismissesKeyboard(.interactively)
                .refreshable { await load() }
                .onChange(of: detail?.latestReply ?? "") { _, _ in
                    withAnimation { proxy.scrollTo("end", anchor: .bottom) }
                }
                .onChange(of: detail?.history.count ?? 0) { _, _ in
                    proxy.scrollTo("end", anchor: .bottom)
                }
            }
            .safeAreaInset(edge: .bottom, spacing: 0) {
                bottomBar
            }
        }
    }

    private func header(now: Date) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 8) {
                StatePill(state: chat?.state ?? .idle, offline: chat?.offline ?? false)
                Text(chat?.title ?? ref.session)
                    .font(Theme.ui(20, weight: .bold))
                    .foregroundStyle(Theme.textStrong)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            HStack(spacing: 6) {
                Circle()
                    .fill(hostColour)
                    .frame(width: 6, height: 6)
                Text(ref.host)
                    .foregroundStyle(Theme.textMeta)
                if let shown = timing(now: now) {
                    Text("·").foregroundStyle(Theme.dot)
                    Text(shown.0).foregroundStyle(shown.1)
                }
            }
            .font(Theme.mono(12))
        }
        .padding(.horizontal, 18)
        .padding(.bottom, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.header)
        .overlay(alignment: .bottom) {
            Rectangle().fill(Theme.divider).frame(height: 1)
        }
    }

    private func youBubble(_ text: String, at: TimeInterval, now: Date) -> some View {
        VStack(alignment: .trailing, spacing: 5) {
            Text(at > 0 ? "You · \(Format.stamp(Format.date(at), now: now))" : "You")
                .font(Theme.ui(12))
                .foregroundStyle(Theme.textMeta)
            Text(text.trimmingCharacters(in: .whitespacesAndNewlines))
                .font(Theme.ui(15.5))
                .lineSpacing(3)
                .foregroundStyle(Theme.textStrong)
                .textSelection(.enabled)
                .padding(.horizontal, 14)
                .padding(.vertical, 11)
                .background(Theme.bubble, in: UnevenRoundedRectangle(
                    topLeadingRadius: 18, bottomLeadingRadius: 18,
                    bottomTrailingRadius: 5, topTrailingRadius: 18, style: .continuous))
                .overlay(UnevenRoundedRectangle(
                    topLeadingRadius: 18, bottomLeadingRadius: 18,
                    bottomTrailingRadius: 5, topTrailingRadius: 18, style: .continuous)
                    .stroke(Theme.border, lineWidth: 1))
                .frame(maxWidth: 520, alignment: .trailing)
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .padding(.leading, 40)
    }

    private func claudeCard(_ text: String, meta: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text("✻")
                    .font(Theme.mono(12, weight: .bold))
                    .foregroundStyle(Theme.claude)
                    .frame(width: 20, height: 20)
                    .background(Theme.claudeTile, in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                Text(meta)
                    .font(Theme.ui(12))
                    .foregroundStyle(Theme.textMeta)
            }
            VStack(alignment: .leading, spacing: 12) {
                Text(ReplyText.attributed(text))
                    .font(Theme.ui(15))
                    .lineSpacing(4)
                    .foregroundStyle(Theme.textReply)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                ForEach(ReplyText.links(in: text), id: \.absoluteString) { url in
                    previewButton(url)
                }
            }
            .padding(.horizontal, 15)
            .padding(.top, 14)
            .padding(.bottom, 15)
            .background(Theme.surface, in: UnevenRoundedRectangle(
                topLeadingRadius: 18, bottomLeadingRadius: 5,
                bottomTrailingRadius: 18, topTrailingRadius: 18, style: .continuous))
            .overlay(UnevenRoundedRectangle(
                topLeadingRadius: 18, bottomLeadingRadius: 5,
                bottomTrailingRadius: 18, topTrailingRadius: 18, style: .continuous)
                .stroke(Theme.divider, lineWidth: 1))
        }
    }

    private func claudeStamp(_ at: TimeInterval, now: Date) -> String {
        at > 0 ? "Claude Code · \(Format.stamp(Format.date(at), now: now))" : "Claude Code"
    }

    private func taskBubble(_ detail: ChatDetail, now: Date) -> some View {
        VStack(alignment: .trailing, spacing: 5) {
            Text(detail.started > 0 ? "You · \(Format.stamp(Format.date(detail.started), now: now))" : "You")
                .font(Theme.ui(12))
                .foregroundStyle(Theme.textMeta)
            Text(detail.task.trimmingCharacters(in: .whitespacesAndNewlines))
                .font(Theme.ui(15.5))
                .lineSpacing(3)
                .foregroundStyle(Theme.textStrong)
                .textSelection(.enabled)
                .padding(.horizontal, 14)
                .padding(.vertical, 11)
                .background(Theme.bubble, in: UnevenRoundedRectangle(
                    topLeadingRadius: 18, bottomLeadingRadius: 18,
                    bottomTrailingRadius: 5, topTrailingRadius: 18, style: .continuous))
                .overlay(UnevenRoundedRectangle(
                    topLeadingRadius: 18, bottomLeadingRadius: 18,
                    bottomTrailingRadius: 5, topTrailingRadius: 18, style: .continuous)
                    .stroke(Theme.border, lineWidth: 1))
                .frame(maxWidth: 520, alignment: .trailing)
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .padding(.leading, 40)
    }

    @ViewBuilder
    private func replyCard(now: Date) -> some View {
        let reply = detail?.latestReply ?? ""
        let fallback = chat?.preview ?? ""
        if !reply.isEmpty || !fallback.isEmpty || loadError != nil {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 8) {
                    Text("✻")
                        .font(Theme.mono(12, weight: .bold))
                        .foregroundStyle(Theme.claude)
                        .frame(width: 20, height: 20)
                        .background(Theme.claudeTile, in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                    Text(replyMeta(now: now))
                        .font(Theme.ui(12))
                        .foregroundStyle(Theme.textMeta)
                }
                VStack(alignment: .leading, spacing: 12) {
                    if !reply.isEmpty {
                        Text(ReplyText.attributed(reply))
                            .font(Theme.ui(15))
                            .lineSpacing(4)
                            .foregroundStyle(Theme.textReply)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                        ForEach(ReplyText.links(in: reply), id: \.absoluteString) { url in
                            previewButton(url)
                        }
                    } else if let loadError = loadError {
                        Text(loadError)
                            .font(Theme.ui(15))
                            .foregroundStyle(Theme.textMeta)
                    } else {
                        Text(fallback)
                            .font(Theme.ui(15))
                            .foregroundStyle(Theme.textReply)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .padding(.horizontal, 15)
                .padding(.top, 14)
                .padding(.bottom, 15)
                .background(Theme.surface, in: UnevenRoundedRectangle(
                    topLeadingRadius: 18, bottomLeadingRadius: 5,
                    bottomTrailingRadius: 18, topTrailingRadius: 18, style: .continuous))
                .overlay(UnevenRoundedRectangle(
                    topLeadingRadius: 18, bottomLeadingRadius: 5,
                    bottomTrailingRadius: 18, topTrailingRadius: 18, style: .continuous)
                    .stroke(Theme.divider, lineWidth: 1))
            }
        } else if detail == nil {
            HStack {
                Spacer()
                ProgressView().tint(Theme.textMeta)
                Spacer()
            }
            .padding(.top, 40)
        }
    }

    private func previewButton(_ url: URL) -> some View {
        Button {
            openURL(url)
        } label: {
            HStack(spacing: 10) {
                Text("Open preview")
                    .font(Theme.ui(14.5, weight: .medium))
                    .foregroundStyle(Color(hex: 0xE3E7ED))
                    .lineLimit(1)
                Spacer(minLength: 6)
                Text(displayHost(url))
                    .font(Theme.mono(11.5))
                    .foregroundStyle(Theme.textMeta)
                    .lineLimit(1)
                Image(systemName: "arrow.up.right.square")
                    .font(.system(size: 14))
                    .foregroundStyle(Color(hex: 0xE3E7ED))
            }
            .padding(.horizontal, 12)
            .frame(height: 44)
            .background(Theme.raised, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).stroke(Theme.selectedRing, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Open preview, \(displayHost(url))")
    }

    private var workingLine: some View {
        HStack(spacing: 8) {
            Spinner(size: 13)
            Text(workingText)
                .font(Theme.ui(14))
                .foregroundStyle(Theme.workingText)
                .lineLimit(2)
        }
        .padding(.leading, 2)
    }

    // MARK: Bottom: permission, quick actions, reply bar

    private var bottomBar: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let chat = chat, let permission = chat.permission, !chat.offline {
                permissionBlock(chat, permission)
            }
            if let message = actionError ?? notice {
                Text(message)
                    .font(Theme.ui(13))
                    .foregroundStyle(actionError != nil ? Theme.hostDarkText : Theme.textSecondary)
                    .padding(.horizontal, 4)
            }
            // Only Interrupt is left here, and only while Claude is working.
            if chat?.state == .working {
                quickActions
            }
            if !attachments.isEmpty {
                attachmentStrip
            }
            replyBar
        }
        .padding(.horizontal, 12)
        .padding(.top, 10)
        .padding(.bottom, 8)
        .background(Theme.header.opacity(0.96))
        .overlay(alignment: .top) {
            Rectangle().fill(Theme.divider).frame(height: 1)
        }
    }

    private func permissionBlock(_ chat: Chat, _ permission: Permission) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(permission.command)
                .font(Theme.mono(12.5))
                .foregroundStyle(Theme.textReply)
                .lineLimit(4)
                .textSelection(.enabled)
                .padding(.horizontal, 11)
                .padding(.vertical, 9)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Theme.surface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).stroke(Theme.codeBorder, lineWidth: 1))
            ForEach(permission.options) { option in
                Button {
                    answer(chat, option)
                } label: {
                    Text(option.label)
                        .lineLimit(2)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 12)
                }
                .modifier(OptionStyle(primary: option.key == permission.allowOnce?.key))
                .disabled(answering)
            }
        }
        .opacity(answering ? 0.6 : 1)
    }

    private var quickActions: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                if chat?.state == .working {
                    quickButton("Interrupt", enabled: !(chat?.offline ?? true)) { interrupt() }
                }
            }
        }
    }

    private func quickButton(_ title: String, enabled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title)
                .font(Theme.ui(14))
                .foregroundStyle(Color(hex: 0xD0D5DE))
                .padding(.horizontal, 14)
                .frame(height: 36)
                .background(Theme.surface2, in: Capsule())
                .overlay(Capsule().stroke(Theme.border, lineWidth: 1))
                .frame(minHeight: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.45)
    }

    private var attachmentStrip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(attachments) { item in
                    ZStack(alignment: .topTrailing) {
                        Image(uiImage: item.thumb)
                            .resizable()
                            .scaledToFill()
                            .frame(width: 58, height: 58)
                            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                            .overlay {
                                if item.path == nil && item.error == nil {
                                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                                        .fill(.black.opacity(0.45))
                                    ProgressView().tint(.white)
                                } else if item.error != nil {
                                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                                        .stroke(Theme.hostDarkRing, lineWidth: 2)
                                }
                            }
                        Button {
                            attachments.removeAll { $0.id == item.id }
                        } label: {
                            Image(systemName: "xmark.circle.fill")
                                .font(.system(size: 18))
                                .symbolRenderingMode(.palette)
                                .foregroundStyle(.white, .black.opacity(0.6))
                        }
                        .buttonStyle(.plain)
                        .offset(x: 6, y: -6)
                        .accessibilityLabel("Remove image")
                    }
                }
            }
            .padding(.top, 8)
            .padding(.horizontal, 4)
        }
    }

    private var replyBar: some View {
        HStack(alignment: .bottom, spacing: 8) {
            PhotosPicker(selection: $picked, maxSelectionCount: 4, matching: .images) {
                Image(systemName: "plus")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(Color(hex: 0xD0D5DE))
                    .frame(width: 44, height: 44)
                    .background(Circle().fill(Theme.surface2))
            }
            .disabled(chat == nil || chat?.offline == true)
            .accessibilityLabel("Add images")
            .onChange(of: picked) { _, items in
                guard !items.isEmpty else { return }
                picked = []
                for item in items { attach(item) }
            }

            TextField("", text: $replyText,
                      prompt: Text("Reply to Claude").foregroundColor(Theme.textMeta),
                      axis: .vertical)
                .font(Theme.ui(16))
                .foregroundStyle(Theme.textStrong)
                .lineLimit(1...6)
                .focused($replyFocused)
                .padding(.horizontal, 16)
                .padding(.vertical, 11)
                .frame(minHeight: 44)
                .background(Theme.surface2, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 22, style: .continuous).stroke(Theme.border, lineWidth: 1))

            RoundPrimaryButton(systemImage: "arrow.up", label: "Send", enabled: canSend) {
                send()
            }
            .keyboardShortcut(.return, modifiers: .command)
        }
    }

    // MARK: Derived

    private var canSend: Bool {
        guard let chat = chat, !chat.offline, !sending else { return false }
        if attachments.contains(where: { $0.path == nil && $0.error == nil }) { return false }
        return !replyText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            || attachments.contains(where: { $0.path != nil })
    }

    private var workingText: String {
        let preview = chat?.preview ?? ""
        return preview.isEmpty ? "Working" : preview
    }

    private var hostColour: Color {
        switch model.host(ref.host)?.state {
        case .some(.up): return Theme.hostUp
        case .some(.needsLogin): return Theme.needsYou
        case .some(.dark): return Theme.hostDarkRing
        case .none: return Theme.textMeta
        }
    }

    private func timing(now: Date) -> (String, Color)? {
        guard let chat = chat, chat.lastEvent > 0 else { return nil }
        let age = Format.age(now.timeIntervalSince(Format.date(chat.lastEvent)))
        if chat.offline { return ("not answering", Theme.hostDarkText) }
        switch chat.state {
        case .needsYou: return ("waiting \(age)", Theme.needsYou)
        case .working: return ("working \(age)", Theme.working)
        case .idle: return ("idle \(age)", Theme.textMeta)
        }
    }

    private func replyMeta(now: Date) -> String {
        var parts = ["Claude Code"]
        if let chat = chat, chat.lastEvent > 0 {
            parts.append(Format.stamp(Format.date(chat.lastEvent), now: now))
            // How long the last turn took: your last message to Claude's
            // answer, from the transcript. Not from the session's start --
            // a tmux session weeks old said "ran 16d".
            if chat.state != .working, let history = detail?.history,
               let answered = history.last(where: { !$0.isYou }),
               let asked = history.last(where: { $0.isYou && $0.at <= answered.at }),
               asked.at > 0, answered.at > asked.at {
                let ran = Format.span(from: asked.at, to: answered.at)
                if !ran.isEmpty && ran != "now" { parts.append("ran \(ran)") }
            }
        }
        return parts.joined(separator: " · ")
    }

    private func displayHost(_ url: URL) -> String {
        let host = url.host ?? url.absoluteString
        return host.hasPrefix("www.") ? String(host.dropFirst(4)) : host
    }

    // MARK: Actions

    private func load() async {
        guard let api = model.api else { return }
        // A chat that was just started may not be in the agent's view yet:
        // ask again for a little while before saying so.
        var tries = 0
        while !Task.isCancelled {
            do {
                let fresh = try await api.chat(ref)
                detail = fresh
                loadError = nil
                return
            } catch let error as APIError {
                if case .server(let status, _) = error, status == 404, tries < 10 {
                    tries += 1
                    try? await Task.sleep(nanoseconds: 2_000_000_000)
                    continue
                }
                if case .server(let status, _) = error, status == 404 {
                    loadError = "This chat has ended"
                    return
                }
                let message = model.note(error)
                if detail == nil { loadError = message }
                return
            } catch {
                return
            }
        }
    }

    private func send() {
        let text = replyText
        guard canSend else { return }
        // The images go as their paths on the server, after the words:
        // Claude Code reads an image it is given the path of.
        let sent = attachments.filter { $0.path != nil }
        let paths = sent.compactMap(\.path)
        let words = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let message = [words, paths.joined(separator: "\n")]
            .filter { !$0.isEmpty }.joined(separator: "\n\n")
        sending = true
        actionError = nil
        Task {
            let failure = await model.reply(ref, text: message)
            sending = false
            if let failure = failure {
                actionError = "Not sent: \(failure)"
            } else {
                if replyText == text { replyText = "" }
                let ids = Set(sent.map(\.id))
                attachments.removeAll { ids.contains($0.id) }
                Haptics.tap()
            }
        }
    }

    /// A picked photo: shrunk to a JPEG, shown at once, sent to the chat's
    /// server in the background.
    private func attach(_ item: PhotosPickerItem) {
        Task {
            guard let raw = try? await item.loadTransferable(type: Data.self),
                  let jpeg = Attachment.jpeg(from: raw),
                  let thumb = UIImage(data: jpeg) else {
                actionError = "That image could not be read"
                return
            }
            let entry = Attachment(thumb: thumb)
            attachments.append(entry)
            let got = await model.uploadImage(ref, jpeg: jpeg)
            guard let index = attachments.firstIndex(where: { $0.id == entry.id }) else { return }
            if let path = got.path {
                attachments[index].path = path
            } else {
                attachments[index].error = got.error ?? "not sent"
                actionError = "Image not sent: \(got.error ?? "no answer")"
            }
        }
    }

    private func answer(_ chat: Chat, _ option: PermissionOption) {
        answering = true
        actionError = nil
        Task {
            let failure = await model.approve(chat, option: option)
            answering = false
            actionError = failure
        }
    }

    private func interrupt() {
        actionError = nil
        Task {
            if let failure = await model.interrupt(ref) {
                actionError = failure
            }
        }
    }

    private func toggleStar() {
        guard let chat = chat else { return }
        Task {
            actionError = await model.setStar(chat, !chat.starred)
        }
    }
}

/// Permission options: the allow option amber, the rest secondary.
private struct OptionStyle: ViewModifier {
    let primary: Bool

    @ViewBuilder
    func body(content: Content) -> some View {
        if primary {
            content.buttonStyle(AllowButtonStyle())
        } else {
            content.buttonStyle(SecondaryButtonStyle())
        }
    }
}


/// An image on its way into a reply.
struct Attachment: Identifiable {
    let id = UUID()
    let thumb: UIImage
    var path: String?
    var error: String?

    /// The photo as a JPEG no bigger than `maxSide` on its long edge: what
    /// Claude needs to see it, at a size the phone sends quickly.
    static func jpeg(from data: Data, maxSide: CGFloat = 2048) -> Data? {
        guard let image = UIImage(data: data) else { return nil }
        let size = image.size
        guard size.width > 0, size.height > 0 else { return nil }
        let scale = min(1, maxSide / max(size.width, size.height))
        let target = CGSize(width: floor(size.width * scale), height: floor(size.height * scale))
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        let drawn = UIGraphicsImageRenderer(size: target, format: format).image { _ in
            image.draw(in: CGRect(origin: .zero, size: target))
        }
        return drawn.jpegData(compressionQuality: 0.8)
    }
}
