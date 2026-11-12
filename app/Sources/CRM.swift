import SwiftUI

nonisolated struct CRMRecording: Decodable, Identifiable, Sendable {
    var key: String
    var path: String
    var at: String
    var recorded_at: String?
    var duration_s: Double?
    var id: String { key }
    var filename: String { URL(fileURLWithPath: path).lastPathComponent }
    var detail: String {
        let raw = recorded_at ?? at
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let date = formatter.date(from: raw) ?? ISO8601DateFormatter().date(from: raw)
        let time = date?.formatted(date: .abbreviated, time: .shortened) ?? raw
        let duration = duration_s.flatMap { value -> String? in
            guard value.isFinite, value >= 0, value < Double(Int32.max) else { return nil }
            return String(format: "%d:%02d", Int(value) / 60, Int(value) % 60)
        } ?? "길이 알 수 없음"
        return "\(time) · \(duration)"
    }
}

nonisolated struct CRMLead: Decodable, Identifiable, Sendable {
    var id: String
    var name: String
    var stage: String
    var updatedAt: String
}

extension Store {
    func refreshCRMPending() async {
        let url = setting("crm.url")
        guard !url.isEmpty else { crmPending = []; return }
        let result = await CLI.execute(["crm", "pending"])
        guard url == setting("crm.url") else { return }
        if result.status == 0, let items = try? JSONDecoder().decode([CRMRecording].self, from: Data(result.output.utf8)) {
            crmPending = items
        }
    }

    func connectCRM() async {
        guard !crmWorking else { return }
        crmWorking = true
        crmConnecting = true
        crmCode = ""
        crmNotice = ""
        let name = String((Host.current().localizedName ?? "Mac").prefix(80))
        let result = await CLI.execute(["crm", "connect", "--name", name]) { line in
            if line.hasPrefix("승인 코드: ") { self.crmCode = String(line.dropFirst("승인 코드: ".count)) }
            if line == "승인 화면에서 이 코드를 넣으세요" { self.crmNotice = line }
        }
        crmNotice = result.status == 0 ? "연결됨" : result.message
        crmConnecting = false
        crmWorking = false
    }
}

enum CRMWindow {
    private static var window: NSWindow?

    static func show() {
        // A new window session clears skipped rows and every card selection.
        if window?.isVisible != true {
            let w = NSWindow(contentViewController: NSHostingController(rootView: CRMView()))
            w.title = "velcro · CRM 에 붙이기"
            w.styleMask = [.titled, .closable, .resizable, .miniaturizable]
            w.isReleasedWhenClosed = false
            w.setContentSize(NSSize(width: 780, height: 480))
            w.minSize = NSSize(width: 680, height: 320)
            w.center()
            window = w
        }
        if #available(macOS 14, *) { NSApp.activate() }
        else { NSApp.activate(ignoringOtherApps: true) }
        window?.makeKeyAndOrderFront(nil)
        window?.orderFrontRegardless()
    }
}

struct CRMView: View {
    @ObservedObject private var store = Store.shared
    @State private var skipped: Set<String> = []
    @State private var leads: [CRMLead] = []
    @State private var pasteAddress = false
    @State private var loading = true
    @State private var error = ""
    @State private var origin = ""

    private var items: [CRMRecording] { store.crmPending.filter { !skipped.contains($0.id) } }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("CRM 에 붙이기").font(.title2.bold())
            Text("녹음마다 카드를 직접 고른 뒤 올리세요.").foregroundStyle(.secondary)
            if loading { ProgressView().controlSize(.small).accessibilityLabel("카드 불러오기") }
            if !error.isEmpty {
                Text(error).foregroundStyle(.red)
                Button("다시 불러오기") { Task { await load() } }.disabled(loading)
            }
            if origin != store.setting("crm.url") && !loading {
                Text("CRM 서버가 바뀌었습니다. 창을 닫고 다시 여세요.").foregroundStyle(.red)
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 16) {
                    ForEach(items) { item in
                        CRMRow(item: item, leads: leads, pasteAddress: pasteAddress, origin: origin,
                               available: !loading && error.isEmpty) { skipped.insert(item.id) }
                        Divider()
                    }
                    if items.isEmpty && !loading {
                        Text("이번 창에 표시할 녹음이 없습니다.").foregroundStyle(.secondary)
                    }
                }
            }
        }
        .padding(20)
        .task { await load() }
    }

    private func load() async {
        loading = true
        error = ""
        origin = store.setting("crm.url")
        await store.refreshCRMPending()
        let result = await CLI.execute(["crm", "leads", "--json"])
        pasteAddress = result.status != 0 && result.message.contains("카드 주소를 붙여 넣으세요")
        if result.status == 0, let cards = try? JSONDecoder().decode([CRMLead].self, from: Data(result.output.utf8)) {
            leads = cards // The command sorts by updatedAt, newest first.
        } else if !pasteAddress {
            error = result.status == 0 ? "카드 목록을 읽지 못했습니다." : result.message
        }
        loading = false
    }
}

private struct CRMRow: View {
    @ObservedObject private var store = Store.shared
    let item: CRMRecording
    let leads: [CRMLead]
    let pasteAddress: Bool
    let origin: String
    let available: Bool
    let skip: () -> Void
    @State private var choice = ""
    @State private var sending = false
    @State private var error = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(item.filename).fontWeight(.medium).lineLimit(1).truncationMode(.middle)
            Text(item.detail).font(.caption).foregroundStyle(.secondary)
            HStack {
                if pasteAddress {
                    TextField("카드 주소를 붙여 넣으세요", text: $choice)
                } else {
                    Picker("카드", selection: $choice) {
                        Text("카드 선택…").tag("")
                        ForEach(leads) { lead in
                            Text("\(lead.name) · \(lead.stage)").tag(lead.id)
                        }
                    }
                }
                if sending {
                    ProgressView().controlSize(.small).accessibilityLabel("녹음 올리기")
                } else {
                    Button("올리기") { Task { await send() } }
                        .disabled(choice.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !available)
                }
                Button("건너뛰기", action: skip)
            }
            .disabled(sending || store.crmWorking || origin != store.setting("crm.url"))
            if !error.isEmpty { Text(error).font(.caption).foregroundStyle(.red) }
        }
    }

    private func send() async {
        let lead = choice.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !lead.isEmpty, available, !sending, !store.crmWorking, origin == store.setting("crm.url") else { return }
        sending = true
        store.crmWorking = true
        error = ""
        let result = await CLI.execute(["crm", "send", item.path, "--lead", lead])
        if result.status == 0 {
            skip()
            await store.refreshCRMPending()
        } else { error = result.message }
        sending = false
        store.crmWorking = false
    }
}

struct CRMPane: View {
    @ObservedObject private var store = Store.shared
    @State private var url = ""
    @State private var approve = ""

    private var dirty: Bool { url != store.setting("crm.url") || approve != store.setting("crm.approve") }

    var body: some View {
        Form {
            Section {
                TextField("서버 주소", text: $url, prompt: Text("https://api.example.com"))
                TextField("승인 화면 주소", text: $approve, prompt: Text("https://auth.example.com/device/"))
                HStack {
                    Spacer()
                    Button("저장") { Task { await save() } }.disabled(!dirty)
                }
            } header: { Text("CRM") }
            Section {
                HStack {
                    Button("연결") { Task { await store.connectCRM() } }
                        .disabled(dirty || store.setting("crm.url").isEmpty)
                    Button("연결 끊기") {
                        Task {
                            guard !store.crmWorking else { return }
                            store.crmWorking = true
                            let result = await CLI.execute(["crm", "disconnect"])
                            store.crmCode = ""
                            store.crmNotice = result.message
                            store.crmWorking = false
                        }
                    }
                    .disabled(dirty || store.setting("crm.url").isEmpty)
                }
                if !store.crmCode.isEmpty {
                    Text("승인 코드").foregroundStyle(.secondary)
                    Text(store.crmCode).font(.system(size: 32, weight: .semibold, design: .monospaced)).textSelection(.enabled)
                }
                if store.crmConnecting { ProgressView().controlSize(.small).accessibilityLabel("CRM 승인 대기") }
                if !store.crmNotice.isEmpty { Text(store.crmNotice).textSelection(.enabled) }
            } footer: {
                Text("승인 화면에서 코드를 입력하세요. 잠재 고객 카드에 파일을 올리는 권한만 연결합니다.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .disabled(store.crmWorking)
        .onAppear { url = store.setting("crm.url"); approve = store.setting("crm.approve") }
    }

    private func save() async {
        guard !store.crmWorking else { return }
        store.crmWorking = true
        store.crmCode = ""
        store.crmNotice = ""
        for (key, value) in [("crm.url", url), ("crm.approve", approve)] {
            let result = await CLI.execute(["set", key] + (value.isEmpty ? [] : [value]))
            if result.status != 0 { store.crmNotice = result.message; break }
        }
        await store.loadSettings()
        url = store.setting("crm.url")
        approve = store.setting("crm.approve")
        store.crmWorking = false
    }
}
