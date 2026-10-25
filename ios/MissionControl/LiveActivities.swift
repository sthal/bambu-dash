import ActivityKit
import Foundation

enum LiveActivities {
    /// One activity per printer that is printing; finished prints stay on the Lock Screen for a while.
    @MainActor
    static func sync(_ printers: [PrinterStatus], enabled: Bool) async {
        let running = Activity<PrintActivityAttributes>.activities
        guard enabled, ActivityAuthorizationInfo().areActivitiesEnabled else {
            for a in running { await a.end(nil, dismissalPolicy: .immediate) }
            return
        }
        for p in printers {
            let existing = running.first { $0.attributes.printerID == p.id }
            let eta = p.etaDate ?? Date().addingTimeInterval((p.remaining_min ?? 0) * 60)
            let state = PrintActivityAttributes.ContentState(
                pct: p.pct, layer: p.layer, layers: p.layers, stage: p.stageText, state: p.state,
                start: p.startDate, eta: eta)
            if p.isActive {
                let content = ActivityContent(state: state, staleDate: eta.addingTimeInterval(15 * 60))
                if let existing, existing.attributes.task == p.task {
                    await existing.update(content)
                } else {
                    if let existing { await existing.end(nil, dismissalPolicy: .immediate) }
                    _ = try? Activity.request(
                        attributes: PrintActivityAttributes(printerID: p.id, printerName: p.name, task: p.task),
                        content: content, pushType: nil)
                }
            } else if let existing {
                var final = state
                final.eta = Date()
                if p.state == "FINISH" { final.pct = 100 }
                await existing.end(ActivityContent(state: final, staleDate: nil),
                                   dismissalPolicy: .after(Date().addingTimeInterval(30 * 60)))
            }
        }
    }
}
