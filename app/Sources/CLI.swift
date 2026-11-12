import Foundation

/// The velcro script inside the app bundle. Checking, mounting, pausing, and removing shares all
/// go through it, so the menu bar app and the command line always behave the same way.
nonisolated enum CLI {
    static let path = Bundle.main.url(forResource: "velcro", withExtension: nil)!.path

    struct Result: Sendable {
        var status: Int32
        var output: String
        var message: String {
            output.split(whereSeparator: \.isNewline).last.map(String.init)
                ?? "CRM 작업 실패. 설정과 연결을 확인하세요."
        }
    }

    /// CRM commands report safe user-facing errors on stderr. Stream approval codes while
    /// the command waits; Python must flush even when its output goes to a pipe.
    static func execute(_ args: [String], onLine: @escaping @MainActor @Sendable (String) -> Void = { _ in }) async -> Result {
        await withCheckedContinuation { done in
            DispatchQueue.global(qos: .utility).async {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: "/bin/zsh")
                process.arguments = [path] + args
                process.environment = ProcessInfo.processInfo.environment.merging(
                    ["VELCRO_APP": "1", "PYTHONUNBUFFERED": "1"]) { _, new in new }
                let pipe = Pipe()
                process.standardOutput = pipe
                process.standardError = pipe
                process.standardInput = FileHandle.nullDevice
                do { try process.run() } catch {
                    done.resume(returning: Result(status: -1, output: ""))
                    return
                }
                var all = Data(), pending = Data()
                while true {
                    let data = pipe.fileHandleForReading.availableData
                    if data.isEmpty { break }
                    all.append(data)
                    pending.append(data)
                    while let end = pending.firstIndex(of: 10) {
                        let line = String(decoding: pending[..<end], as: UTF8.self)
                        pending.removeSubrange(...end)
                        DispatchQueue.main.async { onLine(line) }
                    }
                }
                process.waitUntilExit()
                done.resume(returning: Result(status: process.terminationStatus, output: String(decoding: all, as: UTF8.self)))
            }
        }
    }

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
