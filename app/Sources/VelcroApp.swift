import SwiftUI
import UserNotifications

@main
struct VelcroApp: App {
    @NSApplicationDelegateAdaptor private var delegate: AppDelegate
    @ObservedObject private var store = Store.shared

    var body: some Scene {
        MenuBarExtra {
            MenuContent()
        } label: {
            MenuBarLabel()
        }
        .menuBarExtraStyle(.menu)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        SettingsWindow.show()
        return true
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        UNUserNotificationCenter.current().delegate = self
        Engine.shared.start()
        Clipboard.shared.start()
        Mic.shared.start()
        Task {
            await Store.shared.loadEnv()
            await Store.shared.loadSettings()
            Importer.shared.start()
            await Setup.firstLaunch()
        }
    }

    /// Show velcro's notifications even while its Settings window is in front.
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            willPresent notification: UNNotification) async
        -> UNNotificationPresentationOptions {
        [.banner, .list]
    }
}

/// The menu bar icon, with how far an import has got next to it.
struct MenuBarLabel: View {
    @ObservedObject private var store = Store.shared
    @ObservedObject private var importer = Importer.shared

    var body: some View {
        HStack(spacing: 4) {
            Image("Menubar" + store.icon.rawValue.capitalized)
            if let progress = importer.progress {
                Text(progress.percent).monospacedDigit()
            }
        }
        .accessibilityLabel("velcro, \(importer.progress?.summary ?? store.summary)")
    }
}

/// The menu: plain text and velcro's own icons, laid out like assets/logo/mockup.html.
struct MenuContent: View {
    @ObservedObject private var store = Store.shared

    var body: some View {
        Button { SettingsWindow.show() } label: {
            Label { Text("velcro") } icon: { Image("AppMark") }
        }
        .labelStyle(.titleAndIcon)
        Text(store.summary)

        ForEach(store.servers) { server in
            Divider()
            Text("\(server.name)   \(server.attached)/\(server.shares.count)")
            ForEach(server.shares) { share in
                Menu {
                    if share.state == .mounted {
                        Button("Open in Finder") {
                            NSWorkspace.shared.open(URL(fileURLWithPath: share.mountPoint))
                        }
                    } else {
                        Button("Reconnect") { Engine.shared.reconnect(after: 0) }
                            .disabled(store.paused)
                    }
                    Divider()
                    Button("Remove from velcro") { Task { await store.remove(share) } }
                } label: {
                    Label { Text(share.name) } icon: { Image(icon(for: share)) }
                }
                .labelStyle(.titleAndIcon)
            }
        }

        ImportMenu()
        SendMenu()
        MicMenu()
        TranscribeMenu()

        Divider()
        if store.paused {
            Button("Resume") { Task { await store.resume() } }
        } else {
            Button("Pause") { Task { await store.pause() } }
            Button("Reconnect Now") { Engine.shared.reconnect(after: 0) }
        }
        Button("Add Share…") { SettingsWindow.show(adding: true) }
        Button("Settings…") { SettingsWindow.show() }
            .keyboardShortcut(",")
        Button("Open Log") { store.openLog() }
        Divider()
        Button("Quit velcro") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }

    private func icon(for share: Share) -> String {
        if share.state == .mounted { return "ItemConnected" }
        return store.paused ? "ItemPaused" : "ItemDetached"
    }
}

/// Both automatic recorder imports and manual uploads use the recording import folder.
struct ImportMenu: View {
    @ObservedObject private var importer = Importer.shared
    @ObservedObject private var store = Store.shared

    var body: some View {
        if !store.setting("import.dest").isEmpty || !importer.recorders.isEmpty || !importer.candidates.isEmpty || importer.waiting > 0 {
            Divider()
            Text("Recordings")
            if let progress = importer.progress {
                Text(progress.summary)
            } else if importer.running {
                Text("Importing…")
            } else if importer.waiting > 0 {
                Text("\(importer.waiting) waiting on this Mac")
            }
            if !importer.recorders.isEmpty {
                Button("Import Now") { importer.run(auto: false) }
                    .disabled(importer.running)
            }
            Button("Upload Recordings…") { importer.chooseRecordings() }
                .disabled(importer.running)
            if !importer.candidates.isEmpty {
                Menu("Register Recorder") {
                    ForEach(importer.candidates) { volume in
                        Button(volume.title) { importer.register(volume) }
                    }
                }
            }
        } else {
            Divider()
            Button("Upload Recordings…") { importer.chooseRecordings() }
        }
    }
}

/// Recent copied images and files; clicking one saves it to the inbox folder and copies its path.
struct SendMenu: View {
    @ObservedObject private var clipboard = Clipboard.shared
    @ObservedObject private var store = Store.shared

    var body: some View {
        Divider()
        Text("Send to Inbox")
        if store.setting("send.inbox").isEmpty {
            Button("Set Up Inbox…") { SettingsWindow.show(tab: .send) }
        } else if clipboard.clips.isEmpty {
            Text("Copy an image or a file to send it")
        } else {
            ForEach(clipboard.clips) { clip in
                Button { clipboard.send(clip) } label: {
                    Label { Text(clip.title) } icon: { Image(nsImage: clip.thumbnail) }
                }
                .labelStyle(.titleAndIcon)
                .disabled(clipboard.sending)
            }
        }
    }
}

/// Mac mic section: only once `velcro mic setup <host>` has picked a machine.
struct MicMenu: View {
    @ObservedObject private var mic = Mic.shared
    @ObservedObject private var store = Store.shared

    var body: some View {
        if !mic.host.isEmpty {
            Divider()
            Text("Mac Mic → \(mic.host)")
            if mic.state != .off {
                Text(mic.summary.prefix(1).uppercased() + mic.summary.dropFirst())
            }
            if mic.replaced {
                Button("Use This Mac's Microphone") { mic.takeOver() }
            }
            Button(mic.state == .off ? "Lend Microphone" : "Stop Lending Microphone") { mic.toggle() }
        }
    }
}

/// Audio file to text through the speech-to-text Claude Code's /voice uses.
struct TranscribeMenu: View {
    @ObservedObject private var transcriber = Transcriber.shared

    var body: some View {
        Divider()
        if let file = transcriber.running {
            Text("Transcribing \(file)…")
        }
        Button("Transcribe Audio File…") { transcriber.pick() }
            .disabled(transcriber.running != nil)
    }
}
