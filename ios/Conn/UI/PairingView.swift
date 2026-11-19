import SwiftUI
import UIKit

/// First launch: "Pair with Conn". You run `conn_agent.py pair` on the Mac
/// mini and type the code it prints. The certificate the agent shows here
/// is pinned from then on (trust on first use); typing the start of the
/// sha256 the same command printed makes that first use checked too.
@MainActor
struct PairingView: View {
    @EnvironmentObject private var model: AppModel

    @State private var host = AgentAddress.defaultHost
    @State private var port = String(AgentAddress.defaultPort)
    @State private var code = ""
    @State private var certificate = ""
    @State private var deviceName = ""
    @State private var busy = false
    @State private var error: String?

    private enum Field: Hashable { case host, port, code, certificate, name }
    @FocusState private var focus: Field?

    private var canPair: Bool {
        !busy && !host.trimmingCharacters(in: .whitespaces).isEmpty
            && Int(port) != nil
            && code.filter { $0.isLetter || $0.isNumber }.count >= 6
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text("Pair with Conn")
                    .font(Theme.ui(32, weight: .bold))
                    .foregroundStyle(Theme.textStrong)
                    .padding(.top, 24)

                Text("On the Mac mini, run `conn_agent.py pair` and type the code it shows. A code works once, for 5 minutes. This iPhone needs to be on the same network.")
                    .font(Theme.ui(15))
                    .foregroundStyle(Theme.textSecondary)
                    .lineSpacing(3)

                field("Mac mini address", text: $host, placeholder: "the mini's address", field: .host,
                      keyboard: .numbersAndPunctuation, mono: true)
                field("Port", text: $port, placeholder: String(AgentAddress.defaultPort), field: .port,
                      keyboard: .numberPad, mono: true)
                field("Code", text: $code, placeholder: "7K4Q-2M", field: .code,
                      keyboard: .asciiCapable, mono: true, capitalise: true)
                field("Certificate (optional)", text: $certificate,
                      placeholder: "first characters of its sha256", field: .certificate,
                      keyboard: .asciiCapable, mono: true)
                field("Name for this device", text: $deviceName, placeholder: "iPhone", field: .name,
                      keyboard: .default, mono: false)

                if let error = error {
                    Text(error)
                        .font(Theme.ui(14))
                        .foregroundStyle(Theme.hostDarkText)
                        .fixedSize(horizontal: false, vertical: true)
                }

                Button {
                    pair()
                } label: {
                    HStack(spacing: 10) {
                        if busy { ProgressView().tint(Theme.onPrimary) }
                        Text(busy ? "Pairing" : "Pair")
                    }
                }
                .buttonStyle(PrimaryButtonStyle())
                .disabled(!canPair)
                .opacity(canPair ? 1 : 0.5)
                .padding(.top, 4)
            }
            .padding(.horizontal, 20)
            .padding(.bottom, 40)
            .frame(maxWidth: 520)
            .frame(maxWidth: .infinity)
        }
        .scrollDismissesKeyboard(.interactively)
        .background(Theme.bg.ignoresSafeArea())
        .onAppear {
            if deviceName.isEmpty { deviceName = UIDevice.current.name }
            applyPrefill()
        }
        .onChange(of: model.pairPrefill) { _, _ in applyPrefill() }
    }

    private func field(_ title: String, text: Binding<String>, placeholder: String, field: Field,
                       keyboard: UIKeyboardType, mono: Bool, capitalise: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(Theme.ui(13, weight: .semibold))
                .foregroundStyle(Theme.textMeta)
            TextField("", text: text, prompt: Text(placeholder).foregroundColor(Color(hex: 0x5A6170)))
                .font(mono ? Theme.mono(17) : Theme.ui(17))
                .foregroundStyle(Theme.textStrong)
                .keyboardType(keyboard)
                .textInputAutocapitalization(capitalise ? .characters : .never)
                .autocorrectionDisabled()
                .focused($focus, equals: field)
                .submitLabel(field == .name ? .go : .next)
                .onSubmit { advance(from: field) }
                .padding(.horizontal, 14)
                .frame(minHeight: 48)
                .background(Theme.surface2, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).stroke(Theme.border, lineWidth: 1))
        }
    }

    private func advance(from field: Field) {
        switch field {
        case .host: focus = .port
        case .port: focus = .code
        case .code: focus = .certificate
        case .certificate: focus = .name
        case .name:
            focus = nil
            if canPair { pair() }
        }
    }

    private func applyPrefill() {
        guard let prefill = model.pairPrefill else { return }
        host = prefill.host
        port = String(prefill.port)
        if !prefill.code.isEmpty { code = prefill.code }
    }

    private func pair() {
        guard canPair, let portNumber = Int(port) else { return }
        let address = AgentAddress(host: host.trimmingCharacters(in: .whitespaces), port: portNumber)
        let typed = code.uppercased().trimmingCharacters(in: .whitespaces)
        let name = deviceName.trimmingCharacters(in: .whitespaces)
        let prefix = certificate.trimmingCharacters(in: .whitespaces)
        busy = true
        error = nil
        focus = nil
        Task {
            do {
                let pairing = try await ConnAPI.pair(address: address, code: typed,
                                                     deviceName: name.isEmpty ? "iPhone" : name,
                                                     fingerprintPrefix: prefix.isEmpty ? nil : prefix)
                busy = false
                Haptics.success()
                model.paired(pairing)
            } catch let failure as APIError {
                busy = false
                error = message(for: failure, address: address)
            } catch {
                busy = false
                self.error = "Pairing failed: \(error.localizedDescription)"
            }
        }
    }

    private func message(for failure: APIError, address: AgentAddress) -> String {
        switch failure {
        case .unreachable(let why):
            let detail = why.isEmpty ? "" : " (\(why))"
            return "Can't reach the Mac mini at \(address.label)\(detail). Check the address and port, and that Conn may use the local network (Settings › Privacy › Local Network), then try again."
        case .certificateChanged:
            return "The Mac mini's certificate doesn't start with what you typed. Check the sha256 that conn_agent.py pair printed."
        case .server(let status, let text):
            if status == 403 { return "That code is wrong or has expired. Run conn_agent.py pair for a new one." }
            return text.isEmpty ? "The Mac mini refused (\(status))." : text
        case .unauthorized, .badResponse:
            return failure.message
        }
    }
}
