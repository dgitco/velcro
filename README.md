<p align="center"><img src="assets/logo/velcro-icon.svg" width="128" alt="velcro"></p>

<h1 align="center">velcro</h1>

Keep network shares stuck to your Mac.

macOS drops SMB mounts whenever Wi‑Fi blips, the lid closes, or you change networks — and then you're back in **Finder → Go → Connect to Server**. velcro watches your shares and quietly remounts them, like iCloud Drive but for your NAS.

- Remounts every 60 seconds and on every network change
- No error dialogs — failures go to a log file
- Never touches a share just to check it, so a dead network can't freeze your terminal
- Fallback hosts: try the LAN address first, then Tailscale (or any other route)
- `pause` when you want an ejected share to stay ejected
- Optional menu bar icon via SwiftBar
- One zsh script, no dependencies. Uses the password macOS already saved in your Keychain

## Install

```sh
git clone https://github.com/dgitco/velcro && cd velcro
./velcro install          # copies itself to ~/.local/bin and starts a LaunchAgent
```

## Use

```sh
velcro add smb://me@nas.local/home
velcro add "smb://me@nas.local/Media Library"          # spaces are fine
velcro add smb://me@192.168.1.10/photos nas.tail1234.ts.net   # LAN first, then Tailscale

velcro status
# agent:  running (every 60s + on network change)
# state:  active
#
# ● mounted                  smb://me@nas.local/home
# ○ offline                  smb://me@192.168.1.10/photos nas.tail1234.ts.net

velcro pause      # eject in Finder and it stays ejected
velcro resume
velcro logs
velcro rm photos
velcro uninstall
```

The first mount may show the usual macOS login prompt — tick **Remember this password in my keychain** and you won't see it again.

## Menu bar

With [SwiftBar](https://github.com/swiftbar/SwiftBar) installed (`brew install --cask swiftbar`), `velcro install` also adds a menu bar icon:

| Icon | Meaning |
|---|---|
| <img src="assets/logo/menubar/connected.svg" width="18"> | every share attached |
| <img src="assets/logo/menubar/detached.svg" width="18"> | something dropped — velcro is reattaching |
| <img src="assets/logo/menubar/paused.svg" width="18"> | paused |

The menu groups shared folders by server, each with open in Finder, reconnect, and remove, plus pause/resume, reconnect now, add share, and the log. Icons are rendered from `assets/logo` by `scripts/build-icons`.

## How it works

A LaunchAgent runs `velcro run` every 60 seconds and whenever `/Library/Preferences/SystemConfiguration` changes (network up/down, Wi‑Fi switch, wake). For each share in `~/.config/velcro/mounts` that isn't in `mount` output, it checks whether a host answers on port 445, then asks macOS to `mount volume` with errors swallowed into `~/Library/Logs/velcro.log`.

## Notes

- **Fallback hosts and untrusted networks:** velcro connects to the first host that answers on port 445. A private LAN address like `192.168.1.10` may belong to a stranger's device on café Wi‑Fi. If you roam, prefer a name only your network resolves (Tailscale MagicDNS, `.local` Bonjour names) or list the private IP last.
- A share that drops while a file is open can still make Finder hang for a moment — that's the macOS SMB client, not velcro.

## Design

Logo and menu bar states live in `assets/logo/` (open `mockup.html`). Every draft that led there is kept in `assets/concepts/`.

## License

MIT
