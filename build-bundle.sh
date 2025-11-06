#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
PAYLOAD="$ROOT/tts-hotkeys.payload.b64"
BUNDLE="$ROOT/tts-hotkeys.bundle.sh"

# ★ ここがポイント：files/ をルートにして中身を全て詰める
tar -C "$ROOT/files" -czf - . | base64 > "$PAYLOAD"

# バンドルのスタブ
cat > "$BUNDLE" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
PAYLOAD_LINE=$(awk '/^__PAYLOAD_BELOW__/ {print NR+1; exit 0}' "$0")
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

tail -n +"$PAYLOAD_LINE" "$0" | base64 -d > "$tmp/payload.tgz"
mkdir -p "$HOME"
tar xzf "$tmp/payload.tgz" -C "$HOME"

chmod 755 "$HOME/.local/bin/tts-hotkeys.py" \
          "$HOME/.local/bin/tts-mute.sh" \
          "$HOME/.local/bin/tts-unmute.sh"

echo "Installed to \$HOME. Next:"
echo "  systemctl --user daemon-reload"
echo "  systemctl --user enable --now tts-hotkeys.service"
exit 0
__PAYLOAD_BELOW__
STUB

# ペイロード結合＆実行権限
cat "$PAYLOAD" >> "$BUNDLE"
chmod +x "$BUNDLE"

echo "Bundle rebuilt: $BUNDLE"
