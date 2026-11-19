import SwiftUI

/// New (iOSNew mockup): a Chat / Note switch, a project picker with recent
/// chips, a big text box, and for a chat the Project notes toggle and
/// "Start on <server>". Starting a chat opens it; saving a note returns to
/// Notes.
@MainActor
struct NewSheet: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss

    @State private var kind: ComposeKind
    @State private var projectID: String?
    @State private var server: String?
    @State private var text = ""
    @State private var withNotes = true
    @State private var busy = false
    @State private var error: String?
    @FocusState private var textFocused: Bool

    init(request: ComposeRequest) {
        _kind = State(initialValue: request.kind)
        _projectID = State(initialValue: request.project)
    }

    private var project: Project? {
        if let id = projectID, let found = model.project(id) { return found }
        return model.recentProjects.first
    }

    private var targetServer: String? {
        guard let project = project else { return nil }
        if let server = server, project.servers[server] != nil { return server }
        return project.defaultServer
    }

    private var trimmed: String { text.trimmingCharacters(in: .whitespacesAndNewlines) }

    private var canGo: Bool { !busy && !trimmed.isEmpty && project != nil }

    var body: some View {
        VStack(spacing: 0) {
            topBar
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    projectPicker
                    recentChips
                    textBox
                    if kind == .chat {
                        chatOptions
                    } else {
                        Text("Saved to \(project?.name ?? "the project")'s notes. Start a chat from it any time.")
                            .font(Theme.ui(14))
                            .foregroundStyle(Theme.textMeta)
                            .lineSpacing(3)
                    }
                    if let error = error {
                        Text(error)
                            .font(Theme.ui(14))
                            .foregroundStyle(Theme.hostDarkText)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.top, 16)
            }
            .scrollDismissesKeyboard(.interactively)
            goButton
        }
        .background(Theme.surface2)
        .onAppear {
            if projectID == nil { projectID = model.recentProjects.first?.id }
            textFocused = true
        }
        .onChange(of: projectID) { _, _ in server = nil }
    }

    // MARK: Parts

    private var topBar: some View {
        ZStack {
            HStack {
                Button("Cancel") { dismiss() }
                    .font(Theme.ui(16))
                    .foregroundStyle(Theme.textChip)
                    .frame(minWidth: 44, minHeight: 44)
                    .padding(.horizontal, 10)
                    .buttonStyle(.plain)
                Spacer()
            }
            HStack(spacing: 2) {
                segment("Chat", .chat)
                segment("Note", .note)
            }
            .padding(2)
            .background(Theme.bg, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).stroke(Color(hex: 0x262B34), lineWidth: 1))
        }
        .padding(.horizontal, 8)
        .padding(.top, 14)
    }

    private func segment(_ title: String, _ value: ComposeKind) -> some View {
        Button {
            kind = value
            error = nil
        } label: {
            Text(title)
                .font(Theme.ui(14, weight: .semibold))
                .foregroundStyle(kind == value ? Theme.textStrong : Theme.textMeta)
                .padding(.horizontal, 18)
                .frame(height: 32)
                .background(kind == value ? Theme.border : Color.clear,
                            in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                .frame(minHeight: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(kind == value ? .isSelected : [])
    }

    private var projectPicker: some View {
        Menu {
            ForEach(model.recentProjects) { item in
                Button(item.name) { projectID = item.id }
            }
        } label: {
            HStack(spacing: 12) {
                ProjectTile(project: project, size: 40)
                VStack(alignment: .leading, spacing: 2) {
                    Text(project?.name ?? "Pick a project")
                        .font(Theme.ui(16, weight: .semibold))
                        .foregroundStyle(Theme.textStrong)
                    Text(whereLine)
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.textMeta)
                        .lineLimit(1)
                }
                Spacer(minLength: 6)
                Image(systemName: "chevron.down")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Theme.textMeta)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .frame(minHeight: 60)
            .background(Theme.raised, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).stroke(Theme.border, lineWidth: 1))
            .contentShape(Rectangle())
        }
        .accessibilityLabel("Project, \(project?.name ?? "none")")
    }

    private var whereLine: String {
        guard let project = project else { return "" }
        if kind == .note {
            let count = model.notes.filter { $0.project == project.id && !$0.isDone }.count
            return count == 1 ? "1 note" : "\(count) notes"
        }
        guard let server = targetServer else { return "no servers" }
        let folder = project.servers[server] ?? ""
        return folder.isEmpty ? server : "\(server) · \(folder)"
    }

    private var recentChips: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(Array(model.recentProjects.prefix(6))) { item in
                    Button {
                        projectID = item.id
                    } label: {
                        ProjectChip(project: item, isOn: item.id == project?.id)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .scrollClipDisabled()
    }

    private var textBox: some View {
        ZStack(alignment: .topLeading) {
            if text.isEmpty {
                Text(kind == .chat ? "What should Claude do?" : "A note for \(project?.name ?? "this project")")
                    .font(Theme.ui(17))
                    .foregroundStyle(Theme.textMeta)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 14)
                    .allowsHitTesting(false)
            }
            TextEditor(text: $text)
                .font(Theme.ui(17))
                .foregroundStyle(Theme.textStrong)
                .scrollContentBackground(.hidden)
                .focused($textFocused)
                .keyboardDone()
                .padding(.horizontal, 9)
                .padding(.vertical, 6)
        }
        .frame(minHeight: 150)
        .background(Color(hex: 0x0F1114), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).stroke(Theme.border, lineWidth: 1))
        .accessibilityLabel(kind == .chat ? "Task for Claude on \(project?.name ?? "")" : "Note for \(project?.name ?? "")")
    }

    private var chatOptions: some View {
        // Agent and mode come from the project on the Mac mini (POST /chats
        // takes neither), so they are shown, not chosen.
        FlowRow {
            ChipLabel(title: agentName)
                .opacity(0.8)
            if let mode = project?.mode, !mode.isEmpty {
                ChipLabel(title: "\(mode) mode", tint: Theme.mode)
            }
            if let project = project, project.serverNames.count > 1 {
                Menu {
                    ForEach(project.serverNames, id: \.self) { name in
                        Button {
                            server = name
                        } label: {
                            Text(project.serverStatus[name] == "up" ? name : "\(name) (not answering)")
                        }
                    }
                } label: {
                    ChipLabel(title: targetServer ?? "Server", chevron: true)
                }
            }
            Button {
                withNotes.toggle()
            } label: {
                ChipLabel(title: "Project notes", isOn: withNotes) {
                    if withNotes {
                        Image(systemName: "checkmark")
                            .font(.system(size: 12, weight: .bold))
                    }
                }
            }
            .buttonStyle(.plain)
            .accessibilityAddTraits(withNotes ? .isSelected : [])
        }
    }

    private var agentName: String {
        let agent = project?.agent ?? ""
        if agent.isEmpty || agent == "claude" { return "Claude Code" }
        return agent
    }

    private var goButton: some View {
        Button {
            go()
        } label: {
            HStack(spacing: 10) {
                if busy {
                    ProgressView().tint(Theme.onPrimary)
                }
                Text(kind == .chat ? "Start on \(targetServer ?? "server")" : "Save note")
            }
        }
        .buttonStyle(PrimaryButtonStyle())
        .disabled(!canGo)
        .opacity(canGo ? 1 : 0.5)
        .keyboardShortcut(.return, modifiers: .command)
        .padding(.horizontal, 16)
        .padding(.top, 12)
        .padding(.bottom, 12)
    }

    // MARK: Go

    private func go() {
        guard canGo, let project = project else { return }
        busy = true
        error = nil
        let body = trimmed
        let chosen = targetServer
        let projectId = project.id
        let isChat = kind == .chat
        let notesOn = withNotes
        Task {
            let failure: String?
            if isChat {
                failure = await model.startChat(project: projectId, host: chosen, text: body, includeNotes: notesOn)
            } else {
                failure = await model.createNote(project: projectId, text: body)
            }
            busy = false
            if let failure = failure {
                error = failure
                return
            }
            Haptics.success()
            if !isChat {
                model.notesProject = projectId
                model.tab = .notes
            }
            dismiss()
        }
    }
}

/// Chips that wrap onto a second line when they run out of room.
struct FlowRow: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0
        var y: CGFloat = 0
        var rowHeight: CGFloat = 0
        var widest: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > 0 && x + size.width > width {
                x = 0
                y += rowHeight + spacing
                rowHeight = 0
            }
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
            widest = max(widest, x - spacing)
        }
        return CGSize(width: width.isFinite ? width : widest, height: y + rowHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX
        var y = bounds.minY
        var rowHeight: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > bounds.minX && x + size.width > bounds.maxX {
                x = bounds.minX
                y += rowHeight + spacing
                rowHeight = 0
            }
            view.place(at: CGPoint(x: x, y: y), anchor: .topLeading, proposal: ProposedViewSize(size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}
