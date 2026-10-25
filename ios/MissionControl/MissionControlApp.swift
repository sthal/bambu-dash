import BackgroundTasks
import SwiftUI

@main
struct MissionControlApp: App {
    @StateObject private var model = AppModel()
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(model)
                .preferredColorScheme(.dark)
        }
        .onChange(of: phase) { _, newPhase in
            switch newPhase {
            case .active: model.startPolling()
            case .background: model.stopPolling(); AppModel.scheduleRefresh()
            default: break
            }
        }
        // iOS wakes the app now and then (it decides how often); refresh the Live Activity and notify.
        .backgroundTask(.appRefresh(AppModel.refreshID)) {
            await AppModel.backgroundRefresh()
            AppModel.scheduleRefresh()
        }
    }
}
