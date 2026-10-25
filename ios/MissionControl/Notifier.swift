import Foundation
import UserNotifications

/// Local notifications when a print finishes, fails, pauses or reports an error (no push server needed).
enum Notifier {
    static func requestPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
    }

    static func check(_ printers: [PrinterStatus]) {
        let d = UserDefaults.standard
        var last = d.dictionary(forKey: "lastStates") as? [String: String] ?? [:]
        for p in printers {
            let key = "\(p.state)|\(p.error)"
            defer { last[p.id] = key }
            guard let prev = last[p.id], prev != key else { continue }
            let wasActive = ["RUNNING", "PREPARE", "PAUSE"].contains(prev.components(separatedBy: "|")[0])
            switch (p.state, wasActive) {
            case ("FINISH", true): send(p, L10n.t("Печать готова", "Print finished"))
            case ("FAILED", true): send(p, L10n.t("Печать сорвалась", "Print failed"))
            case ("PAUSE", _): send(p, L10n.t("Печать на паузе", "Print paused"))
            default: if p.error && !prev.hasSuffix("|true") { send(p, L10n.t("Ошибка принтера", "Printer error")) }
            }
        }
        d.set(last, forKey: "lastStates")
    }

    private static func send(_ p: PrinterStatus, _ title: String) {
        let c = UNMutableNotificationContent()
        c.title = "\(p.name): \(title)"
        c.body = p.task.isEmpty ? p.stateText : "\(p.task) · \(p.pct)%"
        c.sound = .default
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: "\(p.id)-\(p.state)-\(Date().timeIntervalSince1970)",
                                                                     content: c, trigger: nil))
    }
}
