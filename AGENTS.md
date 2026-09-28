# velcro for agents

velcro is a macOS (13+) menu bar app and `velcro` command. It keeps SMB and NFS shares mounted, imports recordings from a USB voice recorder into any folder, and sends files to an inbox folder. Everything is configured with `velcro` commands or the app's Settings; users never need to edit code.

## If the user wants to use velcro

Do these in order. Ask before adding or changing anything, and change things only with `velcro` commands.

1. **Install.** `curl -fsSL https://velcro.dgit.co/install | sh` installs the menu bar app and the command. For the command only (no app): `curl -fsSL https://velcro.dgit.co/install | sh -s -- --cli-only`. The command lands in `~/.local/bin/velcro`; use that path if it isn't on `PATH` yet. From a clone of this repository, `./velcro install` does the command-only setup.
2. **Shares (only if they have a NAS or file server).** List what's mounted now with `mount -t smbfs` and `mount -t nfs`, show the user, and add the ones they pick: `velcro add smb://user@host/share` (spaces as `%20`). If they also reach the server another way, such as a Tailscale name, append it as a fallback host: `velcro add smb://me@192.168.1.10/home nas.tail1234.ts.net`. If macOS asks for a password, tell them to tick "Remember this password in my keychain".
3. **Voice recorder (optional, no NAS needed).** With the recorder plugged in: `velcro recorder add`. Ask where recordings should go (a folder on the Mac such as `~/Recordings`, iCloud Drive, an external drive, or a share) and run `velcro set import.dest <folder>`. Look inside the recorder; if recordings live in one folder (often `RECORD`), run `velcro set import.folder <that folder>` so nothing else on it (music, settings files) is touched. Recordings are deleted from the recorder only after every copy verifies; `velcro set import.delete no` keeps them. `velcro import` runs it now.
4. **Send to an inbox (optional).** `velcro set send.inbox <folder>`: a folder on a share, or one Dropbox or Syncthing keeps in sync. If another machine (for example a server they use over SSH) sees that folder at a different path, `velcro set map "<path on this Mac> -> <path there>"`. Then `velcro send <file>` (or the app's Send to Inbox menu) saves the file there and copies its path.
5. **Finish** with `velcro status` and `velcro settings`, and tell the user what's set up.

`velcro help` lists every command and setting. Logs: `velcro logs`. Undo: `velcro rm <share>`, `velcro recorder rm <name>`, `velcro set <key>` (back to default), `velcro uninstall`.

## If you're working on this code

- `velcro` (zsh) does all the work; `app/` (SwiftUI, xcodegen) runs it for the menu bar. The version is `VERSION=` in `velcro`.
- Build: `scripts/build-app`. Site: `scripts/deploy-site --stage` prepares `site/public`; without `--stage` it also deploys.
- Test with a throwaway `HOME` (`env -u XDG_CONFIG_HOME HOME=/tmp/x zsh ./velcro …`) and a disk image as a fake recorder (`hdiutil create -fs MS-DOS …`). `velcro install` and `uninstall` call the real `launchctl` and remove the real SwiftBar plugin even with a fake `HOME`, so put stub `launchctl`, `defaults`, `open`, and `osascript` first on `PATH` when testing them.
- With `pipe_fail` on, don't `return` from inside `cmd | while …`; loop over an array instead.
- Keep personal hosts, user names, and paths out of the repository; examples use `nas.local`, `/Volumes/home`, `/mnt/nas/me`.
