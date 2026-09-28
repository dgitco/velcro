// velcro-sha <file>: prints the file's SHA-256, read with the cache off (F_NOCACHE). On a network
// share, macOS would otherwise answer a read of a file this Mac just wrote from its own memory,
// so "read it back and compare" would never reach the server. velcro's script uses this when it
// runs inside velcro.app, and falls back to openssl otherwise.
import CryptoKit
import Foundation

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write(Data("usage: velcro-sha <file>\n".utf8))
    exit(2)
}
let fd = open(CommandLine.arguments[1], O_RDONLY)
guard fd >= 0 else {
    perror("velcro-sha")
    exit(1)
}
_ = fcntl(fd, F_NOCACHE, 1)
var hasher = SHA256()
let size = 4 << 20
let buffer = UnsafeMutableRawPointer.allocate(byteCount: size, alignment: 16)
while true {
    let n = read(fd, buffer, size)
    if n < 0 {
        perror("velcro-sha")
        exit(1)
    }
    if n == 0 { break }
    hasher.update(bufferPointer: UnsafeRawBufferPointer(start: buffer, count: n))
}
close(fd)
print(hasher.finalize().map { String(format: "%02x", $0) }.joined())
