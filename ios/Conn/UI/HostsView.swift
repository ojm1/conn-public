import SwiftUI

/// Hosts: a read-only list from GET /hosts, and this device's pairing.
@MainActor
struct HostsTab: View {
    var body: some View {
        NavigationStack {
            HostsView()
        }
    }
}

@MainActor
struct HostsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var confirmUnpair = false

    var body: some View {
        List {
            LinkLine()
                .listRowBackground(Theme.bg)
                .listRowSeparator(.hidden)

            Section {
                ForEach(model.hosts) { host in
                    row(host)
                        .listRowBackground(Theme.list)
                        .listRowSeparatorTint(Theme.divider)
                }
                if model.hosts.isEmpty && model.loaded {
                    Text("No servers")
                        .font(Theme.ui(15))
                        .foregroundStyle(Theme.textMeta)
                        .listRowBackground(Theme.list)
                }
            } header: {
                Text("\(model.hostsUp) up · \(model.hostsDark) dark")
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.textMeta)
            }

            if let pairing = model.pairing {
                Section {
                    detailRow("Mac mini", pairing.address.label)
                    detailRow("This device", pairing.deviceName.isEmpty ? pairing.deviceId : pairing.deviceName)
                    detailRow("Certificate", fingerprint(pairing.fingerprint))
                    Button(role: .destructive) {
                        confirmUnpair = true
                    } label: {
                        Text("Unpair this device")
                            .font(Theme.ui(16))
                            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    }
                    .listRowBackground(Theme.list)
                } header: {
                    Text("Paired")
                        .font(Theme.ui(12, weight: .semibold))
                        .foregroundStyle(Theme.textMeta)
                }
            }
        }
        .listStyle(.insetGrouped)
        .scrollContentBackground(.hidden)
        .background(Theme.bg)
        .navigationTitle("Hosts")
        .refreshable { await model.refresh(force: true) }
        .confirmationDialog("Unpair this device?", isPresented: $confirmUnpair, titleVisibility: .visible) {
            Button("Unpair", role: .destructive) {
                Task { await model.unpair(revoke: true) }
            }
        } message: {
            Text("Its token is revoked on the Mac mini. Pair again with a new code from conn_agent.py pair.")
        }
    }

    private func row(_ host: HostStatus) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Circle()
                .fill(colour(host.state))
                .frame(width: 8, height: 8)
                .alignmentGuide(.firstTextBaseline) { d in d[.bottom] - 1 }
            VStack(alignment: .leading, spacing: 3) {
                Text(host.host)
                    .font(Theme.mono(15, weight: .medium))
                    .foregroundStyle(host.state == .dark ? Theme.hostDarkText : Theme.textStrong)
                if host.state != .up || !host.detail.isEmpty {
                    Text(host.detail.isEmpty ? label(host.state) : host.detail)
                        .font(Theme.ui(13))
                        .foregroundStyle(Theme.textMeta)
                        .lineLimit(3)
                }
            }
            Spacer(minLength: 8)
            if host.chats > 0 {
                Text(host.chats == 1 ? "1 chat" : "\(host.chats) chats")
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.textSecondary)
            }
        }
        .frame(minHeight: 44)
        .accessibilityElement(children: .combine)
    }

    private func detailRow(_ title: String, _ value: String) -> some View {
        HStack {
            Text(title)
                .font(Theme.ui(15))
                .foregroundStyle(Theme.textSecondary)
            Spacer()
            Text(value)
                .font(Theme.mono(13))
                .foregroundStyle(Theme.text)
                .lineLimit(1)
                .truncationMode(.middle)
                .textSelection(.enabled)
        }
        .frame(minHeight: 44)
        .listRowBackground(Theme.list)
    }

    private func fingerprint(_ hex: String) -> String {
        guard hex.count > 16 else { return hex }
        return "\(hex.prefix(8))…\(hex.suffix(8))"
    }

    private func colour(_ state: HostState) -> Color {
        switch state {
        case .up: return Theme.hostUp
        case .needsLogin: return Theme.needsYou
        case .dark: return Theme.hostDarkRing
        }
    }

    private func label(_ state: HostState) -> String {
        switch state {
        case .up: return "up"
        case .needsLogin: return "needs a login on the Mac mini"
        case .dark: return "not answering"
        }
    }
}
