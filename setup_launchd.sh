#!/bin/bash
# Register a launchd agent that runs watch_jobs.py once a day on macOS.
# 在 macOS 上注册一个 launchd 定时任务，每天跑一次 watch_jobs.py。
#
#   ./setup_launchd.sh                     defaults: 09:07
#   ./setup_launchd.sh --at 07:23
#   ./setup_launchd.sh --python /path/to/python3
#   ./setup_launchd.sh --uninstall
#
# Why launchd rather than cron: if the machine is asleep or off at the scheduled
# time, launchd runs the job once after it wakes or boots. cron simply skips it.
# 为什么用 launchd 而不是 cron：到点时机器睡着或关机，launchd 会在唤醒/开机后补跑
# 一次，cron 则直接跳过。

set -euo pipefail

LABEL="local.predoc-watcher-email"
AT="09:07"
PYTHON=""
UNINSTALL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --at)        AT="$2"; shift 2 ;;
        --python)    PYTHON="$2"; shift 2 ;;
        --label)     LABEL="$2"; shift 2 ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help)   sed -n '2,14p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$PROJECT_DIR/watch_jobs.py"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

unload_if_present() {
    if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
        launchctl bootout "gui/$UID/$LABEL" 2>/dev/null \
            || launchctl unload -w "$PLIST" 2>/dev/null || true
    fi
}

if [ "$UNINSTALL" -eq 1 ]; then
    unload_if_present
    [ -f "$PLIST" ] && rm -f "$PLIST" && echo "Removed $PLIST"
    echo "Uninstalled. / 已卸载。"
    exit 0
fi

[ -f "$SCRIPT" ] || { echo "watch_jobs.py not found in $PROJECT_DIR" >&2; exit 1; }

# Resolve an interpreter. An absolute path matters here: launchd jobs get a
# minimal PATH, so a bare "python3" may not be found.
# 必须用绝对路径：launchd 任务的 PATH 很小，只写 python3 可能找不到。
if [ -z "$PYTHON" ]; then
    PYTHON="$(command -v python3 || true)"
fi
[ -n "$PYTHON" ] || { echo "No python3 found. Pass one with --python /full/path" >&2; exit 1; }
[ -x "$PYTHON" ] || { echo "Not executable: $PYTHON" >&2; exit 1; }

HOUR="${AT%%:*}"
MINUTE="${AT##*:}"
case "$HOUR$MINUTE" in
    *[!0-9]*) echo "--at must look like HH:MM, got '$AT'" >&2; exit 2 ;;
esac
HOUR=$((10#$HOUR)); MINUTE=$((10#$MINUTE))
[ "$HOUR" -le 23 ] && [ "$MINUTE" -le 59 ] || { echo "--at out of range: $AT" >&2; exit 2; }

echo "Agent to install / 即将安装的任务:"
echo "  Label      : $LABEL"
echo "  Python     : $PYTHON"
echo "  Script     : $SCRIPT"
echo "  Runs daily : $(printf '%02d:%02d' "$HOUR" "$MINUTE")"
echo "  Plist      : $PLIST"
echo

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/logs"
unload_if_present

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SCRIPT</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>$HOUR</integer>
        <key>Minute</key>
        <integer>$MINUTE</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>$PROJECT_DIR/logs/launchd.out</string>
    <key>StandardErrorPath</key>
    <string>$PROJECT_DIR/logs/launchd.err</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
PLISTEOF

plutil -lint "$PLIST" >/dev/null || { echo "Generated plist is invalid" >&2; exit 1; }

launchctl bootstrap "gui/$UID" "$PLIST" 2>/dev/null \
    || launchctl load -w "$PLIST"

echo "Installed. / 安装完成。"
echo
echo "Check it      / 查看状态: launchctl print gui/$UID/$LABEL"
echo "Run it now    / 立即运行: launchctl kickstart -k gui/$UID/$LABEL"
echo "Read the log  / 查看日志: tail -30 '$PROJECT_DIR/logs/watch.log'"
echo "Uninstall     / 卸载    : $0 --uninstall"
