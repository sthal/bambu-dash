import BackgroundTasks
import Foundation
import SwiftUI

@MainActor
final class AppModel: ObservableObject {
    nonisolated static let refreshID = "com.sthal.missioncontrol.refresh"

    @AppStorage("server") var server: String = ""
    @AppStorage("liveActivities") var liveActivitiesOn = true
    @AppStorage("notifications") var notificationsOn = true
    @Published var printers: [PrinterStatus] = []
    @Published var reachable: Bool? = nil
    @Published var reloadToken = UUID()

    private var poller: Task<Void, Never>?

    var baseURL: URL? { API.normalize(server) }

    func startPolling() {
        guard poller == nil else { return }
        if notificationsOn { Notifier.requestPermission() }
        poller = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    func stopPolling() {
        poller?.cancel()
        poller = nil
    }

    func refresh() async {
        guard let base = baseURL else { return }
        do {
            let list = try await API.status(base)
            printers = list
            reachable = true
            await Self.react(to: list, liveActivities: liveActivitiesOn, notifications: notificationsOn)
        } catch {
            reachable = false
        }
    }

    /// Live Activities and notifications from a fresh status, shared by foreground polling and background refresh.
    static func react(to list: [PrinterStatus], liveActivities: Bool, notifications: Bool) async {
        await LiveActivities.sync(list, enabled: liveActivities)
        if notifications { Notifier.check(list) }
    }

    nonisolated static func scheduleRefresh() {
        let req = BGAppRefreshTaskRequest(identifier: refreshID)
        req.earliestBeginDate = Date().addingTimeInterval(15 * 60)
        try? BGTaskScheduler.shared.submit(req)
    }

    nonisolated static func backgroundRefresh() async {
        let d = UserDefaults.standard
        guard let base = API.normalize(d.string(forKey: "server") ?? ""),
              let list = try? await API.status(base) else { return }
        await react(to: list,
                    liveActivities: d.object(forKey: "liveActivities") as? Bool ?? true,
                    notifications: d.object(forKey: "notifications") as? Bool ?? true)
    }
}
