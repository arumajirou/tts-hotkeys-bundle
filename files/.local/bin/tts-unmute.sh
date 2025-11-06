#!/usr/bin/env bash
set -euo pipefail

STATE_FILE="${XDG_RUNTIME_DIR:-/tmp}/tts-muted.json"

notify() {
  if command -v notify-send >/dev/null 2>&1; then
    notify-send "$1" "$2"
  fi
}

if [[ ! -s "$STATE_FILE" ]]; then
  notify "TTS: 解除" "保存されたミュート対象がありません"
  exit 0
fi

PW_IDS=$(jq -r '.pipewire_ids[]?' "$STATE_FILE" 2>/dev/null || true)
PA_IDXS=$(jq -r '.pulse_indexes[]?' "$STATE_FILE" 2>/dev/null || true)

if command -v wpctl >/dev/null 2>&1; then
  while IFS= read -r id; do
    [[ -z "$id" ]] && continue
    wpctl set-mute "$id" 0 || true
  done <<< "$PW_IDS"
fi

if command -v pactl >/dev/null 2>&1; then
  while IFS= read -r idx; do
    [[ -z "$idx" ]] && continue
    pactl set-sink-input-mute "$idx" 0 || true
  done <<< "$PA_IDXS"
fi

: > "$STATE_FILE"
notify "TTS: 解除" "保存したBGMミュートを解除しました"
