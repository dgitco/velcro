import AppKit
import Network

/// Decides when to reconnect: on a timer, when the network changes, and when the Mac wakes.
/// The reconnecting itself is `velcro run`, which does nothing if a pass is already going.
final class Engine {
    static let shared = Engine()

    /// Seconds between timed passes; set in Settings.
    static let intervalKey = "interval"
    static let defaultInterval: TimeInterval = 60

    private let store = Store.shared
    private let path = NWPathMonitor()
    private var tick: Timer?
    private var soon: Task<Void, Never>?
    private var lastRun = Date.distantPast
    private var lastRefresh = Date.distantPast
    private var lastInterfaces: [String] = []
    private var lastQueueTry = Date()
    private var lastInboxClean = Date.distantPast

    private var interval: TimeInterval {
        let value = UserDefaults.standard.double(forKey: Self.intervalKey)
        return value > 0 ? value : Self.defaultInterval
    }

    func start() {
        path.pathUpdateHandler = { [weak self] path in
            let up = path.status == .satisfied
            let interfaces = path.availableInterfaces.map(\.name)
            Task { @MainActor in self?.networkChanged(up: up, interfaces: interfaces) }
        }
        path.start(queue: .global(qos: .utility))

        let workspace = NSWorkspace.shared.notificationCenter
        workspace.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { _ in
            // The network takes a few seconds to come back after sleep.
            Task { @MainActor in Engine.shared.reconnect(after: 5) }
        }
        for name in [NSWorkspace.didMountNotification, NSWorkspace.didUnmountNotification] {
            let mounted = name == NSWorkspace.didMountNotification
            workspace.addObserver(forName: name, object: nil, queue: .main) { _ in
                Task { @MainActor in
                    await Store.shared.refresh()
                    // A recorder plugged in, or the destination back for recordings waiting on this Mac.
                    Importer.shared.volumesChanged(mounted: mounted)
                }
            }
        }

        // One timer covers both the reconnect interval and keeping the menu current,
        // including changes made with the velcro command in a terminal.
        tick = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { _ in
            Task { @MainActor in Engine.shared.onTick() }
        }
        tick?.tolerance = 1

        reconnect(after: 0)
    }

    private func onTick() {
        let now = Date()
        if now.timeIntervalSince(lastRun) >= interval {
            reconnect(after: 0)
        } else if now.timeIntervalSince(lastRefresh) >= 10 {
            lastRefresh = now
            Task { await store.refresh() }
        }
        Importer.shared.followOutsideImport()
        // Mounts usually trigger these; the timer is the backstop.
        if Importer.shared.waiting > 0, now.timeIntervalSince(lastQueueTry) >= 300 {
            lastQueueTry = now
            Importer.shared.run()
        }
        if !store.setting("send.inbox").isEmpty, now.timeIntervalSince(lastInboxClean) >= 6 * 3600 {
            lastInboxClean = now
            Task { await CLI.run("send", "--clean") }
        }
    }

    private func networkChanged(up: Bool, interfaces: [String]) {
        defer { lastInterfaces = interfaces }
        guard interfaces != lastInterfaces else { return }
        if up {
            // Wait for addresses and DNS to settle before trying.
            reconnect(after: 2)
        } else {
            Task { await store.refresh() }
        }
    }

    /// Reconnect after a delay; asking again before it runs restarts the wait.
    func reconnect(after seconds: Double) {
        soon?.cancel()
        soon = Task {
            if seconds > 0 {
                try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
                guard !Task.isCancelled else { return }
            }
            lastRun = Date()
            lastRefresh = lastRun
            await store.reconnect()
        }
    }
}
