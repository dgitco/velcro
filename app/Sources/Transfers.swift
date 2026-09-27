import AppKit
import Combine
import UserNotifications

/// A drive plugged into this Mac, as `velcro recorder candidates` lists it.
struct Volume: Identifiable, Hashable {
    var mountPoint: String
    var uuid: String
    var name: String

    var id: String { mountPoint }
    var title: String { name.isEmpty ? URL(fileURLWithPath: mountPoint).lastPathComponent : name }
}

/// One `recorder` setting: "uuid:<VolumeUUID> <label>" or "name:<volume name>".
struct Recorder: Hashable {
    var setting: String

    var label: String {
        if setting.hasPrefix("name:") { return String(setting.dropFirst(5)) }
        let rest = setting.split(separator: " ", maxSplits: 1)
        return rest.count > 1 ? String(rest[1]) : "Recorder"
    }

    var detail: String {
        if setting.hasPrefix("name:") { return "Any drive with this name" }
        return "Volume " + (setting.split(separator: " ").first.map { String($0.dropFirst(5)) } ?? "")
    }
}

/// Recordings off a USB recorder, through `velcro import`. The script copies, verifies, deletes,
/// and ejects; this decides when to run it and shows how far along it is.
final class Importer: ObservableObject {
    static let shared = Importer()

    struct Progress {
        var phase: String
        var index: Int
        var count: Int
        var name: String
        var done: Double
        var total: Double

        var fraction: Double { total > 0 ? min(1, done / total) : 0 }
        var percent: String { "\(Int(fraction * 100))%" }
        var summary: String {
            let verb = switch phase {
            case "verifying": "Verifying"
            case "hashing": "Checking"
            default: "Copying"
            }
            return "\(verb) \(index) of \(count), \(percent)"
        }
    }

    @Published private(set) var running = false
    @Published private(set) var progress: Progress?
    /// Drives that could be registered as a recorder.
    @Published private(set) var candidates: [Volume] = []
    /// Recordings kept on this Mac until the destination folder's share is attached again.
    @Published private(set) var waiting = 0

    private var again = false
    private var poll: Timer?
    private let store = Store.shared
    private var watching: AnyCancellable?

    private init() {
        // A recorder registered or removed (here or with `velcro recorder` in a terminal) changes
        // which plugged-in drives are candidates.
        watching = store.$settings.dropFirst().sink { _ in
            Task { @MainActor in await Importer.shared.refresh() }
        }
    }

    var recorders: [Recorder] { (store.settings["recorder"] ?? []).map(Recorder.init) }

    func start() {
        Task {
            await refresh()
            if !recorders.isEmpty || waiting > 0 { run() }
        }
    }

    /// Anything mounted or unmounted: maybe a recorder, maybe the NAS coming back for waiting files.
    func volumesChanged(mounted: Bool) {
        Task {
            await refresh()
            if mounted, !recorders.isEmpty || waiting > 0 { run() }
        }
    }

    /// `auto` follows the "import as soon as it's plugged in" setting; Import Now doesn't.
    func run(auto: Bool = true) {
        if running {
            again = true
            return
        }
        running = true
        poll = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { _ in
            Task { @MainActor in Importer.shared.readProgress() }
        }
        Task {
            repeat {
                again = false
                Notifier.handle(await CLI.run(auto ? ["import", "--auto"] : ["import"]))
            } while again
            poll?.invalidate()
            poll = nil
            progress = nil
            running = false
            await refresh()
        }
    }

    func register(_ volume: Volume) {
        Task {
            await CLI.run("recorder", "add", volume.mountPoint)
            await store.loadSettings()
            await refresh()
            if store.setting("import.dest").isEmpty {
                SettingsWindow.show(tab: .imports)
            } else {
                run()
            }
        }
    }

    func forget(_ recorder: Recorder) {
        Task { await store.set("recorder", recorders.filter { $0 != recorder }.map(\.setting)) }
    }

    func refresh() async {
        var found: [Volume] = []
        for line in await CLI.run("recorder", "candidates").split(separator: "\n") {
            let f = line.split(separator: "\u{1f}", omittingEmptySubsequences: false).map(String.init)
            guard f.count >= 3 else { continue }
            found.append(Volume(mountPoint: f[0], uuid: f[1], name: f[2]))
        }
        candidates = found
        if let queue = store.env["QUEUE"] {
            let files = (try? FileManager.default.contentsOfDirectory(atPath: queue)) ?? []
            waiting = files.filter { !$0.hasPrefix(".") }.count
        }
    }

    private func readProgress() {
        guard let path = store.env["IMPORT_STATUS"],
              let line = try? String(contentsOfFile: path, encoding: .utf8)
        else { return }
        let f = line.trimmingCharacters(in: .newlines)
            .split(separator: "\u{1f}", omittingEmptySubsequences: false).map(String.init)
        guard f.count >= 6 else { return }
        progress = Progress(phase: f[0], index: Int(f[1]) ?? 0, count: Int(f[2]) ?? 0, name: f[3],
                            done: Double(f[4]) ?? 0, total: Double(f[5]) ?? 0)
    }
}

/// Recent images and files copied on this Mac, offered in the menu to send to the NAS inbox.
/// Watching is read-only: the clipboard only changes when an item is clicked, to the sent path.
final class Clipboard: ObservableObject {
    static let shared = Clipboard()

    struct Clip: Identifiable {
        let id = UUID()
        let url: URL
        let title: String
        let thumbnail: NSImage
    }

    static let limit = 4

    @Published private(set) var clips: [Clip] = []
    @Published private(set) var sending = false

    private var seen = NSPasteboard.general.changeCount
    private var timer: Timer?

    /// Copied images are saved here as PNG, as `velcro send` with no file does.
    private let folder = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("velcro/clips")

    /// Password managers mark what they copy as concealed or transient; never keep that.
    private static let skip: Set<NSPasteboard.PasteboardType> = [
        .init("org.nspasteboard.ConcealedType"), .init("org.nspasteboard.TransientType"),
        .init("org.nspasteboard.AutoGeneratedType"),
    ]

    func start() {
        try? FileManager.default.removeItem(at: folder)
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in
            Task { @MainActor in Clipboard.shared.check() }
        }
        timer?.tolerance = 0.5
    }

    private func check() {
        let pasteboard = NSPasteboard.general
        guard pasteboard.changeCount != seen else { return }
        seen = pasteboard.changeCount
        guard Self.skip.isDisjoint(with: pasteboard.types ?? []) else { return }

        // A file copied in Finder also carries its icon as an image, so files come first.
        if let urls = pasteboard.readObjects(forClasses: [NSURL.self],
                                             options: [.urlReadingFileURLsOnly: true]) as? [URL],
           !urls.isEmpty {
            for url in urls.reversed() {
                let icon = NSWorkspace.shared.icon(forFile: url.path)
                add(Clip(url: url, title: url.lastPathComponent, thumbnail: Self.thumbnail(icon)))
            }
        } else if let png = Self.png(from: pasteboard) {
            saveImage(png)
        }
    }

    private func saveImage(_ png: Data) {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        let url = folder.appendingPathComponent("clip-\(formatter.string(from: Date())).png")
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try png.write(to: url)
        } catch { return }
        guard let image = NSImage(data: png) else { return }
        let size = image.representations.first.map { "\($0.pixelsWide) × \($0.pixelsHigh)" } ?? ""
        let time = Date().formatted(date: .omitted, time: .shortened)
        add(Clip(url: url, title: "Image \(size), \(time)", thumbnail: Self.thumbnail(image)))
    }

    private func add(_ clip: Clip) {
        clips.removeAll { $0.url == clip.url }
        clips.insert(clip, at: 0)
        for old in clips.dropFirst(Self.limit) where old.url.path.hasPrefix(folder.path) {
            try? FileManager.default.removeItem(at: old.url)
        }
        clips = Array(clips.prefix(Self.limit))
    }

    /// `velcro send <file>` puts it in the inbox and copies the path the server sees.
    func send(_ clip: Clip) {
        guard !sending else { return }
        sending = true
        Task {
            let paths = Notifier.handle(await CLI.run("send", clip.url.path))
            if let path = paths.first {
                Notifier.post("Sent \(clip.url.lastPathComponent). Copied \(path)")
            }
            sending = false
        }
    }

    private static func png(from pasteboard: NSPasteboard) -> Data? {
        if let png = pasteboard.data(forType: .png) { return png }
        guard let tiff = pasteboard.data(forType: .tiff), let bitmap = NSBitmapImageRep(data: tiff)
        else { return nil }
        return bitmap.representation(using: .png, properties: [:])
    }

    /// Menu item icons stay at 16 points whatever the image's size.
    private static func thumbnail(_ image: NSImage) -> NSImage {
        let side: CGFloat = 16
        let scale = min(side / max(image.size.width, 1), side / max(image.size.height, 1))
        let size = NSSize(width: max(1, image.size.width * scale), height: max(1, image.size.height * scale))
        return NSImage(size: size, flipped: false) { rect in
            image.draw(in: rect)
            return true
        }
    }
}

/// Notification Center, for what the script reports with "notify" lines.
nonisolated enum Notifier {
    /// Posts every "notify" line of `velcro` output and returns the other lines.
    @discardableResult
    static func handle(_ output: String) -> [String] {
        var rest: [String] = []
        for line in output.split(separator: "\n").map(String.init) {
            if line.hasPrefix("notify\u{1f}") {
                post(String(line.dropFirst(7)))
            } else {
                rest.append(line)
            }
        }
        return rest
    }

    static func post(_ body: String) {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert]) { granted, _ in
            guard granted else { return }
            let content = UNMutableNotificationContent()
            content.title = "velcro"
            content.body = body
            UNUserNotificationCenter.current().add(
                UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
        }
    }
}
