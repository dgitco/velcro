import AppKit
import UniformTypeIdentifiers

/// Transcribe Audio File…: `velcro transcribe <file>` with the user's own Claude Code or Codex login, and the
/// text on the clipboard. The script does the work and reports failures as notifications.
final class Transcriber: ObservableObject {
    static let shared = Transcriber()

    /// The file being transcribed, while it is.
    @Published private(set) var running: String?

    func pick() {
        let panel = NSOpenPanel()
        panel.title = "Transcribe Audio File"
        panel.message = "velcro copies the text, transcribed with your Claude Code or Codex login."
        panel.prompt = "Transcribe"
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.audio]
        if #available(macOS 14, *) { NSApp.activate() }
        else { NSApp.activate(ignoringOtherApps: true) }
        guard panel.runModal() == .OK, let url = panel.url else { return }
        run(url)
    }

    func run(_ url: URL) {
        guard running == nil else { return }
        running = url.lastPathComponent
        Task {
            let text = Notifier.handle(await CLI.run("transcribe", url.path)).joined(separator: "\n")
            if !text.isEmpty {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(text, forType: .string)
                Notifier.post("Copied the text of \(url.lastPathComponent): \(text.prefix(80))")
            }
            running = nil
        }
    }
}
