import SwiftUI
import UIKit

// MARK: - Project tile

@MainActor
struct ProjectTile: View {
    let project: Project?
    var fallbackLetter: String = "?"
    var size: CGFloat = 44
    var dimmed: Bool = false

    var body: some View {
        let colours = Theme.tile(for: project)
        let letter = project?.letter ?? fallbackLetter
        Text(letter)
            .font(Theme.mono(size * 0.41, weight: .bold))
            .foregroundStyle(colours.1)
            .frame(width: size, height: size)
            .background(colours.0, in: RoundedRectangle(cornerRadius: size * 0.27, style: .continuous))
            .opacity(dimmed ? 0.75 : 1)
            .accessibilityHidden(true)
    }
}

/// The tile with its state badge: an amber dot for needs you, a blue
/// spinner for working, nothing for idle.
@MainActor
struct BadgedTile: View {
    let project: Project?
    let fallbackLetter: String
    let state: ChatState

    var body: some View {
        ProjectTile(project: project, fallbackLetter: fallbackLetter, size: 44, dimmed: state == .idle)
            .overlay(alignment: .bottomTrailing) {
                switch state {
                case .needsYou:
                    Circle()
                        .fill(Theme.needsYou)
                        .frame(width: 10, height: 10)
                        .padding(3)
                        .background(Circle().fill(Theme.bg))
                        .offset(x: 4, y: 4)
                case .working:
                    Spinner(size: 13)
                        .frame(width: 19, height: 19)
                        .background(Circle().fill(Theme.bg))
                        .offset(x: 5, y: 5)
                case .idle:
                    EmptyView()
                }
            }
            .frame(width: 44, height: 44)
    }
}

// MARK: - Spinner

/// The mockups' working arc: a faint ring with a quarter arc going round.
@MainActor
struct Spinner: View {
    var size: CGFloat = 13
    var color: Color = Theme.working
    @State private var turning = false

    var body: some View {
        ZStack {
            Circle()
                .stroke(color.opacity(0.3), lineWidth: size * 0.17)
            Circle()
                .trim(from: 0, to: 0.25)
                .stroke(color, style: StrokeStyle(lineWidth: size * 0.17, lineCap: .round))
                .rotationEffect(.degrees(turning ? 360 : 0))
        }
        .frame(width: size, height: size)
        .onAppear {
            withAnimation(.linear(duration: 0.9).repeatForever(autoreverses: false)) {
                turning = true
            }
        }
        .accessibilityLabel("Working")
    }
}

// MARK: - State pill

@MainActor
struct StatePill: View {
    let state: ChatState
    var offline: Bool = false

    var body: some View {
        HStack(spacing: 6) {
            if offline {
                Circle().fill(Theme.hostDarkRing).frame(width: 8, height: 8)
                Text("Offline")
            } else {
                switch state {
                case .needsYou:
                    Circle().fill(Theme.needsYou).frame(width: 8, height: 8)
                    Text("Needs you")
                case .working:
                    Spinner(size: 10)
                    Text("Working")
                case .idle:
                    Circle().fill(Theme.textMeta).frame(width: 8, height: 8)
                    Text("Idle")
                }
            }
        }
        .font(Theme.ui(12.5, weight: .semibold))
        .foregroundStyle(tint)
        .padding(.leading, 8)
        .padding(.trailing, 10)
        .padding(.vertical, 3)
        .background(tint.opacity(0.14), in: Capsule())
        .fixedSize()
    }

    private var tint: Color {
        if offline { return Theme.hostDarkText }
        switch state {
        case .needsYou: return Theme.needsYou
        case .working: return Theme.working
        case .idle: return Theme.textSecondary
        }
    }
}

// MARK: - Chips

/// A 36pt pill inside a 44pt tap target.
@MainActor
struct ChipLabel<Leading: View>: View {
    let title: String
    var isOn: Bool = false
    var tint: Color? = nil
    var trailing: String? = nil
    var chevron: Bool = false
    @ViewBuilder var leading: () -> Leading

    var body: some View {
        HStack(spacing: 7) {
            leading()
            Text(title)
                .font(Theme.ui(14, weight: .medium))
                .lineLimit(1)
            if let trailing = trailing {
                Text(trailing)
                    .font(Theme.mono(12))
                    .foregroundStyle(tint ?? Theme.textSecondary)
            }
            if chevron {
                Image(systemName: "chevron.down")
                    .font(.system(size: 11, weight: .semibold))
            }
        }
        .foregroundStyle(tint ?? (isOn ? Theme.textStrong : Theme.textChip))
        .padding(.horizontal, 14)
        .frame(height: 36)
        .background(background, in: Capsule())
        .overlay(Capsule().stroke(border, lineWidth: 1))
        .frame(minHeight: 44)
        .contentShape(Rectangle())
    }

    private var background: Color {
        if let tint = tint { return tint.opacity(0.12) }
        return isOn ? Theme.chipOn : Color.clear
    }

    private var border: Color {
        if let tint = tint { return tint.opacity(0.4) }
        return isOn ? Theme.chipOnBorder : Theme.border
    }
}

extension ChipLabel where Leading == EmptyView {
    init(title: String, isOn: Bool = false, tint: Color? = nil, trailing: String? = nil, chevron: Bool = false) {
        self.init(title: title, isOn: isOn, tint: tint, trailing: trailing, chevron: chevron, leading: { EmptyView() })
    }
}

/// A project chip with its small tile.
@MainActor
struct ProjectChip: View {
    let project: Project
    var isOn: Bool
    var count: Int? = nil

    var body: some View {
        ChipLabel(title: project.name, isOn: isOn,
                  trailing: count.map { String($0) }) {
            ProjectTile(project: project, size: 24)
        }
    }
}

// MARK: - Buttons

struct PrimaryButtonStyle: ButtonStyle {
    var height: CGFloat = 52

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(Theme.ui(16.5, weight: .semibold))
            .foregroundStyle(Theme.onPrimary)
            .frame(maxWidth: .infinity)
            .frame(height: height)
            .background(Theme.primary, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            .opacity(configuration.isPressed ? 0.8 : 1)
    }
}

/// Allow once: amber, full width, 44pt.
struct AllowButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(Theme.ui(15, weight: .semibold))
            .foregroundStyle(Theme.onNeedsYou)
            .frame(maxWidth: .infinity)
            .frame(minHeight: 44)
            .background(Theme.needsYou, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            .opacity(configuration.isPressed ? 0.8 : 1)
    }
}

struct SecondaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(Theme.ui(15, weight: .medium))
            .foregroundStyle(Color(hex: 0xE3E7ED))
            .frame(maxWidth: .infinity)
            .frame(minHeight: 44)
            .background(Theme.secondaryButton, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).stroke(Theme.selectedRing, lineWidth: 1))
            .opacity(configuration.isPressed ? 0.8 : 1)
    }
}

/// The round 44pt light button: compose, new note, send.
@MainActor
struct RoundPrimaryButton: View {
    let systemImage: String
    let label: String
    var enabled: Bool = true
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: systemImage)
                .font(.system(size: 17, weight: .bold))
                .foregroundStyle(Theme.onPrimary)
                .frame(width: 44, height: 44)
                .background(Circle().fill(enabled ? Theme.primary : Theme.primary.opacity(0.35)))
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .accessibilityLabel(label)
    }
}

// MARK: - Link line

/// The one quiet line for a link that is not working. Never a dialog.
@MainActor
struct LinkLine: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        switch model.link {
        case .ok, .connecting:
            EmptyView()
        case .unreachable:
            line(text: "Can't reach the Mac mini", button: "Retry") { model.retry() }
        case .unpaired:
            line(text: "This device is no longer paired", button: "Pair again") {
                Task { await model.unpair(revoke: false) }
            }
        case .certificateChanged:
            line(text: "The Mac mini's certificate changed", button: "Pair again") {
                Task { await model.unpair(revoke: false) }
            }
        }
    }

    private func line(text: String, button: String, action: @escaping () -> Void) -> some View {
        HStack(spacing: 8) {
            Circle().fill(Theme.hostDarkRing).frame(width: 6, height: 6)
            Text(text)
                .font(Theme.ui(14))
                .foregroundStyle(Theme.textSecondary)
            Spacer(minLength: 8)
            Button(button, action: action)
                .font(Theme.ui(14, weight: .semibold))
                .foregroundStyle(Theme.textStrong)
                .frame(minWidth: 44, minHeight: 44)
                .buttonStyle(.plain)
        }
    }
}

// MARK: - Reply text

enum ReplyText {
    /// Claude's reply as shown: line breaks kept, **bold** rendered,
    /// everything else plain.
    static func attributed(_ text: String, size: CGFloat = 15) -> AttributedString {
        var out = AttributedString()
        let parts = text.components(separatedBy: "**")
        let balanced = parts.count % 2 == 1
        for (index, part) in parts.enumerated() {
            let odd = index % 2 == 1
            if odd && !balanced && index == parts.count - 1 {
                // An unmatched "**": put it back and leave the rest plain.
                out.append(AttributedString("**" + part))
                continue
            }
            var piece = AttributedString(part)
            if odd {
                piece.font = Font.system(size: size, weight: .semibold)
                piece.foregroundColor = Theme.textStrong
            }
            out.append(piece)
        }
        return out
    }

    /// http(s) links in a reply, in order, without repeats, at most three.
    static func links(in text: String) -> [URL] {
        guard let detector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue) else {
            return []
        }
        let range = NSRange(text.startIndex..<text.endIndex, in: text)
        var seen = Set<String>()
        var out: [URL] = []
        for match in detector.matches(in: text, options: [], range: range) {
            guard let url = match.url,
                  let scheme = url.scheme?.lowercased(),
                  scheme == "http" || scheme == "https" else { continue }
            if seen.insert(url.absoluteString).inserted {
                out.append(url)
            }
            if out.count == 3 { break }
        }
        return out
    }
}

// MARK: - Haptics

@MainActor
enum Haptics {
    static func success() {
        UINotificationFeedbackGenerator().notificationOccurred(.success)
    }

    static func tap() {
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
    }
}
