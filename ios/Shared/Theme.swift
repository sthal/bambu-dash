import SwiftUI

enum Theme {
    static let bg = Color(red: 0.016, green: 0.020, blue: 0.051)
    static let panel = Color(red: 0.07, green: 0.08, blue: 0.17)
    static let cyan = Color(red: 0.282, green: 0.878, blue: 1.0)
    static let violet = Color(red: 0.616, green: 0.482, blue: 1.0)
    static let pink = Color(red: 1.0, green: 0.373, blue: 0.659)
    static let green = Color(red: 0.239, green: 1.0, blue: 0.667)
    static let amber = Color(red: 1.0, green: 0.71, blue: 0.278)
    static let red = Color(red: 1.0, green: 0.302, blue: 0.416)
    static let dim = Color(red: 0.533, green: 0.565, blue: 0.71)
    static let ring = AngularGradient(colors: [cyan, violet, pink, cyan], center: .center)

    static func stateColor(_ state: String) -> Color {
        switch state {
        case "RUNNING": return cyan
        case "PREPARE": return violet
        case "PAUSE": return amber
        case "FINISH": return green
        case "FAILED", "OFFLINE": return red
        default: return dim
        }
    }
}

/// Circular progress ring in the dashboard's gradient.
struct ProgressRing: View {
    var value: Double
    var lineWidth: CGFloat = 6
    var body: some View {
        ZStack {
            Circle().stroke(Color.white.opacity(0.1), lineWidth: lineWidth)
            Circle().trim(from: 0, to: max(0.01, min(1, value)))
                .stroke(Theme.ring, style: StrokeStyle(lineWidth: lineWidth, lineCap: .round))
                .rotationEffect(.degrees(-90))
        }
    }
}
