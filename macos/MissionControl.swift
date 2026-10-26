// H2S Mission Control for macOS: a menu bar app that runs the bundled server (Python + ffmpeg inside the app)
// and shows the print status. Build: macos/build-dmg.sh
import AppKit
import ServiceManagement
import SwiftUI

let ru = Locale.preferredLanguages.first?.hasPrefix("ru") ?? false
func tr(_ r: String, _ e: String) -> String { ru ? r : e }

struct PrinterStatus: Decodable, Identifiable {
    struct Txt: Decodable { let ru: String; let en: String }
    let id: String
    let name: String
    let online: Bool
    let state: String?
    let state_txt: Txt?
    let pct: Int?
    let remaining_min: Double?
    let task: String?
    let error: Bool?

    var active: Bool { ["PREPARE", "RUNNING", "PAUSE"].contains(state ?? "") }
    var line: String {
        guard online else { return "\(name) · " + tr("нет связи", "offline") }
        var s = "\(name) · " + (ru ? state_txt?.ru : state_txt?.en ?? state ?? "—")!
        if active, let p = pct { s += " \(p)%" }
        if active, let m = remaining_min, m > 0 { s += " · " + duration(m) }
        if error == true { s += " ⚠︎" }
        return s
    }
}

func duration(_ min: Double) -> String {
    let m = Int(min.rounded())
    return m >= 60 ? "\(m / 60)" + tr(" ч ", "h ") + "\(m % 60)" + tr(" мин", "m") : "\(m)" + tr(" мин", "m")
}

@MainActor
final class Server: ObservableObject {
    @Published var printers: [PrinterStatus] = []
    @Published var running = false
    @Published var failed = false
    @Published var locked = false        // the console has a password: status needs a login
    @Published var loginItem = SMAppService.mainApp.status == .enabled

    let data: URL = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("Mission Control", isDirectory: true)
    var log: URL { data.appendingPathComponent("server.log") }
    private var proc: Process?
    private var quitting = false
    private var quickExits = 0
    private var openedSetup = false
    private var awake: NSObjectProtocol?   // keeps the Mac from idle-sleeping while a print runs

    var port: Int {
        let cfg = try? JSONSerialization.jsonObject(with: Data(contentsOf: data.appendingPathComponent("config.json"))) as? [String: Any]
        return cfg?["port"] as? Int ?? 8790
    }
    var url: URL { URL(string: "http://localhost:\(port)/")! }
    var lanURL: String? {
        lanAddress().map { "http://\($0):\(port)/" }
    }

    init() {
        try? FileManager.default.createDirectory(at: data, withIntermediateDirectories: true)
        // First launch: start with the Mac from now on (can be switched off in the menu).
        if !UserDefaults.standard.bool(forKey: "loginItemOffered") {
            UserDefaults.standard.set(true, forKey: "loginItemOffered")
            try? SMAppService.mainApp.register()
            loginItem = SMAppService.mainApp.status == .enabled
        }
        start()
        Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { _ in Task { @MainActor in await self.poll() } }
    }

    func start() {
        let res = Bundle.main.resourceURL!
        let arch = machineArch() == "arm64" ? "arm64" : "x86_64"
        let p = Process()
        p.executableURL = res.appendingPathComponent("python-\(arch)/bin/python3")
        p.arguments = [res.appendingPathComponent("app/server.py").path]
        p.currentDirectoryURL = data
        var env = ProcessInfo.processInfo.environment
        env["MC_DATA"] = data.path
        env["PYTHONPATH"] = res.appendingPathComponent("app/vendor").path
        env["PYTHONDONTWRITEBYTECODE"] = "1"   // the app bundle is signed and must stay unchanged
        env["PYTHONUNBUFFERED"] = "1"
        env["MC_FFMPEG"] = res.appendingPathComponent("ffmpeg-\(arch)").path
        p.environment = env
        if !FileManager.default.fileExists(atPath: log.path) { FileManager.default.createFile(atPath: log.path, contents: nil) }
        if let h = try? FileHandle(forWritingTo: log) {
            h.seekToEndOfFile()
            p.standardOutput = h
            p.standardError = h
        }
        let started = Date()
        p.terminationHandler = { [weak self] _ in
            Task { @MainActor in
                guard let self, !self.quitting else { return }
                self.running = false
                self.quickExits = Date().timeIntervalSince(started) < 10 ? self.quickExits + 1 : 0
                self.failed = self.quickExits >= 3
                if !self.failed { try? await Task.sleep(for: .seconds(3)); self.start() }
            }
        }
        do { try p.run(); proc = p; running = true; failed = false } catch { failed = true }
    }

    func restart() { quickExits = 0; failed = false; if let p = proc, p.isRunning { p.terminate() } else { start() } }

    func stop() {
        quitting = true
        guard let p = proc, p.isRunning else { return }
        p.terminate()
        let deadline = Date().addingTimeInterval(4)
        while p.isRunning && Date() < deadline { usleep(100_000) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
    }

    func poll() async {
        var req = URLRequest(url: url.appendingPathComponent("api/status"))
        req.timeoutInterval = 4
        guard let (body, resp) = try? await URLSession.shared.data(for: req), let http = resp as? HTTPURLResponse else {
            printers = []; return
        }
        locked = http.statusCode == 401
        struct R: Decodable { let printers: [PrinterStatus] }
        printers = (try? JSONDecoder().decode(R.self, from: body))?.printers ?? []
        stayAwake(printers.contains(where: \.active))
        if http.statusCode == 200 && printers.isEmpty && !openedSetup {  // first run: nothing configured yet
            openedSetup = true
            open("setup")
        }
    }

    /// While printing, the Mac must not idle-sleep or the history gets gaps; the display may still turn off.
    func stayAwake(_ on: Bool) {
        if on, awake == nil {
            awake = ProcessInfo.processInfo.beginActivity(options: [.idleSystemSleepDisabled],
                                                          reason: "Mission Control: a print is running")
        } else if !on, let a = awake {
            ProcessInfo.processInfo.endActivity(a)
            awake = nil
        }
    }

    var printing: Bool { awake != nil }

    func open(_ path: String = "") { NSWorkspace.shared.open(URL(string: path, relativeTo: url)!) }

    func toggleLoginItem() {
        do {
            if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() } else { try SMAppService.mainApp.register() }
        } catch { NSSound.beep() }
        loginItem = SMAppService.mainApp.status == .enabled
    }

    var menuTitle: String {
        if let p = printers.first(where: \.active), let pct = p.pct { return "\(pct)%" }
        return ""
    }
}

func machineArch() -> String {
    var u = utsname()
    uname(&u)
    return withUnsafeBytes(of: &u.machine) { String(decoding: $0.prefix(while: { $0 != 0 }), as: UTF8.self) }
}

func lanAddress() -> String? {
    var ifaddr: UnsafeMutablePointer<ifaddrs>?
    guard getifaddrs(&ifaddr) == 0, let first = ifaddr else { return nil }
    defer { freeifaddrs(ifaddr) }
    for p in sequence(first: first, next: { $0.pointee.ifa_next }) {
        let a = p.pointee
        guard a.ifa_addr.pointee.sa_family == UInt8(AF_INET), String(cString: a.ifa_name).hasPrefix("en") else { continue }
        var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
        getnameinfo(a.ifa_addr, socklen_t(a.ifa_addr.pointee.sa_len), &host, socklen_t(host.count), nil, 0, NI_NUMERICHOST)
        let ip = String(cString: host)
        if !ip.hasPrefix("169.254") { return ip }
    }
    return nil
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var server: Server?
    func applicationWillTerminate(_ n: Notification) { MainActor.assumeIsolated { server?.stop() } }
}

@main
struct MissionControlApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var server = Server()

    var body: some Scene {
        MenuBarExtra {
            Menu(server).onAppear { delegate.server = server }
        } label: {
            HStack(spacing: 3) {
                Image(systemName: server.failed ? "exclamationmark.triangle" : "cube.transparent")
                if !server.menuTitle.isEmpty { Text(server.menuTitle) }
            }
            .onAppear { delegate.server = server }
        }
    }
}

struct Menu: View {
    @ObservedObject var s: Server
    init(_ s: Server) { self.s = s }

    var body: some View {
        if s.failed {
            Text(tr("Сервер не запускается — откройте журнал", "The server won't start — see the log"))
            Button(tr("Перезапустить сервер", "Restart server")) { s.restart() }
        } else if s.locked {
            Text(tr("Консоль под паролем", "Console is password-protected"))
        } else if s.printers.isEmpty {
            Text(s.running ? tr("Принтеры не настроены", "No printers set up") : tr("Сервер запускается…", "Server starting…"))
        }
        ForEach(s.printers) { p in Text(p.line) }
        if let task = s.printers.first(where: \.active)?.task { Text(task).font(.caption) }
        if s.printing { Text(tr("☕︎ Мак не уснёт, пока идёт печать", "☕︎ Mac stays awake while printing")) }
        Divider()
        Button(tr("Открыть дашборд", "Open dashboard")) { s.open() }.keyboardShortcut("o")
        Button(tr("Склад катушек", "Inventory")) { s.open("inventory") }
        Button(tr("Настройки…", "Settings…")) { s.open("setup") }.keyboardShortcut(",")
        if let lan = s.lanURL {
            Button(tr("Скопировать адрес для телефона: ", "Copy address for phone: ") + lan) {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(lan, forType: .string)
            }
        }
        Divider()
        Toggle(tr("Запускать при входе", "Launch at login"), isOn: Binding(get: { s.loginItem }, set: { _ in s.toggleLoginItem() }))
        Button(tr("Папка с данными", "Data folder")) { NSWorkspace.shared.open(s.data) }
        Button(tr("Журнал сервера", "Server log")) { NSWorkspace.shared.open(s.log) }
        Button(tr("Перезапустить сервер", "Restart server")) { s.restart() }
        Divider()
        Button(tr("Выйти", "Quit")) { NSApp.terminate(nil) }.keyboardShortcut("q")
    }
}
