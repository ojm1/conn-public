import SwiftUI
import UIKit

@main
@MainActor
struct ConnApp: App {
    @StateObject private var model = AppModel()

    init() {
        ConnApp.styleBars()
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(model)
                .preferredColorScheme(.dark)
        }
    }

    /// SPEC §8 colours for the UIKit bars SwiftUI still draws with.
    private static func styleBars() {
        let strong = UIColor(Theme.textStrong)
        let meta = UIColor(Theme.textMeta)

        let nav = UINavigationBarAppearance()
        nav.configureWithOpaqueBackground()
        nav.backgroundColor = UIColor(Theme.bg)
        nav.shadowColor = .clear
        nav.titleTextAttributes = [.foregroundColor: strong]
        nav.largeTitleTextAttributes = [.foregroundColor: strong,
                                        .font: UIFont.systemFont(ofSize: 32, weight: .bold)]
        UINavigationBar.appearance().standardAppearance = nav
        UINavigationBar.appearance().scrollEdgeAppearance = nav
        UINavigationBar.appearance().compactAppearance = nav
        UINavigationBar.appearance().tintColor = UIColor(Theme.textChip)

        let tab = UITabBarAppearance()
        tab.configureWithOpaqueBackground()
        tab.backgroundColor = UIColor(Theme.list).withAlphaComponent(0.94)
        tab.shadowColor = UIColor(Theme.divider)
        for layout in [tab.stackedLayoutAppearance, tab.inlineLayoutAppearance, tab.compactInlineLayoutAppearance] {
            layout.normal.iconColor = meta
            layout.normal.titleTextAttributes = [.foregroundColor: meta]
            layout.selected.iconColor = strong
            layout.selected.titleTextAttributes = [.foregroundColor: strong]
            layout.normal.badgeBackgroundColor = UIColor(Theme.needsYou)
            layout.normal.badgeTextAttributes = [.foregroundColor: UIColor(Theme.onNeedsYou)]
            layout.selected.badgeBackgroundColor = UIColor(Theme.needsYou)
            layout.selected.badgeTextAttributes = [.foregroundColor: UIColor(Theme.onNeedsYou)]
        }
        UITabBar.appearance().standardAppearance = tab
        UITabBar.appearance().scrollEdgeAppearance = tab
    }
}
