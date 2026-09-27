# Homebrew cask for the dgitco/tap tap (github.com/dgitco/homebrew-tap, Casks/velcro.rb).
# On each release: set version, and sha256 to the one scripts/build-app prints.
cask "velcro" do
  version "0.3.0"
  sha256 "REPLACE_WITH_ZIP_SHA256"

  url "https://github.com/dgitco/velcro/releases/download/v#{version}/velcro-#{version}.zip"
  name "velcro"
  desc "Keep network shares attached to your Mac"
  homepage "https://github.com/dgitco/velcro"

  depends_on macos: ">= :ventura"

  app "velcro.app"
  binary "#{appdir}/velcro.app/Contents/Resources/velcro"

  uninstall quit:       "io.github.dgitco.velcro.app",
            login_item: "velcro"

  zap trash: [
    "~/.config/velcro",
    "~/Library/Logs/velcro.log",
    "~/Library/Logs/velcro.log.1",
    "~/Library/Preferences/io.github.dgitco.velcro.app.plist",
  ]
end
