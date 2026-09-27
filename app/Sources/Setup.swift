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
    static func firstLaunch() async {
        let defaults = UserDefaults.standard
        guard isInstalled, !defaults.bool(forKey: "setUp") else { return }
        defaults.set(true, forKey: "setUp")
        try? SMAppService.mainApp.register()
        await linkCommand()
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
