import SwiftUI
import WebKit

/// The dashboard itself, full screen. Video plays inline; pull down to reload.
struct DashboardWebView: UIViewRepresentable {
    let url: URL
    let reloadToken: UUID
    @Binding var failed: Bool

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeUIView(context: Context) -> WKWebView {
        let cfg = WKWebViewConfiguration()
        cfg.allowsInlineMediaPlayback = true
        cfg.mediaTypesRequiringUserActionForPlayback = []
        let web = WKWebView(frame: .zero, configuration: cfg)
        web.isOpaque = false
        web.backgroundColor = UIColor(Theme.bg)
        web.scrollView.backgroundColor = UIColor(Theme.bg)
        web.scrollView.contentInsetAdjustmentBehavior = .never
        web.allowsBackForwardNavigationGestures = true
        web.navigationDelegate = context.coordinator
        let refresh = UIRefreshControl()
        refresh.tintColor = UIColor(Theme.cyan)
        refresh.addTarget(context.coordinator, action: #selector(Coordinator.pull(_:)), for: .valueChanged)
        web.scrollView.refreshControl = refresh
        web.load(URLRequest(url: url))
        context.coordinator.token = reloadToken
        return web
    }

    func updateUIView(_ web: WKWebView, context: Context) {
        if context.coordinator.token != reloadToken || web.url?.host != url.host || web.url?.port != url.port {
            context.coordinator.token = reloadToken
            web.load(URLRequest(url: url))
        }
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        var parent: DashboardWebView
        var token: UUID?
        init(_ parent: DashboardWebView) { self.parent = parent }

        @objc func pull(_ sender: UIRefreshControl) {
            (sender.superview?.superview as? WKWebView)?.reload()
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { sender.endRefreshing() }
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { parent.failed = false }
        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { parent.failed = true }
        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            parent.failed = true
        }
    }
}
