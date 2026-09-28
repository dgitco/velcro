<p align="center"><img src="assets/logo/velcro-icon.svg" width="128" alt="velcro"></p>

<h1 align="center">velcro</h1>

Keep network shares stuck to your Mac.

macOS drops SMB mounts whenever Wi‑Fi blips, the lid closes, or you change networks — and then you're back in **Finder → Go → Connect to Server**. velcro watches your shares and quietly remounts them, like iCloud Drive but for your NAS.

- Remounts right after the network changes or your Mac wakes, and on a timer
- No error dialogs — failures go to a log file
- Never touches a share just to check it, so a dead network can't freeze your Mac
- Fallback hosts: try the LAN address first, then Tailscale (or any other route)
- Pause when you want an ejected share to stay ejected
- A menu bar app and a `velcro` command that share the same list
- Uses the password macOS already saved in your Keychain

It also does two things that don't need a NAS at all:

- **[Voice recorder import](#import-from-a-voice-recorder):** plug in a USB recorder and its recordings are copied to any folder you choose, checked by SHA-256, then cleared from the recorder
- **[Send to an inbox](#send-to-an-inbox):** save a recent screenshot or file to a folder (a share, Dropbox, Syncthing) and copy its path, even the path a server sees

## Install

**Let your agent do it.** Give Claude Code, Codex, or Cursor this repository (or the setup prompt from **[velcro.dgit.co](https://velcro.dgit.co/#install)**) and ask it to set up velcro. [AGENTS.md](AGENTS.md) walks it through installing, finding the shares you already have mounted, and asking where recordings and sent files should go, before it adds anything.

**Or run it yourself:**

```sh
curl -fsSL https://velcro.dgit.co/install | sh                    # menu bar app + velcro command
curl -fsSL https://velcro.dgit.co/install | sh -s -- --cli-only   # just the command and its LaunchAgents
```

The [installer](site/public/install) checks each download against the SHA-256 in [velcro.dgit.co/latest](https://velcro.dgit.co/latest) before installing anything. Run it again to update. On first launch the app:

- adds itself to Login Items, so shares come back after a restart
- links the `velcro` command to `~/.local/bin/velcro`
- replaces the LaunchAgent and SwiftBar plugin from velcro 0.2, if you had them

**Or download the zip** from [velcro.dgit.co](https://velcro.dgit.co), unzip, and drag **velcro.app** into Applications (see the FAQ for the first launch), or clone this repository and run `./velcro install`.

## Menu bar

| Icon | Meaning |
|---|---|
| <img src="assets/logo/menubar/connected.svg" width="18"> | every share attached |
| <img src="assets/logo/menubar/detached.svg" width="18"> | something dropped — velcro is reattaching |
| <img src="assets/logo/menubar/paused.svg" width="18"> | paused |

The menu groups shared folders by server, each with open in Finder, reconnect, and remove, plus pause/resume, reconnect now, add share, settings, and the log. Settings edits the list (with fallback hosts), turns login launch on or off, and sets how often velcro checks.

## Command line

```sh
velcro add smb://me@nas.local/home
velcro add "smb://me@nas.local/Media Library"          # spaces are fine
velcro add smb://me@192.168.1.10/photos nas.tail1234.ts.net   # LAN first, then Tailscale
velcro add nfs://nas.local/volume1/backup

velcro status
# agent:  velcro.app (on network change, wake, and a timer)
# state:  active
#
# nas.local
#   ● mounted                  home                   /Volumes/home
#   ○ offline                  backup

velcro pause      # eject in Finder and it stays ejected
velcro resume
velcro logs
velcro rm photos
velcro uninstall
```

The first mount may show the usual macOS login prompt — tick **Remember this password in my keychain** and you won't see it again.

## Import from a voice recorder

Plug in a USB voice recorder and velcro copies its recordings to a folder you choose, checks every copy, then clears the recorder and ejects it. The folder can be on your Mac (`~/Recordings`), an external drive, iCloud Drive, or a NAS; you don't need a NAS or any shares for this.

```sh
velcro recorder add                              # with the recorder plugged in
velcro set import.dest ~/Recordings             # or /Volumes/home/recordings on a share
velcro set import.folder RECORD                  # only the recorder's recordings folder
velcro import                                    # or just plug it in next time
```

- The recorder is recognized by its volume UUID (`diskutil info`), so another drive with the same name is left alone.
- Each file is copied under a hidden name, read back from the NAS, and compared by size and SHA-256. Only when every file matches are they deleted from the recorder. A file that's already there with the same contents is skipped; a different file with the same name is saved as `name-2.WAV`.
- If the folder is on a drive or share that isn't attached, recordings are verified into `~/Library/Application Support/velcro/import-queue` first, and move to the NAS (verified again) when it comes back.
- Every import is appended to `.velcro-import.jsonl` in the folder, one JSON object per file: `file`, `size`, `sha256`, `imported_at`, `device`, `device_uuid`, `original`, `duration_s`. A server can watch it for new recordings and check the hash before using one.
- In the menu: progress next to the icon, **Import Now**, and **Register Recorder** for a drive that's plugged in. Settings has the folder, file types, and switches for importing on plug-in and deleting after verifying.

## Send to an inbox

velcro keeps the last few images and files you copied. Pick one under **Send to Inbox** in the menu and it's saved in your inbox folder, and its path is put on the clipboard. Use a folder on a share, or one Dropbox or Syncthing keeps in sync; with a `map` rule, the path is the one another machine (say, a server you use over SSH) sees. velcro never changes the clipboard on its own.

```sh
velcro set send.inbox /Volumes/home/inbox
velcro set map "/Volumes/home -> /mnt/nas/me"    # how the server sees that share
velcro send screenshot.png                       # → /mnt/nas/me/inbox/screenshot.png, copied
velcro send                                      # whatever's on the clipboard
```

Inbox items are deleted after 14 days (`velcro set send.keep 30`, or `0` to keep them).

## Settings

`velcro settings` shows them all; `velcro set <key> [value ...]` changes one, and `velcro set <key>` puts the default back. They're kept in `~/.config/velcro/settings`:

| Key | Default | |
|---|---|---|
| `import.dest` | | folder for recordings |
| `import.ext` | `wav mp3 m4a aac flac wma ogg` | file types to take |
| `import.folder` | | only this folder on the recorder, like `RECORD`; set it if the recorder also holds music |
| `import.delete` | `yes` | delete from the recorder once every copy verifies |
| `import.auto` | `yes` | import as soon as a recorder is plugged in |
| `recorder` | | `uuid:<VolumeUUID> <label>` or `name:<volume name>`, one per recorder |
| `send.inbox` | | where sent files go |
| `send.keep` | `14` | days before inbox items are deleted |
| `map` | | `<path on this Mac> -> <path on the server>`, one per share |

## How it works

The list lives in `~/.config/velcro/mounts`, one share per line. For each share that isn't in `mount` output, velcro checks whether a host answers on the share's port (445 for SMB, 2049 for NFS), then asks macOS to `mount volume` with errors swallowed into `~/Library/Logs/velcro.log`.

velcro.app runs that pass itself when the network changes, when the Mac wakes, and on a timer (1 minute by default), and only while it's running. The app calls the same `velcro` script it ships inside its bundle, so the menu and the command line always agree. Without the app, `velcro install` sets up a LaunchAgent that runs the pass every 60 seconds and whenever `/Library/Preferences/SystemConfiguration` changes, and a second one that runs `velcro import --auto` whenever a volume mounts. Imports take their own lock, so a long copy never holds up reconnecting.

## Notes

- **Use SMB on a Mac if you can.** NFS works, but macOS's NFS client is effectively NFSv3 with `sys` security, which trusts any machine with an allowed IP address to say which user it is. It also needs extra ports (rpcbind, mountd, lockd) that often don't make it across Tailscale or other VPNs, where SMB needs only 445.
- **Fallback hosts and untrusted networks:** velcro connects to the first host that answers. A private LAN address like `192.168.1.10` may belong to a stranger's device on café Wi‑Fi. If you roam, prefer a name only your network resolves (Tailscale MagicDNS, `.local` Bonjour names) or list the private IP last.
- A share that drops while a file is open can still make Finder hang for a moment — that's the macOS client, not velcro.

## Guides

- [How to automatically reconnect network shares on a Mac](https://velcro.dgit.co/guides/reconnect-network-shares-mac): Open at Login, `mount volume` scripts, autofs, and velcro compared
- [Why your Mac loses its NAS shares after sleep](https://velcro.dgit.co/guides/mac-nas-disconnects-after-sleep)
- [Keep NAS shares mounted at home and away (LAN and Tailscale)](https://velcro.dgit.co/guides/mac-nas-over-tailscale)
- [Copy recordings from a USB voice recorder to your NAS automatically](https://velcro.dgit.co/guides/usb-voice-recorder-to-nas)
- [Send a screenshot to a remote server for Claude Code or Codex over SSH](https://velcro.dgit.co/guides/send-screenshots-to-remote-server)

## FAQ

**Why does macOS say it can't check velcro?** velcro isn't notarized: that takes a paid Apple Developer membership, and velcro is free. macOS only checks apps a browser downloaded, so the `curl` installer skips the dialog and checks the SHA-256 instead. With the zip, open velcro once, click Done, then click **Open Anyway** in System Settings › Privacy & Security, or run `xattr -dr com.apple.quarantine /Applications/velcro.app`.

**What will macOS ask me to allow?** Notifications (import and send results) and Login Items (start at login). The first import or send may ask to open files on a removable or network volume. No Full Disk Access, Accessibility, or Screen Recording.

**Does velcro see my passwords?** No. Share passwords stay in your keychain and macOS uses them to mount. velcro has no tokens or keys of its own.

**What does it do with my clipboard?** It lists the last four images or files you copied. Copied images are kept in `~/Library/Application Support/velcro/clips` until they drop off the list or velcro quits; items password managers mark as private are skipped. It only writes to the clipboard when you pick something to send.

**How do I uninstall it?** `velcro uninstall`, then move velcro.app to the Trash. Settings live in `~/.config/velcro`.

## Build

```sh
brew install xcodegen librsvg
scripts/build-icons       # only after changing assets/logo
scripts/build-app         # → build/velcro.app and build/velcro-<version>.zip
```

`SIGN_IDENTITY` and `NOTARY_PROFILE` sign and notarize a release; see the top of `scripts/build-app`. Both name things in your keychain; no key or password goes in the repository. `scripts/deploy-site` publishes `site/`, the installer, the zip, and `latest` (version and checksums) to velcro.dgit.co; `--stage` only prepares `site/public` so you can try the installer locally with `VELCRO_BASE=http://localhost:8000`. The version comes from `VERSION=` in the `velcro` script.

## Design

Logo and menu bar states live in `assets/logo/` (open `mockup.html`). Every draft that led there is kept in `assets/concepts/`.

## License

MIT
