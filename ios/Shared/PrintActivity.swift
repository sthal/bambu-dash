import ActivityKit
import Foundation

/// Live Activity for a print. Without push notifications (free signing) the state only changes when
/// the app runs, so the views lean on `start...eta`: countdowns and time-based progress tick by themselves.
struct PrintActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        var pct: Int
        var layer: Int
        var layers: Int
        var stage: String
        var state: String
        var start: Date
        var eta: Date
    }

    var printerID: String
    var printerName: String
    var task: String
}

extension PrintActivityAttributes.ContentState {
    /// Progress for a bar that keeps moving between updates: the later of the reported % and elapsed time.
    var timeRange: ClosedRange<Date> { start...max(eta, start.addingTimeInterval(60)) }
    /// Countdown range that never inverts (a ClosedRange with eta in the past would crash the view).
    var countdown: ClosedRange<Date> { let now = Date(); return now...max(eta, now.addingTimeInterval(1)) }
    var finished: Bool { state == "FINISH" }
}
