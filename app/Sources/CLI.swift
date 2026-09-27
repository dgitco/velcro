import Foundation

/// The velcro script inside the app bundle. Checking, mounting, pausing, and removing shares all
/// go through it, so the menu bar app and the command line always behave the same way.
nonisolated enum CLI {
    static let path = Bundle.main.url(forResource: "velcro", withExtension: nil)!.path

    /// Runs `velcro <args>` and returns what it printed. Never throws: a failure is empty output,
    /// and the script itself logs anything worth knowing.
    @discardableResult
    static func run(_ args: String...) async -> String {
        await run(args)
    }

    @discardableResult
    static func run(_ args: [String]) async -> String {
        await withCheckedContinuation { done in
            DispatchQueue.global(qos: .utility).async {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: "/bin/zsh")
                process.arguments = [path] + args
                // Messages for the user come back as "notify" lines instead of the script's own notifications.
                process.environment = ProcessInfo.processInfo.environment.merging(["VELCRO_APP": "1"]) { _, new in new }
                let out = Pipe()
                process.standardOutput = out
                process.standardError = FileHandle.nullDevice
                process.standardInput = FileHandle.nullDevice
                do { try process.run() } catch {
                    done.resume(returning: "")
                    return
                }
                let data = out.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                done.resume(returning: String(decoding: data, as: UTF8.self))
            }
        }
    }
}
