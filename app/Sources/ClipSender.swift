import AppKit
import Combine

/// Mac clipboard: sends each image copied on this Mac to a Linux machine used over SSH
/// (`velcro clip on <host>`), where velcro-clip-host keeps it so ⌃V in Claude Code pastes it.
/// Clipboard hands over images only, as PNG; text and files never leave the Mac.
final class ClipSender: ObservableObject {
    static let shared = ClipSender()

    enum State: Equatable {
        case off, ready, sending
        case sent(Date)
        case failed(String)
    }

    @Published private(set) var state: State = .off
    var host: String { Store.shared.setting("clip.host") }

    private var ssh: Process?
    private var watching: AnyCancellable?
    /// Hosts this run already put the bundled velcro-clip-host on, so a failed update can't loop.
    private var updated: Set<String> = []
    /// Counts copies, so a send retried after an install can tell a newer image came meanwhile.
    private var copies = 0

    /// On, with a machine to send to: Clipboard asks before reading a copied file.
    var sending: Bool {
        Store.shared.setting("clip.on") == "yes" && !host.isEmpty
    }
    private var wanted: Bool { sending }

    func start() {
        // A write to an SSH session that just died must not end the app.
        signal(SIGPIPE, SIG_IGN)
        watching = Store.shared.$settings.receive(on: RunLoop.main).sink { _ in
            Task { @MainActor in ClipSender.shared.sync() }
        }
    }

    /// The menu's switch; `velcro clip on|off` in a terminal does the same.
    func toggle() {
        let on = !wanted
        Task { await Store.shared.set("clip.on", [on ? "yes" : "no"]) }
    }

    private func sync() {
        if !wanted {
            stop()
            set(.off)
        } else if state == .off {
            set(.ready)
        }
    }

    /// Clipboard saw a new image. The newest one wins: a send still going is dropped.
    func copied(_ png: Data) {
        guard wanted else { return }
        copies += 1
        send(png, to: host, retry: true)
    }

    private func send(_ png: Data, to host: String, retry: Bool) {
        stop()
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        process.arguments = [
            "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "--", host, "exec ~/.local/bin/velcro-clip-host put",
        ]
        let input = Pipe(), output = Pipe(), errors = Pipe()
        process.standardInput = input
        process.standardOutput = output
        process.standardError = errors
        // Both answers are a line or two, well under what a pipe holds until the end.
        process.terminationHandler = { process in
            let said = String(decoding: output.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            let problem = String(decoding: errors.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            Task { @MainActor in
                ClipSender.shared.ended(process, said: said, problem: problem, png: png, host: host, retry: retry)
            }
        }
        do {
            try process.run()
        } catch {
            set(.failed("Couldn't run ssh: \(error.localizedDescription)"))
            return
        }
        ssh = process
        set(.sending)
        let writer = input.fileHandleForWriting
        DispatchQueue.global(qos: .userInitiated).async {
            try? writer.write(contentsOf: png)
            try? writer.close()
        }
    }

    private func ended(_ process: Process, said: String, problem: String, png: Data, host: String, retry: Bool) {
        guard process === ssh else { return }  // replaced by a newer image, or turned off
        ssh = nil
        let reply = said.split(separator: "\n").last.map(String.init) ?? ""
        if process.terminationStatus == 0, reply.hasPrefix("ok ") {
            set(.sent(Date()))
            log("clip: sent an image to \(host) (\(png.count / 1024) KB)")
            // An older (or newer) velcro-clip-host there: put this app's one in its place.
            if reply.dropFirst(3) != Self.helperVersion, !Self.helperVersion.isEmpty, !updated.contains(host) {
                updated.insert(host)
                Task { await install(on: host) }
            }
            return
        }
        // Not there yet (set up with an older velcro, or removed by hand): install it and try once more.
        if retry, process.terminationStatus == 127 || problem.contains("No such file"), !updated.contains(host) {
            updated.insert(host)
            let copy = copies
            Task {
                await install(on: host)
                // Turned off, moved to another machine, or a newer image went out while installing.
                guard wanted, host == self.host, copy == copies, ssh == nil else { return }
                send(png, to: host, retry: false)
            }
            return
        }
        let line = problem.split(separator: "\n").last.map(String.init) ?? ""
        let why = line.hasPrefix("error ") ? String(line.dropFirst(6))
            : line.isEmpty ? "couldn't reach \(host)" : line
        log("clip: \(why)")
        set(.failed(why))
    }

    private func install(on host: String) async {
        log("clip: updating velcro-clip-host on \(host)")
        await CLI.run("clip", "setup", host)
    }

    private func stop() {
        if let ssh, ssh.isRunning { ssh.terminate() }
        ssh = nil
    }

    /// VERSION in the velcro-clip-host this app ships.
    private static let helperVersion: String = {
        guard let url = Bundle.main.url(forResource: "velcro-clip-host", withExtension: nil),
              let text = try? String(contentsOf: url, encoding: .utf8),
              let line = text.split(separator: "\n").first(where: { $0.hasPrefix("VERSION = ") }) else { return "" }
        return line.split(separator: "\"").dropFirst().first.map(String.init) ?? ""
    }()

    private func set(_ new: State) {
        guard new != state else { return }
        state = new
        // For `velcro clip status`.
        if let path = Store.shared.env["CLIP_STATUS"] {
            try? FileManager.default.createDirectory(
                atPath: (path as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
            try? Data((summary + "\n").utf8).write(to: URL(fileURLWithPath: path))
        }
    }

    var summary: String {
        switch state {
        case .off: "off"
        case .ready: "images you copy paste on \(host) with ⌃V"
        case .sending: "sending an image to \(host)"
        case .sent(let when): "sent an image at \(when.formatted(date: .omitted, time: .shortened))"
        case .failed(let why): why
        }
    }

    private func log(_ text: String) {
        guard let path = Store.shared.env["LOG"], let handle = FileHandle(forWritingAtPath: path) else { return }
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        handle.seekToEndOfFile()
        handle.write(Data("\(formatter.string(from: Date())) \(text)\n".utf8))
        try? handle.close()
    }
}
