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
    func applicationDidFinishLaunching(_ notification: Notification) {
        UNUserNotificationCenter.current().delegate = self
        Engine.shared.start()
        Clipboard.shared.start()
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

/// Recorder section: only once there's a recorder, a drive to register, or recordings waiting.
struct ImportMenu: View {
    @ObservedObject private var importer = Importer.shared
    @ObservedObject private var store = Store.shared

    var body: some View {
        if !importer.recorders.isEmpty || !importer.candidates.isEmpty || importer.waiting > 0 {
            Divider()
            Text("Recorder")
            if let progress = importer.progress {
                Text(progress.summary)
            } else if importer.running {
                Text("Importing…")
            } else if importer.waiting > 0 {
                Text("\(importer.waiting) waiting on this Mac for the NAS")
            }
            if !importer.recorders.isEmpty {
                Button("Import Now") { importer.run(auto: false) }
                    .disabled(importer.running)
            }
            if !importer.candidates.isEmpty {
                Menu("Register Recorder") {
                    ForEach(importer.candidates) { volume in
                        Button(volume.title) { importer.register(volume) }
                    }
                }
            }
        }
    }
}

/// Recent copied images and files; clicking one saves it to the NAS inbox and copies its path.
struct SendMenu: View {
    @ObservedObject private var clipboard = Clipboard.shared
    @ObservedObject private var store = Store.shared

    var body: some View {
        Divider()
        Text("Send to NAS")
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
