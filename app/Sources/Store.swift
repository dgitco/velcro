import AppKit

struct Share: Identifiable, Hashable {
    enum State: String { case mounted, reachable, offline }

    var state: State
    var name: String
    var mountPoint: String
    var server: String
    var address: String
    var fallbacks: [String]

    var id: String { address }
}

struct Server: Identifiable {
    var name: String
    var shares: [Share]

    var id: String { name }
    var attached: Int { shares.filter { $0.state == .mounted }.count }
}

/// What the menu shows: every share and whether it's attached, as `velcro scan` reports it.
final class Store: ObservableObject {
    static let shared = Store()

    @Published private(set) var shares: [Share] = []
    @Published private(set) var paused = false
    /// Paths and version from `velcro env`: CONF, PAUSE, LOG, BIN, SELF, VERSION.
    @Published private(set) var env: [String: String] = [:]

    private var refreshing = false
    private var refreshAgain = false
    private var settingsStamp: Date?

    var attached: Int { shares.filter { $0.state == .mounted }.count }

    /// Shares grouped by the server in their address, in config order.
    var servers: [Server] {
        var groups: [Server] = []
        for share in shares {
            if let i = groups.firstIndex(where: { $0.name == share.server }) {
                groups[i].shares.append(share)
            } else {
                groups.append(Server(name: share.server, shares: [share]))
            }
        }
        return groups
    }

    enum Icon: String { case connected, detached, paused }

    var icon: Icon {
        if paused { return .paused }
        if shares.isEmpty || attached < shares.count { return .detached }
        return .connected
    }

    var summary: String {
        if paused { return "Paused" }
        if shares.isEmpty { return "No shares yet" }
        return "\(attached) of \(shares.count) attached"
    }

    func loadEnv() async {
        var values: [String: String] = [:]
        for line in await CLI.run("env").split(separator: "\n") {
            guard let eq = line.firstIndex(of: "=") else { continue }
            values[String(line[..<eq])] = String(line[line.index(after: eq)...])
        }
        env = values
    }

    /// Import and send settings from `velcro settings`, defaults filled in. `recorder` and `map`
    /// can have several values.
    @Published private(set) var settings: [String: [String]] = [:]

    func setting(_ key: String) -> String { settings[key]?.first ?? "" }

    /// Settings changed with `velcro set` in a terminal show up on the next refresh.
    private func reloadSettingsIfChanged() async {
        guard let path = env["SETTINGS"] else { return }
        let stamp = (try? FileManager.default.attributesOfItem(atPath: path))?[.modificationDate] as? Date
        guard stamp != settingsStamp else { return }
        settingsStamp = stamp
        await loadSettings()
    }

    func loadSettings() async {
        var values: [String: [String]] = [:]
        for line in await CLI.run("settings").split(separator: "\n") {
            guard let eq = line.firstIndex(of: "=") else { continue }
            values[String(line[..<eq]), default: []].append(String(line[line.index(after: eq)...]))
        }
        settings = values
    }

    /// `velcro set <key> <values...>`; no values puts the default back.
    func set(_ key: String, _ values: [String]) async {
        await CLI.run(["set", key] + values)
        await loadSettings()
    }

    /// Re-reads mount state without probing the network. Calls that arrive mid-refresh
    /// collapse into one more pass afterwards.
    func refresh() async {
        if refreshing {
            refreshAgain = true
            return
        }
        refreshing = true
        defer { refreshing = false }
        repeat {
            refreshAgain = false
            apply(await CLI.run("scan", "fast"))
            await reloadSettingsIfChanged()
        } while refreshAgain
    }

    private func apply(_ output: String) {
        var lines = output.split(separator: "\n", omittingEmptySubsequences: true)
        guard !lines.isEmpty else { return }
        paused = lines.removeFirst() == "paused"
        shares = lines.compactMap { line in
            let f = line.split(separator: "\u{1f}", omittingEmptySubsequences: false).map(String.init)
            guard f.count >= 5 else { return nil }
            let words = f[4].split(whereSeparator: \.isWhitespace).map(String.init)
            return Share(
                state: Share.State(rawValue: f[0]) ?? .offline,
                name: f[1], mountPoint: f[2], server: f[3],
                address: words.first ?? "", fallbacks: Array(words.dropFirst()))
        }
    }

    /// One reconnect pass, then show the result.
    func reconnect() async {
        await CLI.run("run")
        await refresh()
    }

    func pause() async {
        await CLI.run("pause")
        await refresh()
    }

    func resume() async {
        await CLI.run("resume")
        await refresh()
    }

    func remove(_ share: Share) async {
        await CLI.run("rm", share.address)
        await refresh()
    }

    func openLog() {
        guard let log = env["LOG"] else { return }
        if !FileManager.default.fileExists(atPath: log) {
            FileManager.default.createFile(atPath: log, contents: nil)
        }
        NSWorkspace.shared.open(URL(fileURLWithPath: log))
    }
}
