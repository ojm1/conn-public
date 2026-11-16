import Foundation
import Security

/// What pairing leaves behind: where the agent is, this device's token and
/// id, and the certificate fingerprint to accept from then on.
struct Pairing: Codable, Hashable {
    var address: AgentAddress
    var token: String
    var deviceId: String
    var fingerprint: String
    var deviceName: String
    var paired: TimeInterval
}

/// The pairing, kept in the Keychain as one generic-password item, readable
/// after the first unlock and never leaving this device (no iCloud Keychain,
/// no backup restore onto another phone).
enum PairingStore {
    private static let service = "com.ojm.conn.agent"
    private static let account = "pairing"

    private static var baseQuery: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }

    static func load() -> Pairing? {
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        guard status == errSecSuccess, let data = item as? Data else { return nil }
        return try? JSONDecoder().decode(Pairing.self, from: data)
    }

    @discardableResult
    static func save(_ pairing: Pairing) -> Bool {
        guard let data = try? JSONEncoder().encode(pairing) else { return false }
        SecItemDelete(baseQuery as CFDictionary)
        var item = baseQuery
        item[kSecValueData as String] = data
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        return SecItemAdd(item as CFDictionary, nil) == errSecSuccess
    }

    static func clear() {
        SecItemDelete(baseQuery as CFDictionary)
    }
}
