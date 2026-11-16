import Foundation

/// Times the way the mockups write them: "14m" in a row, "Wed 11:23" on a
/// message, "waiting 14m" in a header.
enum Format {
    private static let iso: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    private static let isoFraction: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    private static func formatter(_ template: String) -> DateFormatter {
        let formatter = DateFormatter()
        formatter.locale = Locale.current
        formatter.setLocalizedDateFormatFromTemplate(template)
        return formatter
    }

    private static let hourMinute: DateFormatter = formatter("HH:mm")
    private static let weekday: DateFormatter = formatter("EEE")
    private static let dayMonth: DateFormatter = formatter("d MMM")

    static func isoDate(_ text: String) -> Date? {
        if text.isEmpty { return nil }
        return iso.date(from: text) ?? isoFraction.date(from: text)
    }

    static func date(_ unix: TimeInterval) -> Date {
        Date(timeIntervalSince1970: unix)
    }

    /// "now", "4m", "3h", "2d".
    static func age(_ seconds: TimeInterval) -> String {
        let s = max(0, Int(seconds))
        if s < 60 { return "now" }
        if s < 3600 { return "\(s / 60)m" }
        if s < 86_400 { return "\(s / 3600)h" }
        return "\(s / 86_400)d"
    }

    /// A row's time: an age today, a weekday this week, else "5 Nov".
    static func rowTime(_ unix: TimeInterval, now: Date) -> String {
        guard unix > 0 else { return "" }
        let when = date(unix)
        let calendar = Calendar.current
        if calendar.isDate(when, inSameDayAs: now) {
            return age(now.timeIntervalSince(when))
        }
        if now.timeIntervalSince(when) < 6 * 86_400 {
            return weekday.string(from: when)
        }
        return dayMonth.string(from: when)
    }

    /// A message's time: "Today 10:31", "Wed 11:23", "5 Nov".
    static func stamp(_ when: Date, now: Date) -> String {
        let calendar = Calendar.current
        if calendar.isDate(when, inSameDayAs: now) {
            return "Today \(hourMinute.string(from: when))"
        }
        if now.timeIntervalSince(when) < 6 * 86_400 {
            return "\(weekday.string(from: when)) \(hourMinute.string(from: when))"
        }
        return dayMonth.string(from: when)
    }

    /// "ran 25m" between two unix times, or "" when either is missing.
    static func span(from start: TimeInterval, to end: TimeInterval) -> String {
        guard start > 0, end > start else { return "" }
        return age(end - start)
    }

    /// The source of a note as you would say it.
    static func source(_ raw: String) -> String {
        switch raw {
        case "quick-add": return "quick add"
        case "desktop": return "desktop"
        case "phone": return "phone"
        case "recap": return "recap"
        default: return raw
        }
    }
}
