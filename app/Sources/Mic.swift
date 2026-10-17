import AVFoundation
import AppKit
import Combine

/// Mac mic: lends this Mac's microphone to a Linux machine used over SSH (`velcro mic on <host>`).
/// While it's on, an SSH session runs velcro-mic-host there, which adds a PipeWire microphone and
/// prints "start" when something records from it and "stop" when that ends. The Mac's microphone
/// is open only in between (plus `mic.warm` seconds), and its sound goes down the same SSH session.
final class Mic: ObservableObject {
    static let shared = Mic()

    enum State: Equatable {
        case off, connecting, ready, listening
        case failed(String)
    }

    @Published private(set) var state: State = .off
    /// Another Mac turned Mac Mic on for the same machine after this one.
    @Published private(set) var replaced = false
    var host: String { Store.shared.setting("mic.host") }

    private let capture = MicCapture()
    private var ssh: Process?
    private var connectedHost = ""
    private var lastError = ""
    private var failures = 0
    private var retry: Task<Void, Never>?
    private var cool: Task<Void, Never>?
    private var watching: AnyCancellable?
    /// Hosts this run already put the bundled velcro-mic-host on, so a failed update can't loop.
    private var updated: Set<String> = []
    /// `velcro mic off` in a terminal has to close the microphone now, not at the next refresh.
    private var settingsWatch: DispatchSourceFileSystemObject?

    func start() {
        // A write to an SSH session that just died must not end the app.
        signal(SIGPIPE, SIG_IGN)
        watching = Store.shared.$settings.receive(on: RunLoop.main).sink { _ in
            Task { @MainActor in Mic.shared.sync() }
        }
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { _ in
            Task { @MainActor in Mic.shared.reconnectSoon() }
        }
    }

    /// Re-read settings as soon as the folder they're in changes (`velcro set` replaces the file).
    private func watchSettings() {
        guard settingsWatch == nil, let path = Store.shared.env["SETTINGS"] else { return }
        let folder = (path as NSString).deletingLastPathComponent
        let fd = open(folder, O_EVTONLY)
        guard fd >= 0 else { return }
        let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: .write, queue: .main)
        let take = (folder as NSString).appendingPathComponent(".mic-take")
        source.setEventHandler {
            Task { @MainActor in
                await Store.shared.reloadSettingsIfChanged()
                // `velcro mic on` in a terminal: take the microphone back from another Mac.
                if (try? FileManager.default.removeItem(atPath: take)) != nil { Mic.shared.takeOver() }
            }
        }
        source.setCancelHandler { close(fd) }
        source.resume()
        settingsWatch = source
    }

    private var wanted: Bool {
        Store.shared.setting("mic.on") == "yes" && !host.isEmpty
    }

    /// Turn on or off as the menu asks; `velcro mic on|off` in a terminal does the same.
    func toggle() {
        let on = !wanted
        Task { await Store.shared.set("mic.on", [on ? "yes" : "no"]) }
    }

    /// Follow the settings: connect when on, disconnect when off or the host changed.
    func sync() {
        watchSettings()
        guard wanted else {
            disconnect()
            replaced = false
            set(.off)
            return
        }
        // Replaced by another Mac: only the menu's "Use This Mac's Microphone", or turning it off
        // and on again, takes it back. Anything else would have the two Macs taking turns.
        if replaced && connectedHost == host { return }
        if ssh != nil && connectedHost == host { return }
        disconnect()
        connect()
    }

    /// After another Mac took over: use this Mac's microphone again.
    func takeOver() {
        guard wanted, replaced || ssh == nil else { return }
        failures = 0
        disconnect()
        connect()
    }

    private func reconnectSoon() {
        guard wanted, !replaced else { return }
        failures = 0
        disconnect()
        retry = Task {
            // The network takes a few seconds to come back after sleep.
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            guard !Task.isCancelled else { return }
            connect()
        }
    }

    private func connect() {
        retry?.cancel()
        replaced = false
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: break
        case .notDetermined:
            set(.connecting)
            AVCaptureDevice.requestAccess(for: .audio) { _ in
                Task { @MainActor in Mic.shared.sync() }
            }
            return
        default:
            set(.failed("Allow velcro in System Settings › Privacy & Security › Microphone"))
            return
        }

        let host = self.host
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        process.arguments = [
            "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
            "--", host, "exec ~/.local/bin/velcro-mic-host \(Self.macName)",
        ]
        let input = Pipe(), output = Pipe(), errors = Pipe()
        process.standardInput = input
        process.standardOutput = output
        process.standardError = errors
        let lines = LineReader { line in Task { @MainActor in Mic.shared.heard(line, from: process) } }
        output.fileHandleForReading.readabilityHandler = { handle in lines.feed(handle.availableData) }
        let problems = LineReader { line in Task { @MainActor in Mic.shared.lastError = line } }
        errors.fileHandleForReading.readabilityHandler = { handle in problems.feed(handle.availableData) }
        process.terminationHandler = { process in
            // The last lines (say, "error taken over by …") may still be in the pipes: hand them
            // over before the end, or the end would be taken for a dropped connection.
            output.fileHandleForReading.readabilityHandler = nil
            errors.fileHandleForReading.readabilityHandler = nil
            lines.feed(output.fileHandleForReading.readDataToEndOfFile())
            problems.feed(errors.fileHandleForReading.readDataToEndOfFile())
            Task { @MainActor in Mic.shared.ended(process) }
        }
        lastError = ""
        do {
            try process.run()
        } catch {
            set(.failed("Couldn't run ssh: \(error.localizedDescription)"))
            return
        }
        ssh = process
        connectedHost = host
        capture.output = input.fileHandleForWriting
        set(.connecting)
        log("mic: connecting to \(host)")
    }

    /// Tells velcro-mic-host which Mac this is, so a second Mac can take over without the two
    /// taking turns: the computer's name plus a random tag kept for this install, since two Macs
    /// can share a name. ASCII letters, digits, dots and dashes only: it goes through the remote shell.
    private static let macName: String = {
        let name = Host.current().localizedName ?? ProcessInfo.processInfo.hostName
        let ok = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")
        let safe = String(name.unicodeScalars.map { ok.contains($0) ? Character($0) : "-" })
            .trimmingCharacters(in: CharacterSet(charactersIn: "-.")).prefix(40)
        let key = "micTag"
        var tag = UserDefaults.standard.string(forKey: key) ?? ""
        if tag.count != 6 {
            tag = String(UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(6)).lowercased()
            UserDefaults.standard.set(tag, forKey: key)
        }
        return "\(safe.isEmpty ? "Mac" : safe)-\(tag)"
    }()

    /// A name from macName, without its tag, for the menu.
    private static func readable(_ name: Substring) -> String {
        guard let dash = name.lastIndex(of: "-") else { return String(name) }
        return String(name[..<dash])
    }

    /// VERSION in the velcro-mic-host this app ships.
    private static let helperVersion: String = {
        guard let url = Bundle.main.url(forResource: "velcro-mic-host", withExtension: nil),
              let text = try? String(contentsOf: url, encoding: .utf8),
              let line = text.split(separator: "\n").first(where: { $0.hasPrefix("VERSION = ") }) else { return "" }
        return line.split(separator: "\"").dropFirst().first.map(String.init) ?? ""
    }()

    /// The machine has an older (or newer) velcro-mic-host than this app: put this one there.
    private func updateHelper() {
        let host = connectedHost
        guard !updated.contains(host) else {
            return set(.failed("velcro-mic-host on \(host) doesn't match this velcro: run velcro mic setup \(host)"))
        }
        updated.insert(host)
        log("mic: updating velcro-mic-host on \(host)")
        disconnect()
        set(.connecting)
        Task {
            await CLI.run("mic", "setup", host)
            sync()
        }
    }

    private func heard(_ line: String, from process: Process) {
        guard process === ssh else { return }
        if line.hasPrefix("hello ") {
            if line.dropFirst(6) != Self.helperVersion && !Self.helperVersion.isEmpty { updateHelper() }
            return
        }
        switch line {
        case "ready":
            failures = 0
            if state != .listening { set(.ready) }
        case "start":
            cool?.cancel()
            do {
                try capture.start()
                set(.listening)
            } catch {
                set(.failed("Couldn't open the microphone: \(error.localizedDescription)"))
            }
        case "stop":
            capture.pause()
            set(.ready)
            let warm = Double(Store.shared.setting("mic.warm")) ?? 0
            cool?.cancel()
            cool = Task {
                if warm > 0 { try? await Task.sleep(nanoseconds: UInt64(warm * 1_000_000_000)) }
                guard !Task.isCancelled, state != .listening else { return }
                capture.stop()
            }
        default:
            if line.hasPrefix("error ") {
                lastError = String(line.dropFirst(6))
                // Whatever went wrong there, nothing is recording from this microphone now.
                cool?.cancel()
                capture.stop()
                if state == .listening { set(.ready) }
            }
        }
    }

    private func ended(_ process: Process) {
        guard process === ssh else { return }
        ssh = nil
        capture.output = nil
        capture.stop()
        guard wanted else { return set(.off) }
        if lastError.hasPrefix("usage:") {
            // A velcro-mic-host from before it took the Mac's name.
            return updateHelper()
        }
        if lastError.hasPrefix("taken over by ") {
            // Another Mac turned it on later. This one waits for the menu, `velcro mic on`, or a wake.
            let other = Self.readable(lastError.dropFirst("taken over by ".count))
            log("mic: \(other) took over \(connectedHost)")
            replaced = true
            return set(.failed("\(other) is lending its microphone to \(connectedHost) now"))
        }
        failures += 1
        let reason = lastError.isEmpty ? "lost the connection to \(connectedHost)" : lastError
        log("mic: \(reason)")
        set(failures > 1 ? .failed(reason) : .connecting)
        // Try again soon, then back off to once a minute.
        let wait = min(60, 2 << min(failures, 5))
        retry = Task {
            try? await Task.sleep(nanoseconds: UInt64(wait) * 1_000_000_000)
            guard !Task.isCancelled else { return }
            sync()
        }
    }

    private func disconnect() {
        retry?.cancel()
        cool?.cancel()
        capture.output = nil
        capture.stop()
        if let ssh, ssh.isRunning { ssh.terminate() }
        ssh = nil
    }

    private func set(_ new: State) {
        guard new != state else { return }
        state = new
        // For `velcro mic status`.
        if let path = Store.shared.env["MIC_STATUS"] {
            try? FileManager.default.createDirectory(
                atPath: (path as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
            try? Data((summary + "\n").utf8).write(to: URL(fileURLWithPath: path))
        }
    }

    var summary: String {
        switch state {
        case .off: "off"
        case .connecting: "connecting to \(host)"
        case .ready: "ready on \(host)"
        case .listening: "listening"
        case .failed(let why): why
        }
    }

    private func log(_ text: String) {
        guard let path = Store.shared.env["LOG"], let handle = FileHandle(forWritingAtPath: path) else { return }
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        let stamp = formatter.string(from: Date())
        handle.seekToEndOfFile()
        handle.write(Data("\(stamp) \(text)\n".utf8))
        try? handle.close()
    }
}

/// Splits what a pipe delivers into lines; the pipe calls it on its own queue.
nonisolated final class LineReader: @unchecked Sendable {
    private var pending = Data()
    private let lock = NSLock()
    private let onLine: @Sendable (String) -> Void

    init(_ onLine: @escaping @Sendable (String) -> Void) { self.onLine = onLine }

    func feed(_ data: Data) {
        lock.lock()
        pending.append(data)
        var lines: [String] = []
        while let nl = pending.firstIndex(of: 0x0A) {
            lines.append(String(decoding: pending[pending.startIndex..<nl], as: UTF8.self))
            pending.removeSubrange(pending.startIndex...nl)
        }
        lock.unlock()
        for line in lines { onLine(line.trimmingCharacters(in: .whitespaces)) }
    }
}

/// The Mac's microphone as 16 kHz mono 16-bit PCM, written to `output` while sending.
/// Lives off the main actor: AVAudioEngine calls the tap on its own thread. The engine is started
/// and stopped on `queue`; the tap only takes `lock`, so stopping never waits on a tap in flight.
nonisolated final class MicCapture: @unchecked Sendable {
    private let queue = DispatchQueue(label: "velcro.mic")
    private let writer = DispatchQueue(label: "velcro.mic.write")
    private let lock = NSLock()
    private let engine = AVAudioEngine()
    private let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
    private var converter: AVAudioConverter?  // tap (made for the buffers' format), teardown, under lock
    private var running = false                // queue
    private var sending = false                // lock
    private var handle: FileHandle?            // lock
    private var observer: NSObjectProtocol?

    var output: FileHandle? {
        get { lock.withLock { handle } }
        set { lock.withLock { handle = newValue } }
    }

    init() {
        // A different input (AirPods connecting, say) stops the engine; start it again on the new one.
        observer = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil
        ) { [weak self] _ in
            guard let self else { return }
            self.queue.async {
                guard self.running else { return }
                self.teardown()
                try? self.setup()
            }
        }
    }

    /// Open the microphone (if it isn't already) and send what it hears.
    func start() throws {
        try queue.sync { if !running { try setup() } }
        lock.withLock { sending = true }
    }

    /// Stop sending but keep the microphone open (mic.warm).
    func pause() { lock.withLock { sending = false } }

    /// Close the microphone.
    func stop() {
        lock.withLock { sending = false }
        queue.sync { if running { teardown() } }
    }

    private func setup() throws {
        let input = engine.inputNode
        guard input.outputFormat(forBus: 0).sampleRate > 0 else {
            throw NSError(domain: "velcro", code: 1, userInfo: [NSLocalizedDescriptionKey: "no microphone"])
        }
        // No format: the tap takes whatever the device gives now (passing one that's a moment old
        // raises if AirPods connect in between), and the converter follows the buffers.
        input.installTap(onBus: 0, bufferSize: 1600, format: nil) { [weak self] buffer, _ in
            self?.convert(buffer)
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            throw error
        }
        running = true
    }

    private func teardown() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        lock.withLock { converter = nil }
        running = false
    }

    private func convert(_ buffer: AVAudioPCMBuffer) {
        let (sending, current, handle) = lock.withLock { (self.sending, self.converter, self.handle) }
        guard sending, let handle else { return }
        var converter = current
        if converter?.inputFormat != buffer.format {
            converter = AVAudioConverter(from: buffer.format, to: format)
            lock.withLock { self.converter = converter }
        }
        guard let converter else { return }
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * format.sampleRate / buffer.format.sampleRate) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: capacity) else { return }
        var given = false
        var error: NSError?
        converter.convert(to: out, error: &error) { _, status in
            if given {
                status.pointee = .noDataNow
                return nil
            }
            given = true
            status.pointee = .haveData
            return buffer
        }
        guard error == nil, out.frameLength > 0, let samples = out.int16ChannelData else { return }
        let data = Data(bytes: samples[0], count: Int(out.frameLength) * 2)
        // A stalled connection blocks here, not the tap; ServerAlive ends it within seconds.
        writer.async { try? handle.write(contentsOf: data) }
    }
}
