import ActivityKit
import AppIntents
import SwiftUI
import UIKit
import WidgetKit

@main
struct MissionWidgets: WidgetBundle {
    var body: some Widget {
        StatusWidget()
        PrintLiveActivity()
    }
}

// MARK: - Home and Lock Screen widget

/// Free signing has no shared App Group, so the widget keeps its own server address (long-press → Edit Widget).
struct ServerIntent: WidgetConfigurationIntent {
    static var title: LocalizedStringResource = "Mission Control"
    static var description = IntentDescription("Printer status from your Mission Control dashboard.")

    @Parameter(title: "Dashboard address", default: "")
    var server: String
}

struct StatusEntry: TimelineEntry {
    let date: Date
    let printer: PrinterStatus?
    let photo: UIImage?
    let message: String?
}

struct StatusProvider: AppIntentTimelineProvider {
    func placeholder(in context: Context) -> StatusEntry { .init(date: .now, printer: nil, photo: nil, message: nil) }

    func snapshot(for configuration: ServerIntent, in context: Context) async -> StatusEntry {
        await load(configuration, photo: false)
    }

    func timeline(for configuration: ServerIntent, in context: Context) async -> Timeline<StatusEntry> {
        let entry = await load(configuration, photo: context.family != .systemSmall && context.family != .accessoryCircular
                               && context.family != .accessoryRectangular)
        // A print in progress refreshes sooner; iOS still decides the real cadence (roughly 15+ minutes).
        let next = entry.printer?.isActive == true ? 15.0 : 45.0
        return Timeline(entries: [entry], policy: .after(.now.addingTimeInterval(next * 60)))
    }

    private func load(_ cfg: ServerIntent, photo: Bool) async -> StatusEntry {
        guard let base = API.normalize(cfg.server) else {
            return .init(date: .now, printer: nil, photo: nil,
                         message: L10n.t("Удерживайте виджет → «Изменить виджет» и впишите адрес дашборда",
                                         "Long-press → Edit Widget and enter the dashboard address"))
        }
        guard let list = try? await API.status(base), let p = list.first(where: \.isActive) ?? list.first else {
            return .init(date: .now, printer: nil, photo: nil, message: L10n.t("Дашборд недоступен", "Dashboard unreachable"))
        }
        var image: UIImage?
        if photo, let data = await API.snapshot(base, printer: p.id), let full = UIImage(data: data) {
            image = full.preparingThumbnail(of: CGSize(width: 640, height: 412))  // widgets have a tight memory budget
        }
        return .init(date: .now, printer: p, photo: image, message: nil)
    }
}

struct StatusWidget: Widget {
    var body: some WidgetConfiguration {
        AppIntentConfiguration(kind: "status", intent: ServerIntent.self, provider: StatusProvider()) { entry in
            StatusWidgetView(entry: entry).containerBackground(for: .widget) { Theme.bg }
        }
        .configurationDisplayName("Mission Control")
        .description(L10n.t("Прогресс печати, время окончания и камера", "Print progress, finish time and camera"))
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge, .accessoryCircular, .accessoryRectangular])
    }
}

struct StatusWidgetView: View {
    @Environment(\.widgetFamily) var family
    let entry: StatusEntry

    var body: some View {
        if let p = entry.printer {
            switch family {
            case .accessoryCircular:
                Gauge(value: Double(p.pct), in: 0...100) { Text("%") } currentValueLabel: { Text("\(p.pct)") }
                    .gaugeStyle(.accessoryCircular)
            case .accessoryRectangular:
                VStack(alignment: .leading, spacing: 2) {
                    Text("\(p.name) · \(p.pct)%").font(.headline)
                    Text(p.task.isEmpty ? p.stateText : p.task).font(.caption).lineLimit(1)
                    if p.isActive, let eta = p.etaDate { Text(eta, style: .time).font(.caption2) }
                }
            case .systemSmall:
                small(p)
            default:
                wide(p)
            }
        } else {
            VStack(spacing: 6) {
                Image(systemName: "printer").font(.title2).foregroundStyle(Theme.cyan)
                Text(entry.message ?? "Mission Control").font(.caption).multilineTextAlignment(.center).foregroundStyle(Theme.dim)
            }
        }
    }

    func small(_ p: PrinterStatus) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Circle().fill(Theme.stateColor(p.state)).frame(width: 7, height: 7)
                Text(p.name).font(.caption.bold()).lineLimit(1)
            }
            ZStack {
                ProgressRing(value: Double(p.pct) / 100, lineWidth: 7)
                Text("\(p.pct)%").font(.system(size: 22, weight: .heavy, design: .monospaced))
            }
            .frame(maxWidth: .infinity)
            Text(p.isActive ? eta(p) : p.stateText).font(.caption2.monospaced()).foregroundStyle(Theme.cyan).lineLimit(1)
        }
    }

    func wide(_ p: PrinterStatus) -> some View {
        let photo = Group {
            if let img = entry.photo {
                Image(uiImage: img).resizable().aspectRatio(1680.0 / 1080.0, contentMode: .fill)
            } else {
                Theme.panel.overlay(Image(systemName: "video.slash").foregroundStyle(Theme.dim))
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 12))
        let info = VStack(alignment: .leading, spacing: 5) {
            HStack {
                Circle().fill(Theme.stateColor(p.state)).frame(width: 7, height: 7)
                Text("\(p.name) · \(p.stateText)").font(.caption.bold()).lineLimit(1)
            }
            Text("\(p.pct)%").font(.system(size: 30, weight: .heavy, design: .monospaced))
            Text(p.task).font(.caption).lineLimit(1)
            if p.layers > 0 { Text(L10n.t("слой \(p.layer)/\(p.layers)", "layer \(p.layer)/\(p.layers)")).font(.caption2.monospaced()).foregroundStyle(Theme.dim) }
            ProgressView(value: Double(p.pct), total: 100).tint(Theme.cyan)
            if p.isActive { Text(eta(p)).font(.caption2.monospaced()).foregroundStyle(Theme.cyan) }
        }
        return Group {
            if family == .systemLarge {
                VStack(alignment: .leading, spacing: 10) { photo; info }
            } else {
                HStack(spacing: 12) { photo.frame(maxWidth: .infinity); info.frame(maxWidth: .infinity, alignment: .leading) }
            }
        }
    }

    func eta(_ p: PrinterStatus) -> String {
        guard let d = p.etaDate else { return p.stageText }
        return L10n.t("готово в ", "done at ") + d.formatted(date: .omitted, time: .shortened)
    }
}

// MARK: - Live Activity (Lock Screen + Dynamic Island)

struct PrintLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: PrintActivityAttributes.self) { context in
            LockScreenActivity(context: context)
                .padding(16)
                .activityBackgroundTint(Theme.bg.opacity(0.85))
                .activitySystemActionForegroundColor(Theme.cyan)
        } dynamicIsland: { context in
            let s = context.state
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(context.attributes.printerName).font(.caption.bold()).foregroundStyle(Theme.dim)
                        Text("\(s.pct)%").font(.system(size: 26, weight: .heavy, design: .monospaced))
                    }
                }
                DynamicIslandExpandedRegion(.trailing) {
                    VStack(alignment: .trailing, spacing: 2) {
                        Text(L10n.t("осталось", "left")).font(.caption2).foregroundStyle(Theme.dim)
                        if s.finished { Text("✓").font(.title2.bold()).foregroundStyle(Theme.green) }
                        else { Text(timerInterval: s.countdown, countsDown: true).font(.title3.monospacedDigit().bold()).multilineTextAlignment(.trailing) }
                    }
                }
                DynamicIslandExpandedRegion(.bottom) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text(context.attributes.task).font(.caption).lineLimit(1)
                        ProgressView(timerInterval: s.timeRange, countsDown: false) { EmptyView() } currentValueLabel: { EmptyView() }
                            .tint(Theme.cyan)
                    }
                }
            } compactLeading: {
                ProgressView(timerInterval: s.timeRange, countsDown: false) { EmptyView() } currentValueLabel: { EmptyView() }
                    .progressViewStyle(.circular).tint(Theme.cyan).frame(width: 20, height: 20)
            } compactTrailing: {
                if s.finished { Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.green) }
                else { Text(timerInterval: s.countdown, countsDown: true).monospacedDigit().frame(width: 56).font(.caption.bold()) }
            } minimal: {
                ProgressView(timerInterval: s.timeRange, countsDown: false) { EmptyView() } currentValueLabel: { EmptyView() }
                    .progressViewStyle(.circular).tint(Theme.cyan)
            }
            .keylineTint(Theme.cyan)
        }
    }
}

struct LockScreenActivity: View {
    let context: ActivityViewContext<PrintActivityAttributes>

    var body: some View {
        let s = context.state
        HStack(spacing: 14) {
            ZStack {
                ProgressRing(value: Double(s.pct) / 100, lineWidth: 6)
                Text(s.finished ? "✓" : "\(s.pct)%").font(.system(size: 15, weight: .heavy, design: .monospaced))
            }
            .frame(width: 54, height: 54)
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(context.attributes.printerName).font(.caption.bold()).foregroundStyle(Theme.dim)
                    Spacer()
                    if s.finished {
                        Text(L10n.t("Готово", "Finished")).font(.caption.bold()).foregroundStyle(Theme.green)
                    } else {
                        Text(L10n.t("готово в ", "done at ") + s.eta.formatted(date: .omitted, time: .shortened))
                            .font(.caption.monospacedDigit()).foregroundStyle(Theme.cyan)
                    }
                }
                Text(context.attributes.task).font(.subheadline.bold()).lineLimit(1)
                if s.finished {
                    ProgressView(value: 1).tint(Theme.green)
                } else {
                    ProgressView(timerInterval: s.timeRange, countsDown: false) { EmptyView() } currentValueLabel: { EmptyView() }
                        .tint(Theme.cyan)
                    HStack {
                        Text(s.layers > 0 ? "\(s.stage) · \(s.layer)/\(s.layers)" : s.stage).font(.caption2).foregroundStyle(Theme.dim).lineLimit(1)
                        Spacer()
                        Text(timerInterval: s.countdown, countsDown: true).font(.caption.monospacedDigit().bold())
                    }
                }
            }
        }
    }
}
