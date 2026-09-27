import ServiceManagement
import SwiftUI

/// Settings lives in a plain AppKit window: a menu bar app has no main window, and SwiftUI's
/// Settings scene can't be opened reliably from a menu bar menu on every macOS version.
enum SettingsWindow {
    private static var window: NSWindow?

    static func show(adding: Bool = false, tab: SettingsModel.Tab? = nil) {
        if window == nil {
            let w = NSWindow(contentViewController: NSHostingController(rootView: SettingsView()))
            w.title = "velcro Settings"
            w.styleMask = [.titled, .closable]
            w.isReleasedWhenClosed = false
            w.center()
            window = w
        }
        if adding {
            SettingsModel.shared.tab = .shares
            SettingsModel.shared.editing = Draft()
        }
        if let tab { SettingsModel.shared.tab = tab }
        if #available(macOS 14, *) {
            NSApp.activate()
        } else {
            NSApp.activate(ignoringOtherApps: true)
        }
        window?.makeKeyAndOrderFront(nil)
        // Activation is only a request since macOS 14; make sure the window is at least in front.
        window?.orderFrontRegardless()
    }
}

final class SettingsModel: ObservableObject {
    static let shared = SettingsModel()

    enum Tab { case shares, imports, send, general }

    @Published var tab = Tab.shares
    /// The share being added or edited, shown as a sheet.
    @Published var editing: Draft?
}

/// The fields of the add/edit sheet. `entry` is nil when adding.
struct Draft: Identifiable {
    var id = UUID()
    var entry: Entry.ID?
    var address = ""
    var fallbacks = ""
}

struct SettingsView: View {
    @ObservedObject private var model = SettingsModel.shared

    var body: some View {
        VStack(spacing: 0) {
            Picker("Section", selection: $model.tab) {
                Text("Shares").tag(SettingsModel.Tab.shares)
                Text("Import").tag(SettingsModel.Tab.imports)
                Text("Send").tag(SettingsModel.Tab.send)
                Text("General").tag(SettingsModel.Tab.general)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .fixedSize()
            .padding(.top, 14)

            switch model.tab {
            case .shares: SharesPane().padding(20)
            case .imports: ImportPane()
            case .send: SendPane()
            case .general: GeneralPane()
            }
        }
        .frame(width: 560, height: 520)
    }
}

// MARK: - Shares

struct SharesPane: View {
    @ObservedObject private var store = Store.shared
    @ObservedObject private var model = SettingsModel.shared
    @State private var config: Config?
    @State private var selection: Entry.ID?
    @State private var error: String?

    private var entries: [Entry] { config?.entries ?? [] }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            List(selection: $selection) {
                ForEach(entries) { entry in
                    EntryRow(entry: entry, share: store.shares.first { $0.address == entry.address })
                        .tag(entry.id)
                }
            }
            .contextMenu(forSelectionType: Entry.ID.self) { ids in
                if let id = ids.first {
                    Button("Edit…") { edit(id) }
                    Button("Remove") { remove(id) }
                }
            } primaryAction: { ids in
                if let id = ids.first { edit(id) }
            }
            .listStyle(.bordered(alternatesRowBackgrounds: false))
            .overlay {
                if entries.isEmpty {
                    Text("No shares yet").foregroundStyle(.secondary)
                }
            }

            HStack {
                Button("Add…") { model.editing = Draft() }
                Button("Edit…") { selection.map(edit) }.disabled(selection == nil)
                Button("Remove") { selection.map(remove) }.disabled(selection == nil)
                Spacer()
                if let error {
                    Text(error).foregroundStyle(.red).font(.caption)
                }
            }
        }
        .sheet(item: $model.editing) { draft in
            ShareEditor(draft: draft, taken: Set(entries.filter { $0.id != draft.entry }.map(\.address)),
                        onSave: save)
        }
        .onAppear(perform: load)
        .onReceive(store.$shares) { _ in load() }
        .onReceive(store.$env) { _ in load() }
    }

    private func load() {
        guard let path = store.env["CONF"] else { return }
        let fresh = Config(path: path)
        // Keep ids (and so the selection) stable when nothing changed on disk.
        if fresh.entries.map(\.address) != entries.map(\.address)
            || fresh.entries.map(\.fallbacks) != entries.map(\.fallbacks) {
            config = fresh
            selection = nil
        }
    }

    private func edit(_ id: Entry.ID) {
        guard let entry = entries.first(where: { $0.id == id }) else { return }
        model.editing = Draft(entry: id, address: entry.address.replacingOccurrences(of: "%20", with: " "),
                              fallbacks: entry.fallbacks.joined(separator: " "))
    }

    private func save(_ draft: Draft) {
        guard var config else { return }
        let entry = Entry(id: draft.entry ?? UUID(), address: Entry.normalize(draft.address),
                          fallbacks: draft.fallbacks.split(whereSeparator: \.isWhitespace).map(String.init))
        var list = config.entries
        if let i = list.firstIndex(where: { $0.id == draft.entry }) {
            list[i] = entry
        } else {
            list.append(entry)
        }
        write(&config, list)
        Engine.shared.reconnect(after: 0)
    }

    private func remove(_ id: Entry.ID) {
        guard var config else { return }
        write(&config, config.entries.filter { $0.id != id })
        selection = nil
        Task { await store.refresh() }
    }

    private func write(_ config: inout Config, _ list: [Entry]) {
        config.entries = list
        do {
            try config.save()
            error = nil
        } catch {
            self.error = "Couldn't save: \(error.localizedDescription)"
        }
        self.config = config
    }
}

struct EntryRow: View {
    var entry: Entry
    var share: Share?

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text(entry.name)
                Text(([entry.address] + entry.fallbacks).map { $0.replacingOccurrences(of: "%20", with: " ") }
                        .joined(separator: ", then "))
                    .font(.caption).foregroundStyle(.secondary)
                    .lineLimit(1).truncationMode(.middle)
            }
            Spacer()
            Text(share?.state == .mounted ? "Attached" : "Not attached")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(.vertical, 3)
    }
}

struct ShareEditor: View {
    @State var draft: Draft
    /// Addresses other entries already use.
    var taken: Set<String>
    var onSave: (Draft) -> Void
    @Environment(\.dismiss) private var dismiss

    private var problem: String? {
        if let problem = Entry.validate(draft.address) { return problem }
        if taken.contains(Entry.normalize(draft.address)) { return "This share is already in velcro" }
        return nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(draft.entry == nil ? "Add a share" : "Edit share").font(.headline)
            Form {
                TextField("Address", text: $draft.address, prompt: Text(verbatim: "smb://me@nas.local/home"))
                TextField("Fallback hosts", text: $draft.fallbacks, prompt: Text("Optional"))
            }
            VStack(alignment: .leading, spacing: 6) {
                if !draft.address.isEmpty, let problem {
                    Text(problem).foregroundStyle(.red)
                }
                Text("Use smb://user@host/share, or nfs://host/export/path. SMB is the better choice on a Mac.")
                Text("Fallback hosts are tried in order when the first one doesn't answer, like a LAN address and then a Tailscale name. Separate them with spaces.")
                Text("velcro uses the password in your keychain. If macOS asks for it the first time, choose to remember it.")
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button(draft.entry == nil ? "Add" : "Save") {
                    onSave(draft)
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(problem != nil)
            }
        }
        .padding(20)
        .frame(width: 460)
    }
}

// MARK: - General

struct GeneralPane: View {
    @ObservedObject private var store = Store.shared
    @AppStorage(Engine.intervalKey) private var interval = Engine.defaultInterval
    @State private var login = Setup.login
    @State private var linking = false

    private var linked: Bool { Setup.commandIsLinked(store.env["BIN"]) }

    var body: some View {
        Form {
            Section {
                Toggle("Open velcro at login", isOn: Binding(
                    get: { login != .off },
                    set: { Setup.setLogin($0); login = Setup.login }))
                if login == .needsApproval {
                    HStack {
                        Text("Allow velcro in Login Items to finish.")
                            .foregroundStyle(.secondary)
                        Spacer()
                        Button("Open Login Items") { SMAppService.openSystemSettingsLoginItems() }
                    }
                }
                Picker("Check every", selection: $interval) {
                    Text("30 seconds").tag(30.0)
                    Text("1 minute").tag(60.0)
                    Text("2 minutes").tag(120.0)
                    Text("5 minutes").tag(300.0)
                    Text("10 minutes").tag(600.0)
                }
            } footer: {
                Text("velcro also checks right away when the network changes or your Mac wakes. Shares are reattached while velcro is running.")
                    .font(.caption).foregroundStyle(.secondary)
            }

            Section {
                LabeledContent("Command") {
                    Text(linked ? "~/.local/bin/velcro" : "Not installed")
                        .foregroundStyle(.secondary)
                }
                HStack {
                    Spacer()
                    Button(linked ? "Reinstall" : "Install") {
                        linking = true
                        Task {
                            await Setup.linkCommand()
                            linking = false
                        }
                    }
                    .disabled(linking)
                }
            } header: {
                Text("Command line")
            } footer: {
                Text("The velcro command uses the same shares as this app: velcro status, add, rm, pause, resume, logs.")
                    .font(.caption).foregroundStyle(.secondary)
            }

            Section {
                HStack {
                    Button("Open Log") { store.openLog() }
                    Button("Show Config File") {
                        if let conf = store.env["CONF"] {
                            NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: conf)])
                        }
                    }
                    Spacer()
                    Text("velcro \(store.env["VERSION"] ?? "")").foregroundStyle(.secondary)
                }
            }
        }
        .formStyle(.grouped)
        .onAppear { login = Setup.login }
    }
}
