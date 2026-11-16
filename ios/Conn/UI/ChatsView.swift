import SwiftUI

/// The Chats tab: a stack on iPhone, a split view on iPad (list left, chat
/// right, like the desktop).
@MainActor
struct ChatsTab: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.horizontalSizeClass) private var sizeClass
    @State private var path: [ChatRef] = []
    @State private var selection: ChatRef?

    var body: some View {
        Group {
            if sizeClass == .regular {
                NavigationSplitView {
                    ChatListView(selectedID: selection?.id) { ref in selection = ref }
                        .navigationSplitViewColumnWidth(min: 320, ideal: 380, max: 460)
                } detail: {
                    NavigationStack {
                        if let ref = selection {
                            ChatView(ref: ref).id(ref.id)
                        } else {
                            Text("Pick a chat")
                                .font(Theme.ui(15))
                                .foregroundStyle(Theme.textMeta)
                                .frame(maxWidth: .infinity, maxHeight: .infinity)
                                .background(Theme.bg)
                        }
                    }
                }
                .navigationSplitViewStyle(.balanced)
            } else {
                NavigationStack(path: $path) {
                    ChatListView(selectedID: nil) { ref in path.append(ref) }
                        .navigationDestination(for: ChatRef.self) { ref in
                            ChatView(ref: ref)
                        }
                }
            }
        }
        .onAppear { takeOpenRequest() }
        .onChange(of: model.openRequest) { _, _ in takeOpenRequest() }
    }

    private func takeOpenRequest() {
        guard let ref = model.openRequest else { return }
        model.openRequest = nil
        if sizeClass == .regular {
            selection = ref
        } else {
            path = [ref]
        }
    }
}

private struct ChatSection: Identifiable {
    let id: String
    let label: String
    let starred: Bool
    let chats: [Chat]
}

@MainActor
struct ChatListView: View {
    @EnvironmentObject private var model: AppModel
    let selectedID: String?
    let open: (ChatRef) -> Void

    @State private var query = ""
    @State private var needsOnly = false
    @State private var projectFilter: String?
    @State private var serverFilter: String?
    @State private var busy: Set<String> = []
    @State private var rowError: [String: String] = [:]
    @FocusState private var searchFocused: Bool

    var body: some View {
        TimelineView(.periodic(from: .now, by: 30)) { context in
            list(now: context.date)
        }
        .navigationTitle("Chats")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button {
                    model.compose = ComposeRequest(kind: .chat, project: projectFilter)
                } label: {
                    Image(systemName: "square.and.pencil")
                        .font(.system(size: 16, weight: .bold))
                        .foregroundStyle(Theme.onPrimary)
                        .frame(width: 36, height: 36)
                        .background(Circle().fill(Theme.primary))
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .keyboardShortcut("n", modifiers: .command)
                .accessibilityLabel("New chat")
            }
        }
        .background {
            // Cmd+K: search.
            Button("Search") { searchFocused = true }
                .keyboardShortcut("k", modifiers: .command)
                .opacity(0)
                .accessibilityHidden(true)
        }
    }

    private func list(now: Date) -> some View {
        let sections = makeSections(now: now)
        return List {
            header
                .listRowInsets(EdgeInsets(top: 0, leading: 16, bottom: 0, trailing: 16))
                .listRowBackground(Theme.bg)
                .listRowSeparator(.hidden)

            ForEach(sections) { section in
                sectionLabel(section)
                    .listRowInsets(EdgeInsets(top: 14, leading: 16, bottom: 4, trailing: 16))
                    .listRowBackground(Theme.bg)
                    .listRowSeparator(.hidden)

                ForEach(section.chats) { chat in
                    row(chat, now: now)
                        .listRowInsets(EdgeInsets(top: 0, leading: 16, bottom: 0, trailing: 16))
                        .listRowBackground(chat.id == selectedID ? Theme.selected : Theme.bg)
                        .listRowSeparator(.hidden)
                        .swipeActions(edge: .leading, allowsFullSwipe: true) {
                            starButton(chat)
                        }
                        .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                            starButton(chat)
                        }
                }
            }

            if sections.isEmpty && model.loaded {
                Text(emptyText)
                    .font(Theme.ui(15))
                    .foregroundStyle(Theme.textMeta)
                    .frame(maxWidth: .infinity, alignment: .center)
                    .padding(.vertical, 40)
                    .listRowBackground(Theme.bg)
                    .listRowSeparator(.hidden)
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .background(Theme.bg)
        .refreshable { await model.refresh(force: true) }
    }

    // MARK: Header: fleet line, link line, search, chips

    private var header: some View {
        VStack(alignment: .leading, spacing: 0) {
            fleetLine
                .padding(.top, 2)
            LinkLine()
            searchField
                .padding(.top, 12)
            chips
                .padding(.top, 6)
        }
    }

    private var fleetLine: some View {
        let up = model.hostsUp
        let dark = model.hostsDark
        let total = CGFloat(max(up + dark, 1))
        let upHeight: CGFloat = up + dark == 0 ? 6 : 12 * CGFloat(up) / total
        let darkHeight: CGFloat = up + dark == 0 ? 6 : 12 * CGFloat(dark) / total
        return HStack(spacing: 8) {
            VStack(spacing: 0) {
                Rectangle().fill(Theme.hostUp).frame(height: upHeight)
                Rectangle().fill(Theme.hostDarkRing).frame(height: darkHeight)
            }
            .frame(width: 3, height: 12)
            .clipShape(RoundedRectangle(cornerRadius: 2))
            .accessibilityHidden(true)
            HStack(spacing: 0) {
                Text("\(up) up")
                if dark > 0 {
                    Text(" · ")
                    Text("\(dark) dark").foregroundStyle(Theme.hostDarkText)
                }
            }
            .font(Theme.mono(12))
            .foregroundStyle(Theme.textMeta)
        }
        .frame(minHeight: 20)
        .onTapGesture { model.tab = .hosts }
    }

    private var searchField: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 15, weight: .medium))
                .foregroundStyle(Theme.textMeta)
            TextField("", text: $query, prompt: Text("Search chats").foregroundColor(Theme.textMeta))
                .font(Theme.ui(16))
                .foregroundStyle(Theme.text)
                .focused($searchFocused)
                .submitLabel(.search)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.never)
            if !query.isEmpty {
                Button {
                    query = ""
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(Theme.textMeta)
                        .frame(width: 32, height: 40)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Clear search")
            }
        }
        .padding(.horizontal, 12)
        .frame(height: 44)
        .background(Theme.surface2, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    private var chips: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                Button {
                    needsOnly.toggle()
                } label: {
                    ChipLabel(title: "Needs you", isOn: needsOnly,
                              tint: needsOnly || model.needsYouCount > 0 ? Theme.needsYou : nil,
                              trailing: String(model.needsYouCount))
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(needsOnly ? .isSelected : [])

                Menu {
                    Button("All projects") { projectFilter = nil }
                    ForEach(model.recentProjects) { project in
                        Button(project.name) { projectFilter = project.id }
                    }
                } label: {
                    ChipLabel(title: projectFilter.flatMap { model.project($0)?.name } ?? "Project",
                              isOn: projectFilter != nil, chevron: true)
                }

                Menu {
                    Button("All servers") { serverFilter = nil }
                    ForEach(model.hosts) { host in
                        Button(host.host) { serverFilter = host.host }
                    }
                } label: {
                    ChipLabel(title: serverFilter ?? "Server", isOn: serverFilter != nil, chevron: true)
                }
            }
        }
        .scrollClipDisabled()
    }

    private func sectionLabel(_ section: ChatSection) -> some View {
        HStack(spacing: 6) {
            if section.starred {
                Image(systemName: "star.fill")
                    .font(.system(size: 11))
                    .foregroundStyle(Theme.textChip)
            }
            Text(section.label.uppercased())
                .font(Theme.ui(12, weight: .semibold))
                .tracking(0.96)
                .foregroundStyle(Theme.textMeta)
        }
        .accessibilityAddTraits(.isHeader)
    }

    // MARK: Rows

    private func row(_ chat: Chat, now: Date) -> some View {
        let project = model.project(chat.project)
        return VStack(alignment: .leading, spacing: 0) {
            Button {
                open(chat.ref)
            } label: {
                HStack(spacing: 12) {
                    BadgedTile(project: project,
                               fallbackLetter: String(chat.project.prefix(1)).uppercased(),
                               state: chat.state)
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(chat.title)
                                .font(Theme.ui(16, weight: chat.state == .needsYou ? .semibold : .medium))
                                .foregroundStyle(chat.state == .idle ? Theme.textDim : Theme.textStrong)
                                .lineLimit(1)
                            Spacer(minLength: 4)
                            Text(Format.rowTime(chat.lastEvent, now: now))
                                .font(Theme.mono(12))
                                .foregroundStyle(timeColor(chat.state))
                        }
                        HStack(alignment: .firstTextBaseline, spacing: 5) {
                            Text(chat.host)
                                .font(Theme.mono(12))
                                .foregroundStyle(chat.offline ? Theme.hostDarkText : Theme.textSecondary)
                                .lineLimit(1)
                                .layoutPriority(1)
                            if !previewText(chat).isEmpty {
                                Text("·").foregroundStyle(Theme.dot)
                                Text(previewText(chat))
                                    .font(Theme.ui(14))
                                    .foregroundStyle(Theme.textMeta)
                                    .lineLimit(1)
                            }
                        }
                        .font(Theme.ui(14))
                    }
                }
                .padding(.vertical, 9)
                .frame(minHeight: 62)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityElement(children: .combine)
            .accessibilityHint(stateName(chat.state))

            if let permission = chat.permission, !chat.offline {
                permissionBlock(chat, permission)
                    .padding(.leading, 56)
                    .padding(.bottom, 12)
                    .padding(.top, 2)
            }
            if let message = rowError[chat.id] {
                Text(message)
                    .font(Theme.ui(13))
                    .foregroundStyle(Theme.hostDarkText)
                    .padding(.leading, 56)
                    .padding(.bottom, 8)
            }
        }
    }

    private func permissionBlock(_ chat: Chat, _ permission: Permission) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(permission.command)
                .font(Theme.mono(12.5))
                .foregroundStyle(Theme.textReply)
                .lineLimit(1)
                .truncationMode(.tail)
                .padding(.horizontal, 11)
                .padding(.vertical, 9)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Theme.surface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).stroke(Theme.codeBorder, lineWidth: 1))
            HStack(spacing: 8) {
                if let allow = permission.allowOnce {
                    Button("Allow once") { answer(chat, allow) }
                        .buttonStyle(AllowButtonStyle())
                }
                if let deny = permission.deny {
                    Button("Deny") { answer(chat, deny) }
                        .buttonStyle(SecondaryButtonStyle())
                }
            }
            .disabled(busy.contains(chat.id))
            .opacity(busy.contains(chat.id) ? 0.6 : 1)
        }
    }

    private func starButton(_ chat: Chat) -> some View {
        Button {
            Task {
                if let message = await model.setStar(chat, !chat.starred) {
                    rowError[chat.id] = message
                } else {
                    rowError[chat.id] = nil
                }
            }
        } label: {
            Label(chat.starred ? "Unstar" : "Star", systemImage: chat.starred ? "star.slash" : "star.fill")
        }
        .tint(Color(hex: 0x3A404B))
    }

    private func answer(_ chat: Chat, _ option: PermissionOption) {
        busy.insert(chat.id)
        Task {
            let message = await model.approve(chat, option: option)
            busy.remove(chat.id)
            rowError[chat.id] = message
        }
    }

    // MARK: Filtering and sections

    private func makeSections(now: Date) -> [ChatSection] {
        let needle = query.trimmingCharacters(in: .whitespaces).lowercased()
        let visible = model.chats.filter { chat in
            if needsOnly && chat.state != .needsYou { return false }
            if let project = projectFilter, chat.project != project { return false }
            if let server = serverFilter, chat.host != server { return false }
            if needle.isEmpty { return true }
            let projectName = model.project(chat.project)?.name ?? chat.project
            let haystack = [chat.title, chat.host, chat.session, projectName, chat.preview]
                .joined(separator: " ").lowercased()
            return haystack.contains(needle)
        }
        var starred: [Chat] = []
        var today: [Chat] = []
        var yesterday: [Chat] = []
        var earlier: [Chat] = []
        let calendar = Calendar.current
        for chat in visible {
            let when = Format.date(chat.lastEvent)
            if chat.starred {
                starred.append(chat)
            } else if calendar.isDate(when, inSameDayAs: now) {
                today.append(chat)
            } else if let previous = calendar.date(byAdding: .day, value: -1, to: now),
                      calendar.isDate(when, inSameDayAs: previous) {
                yesterday.append(chat)
            } else {
                earlier.append(chat)
            }
        }
        var out: [ChatSection] = []
        if !starred.isEmpty { out.append(ChatSection(id: "starred", label: "Starred", starred: true, chats: starred)) }
        if !today.isEmpty { out.append(ChatSection(id: "today", label: "Today", starred: false, chats: today)) }
        if !yesterday.isEmpty { out.append(ChatSection(id: "yesterday", label: "Yesterday", starred: false, chats: yesterday)) }
        if !earlier.isEmpty { out.append(ChatSection(id: "earlier", label: "Earlier", starred: false, chats: earlier)) }
        return out
    }

    private var emptyText: String {
        if !query.isEmpty || needsOnly || projectFilter != nil || serverFilter != nil {
            return "No chats match"
        }
        return "No chats yet"
    }

    private func previewText(_ chat: Chat) -> String {
        if chat.offline { return "\(chat.host) is not answering" }
        if chat.permission != nil { return "Wants to run a command" }
        return chat.preview
    }

    private func timeColor(_ state: ChatState) -> Color {
        switch state {
        case .needsYou: return Theme.needsYou
        case .working: return Theme.working
        case .idle: return Theme.textMeta
        }
    }

    private func stateName(_ state: ChatState) -> String {
        switch state {
        case .needsYou: return "Needs you"
        case .working: return "Working"
        case .idle: return "Idle"
        }
    }
}
