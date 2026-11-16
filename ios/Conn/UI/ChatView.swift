import SwiftUI

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
        .toolbarBackground(Theme.header, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
        .toolbar {
            ToolbarItemGroup(placement: .topBarTrailing) {
                Button {
                } label: {
                    Image(systemName: "terminal")
                        .frame(width: 44, height: 44)
                }
                .disabled(true)
                .accessibilityLabel("Show terminal (coming later)")

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
                        if let detail = detail, !detail.task.isEmpty {
                            taskBubble(detail, now: now)
                        }
                        replyCard(now: now)
                        if chat?.state == .working {
                            workingLine
                        }
                        Color.clear.frame(height: 1).id("end")
                    }
                    .padding(.horizontal, 16)
                    .padding(.top, 18)
                    .padding(.bottom, 12)
                }
                .scrollDismissesKeyboard(.interactively)
                .refreshable { await load() }
                .onChange(of: detail?.latestReply ?? "") { _, _ in
                    withAnimation { proxy.scrollTo("end", anchor: .bottom) }
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
            quickActions
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
                quickButton("Save recap to notes", enabled: chat != nil) { saveRecap() }
                quickButton("Open terminal", enabled: false) {}
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

    private var replyBar: some View {
        HStack(alignment: .bottom, spacing: 8) {
            Button {
            } label: {
                Image(systemName: "plus")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(Color(hex: 0xB3BAC6))
                    .frame(width: 44, height: 44)
                    .background(Circle().fill(Theme.surface2))
            }
            .buttonStyle(.plain)
            .disabled(true)
            .opacity(0.5)
            .accessibilityLabel("Attach (coming later)")

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
        return !replyText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
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
            if chat.state != .working, let detail = detail {
                let ran = Format.span(from: detail.started, to: chat.lastEvent)
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
        sending = true
        actionError = nil
        Task {
            let failure = await model.reply(ref, text: text)
            sending = false
            if let failure = failure {
                actionError = "Not sent: \(failure)"
            } else {
                if replyText == text { replyText = "" }
                Haptics.tap()
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

    private func saveRecap() {
        actionError = nil
        notice = nil
        Task {
            if let failure = await model.saveRecap(ref) {
                actionError = failure
            } else {
                notice = "Saved to notes"
                Haptics.success()
                try? await Task.sleep(nanoseconds: 2_500_000_000)
                if notice == "Saved to notes" { notice = nil }
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
