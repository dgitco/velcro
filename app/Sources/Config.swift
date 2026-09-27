import Foundation

/// One line of ~/.config/velcro/mounts: an address and the hosts to try after it.
struct Entry: Identifiable, Hashable {
    var id = UUID()
    var address: String
    var fallbacks: [String]

    /// The same checks as the script's `parse`: smb://[user@]host/share or nfs://host/path.
    static func validate(_ address: String) -> String? {
        let trimmed = address.trimmingCharacters(in: .whitespaces)
        guard let scheme = trimmed.range(of: "://") else {
            return "Start with smb:// or nfs://"
        }
        guard ["smb", "nfs"].contains(trimmed[..<scheme.lowerBound].lowercased()) else {
            return "Only smb:// and nfs:// addresses are supported"
        }
        var rest = trimmed[scheme.upperBound...]
        if let at = rest.firstIndex(of: "@") { rest = rest[rest.index(after: at)...] }
        guard let slash = rest.firstIndex(of: "/"), slash != rest.startIndex,
              rest.index(after: slash) != rest.endIndex
        else {
            return "Add the server and the shared folder, like smb://me@nas.local/home"
        }
        return nil
    }

    /// What Finder calls it: the share, or the last folder of an NFS path.
    var name: String {
        let path = address.split(separator: "/", maxSplits: 3).last.map(String.init) ?? address
        let name = address.lowercased().hasPrefix("nfs://")
            ? String(path.split(separator: "/").last ?? Substring(path)) : path
        return name.removingPercentEncoding ?? name
    }

    /// Spaces are written as %20, as `velcro add` does, so each line stays whitespace-separated.
    static func normalize(_ address: String) -> String {
        address.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: " ", with: "%20")
    }
}

/// Reads and writes the config file the script uses, keeping comments and blank lines in place.
struct Config {
    private enum Line {
        case entry(Entry)
        case other(String)
    }

    let url: URL
    private var lines: [Line] = []

    init(path: String) {
        url = URL(fileURLWithPath: path)
        let text = (try? String(contentsOf: url, encoding: .utf8)) ?? ""
        lines = text.split(separator: "\n", omittingEmptySubsequences: false).map { raw in
            let line = String(raw)
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty || trimmed.hasPrefix("#") { return .other(line) }
            let words = trimmed.split(whereSeparator: \.isWhitespace).map(String.init)
            return .entry(Entry(address: words[0], fallbacks: Array(words.dropFirst())))
        }
        if case .other("") = lines.last { lines.removeLast() }
    }

    var entries: [Entry] {
        get {
            lines.compactMap { if case .entry(let e) = $0 { e } else { nil } }
        }
        set {
            // Keep each surviving entry where it was; new ones go at the end.
            var remaining = newValue
            var result: [Line] = []
            for line in lines {
                switch line {
                case .other:
                    result.append(line)
                case .entry(let old):
                    if let i = remaining.firstIndex(where: { $0.id == old.id }) {
                        result.append(.entry(remaining.remove(at: i)))
                    }
                }
            }
            result += remaining.map { .entry($0) }
            lines = result
        }
    }

    func save() throws {
        let text = lines.map { line -> String in
            switch line {
            case .other(let s): s
            case .entry(let e): ([e.address] + e.fallbacks).joined(separator: " ")
            }
        }.joined(separator: "\n")
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try (text.isEmpty ? "" : text + "\n").write(to: url, atomically: true, encoding: .utf8)
    }
}
