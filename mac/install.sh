#!/bin/zsh
# One-time setup on JB's Mac: passphrase (kept outside git), launchd job every 10 minutes, first publish.
# Remove with: zsh mac/install.sh --uninstall
set -eu
ROOT="${0:A:h:h}"; LABEL=com.londoncommuter.controlroom; PL="$HOME/Library/LaunchAgents/$LABEL.plist"
if [[ ${1:-} == --uninstall ]]; then launchctl bootout gui/$(id -u)/$LABEL 2>/dev/null || true; rm -f "$PL"; echo "removed $LABEL"; exit 0; fi
mkdir -p "$HOME/.config/lc-control-room"; chmod 700 "$HOME/.config/lc-control-room"
KEY="$HOME/.config/lc-control-room/pass"
[[ -s $KEY ]] || { /usr/bin/openssl rand -base64 24 | tr '+/' '-_' | tr -d '=\n' > "$KEY"; chmod 600 "$KEY"; echo "new passphrase written to $KEY"; }
cat > "$PL" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$LABEL</string>
<key>ProgramArguments</key><array><string>/bin/zsh</string><string>$ROOT/mac/push.sh</string></array>
<key>StartInterval</key><integer>600</integer>
<key>RunAtLoad</key><true/>
<key>ProcessType</key><string>Background</string>
<key>LowPriorityIO</key><true/>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
</dict></plist>
PLIST
plutil -lint "$PL" >/dev/null
launchctl bootout gui/$(id -u)/$LABEL 2>/dev/null || true
launchctl bootstrap gui/$(id -u) "$PL"
echo "installed $LABEL (every 10 min); log: <notes>/w1/control-room/push.log"
