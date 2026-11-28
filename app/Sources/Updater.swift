import Foundation

/// New-version notice. Every 6 hours reads velcro.dgit.co/latest (the same file the installer checks) and, when its
/// version is above this install's, shows "Update to …" in the menu. Choosing it runs the installer, which checks the
/// SHA-256 of every download, quits this app, swaps it and opens the new one.
@MainActor
final class Updater: ObservableObject {
    static let shared = Updater()
    @Published private(set) var available: String?
    @Published private(set) var updating = false
    private var timer: Timer?
    private var notified: String?
    private static let latest = URL(string: "https://velcro.dgit.co/latest")!

    func start() {
        Task { await check() }
        timer = Timer.scheduledTimer(withTimeInterval: 6 * 3600, repeats: true) { _ in
            Task { @MainActor in await Updater.shared.check() }
        }
    }

    func check() async {
        guard let current = Store.shared.env["VERSION"], !current.isEmpty,
              let (data, response) = try? await URLSession.shared.data(from: Self.latest),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let text = String(data: data, encoding: .utf8) else { return }
        let version = text.split(separator: "\n").first { $0.hasPrefix("version=") }.map { String($0.dropFirst(8)) } ?? ""
        available = Self.isNewer(version, than: current) ? version : nil
        if let v = available, notified != v {
            notified = v
            Notifier.post("velcro \(v) is available. Choose Update to \(v) in the menu.")
        }
    }

    nonisolated static func isNewer(_ a: String, than b: String) -> Bool {
        let x = a.split(separator: ".").compactMap { Int($0) }, y = b.split(separator: ".").compactMap { Int($0) }
        guard !x.isEmpty, !y.isEmpty else { return false }
        for i in 0..<max(x.count, y.count) {
            let p = i < x.count ? x[i] : 0, q = i < y.count ? y[i] : 0
            if p != q { return p > q }
        }
        return false
    }

    func update() {
        guard available != nil, !updating else { return }
        updating = true
        let log = Store.shared.env["LOG"] ?? (NSTemporaryDirectory() + "velcro-update.log")
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/sh")
        // Detached with nohup: the installer quits this app, so it can't be a child that goes down with it.
        p.arguments = ["-c", "nohup /bin/sh -c 'curl -fsSL https://velcro.dgit.co/install | sh' >>\"$1\" 2>&1 &", "velcro-update", log]
        do {
            try p.run()
        } catch {
            updating = false
            Notifier.post("Couldn't start the update: \(error.localizedDescription)")
        }
    }
}
