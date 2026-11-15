#!/usr/bin/env bash
set -euo pipefail

install -D -m 0644 sysmon.py "$STAGE$APP_INSTALL_DIR/sysmon.py"

tmp=$(mktemp)
cat >"$tmp" <<'LAUNCHER'
#!/bin/sh
exec python3 /usr/share/APPLaunch/apps/pocket-sysmon/sysmon.py "$@"
LAUNCHER
install -D -m 0755 "$tmp" "$STAGE$INSTALL_PREFIX/bin/pocket-sysmon"
rm -f "$tmp"
