import SwiftUI

struct ContentView: View {
    @EnvironmentObject var model: AppModel
    @State private var showSettings = false
    @State private var failed = false

    var body: some View {
        ZStack(alignment: .bottomTrailing) {
            Theme.bg.ignoresSafeArea()
            if let url = model.baseURL {
                DashboardWebView(url: url, reloadToken: model.reloadToken, failed: $failed)
                    .ignoresSafeArea(edges: .bottom)
                if failed { OfflineView(server: model.server) { model.reloadToken = UUID() } settings: { showSettings = true } }
                Button { showSettings = true } label: {
                    Image(systemName: "slider.horizontal.3")
                        .font(.system(size: 15, weight: .semibold))
                        .frame(width: 40, height: 40)
                        .background(.ultraThinMaterial, in: Circle())
                        .overlay(Circle().stroke(Theme.cyan.opacity(0.4)))
                }
                .tint(.white)
                .padding(16)
                .opacity(0.85)
            } else {
                SetupView()
            }
        }
        .sheet(isPresented: $showSettings) { SettingsView().environmentObject(model) }
    }
}

/// First launch: where is the dashboard?
struct SetupView: View {
    @EnvironmentObject var model: AppModel
    @State private var input = ""
    @State private var checking = false
    @State private var error: String?

    var body: some View {
        VStack(spacing: 22) {
            Spacer()
            Image(systemName: "printer.filled.and.paper").font(.system(size: 54)).foregroundStyle(Theme.ring)
            Text("Mission Control").font(.system(size: 30, weight: .bold))
            Text(L10n.t("Адрес дашборда на вашем Mac — как в браузере, например 192.168.1.10:8790",
                        "Address of the dashboard on your Mac, as in the browser, e.g. 192.168.1.10:8790"))
                .font(.subheadline).foregroundStyle(Theme.dim).multilineTextAlignment(.center)
            TextField("192.168.1.10:8790", text: $input)
                .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                .font(.system(.body, design: .monospaced))
                .padding(14).background(Theme.panel, in: RoundedRectangle(cornerRadius: 14))
                .overlay(RoundedRectangle(cornerRadius: 14).stroke(Theme.cyan.opacity(0.35)))
            if let error { Text(error).font(.footnote).foregroundStyle(Theme.red).multilineTextAlignment(.center) }
            Button {
                Task { await connect() }
            } label: {
                HStack { if checking { ProgressView() }; Text(L10n.t("Подключиться", "Connect")).bold() }
                    .frame(maxWidth: .infinity).padding(14)
                    .background(LinearGradient(colors: [Theme.cyan.opacity(0.35), Theme.violet.opacity(0.45)], startPoint: .leading, endPoint: .trailing),
                                in: RoundedRectangle(cornerRadius: 14))
            }
            .tint(.white).disabled(checking || input.isEmpty)
            Spacer(); Spacer()
        }
        .padding(28)
    }

    func connect() async {
        guard let url = API.normalize(input) else { error = L10n.t("Не похоже на адрес", "That doesn't look like an address"); return }
        checking = true; defer { checking = false }
        do {
            _ = try await API.status(url)
            model.server = url.absoluteString
            error = nil
        } catch {
            self.error = L10n.t("Дашборд не отвечает по этому адресу. Телефон в той же сети (или в VPN)?",
                                "No dashboard answers at this address. Is the phone on the same network (or VPN)?")
        }
    }
}

struct OfflineView: View {
    let server: String
    let retry: () -> Void
    let settings: () -> Void
    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: "wifi.exclamationmark").font(.system(size: 40)).foregroundStyle(Theme.amber)
            Text(L10n.t("Дашборд недоступен", "Dashboard unreachable")).font(.title3.bold())
            Text(server).font(.footnote.monospaced()).foregroundStyle(Theme.dim)
            Text(L10n.t("Проверьте, что телефон дома в Wi-Fi или подключён к VPN, и что Mac включён.",
                        "Make sure the phone is on the home Wi-Fi or VPN and the Mac is on."))
                .font(.footnote).foregroundStyle(Theme.dim).multilineTextAlignment(.center)
            HStack {
                Button(L10n.t("Повторить", "Retry"), action: retry).buttonStyle(.borderedProminent).tint(Theme.violet)
                Button(L10n.t("Адрес", "Address"), action: settings).buttonStyle(.bordered)
            }
        }
        .padding(28).frame(maxWidth: .infinity, maxHeight: .infinity).background(Theme.bg)
    }
}

struct SettingsView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var input = ""

    var body: some View {
        NavigationStack {
            Form {
                Section(L10n.t("Сервер", "Server")) {
                    TextField("192.168.1.10:8790", text: $input)
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        .font(.system(.body, design: .monospaced))
                    HStack {
                        Circle().fill(model.reachable == true ? Theme.green : model.reachable == false ? Theme.red : Theme.dim).frame(width: 8, height: 8)
                        Text(model.reachable == true ? L10n.t("на связи", "connected") : model.reachable == false ? L10n.t("нет связи", "offline") : "—")
                            .foregroundStyle(Theme.dim)
                    }
                }
                Section {
                    Toggle(L10n.t("Live Activity и Dynamic Island", "Live Activity & Dynamic Island"), isOn: $model.liveActivitiesOn)
                    Toggle(L10n.t("Уведомления", "Notifications"), isOn: $model.notificationsOn)
                } footer: {
                    Text(L10n.t("Без платного аккаунта разработчика iOS не даёт серверу будить приложение. Точный процент обновляется, когда приложение открыто или iOS запускает его в фоне; между этим отсчёт до окончания идёт сам.",
                                "Without a paid developer account iOS doesn't let the server wake the app. The exact percentage updates while the app is open or when iOS runs it in the background; the countdown to the end keeps ticking on its own."))
                }
                Section {
                    ForEach(model.printers) { p in
                        HStack {
                            Circle().fill(Theme.stateColor(p.state)).frame(width: 8, height: 8)
                            Text(p.name).bold()
                            Spacer()
                            Text(p.isActive ? "\(p.pct)% · \(p.stateText)" : p.stateText).foregroundStyle(Theme.dim)
                        }
                    }
                } header: { Text(L10n.t("Принтеры", "Printers")) }
            }
            .navigationTitle(L10n.t("Настройки", "Settings"))
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button(L10n.t("Готово", "Done")) {
                        if let url = API.normalize(input) { model.server = url.absoluteString; model.reloadToken = UUID() }
                        Task { await model.refresh() }
                        dismiss()
                    }
                }
            }
            .onAppear { input = model.server }
        }
    }
}
