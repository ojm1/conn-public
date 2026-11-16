import SwiftUI

/// Pairing until there is a pairing, then the three tabs.
@MainActor
struct RootView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        Group {
            if model.pairing == nil {
                PairingView()
            } else {
                MainTabs()
            }
        }
        .background(Theme.bg.ignoresSafeArea())
        .preferredColorScheme(.dark)
        .onAppear { model.becameActive() }
        .onChange(of: scenePhase) { _, phase in
            // iOS takes the sockets in the background; on the way back,
            // refetch and resume events without a word.
            switch phase {
            case .active: model.becameActive()
            case .background: model.becameInactive()
            default: break
            }
        }
        .onOpenURL { url in model.handle(url: url) }
    }
}

@MainActor
struct MainTabs: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        TabView(selection: $model.tab) {
            ChatsTab()
                .tabItem { Label("Chats", systemImage: "bubble.left") }
                .badge(model.needsYouCount)
                .tag(AppTab.chats)
            NotesTab()
                .tabItem { Label("Notes", systemImage: "doc.text") }
                .tag(AppTab.notes)
            HostsTab()
                .tabItem { Label("Hosts", systemImage: "server.rack") }
                .tag(AppTab.hosts)
        }
        .tint(Theme.textStrong)
        .sheet(item: $model.compose) { request in
            NewSheet(request: request)
                .presentationDragIndicator(.visible)
                .presentationBackground(Theme.surface2)
                .presentationCornerRadius(16)
        }
    }
}
