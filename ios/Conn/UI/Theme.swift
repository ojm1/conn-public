import SwiftUI

/// SPEC §8 design tokens. Dark only in v1.
///
/// The mockups name Geist for UI and JetBrains Mono for hosts, paths, times
/// and code. Neither is bundled yet, so UI text is the system font and
/// `Theme.mono` is SF Mono; swapping in the real faces is one change here.
enum Theme {
    static let bg = Color(hex: 0x0C0D10)
    static let list = Color(hex: 0x101216)
    static let surface = Color(hex: 0x121418)
    static let surface2 = Color(hex: 0x15181D)
    static let raised = Color(hex: 0x1A1D24)
    static let header = Color(hex: 0x0E1013)
    static let selected = Color(hex: 0x1E222A)
    static let selectedRing = Color(hex: 0x2E343E)
    static let divider = Color(hex: 0x1E2229)
    static let border = Color(hex: 0x2A2F38)
    static let codeBorder = Color(hex: 0x232831)
    static let chipOn = Color(hex: 0x262B34)
    static let chipOnBorder = Color(hex: 0x3A404B)
    static let bubble = Color(hex: 0x1C2029)
    static let secondaryButton = Color(hex: 0x1C1F26)

    static let textStrong = Color(hex: 0xF3F5F8)
    static let text = Color(hex: 0xECEEF2)
    static let textSecondary = Color(hex: 0xA3ABB8)
    static let textMeta = Color(hex: 0x8C95A4)
    static let textChip = Color(hex: 0xC7CDD7)
    static let textReply = Color(hex: 0xD7DBE2)
    static let textDim = Color(hex: 0xC9CED8)
    static let dot = Color(hex: 0x4A515E)

    static let needsYou = Color(hex: 0xFFB04A)
    static let onNeedsYou = Color(hex: 0x1A1206)
    static let working = Color(hex: 0x6F9CFF)
    static let workingText = Color(hex: 0x8FB2FF)
    static let hostUp = Color(hex: 0x4FBF7E)
    static let hostDarkRing = Color(hex: 0xE2725F)
    static let hostDarkText = Color(hex: 0xF0806C)
    static let mode = Color(hex: 0xC79BFF)
    static let claude = Color(hex: 0xE8956B)
    static let claudeTile = Color(hex: 0x2A2018)

    static let primary = Color(hex: 0xECEEF2)
    static let onPrimary = Color(hex: 0x0C0D10)

    static func mono(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .monospaced)
    }

    static func ui(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight)
    }

    // MARK: Project tiles

    /// SPEC §8 defaults by project name, folded to letters.
    private static let tileDefaults: [String: (UInt32, UInt32)] = [
        "atlas": (0x1F3A37, 0x8FE0D2),
        "acme": (0x2E2A4A, 0xBFB0FF),
        "widgets": (0x43263A, 0xF5AECF),
        "orbit": (0x2C3A22, 0xB7DE8F),
        "northwind": (0x233240, 0x8EC5F0),
        "summit": (0x3A2E26, 0xE9BC9A),
        "personal": (0x2A2F38, 0xD0D5DE),
    ]

    /// (background, letter colour) for a project: the agent's `bg`/`color`,
    /// else the SPEC default for its name, else a muted background derived
    /// from its colour, else Personal's grey.
    static func tile(for project: Project?) -> (Color, Color) {
        guard let project = project else {
            return (Color(hex: 0x2A2F38), Color(hex: 0xD0D5DE))
        }
        let fg = Color(hexString: project.color)
        if let bg = Color(hexString: project.bg), let fg = fg {
            return (bg, fg)
        }
        let folded = project.name.lowercased().filter { $0.isLetter || $0.isNumber }
        let key = project.id.lowercased().filter { $0.isLetter || $0.isNumber }
        if fg == nil, let pair = tileDefaults[key] ?? tileDefaults[folded] {
            return (Color(hex: pair.0), Color(hex: pair.1))
        }
        if let raw = Color.rgb(hexString: project.color) {
            // 22% of the colour over the page background.
            let base: (Double, Double, Double) = (12.0 / 255, 13.0 / 255, 16.0 / 255)
            let mix = 0.22
            let bg = Color(.sRGB,
                           red: base.0 + (raw.0 - base.0) * mix,
                           green: base.1 + (raw.1 - base.1) * mix,
                           blue: base.2 + (raw.2 - base.2) * mix,
                           opacity: 1)
            return (bg, Color(.sRGB, red: raw.0, green: raw.1, blue: raw.2, opacity: 1))
        }
        return (Color(hex: 0x2A2F38), Color(hex: 0xD0D5DE))
    }
}

extension Color {
    init(hex: UInt32) {
        self.init(.sRGB,
                  red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255,
                  opacity: 1)
    }

    /// "#BFB0FF" or "BFB0FF"; nil for anything else.
    init?(hexString: String) {
        guard let rgb = Color.rgb(hexString: hexString) else { return nil }
        self.init(.sRGB, red: rgb.0, green: rgb.1, blue: rgb.2, opacity: 1)
    }

    static func rgb(hexString: String) -> (Double, Double, Double)? {
        var text = hexString.trimmingCharacters(in: .whitespaces)
        if text.hasPrefix("#") { text.removeFirst() }
        guard text.count == 6, let value = UInt32(text, radix: 16) else { return nil }
        return (Double((value >> 16) & 0xFF) / 255,
                Double((value >> 8) & 0xFF) / 255,
                Double(value & 0xFF) / 255)
    }
}
