import SwiftUI

// MARK: - Import

struct ImportPane: View {
    @ObservedObject private var store = Store.shared
    @ObservedObject private var importer = Importer.shared

    var body: some View {
        Form {
            Section {
                ForEach(importer.recorders, id: \.self) { recorder in
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(recorder.label)
                            Text(recorder.detail).font(.caption).foregroundStyle(.secondary)
                                .lineLimit(1).truncationMode(.middle)
                        }
                        Spacer()
                        Button("Remove") { importer.forget(recorder) }
                    }
                }
                if importer.recorders.isEmpty {
                    Text("No recorder yet. Plug it in and register it here or from the menu.")
                        .foregroundStyle(.secondary)
                }
                if !importer.candidates.isEmpty {
                    HStack {
                        Spacer()
                        Menu("Register Plugged-in Drive") {
                            ForEach(importer.candidates) { volume in
                                Button(volume.title) { importer.register(volume) }
                            }
                        }
                        .fixedSize()
                    }
                }
            } header: {
                Text("Recorders")
            } footer: {
                Text("velcro knows a recorder by its volume's UUID, so another drive with the same name is left alone.")
                    .font(.caption).foregroundStyle(.secondary)
            }

            Section {
                FolderField(title: "Folder", key: "import.dest", prompt: "/Volumes/home/recordings")
                SettingField(title: "File types", key: "import.ext", prompt: "wav mp3 m4a")
                Toggle("Import as soon as a recorder is plugged in", isOn: flag("import.auto"))
                Toggle("Delete from the recorder once every copy verifies", isOn: flag("import.delete"))
            } header: {
                Text("Import")
            } footer: {
                Text("Each file is copied, read back from the folder, and compared by size and SHA-256. If any file doesn't match, nothing is deleted. When the folder's share isn't attached, recordings wait on this Mac and move over once it is. Every import is added to .velcro-import.jsonl in the folder.")
                    .font(.caption).foregroundStyle(.secondary)
            }

            Section {
                HStack {
                    if let progress = importer.progress {
                        Text(progress.summary).foregroundStyle(.secondary)
                    } else if importer.waiting > 0 {
                        Text("\(importer.waiting) waiting on this Mac").foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Import Now") { importer.run(auto: false) }
                        .disabled(importer.running || importer.recorders.isEmpty)
                }
            }
        }
        .formStyle(.grouped)
    }

    private func flag(_ key: String) -> Binding<Bool> {
        Binding(get: { store.setting(key) == "yes" },
                set: { on in Task { await store.set(key, [on ? "yes" : "no"]) } })
    }
}

// MARK: - Send

struct SendPane: View {
    @ObservedObject private var store = Store.shared
    @State private var from = ""
    @State private var to = ""

    private var maps: [String] { store.settings["map"] ?? [] }

    var body: some View {
        Form {
            Section {
                FolderField(title: "Inbox", key: "send.inbox", prompt: "/Volumes/home/inbox")
                Picker("Delete after", selection: Binding(
                    get: { store.setting("send.keep") },
                    set: { days in Task { await store.set("send.keep", [days]) } })) {
                    Text("1 day").tag("1")
                    Text("7 days").tag("7")
                    Text("14 days").tag("14")
                    Text("30 days").tag("30")
                    Text("Never").tag("0")
                }
            } footer: {
                Text("Pick a recent image or file under Send to NAS in the menu: velcro saves it in the inbox and copies the path your server sees, ready to paste into a terminal over SSH. The clipboard only changes when you pick something.")
                    .font(.caption).foregroundStyle(.secondary)
            }

            Section {
                ForEach(maps, id: \.self) { map in
                    HStack {
                        Text(map.replacingOccurrences(of: " -> ", with: "  →  "))
                            .lineLimit(1).truncationMode(.middle)
                        Spacer()
                        Button("Remove") { Task { await store.set("map", maps.filter { $0 != map }) } }
                    }
                }
                HStack {
                    TextField("On this Mac", text: $from, prompt: Text(verbatim: "/Volumes/home"))
                        .labelsHidden()
                    Text("→")
                    TextField("On the server", text: $to, prompt: Text(verbatim: "/mnt/nas/me"))
                        .labelsHidden()
                    Button("Add") {
                        let rule = "\(trimmed(from)) -> \(trimmed(to))"
                        Task { await store.set("map", maps + [rule]) }
                        from = ""
                        to = ""
                    }
                    .disabled(!trimmed(from).hasPrefix("/") || !trimmed(to).hasPrefix("/"))
                }
            } header: {
                Text("Paths on the server")
            } footer: {
                let inbox = store.setting("send.inbox")
                Text(inbox.isEmpty
                     ? "How the server sees a folder on this Mac. The longest matching rule wins."
                     : "The inbox on the server: \(remotePath(inbox))")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
    }

    private func trimmed(_ s: String) -> String {
        var s = s.trimmingCharacters(in: .whitespaces)
        while s.count > 1, s.hasSuffix("/") { s.removeLast() }
        return s
    }

    /// The same longest-prefix rule as the script's remote_path.
    private func remotePath(_ path: String) -> String {
        var best: (from: String, to: String)?
        for map in maps {
            let parts = map.components(separatedBy: " -> ")
            guard parts.count == 2 else { continue }
            let from = trimmed(parts[0]), to = trimmed(parts[1])
            guard path == from || path.hasPrefix(from + "/") else { continue }
            if from.count > (best?.from.count ?? -1) { best = (from, to) }
        }
        guard let best else { return path }
        return best.to + path.dropFirst(best.from.count)
    }
}

// MARK: - Fields

/// A text field for one setting, saved with Return or when focus moves on.
struct SettingField: View {
    var title: String
    var key: String
    var prompt: String
    @ObservedObject private var store = Store.shared
    @State private var text = ""
    @FocusState private var focused: Bool

    var body: some View {
        TextField(title, text: $text, prompt: Text(verbatim: prompt))
            .focused($focused)
            .onSubmit(save)
            .onChange(of: focused) { if !$0 { save() } }
            .onAppear { text = store.setting(key) }
            .onReceive(store.$settings) { _ in if !focused { text = store.setting(key) } }
    }

    private func save() {
        let value = text.trimmingCharacters(in: .whitespaces)
        guard value != store.setting(key) else { return }
        Task { await store.set(key, value.isEmpty ? [] : [value]) }
    }
}

/// A folder setting: typed, or picked in an Open panel.
struct FolderField: View {
    var title: String
    var key: String
    var prompt: String
    @ObservedObject private var store = Store.shared

    var body: some View {
        HStack {
            SettingField(title: title, key: key, prompt: prompt)
            Button("Choose…", action: choose)
        }
    }

    private func choose() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        let current = store.setting(key)
        if !current.isEmpty { panel.directoryURL = URL(fileURLWithPath: current) }
        guard panel.runModal() == .OK, let url = panel.url else { return }
        Task { await store.set(key, [url.path]) }
    }
}
