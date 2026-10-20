import AppKit
import UniformTypeIdentifiers

/// Transcribe Audio File…: `velcro transcribe <file>` with the user's own Claude Code login, and the
/// text on the clipboard. The script does the work and reports failures as notifications.
final class Transcriber: ObservableObject {
    static let shared = Transcriber()

    /// The file being transcribed, while it is.
    @Published private(set) var running: String?

    func pick() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.audio]
        panel.message = "Choose a voice memo or recording. velcro copies its text."
        NSApp.activate(ignoringOtherApps: true)
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
