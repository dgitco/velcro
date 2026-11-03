import AppKit
import AVFoundation
import Carbon.HIToolbox
import Combine

/// "ctrl+alt+r" → a Carbon key code and modifier mask. Same words the Windows build reads.
struct HotKeySpec: Equatable {
    var keyCode: UInt32
    var modifiers: UInt32

    private static let keys: [String: UInt32] = [
        "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12,
        "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
        "9": 25, "7": 26, "8": 28, "0": 29, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40,
        "n": 45, "m": 46,
        "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101,
        "f10": 109, "f11": 103, "f12": 111,
    ]

    /// Needs at least one modifier, so a plain letter can never take over typing.
    static func parse(_ text: String) -> HotKeySpec? {
        var mask: UInt32 = 0
        var key: UInt32?
        for word in text.lowercased().split(separator: "+").map({ $0.trimmingCharacters(in: .whitespaces) }) {
            switch word {
            case "ctrl", "control": mask |= UInt32(controlKey)
            case "alt", "opt", "option": mask |= UInt32(optionKey)
            case "shift": mask |= UInt32(shiftKey)
            case "cmd", "command": mask |= UInt32(cmdKey)
            default:
                guard key == nil, let code = keys[word] else { return nil }
                key = code
            }
        }
        guard let key, mask != 0 else { return nil }
        return HotKeySpec(keyCode: key, modifiers: mask)
    }
}

/// A global shortcut starts a recording; pressing it again stops it and uploads the file the way
/// `velcro import --files` does, so the server sees it like any recorder import.
@MainActor
final class HotkeyRecorder: ObservableObject {
    static let shared = HotkeyRecorder()

    @Published private(set) var recording = false
    @Published private(set) var elapsed = 0
    /// Why the shortcut isn't active (bad value, taken by another app), for the settings pane.
    @Published private(set) var hotkeyError = ""

    private var hotKeyRef: EventHotKeyRef?
    private var handler: EventHandlerRef?
    private var recorder: AVAudioRecorder?
    private var ticker: Timer?
    private var current = ""
    private var registered = "\u{0}"
    private var subscription: AnyCancellable?
    private let store = Store.shared

    static var folder: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("velcro/hotkey", isDirectory: true)
    }

    func start() {
        prune()
        installHandler()
        // `notifyutil -p io.github.dgitco.velcro.record` does what the shortcut does: for a terminal,
        // a Shortcuts action, or a check on a Mac where nobody is at the keyboard.
        CFNotificationCenterAddObserver(CFNotificationCenterGetDarwinNotifyCenter(), nil, { _, _, _, _, _ in
            Task { @MainActor in HotkeyRecorder.shared.toggle() }
        }, "io.github.dgitco.velcro.record" as CFString, nil, .deliverImmediately)
        subscription = store.$settings.receive(on: DispatchQueue.main).sink { [weak self] _ in self?.register() }
    }

    var hotkeyText: String { store.setting("record.hotkey") }

    // MARK: Shortcut

    private func installHandler() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, _ in
            Task { @MainActor in HotkeyRecorder.shared.toggle() }
            return noErr
        }, 1, &spec, nil, &handler)
    }

    private func register() {
        let text = hotkeyText
        guard text != registered else { return }
        registered = text
        if let ref = hotKeyRef { UnregisterEventHotKey(ref); hotKeyRef = nil }
        guard !text.isEmpty else { hotkeyError = ""; return }
        guard let spec = HotKeySpec.parse(text) else {
            hotkeyError = "Use modifiers plus one key, like ctrl+alt+r"
            return
        }
        let id = EventHotKeyID(signature: OSType(0x56454C43), id: 1)  // 'VELC'
        let status = RegisterEventHotKey(spec.keyCode, spec.modifiers, id, GetApplicationEventTarget(), 0, &hotKeyRef)
        hotkeyError = status == noErr ? "" : "Another app already uses \(text)"
        NSLog("velcro hotkey \(text): \(status == noErr ? "registered" : "failed \(status)")")
    }

    // MARK: Recording

    func toggle() { recording ? stop() : begin() }

    private func begin() {
        guard !store.setting("import.dest").isEmpty else {
            Notifier.post("Choose where recordings go first (Settings → Import → Folder)")
            SettingsWindow.show(tab: .imports)
            return
        }
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: record()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { ok in
                Task { @MainActor in
                    if ok { HotkeyRecorder.shared.record() } else { Notifier.post("velcro can't use the microphone. Allow it in System Settings → Privacy & Security → Microphone.") }
                }
            }
        default:
            Notifier.post("velcro can't use the microphone. Allow it in System Settings → Privacy & Security → Microphone.")
        }
    }

    private func record() {
        try? FileManager.default.createDirectory(at: Self.folder, withIntermediateDirectories: true)
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyyMMddHHmmss"
        let url = Self.folder.appendingPathComponent(f.string(from: Date()) + ".m4a")
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 16000, AVNumberOfChannelsKey: 1,
            AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue,
        ]
        guard let r = try? AVAudioRecorder(url: url, settings: settings), r.record() else {
            Notifier.post("Couldn't start recording")
            return
        }
        recorder = r
        current = url.path
        elapsed = 0
        recording = true
        ticker = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in
            Task { @MainActor in HotkeyRecorder.shared.elapsed += 1 }
        }
    }

    func stop() {
        guard recording else { return }
        ticker?.invalidate()
        ticker = nil
        recorder?.stop()
        recorder = nil
        recording = false
        let path = current
        guard elapsed >= 1 else {  // a double tap isn't a recording
            try? FileManager.default.removeItem(atPath: path)
            return
        }
        upload(path, tries: 0)
    }

    /// An import already running (a recorder just plugged in) finishes first; the file stays put meanwhile.
    private func upload(_ path: String, tries: Int) {
        if Importer.shared.running, tries < 120 {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { HotkeyRecorder.shared.upload(path, tries: tries + 1) }
            return
        }
        Importer.shared.run(files: [path])
    }

    var elapsedText: String { String(format: "%d:%02d", elapsed / 60, elapsed % 60) }

    /// Local copies only wait for the upload; a week later they are gone.
    private func prune() {
        let old = Date().addingTimeInterval(-7 * 86400)
        for url in (try? FileManager.default.contentsOfDirectory(at: Self.folder, includingPropertiesForKeys: [.contentModificationDateKey])) ?? [] {
            if let d = try? url.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate, d < old {
                try? FileManager.default.removeItem(at: url)
            }
        }
    }
}
