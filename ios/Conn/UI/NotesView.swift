import SwiftUI

/// Notes (iOSNotes mockup): project chips with counts, note cards, a jot
/// box above the tab bar. Swipe a card to mark it done or delete it.
@MainActor
struct NotesTab: View {
    var body: some View {
        NavigationStack {
            NotesView()
        }
    }
}

@MainActor
struct NotesView: View {
    @EnvironmentObject private var model: AppModel

    @State private var jot = ""
    @State private var jotting = false
    @State private var showDone = false
    @State private var busy: Set<String> = []
    @State private var cardError: [String: String] = [:]
    @State private var jotError: String?
    @FocusState private var jotFocused: Bool

    /// Notes for the chosen chip, done ones last and only when asked for.
    private var shown: [Note] {
        model.notes.filter { note in
            if let project = model.notesProject, note.project != project { return false }
            return showDone || !note.isDone
        }
    }

    private var doneCount: Int {
        model.notes.filter { note in
            note.isDone && (model.notesProject == nil || note.project == model.notesProject)
        }.count
    }

    /// Where a jotted note goes: the chosen chip, else the busiest project.
    private var jotProject: Project? {
        if let id = model.notesProject, let project = model.project(id) { return project }
        return model.recentProjects.first
    }

    var body: some View {
        TimelineView(.periodic(from: .now, by: 60)) { context in
            list(now: context.date)
        }
        .navigationTitle("Notes")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button {
                    model.compose = ComposeRequest(kind: .note, project: model.notesProject)
                } label: {
                    Image(systemName: "plus")
                        .font(.system(size: 17, weight: .bold))
                        .foregroundStyle(Theme.onPrimary)
                        .frame(width: 36, height: 36)
                        .background(Circle().fill(Theme.primary))
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("New note")
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            jotBox
        }
    }

    private func list(now: Date) -> some View {
        List {
            VStack(alignment: .leading, spacing: 0) {
                LinkLine()
                chips
            }
            .listRowInsets(EdgeInsets(top: 0, leading: 16, bottom: 4, trailing: 16))
            .listRowBackground(Theme.bg)
            .listRowSeparator(.hidden)

            ForEach(shown) { note in
                card(note, now: now)
                    .listRowInsets(EdgeInsets(top: 5, leading: 16, bottom: 5, trailing: 16))
                    .listRowBackground(Theme.bg)
                    .listRowSeparator(.hidden)
                    .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                        Button(role: .destructive) {
                            run(note) { await model.deleteNote(note) }
                        } label: {
                            Label("Delete", systemImage: "trash")
                        }
                        if note.isDone {
                            Button {
                                run(note) { await model.setNoteStatus(note, "open") }
                            } label: {
                                Label("Reopen", systemImage: "arrow.uturn.backward")
                            }
                            .tint(Color(hex: 0x3A404B))
                        } else {
                            Button {
                                run(note) { await model.setNoteStatus(note, "done") }
                            } label: {
                                Label("Done", systemImage: "checkmark")
                            }
                            .tint(Color(hex: 0x2E6B4A))
                        }
                    }
                    .swipeActions(edge: .leading, allowsFullSwipe: true) {
                        if !note.isDone {
                            Button {
                                run(note) { await model.setNoteStatus(note, "done") }
                            } label: {
                                Label("Done", systemImage: "checkmark")
                            }
                            .tint(Color(hex: 0x2E6B4A))
                        }
                    }
            }

            if doneCount > 0 {
                Button(showDone ? "Hide done" : "Show done (\(doneCount))") {
                    showDone.toggle()
                }
                .font(Theme.ui(14, weight: .medium))
                .foregroundStyle(Theme.textSecondary)
                .frame(maxWidth: .infinity, minHeight: 44)
                .buttonStyle(.plain)
                .listRowBackground(Theme.bg)
                .listRowSeparator(.hidden)
            }

            if shown.isEmpty && model.loaded {
                Text("No notes yet")
                    .font(Theme.ui(15))
                    .foregroundStyle(Theme.textMeta)
                    .frame(maxWidth: .infinity)
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

    // MARK: Chips

    private var chips: some View {
        let counts = Dictionary(grouping: model.notes.filter { !$0.isDone }, by: { $0.project })
            .mapValues { $0.count }
        let withNotes = model.recentProjects.filter { (counts[$0.id] ?? 0) > 0 || $0.id == model.notesProject }
        return ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                Button {
                    model.notesProject = nil
                } label: {
                    ChipLabel(title: "All", isOn: model.notesProject == nil)
                }
                .buttonStyle(.plain)
                ForEach(withNotes) { project in
                    Button {
                        model.notesProject = project.id
                    } label: {
                        ProjectChip(project: project, isOn: model.notesProject == project.id,
                                    count: counts[project.id])
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.top, 8)
        }
        .scrollClipDisabled()
    }

    // MARK: Cards

    private func card(_ note: Note, now: Date) -> some View {
        let linked = note.chat.isEmpty ? nil : model.chat(note.chat)
        let when = note.createdDate.map { Format.stamp($0, now: now) } ?? ""
        return VStack(alignment: .leading, spacing: 10) {
            if note.isRecap {
                HStack(spacing: 8) {
                    Text("RECAP")
                        .font(Theme.ui(11.5, weight: .semibold))
                        .tracking(0.7)
                        .foregroundStyle(Theme.textSecondary)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 2)
                        .background(Theme.secondaryButton, in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                    if !note.chat.isEmpty {
                        Text("from \(linked?.title ?? note.chat)")
                            .font(Theme.ui(12.5))
                            .foregroundStyle(Theme.textMeta)
                            .lineLimit(1)
                    }
                }
            }
            Text(note.text)
                .font(Theme.ui(15.5))
                .lineSpacing(3)
                .foregroundStyle(note.isOpen || note.isRecap ? Color(hex: 0xE3E7ED) : Theme.textDim)
                .strikethrough(note.isDone, color: Theme.textMeta)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            HStack(spacing: 8) {
                Text(footer(note, when: when))
                    .font(Theme.ui(12.5))
                    .foregroundStyle(Theme.textMeta)
                    .lineLimit(1)
                Spacer(minLength: 6)
                action(note, linked: linked)
            }
            if let message = cardError[note.id] {
                Text(message)
                    .font(Theme.ui(13))
                    .foregroundStyle(Theme.hostDarkText)
            }
        }
        .padding(.horizontal, 14)
        .padding(.top, 14)
        .padding(.bottom, 12)
        .background(Theme.surface, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).stroke(Theme.divider, lineWidth: 1))
        .opacity(busy.contains(note.id) ? 0.6 : 1)
    }

    private func footer(_ note: Note, when: String) -> String {
        var parts: [String] = []
        if !when.isEmpty { parts.append(when) }
        if model.notesProject == nil, let project = model.project(note.project) {
            parts.append(project.name)
        }
        if note.isDone {
            parts.append("done")
        } else if !note.isRecap && !note.source.isEmpty {
            parts.append(Format.source(note.source))
        }
        return parts.joined(separator: " · ")
    }

    @ViewBuilder
    private func action(_ note: Note, linked: Chat?) -> some View {
        if note.isRecap, let ref = ChatRef(id: note.chat) {
            Button {
                model.open(ref)
            } label: {
                Text("Open chat")
                    .font(Theme.ui(13.5, weight: .medium))
                    .foregroundStyle(Color(hex: 0xE3E7ED))
                    .padding(.horizontal, 12)
                    .frame(height: 36)
                    .background(Theme.raised, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).stroke(Theme.border, lineWidth: 1))
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        } else if !note.chat.isEmpty, let ref = ChatRef(id: note.chat) {
            Button {
                model.open(ref)
            } label: {
                HStack(spacing: 7) {
                    if linked?.state == .working {
                        Spinner(size: 12)
                    } else if linked?.state == .needsYou {
                        Circle().fill(Theme.needsYou).frame(width: 8, height: 8)
                    }
                    Text(linked?.title ?? ref.session)
                        .lineLimit(1)
                }
                .font(Theme.ui(13.5, weight: .medium))
                .foregroundStyle(Theme.workingText)
                .padding(.horizontal, 12)
                .frame(height: 36)
                .background(Theme.working.opacity(0.12), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .frame(minHeight: 44)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Open chat \(linked?.title ?? ref.session)")
        } else if note.isOpen {
            Button {
                run(note) { await model.startChat(from: note) }
            } label: {
                HStack(spacing: 7) {
                    if busy.contains(note.id) {
                        ProgressView().tint(Theme.onPrimary).scaleEffect(0.7)
                    } else {
                        Image(systemName: "square.and.pencil")
                            .font(.system(size: 12, weight: .bold))
                    }
                    Text("Start chat")
                }
                .font(Theme.ui(13.5, weight: .semibold))
                .foregroundStyle(Theme.onPrimary)
                .padding(.horizontal, 12)
                .frame(height: 36)
                .background(Theme.primary, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .frame(minHeight: 44)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(busy.contains(note.id))
        }
    }

    // MARK: Jot box

    private var jotBox: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let jotError = jotError {
                Text(jotError)
                    .font(Theme.ui(13))
                    .foregroundStyle(Theme.hostDarkText)
                    .padding(.horizontal, 6)
            }
            HStack(spacing: 6) {
                TextField("", text: $jot,
                          prompt: Text("Jot a note for \(jotProject?.name ?? "a project")").foregroundColor(Theme.textMeta),
                          axis: .vertical)
                    .font(Theme.ui(16))
                    .foregroundStyle(Theme.textStrong)
                    .lineLimit(1...4)
                    .focused($jotFocused)
                    .submitLabel(.done)
                    .padding(.vertical, 11)
                if !jot.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    Button {
                        saveJot()
                    } label: {
                        Group {
                            if jotting {
                                ProgressView().tint(Theme.onPrimary)
                            } else {
                                Image(systemName: "arrow.up")
                                    .font(.system(size: 15, weight: .bold))
                            }
                        }
                        .foregroundStyle(Theme.onPrimary)
                        .frame(width: 34, height: 34)
                        .background(Circle().fill(Theme.primary))
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(jotting || jotProject == nil)
                    .keyboardShortcut(.return, modifiers: .command)
                    .accessibilityLabel("Save note")
                }
            }
            .padding(.leading, 16)
            .padding(.trailing, 4)
            .frame(minHeight: 44)
            .background(Theme.surface2, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 22, style: .continuous).stroke(Theme.border, lineWidth: 1))
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(Theme.bg.opacity(0.96))
        .overlay(alignment: .top) {
            Rectangle().fill(Theme.divider).frame(height: 1)
        }
    }

    // MARK: Actions

    private func run(_ note: Note, _ action: @escaping () async -> String?) {
        busy.insert(note.id)
        cardError[note.id] = nil
        Task {
            let failure = await action()
            busy.remove(note.id)
            cardError[note.id] = failure
        }
    }

    private func saveJot() {
        let text = jot.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, let project = jotProject else { return }
        jotting = true
        jotError = nil
        Task {
            let failure = await model.createNote(project: project.id, text: text)
            jotting = false
            if let failure = failure {
                jotError = failure
            } else {
                if jot.trimmingCharacters(in: .whitespacesAndNewlines) == text { jot = "" }
                Haptics.success()
            }
        }
    }
}
