import AppKit
import ServiceManagement

/// Login item, the `velcro` command, and taking over from the command line setup.
enum Setup {
    /// Installed builds only: a copy run from Xcode or a download folder shouldn't
    /// register itself at login or repoint the command.
    static var isInstalled: Bool {
        let path = Bundle.main.bundlePath
        return path.hasPrefix("/Applications/")
            || path.hasPrefix(NSHomeDirectory() + "/Applications/")
    }

    /// The first time an installed copy runs: start at login, point `velcro` into the app,
    /// and retire the LaunchAgent and SwiftBar plugin. The app's own engine is already
    /// running by then, so shares stay attached throughout.
    ///
    /// After an update, an installed copy whose command is already linked here runs `velcro install`
    /// once more, so what the new version sets up (the import-on-mount agent) reaches existing setups too.
    static func firstLaunch() async {
        let defaults = UserDefaults.standard
        guard isInstalled else { return }
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? ""
        if !defaults.bool(forKey: "setUp") {
            defaults.set(true, forKey: "setUp")
            defaults.set(version, forKey: "setUpVersion")
            try? SMAppService.mainApp.register()
            await linkCommand()
            return
        }
        guard defaults.string(forKey: "setUpVersion") != version else { return }
        await Store.shared.loadEnv()
        guard commandIsLinked(Store.shared.env["BIN"]) else {
            defaults.set(version, forKey: "setUpVersion")
            return
        }
        await linkCommand()
        // Only a loaded import agent counts as set up: if launchctl failed, try again at the next launch
        // rather than leave plug-in imports off until someone runs `velcro install` by hand.
        if await importAgentLoaded() { defaults.set(version, forKey: "setUpVersion") }
    }

    static func importAgentLoaded() async -> Bool {
        await withCheckedContinuation { done in
            DispatchQueue.global(qos: .utility).async {
                let p = Process()
                p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
                p.arguments = ["print", "gui/\(getuid())/io.github.dgitco.velcro.import"]
                p.standardOutput = FileHandle.nullDevice
                p.standardError = FileHandle.nullDevice
                do { try p.run() } catch { return done.resume(returning: false) }
                p.waitUntilExit()
                done.resume(returning: p.terminationStatus == 0)
            }
        }
    }

    /// `velcro install` from inside the bundle: links ~/.local/bin/velcro here and removes the
    /// LaunchAgent, since the app does its job while it runs.
    static func linkCommand() async {
        await CLI.run("install")
        await Store.shared.loadEnv()
    }

    static func commandIsLinked(_ bin: String?) -> Bool {
        guard let bin, let target = try? FileManager.default.destinationOfSymbolicLink(atPath: bin)
        else { return false }
        return URL(fileURLWithPath: target).standardizedFileURL.path
            == URL(fileURLWithPath: CLI.path).standardizedFileURL.path
    }

    enum Login { case on, off, needsApproval }

    static var login: Login {
        switch SMAppService.mainApp.status {
        case .enabled: .on
        case .requiresApproval: .needsApproval
        default: .off
        }
    }

    static func setLogin(_ on: Bool) {
        if on {
            try? SMAppService.mainApp.register()
        } else {
            try? SMAppService.mainApp.unregister()
        }
    }
}
