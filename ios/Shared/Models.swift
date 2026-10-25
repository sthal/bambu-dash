import Foundation

/// One printer as reported by the dashboard's `/api/status`.
struct PrinterStatus: Codable, Identifiable, Hashable {
    let id: String
    let name: String
    let online: Bool
    let state: String
    let state_txt: [String: String]
    let stage: [String: String]
    let pct: Int
    let layer: Int
    let layers: Int
    let task: String
    let remaining_min: Double?
    let eta: Double?
    let started_at: Double?
    let temps: [String: Double?]
    let error: Bool

    var isActive: Bool { ["RUNNING", "PREPARE", "PAUSE"].contains(state) }
    var etaDate: Date? { eta.map { Date(timeIntervalSince1970: $0) } }
    var startDate: Date { started_at.map { Date(timeIntervalSince1970: $0) } ?? Date().addingTimeInterval(-60) }
    var stateText: String { L10n.pick(state_txt) }
    var stageText: String { L10n.pick(stage) }
    func temp(_ key: String) -> Double? { temps[key] ?? nil }
}

struct StatusResponse: Codable {
    let printers: [PrinterStatus]
}

enum API {
    /// Accepts "192.168.1.10:8790", "http://host:8790/" etc. and returns a clean base URL.
    static func normalize(_ raw: String) -> URL? {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { return nil }
        if !s.contains("://") { s = "http://" + s }
        while s.hasSuffix("/") { s.removeLast() }
        guard let url = URL(string: s), url.host != nil else { return nil }
        return url
    }

    static func status(_ base: URL) async throws -> [PrinterStatus] {
        var req = URLRequest(url: base.appendingPathComponent("api/status"))
        req.timeoutInterval = 8
        req.cachePolicy = .reloadIgnoringLocalCacheData
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard (resp as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
        return try JSONDecoder().decode(StatusResponse.self, from: data).printers
    }

    static func snapshot(_ base: URL, printer: String) async -> Data? {
        var comps = URLComponents(url: base.appendingPathComponent("snapshot.jpg"), resolvingAgainstBaseURL: false)
        comps?.queryItems = [URLQueryItem(name: "p", value: printer)]
        guard let url = comps?.url else { return nil }
        var req = URLRequest(url: url)
        req.timeoutInterval = 20
        return try? await URLSession.shared.data(for: req).0
    }
}

enum L10n {
    static var isRussian: Bool { Locale.current.language.languageCode?.identifier == "ru" }
    static func pick(_ d: [String: String]) -> String { d[isRussian ? "ru" : "en"] ?? d["en"] ?? d.values.first ?? "" }
    static func t(_ ru: String, _ en: String) -> String { isRussian ? ru : en }
}
