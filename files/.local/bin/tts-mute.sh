#!/usr/bin/env bash
set -euo pipefail

# ===== 設定 =====
STATE_FILE="${XDG_RUNTIME_DIR:-/tmp}/tts-muted.json"

# タイトルに含まれていれば「BGM候補」とみなすキーワード（正規表現｜'|'区切り）
BGM_PATTERN=${BGM_PATTERN:-'YouTube|YouTube Music|Spotify|SoundCloud|ニコニコ'}

# ミュート時にデスクトップ通知（libnotify-bin があれば）
notify() {
  if command -v notify-send >/dev/null 2>&1; then
    notify-send "$1" "$2"
  fi
}

# ===== 補助関数 =====
pid_to_bin() {
  local p="$1"
  if [[ -n "$p" && -e "/proc/$p/exe" ]]; then
    basename "$(readlink -f "/proc/$p/exe" 2>/dev/null)" 2>/dev/null || true
  elif [[ -r "/proc/$p/comm" ]]; then
    tr -d '\n' < "/proc/$p/comm" 2>/dev/null || true
  fi
}

pw_ids_for_pid_or_bin() {
  # PipeWire Node を PID/バイナリ名で拾う
  local pid="$1" bin="$2"
  pw-dump | jq -r --arg pid "$pid" --arg bin "$bin" '
    .[]
    | select(.type=="PipeWire:Interface:Node")
    | select((.info.props["media.class"] // "") | test("(Audio/Stream$)|(Stream/Output/Audio$)"))
    | select(
        (.info.props["application.process.id"] // "") == $pid
        or ((.info.props["application.process.binary"] // "") | endswith($bin))
        or ((.info.props["application.name"] // "") | test($bin;"i"))
      )
    | .id
  ' 2>/dev/null | sed '/^$/d' || true
}

to_json_array() {
  jq -R -s 'split("\n") | map(select(length>0))'
}

# ===== ここから本処理 =====
# 1) 前面ウィンドウ情報（アンミュート時の参考）
AWID="$(kdotool getactivewindow 2>/dev/null || true)"
ACTIVE_PID="$( [[ -n "$AWID" ]] && kdotool getwindowpid "$AWID" 2>/dev/null || true )"
ACTIVE_APP="$( [[ -n "$AWID" ]] && kdotool getwindowclassname "$AWID" 2>/dev/null || true )"
ACTIVE_TITLE="$( [[ -n "$AWID" ]] && kdotool getwindowname "$AWID" 2>/dev/null || true )"

# 2) BGM候補ウィンドウ → PID 抽出（重複排除／前面PIDは除外）
BGM_PIDS="$(
  kdotool search "$BGM_PATTERN" 2>/dev/null | while read -r wid; do
    p="$(kdotool getwindowpid "$wid" 2>/dev/null || true)"
    [[ -n "$p" ]] && echo "$p"
  done | sort -u | awk -v ap="$ACTIVE_PID" 'NF && $1!=ap {print}'
)"

if [[ -z "$BGM_PIDS" ]]; then
  notify "TTS: ミュート対象なし" "BGM候補（${BGM_PATTERN}）が見つかりませんでした"
  # 何も保存しないで終了
  exit 0
fi

ALL_PW_IDS=""
ALL_PA_INDEXES=""

# 3) 各 PID のストリームをミュート（PipeWire→Pulse の順）
for PID in $BGM_PIDS; do
  BIN="$(pid_to_bin "$PID" || true)"

  # --- PipeWire ---
  if command -v pw-dump >/dev/null 2>&1 && command -v wpctl >/dev/null 2>&1; then
    PW_IDS="$(pw_ids_for_pid_or_bin "$PID" "${BIN:-}")"
    if [[ -n "$PW_IDS" ]]; then
      while IFS= read -r id; do
        [[ -z "$id" ]] && continue
        wpctl set-mute "$id" 1 || true
        ALL_PW_IDS+=" $id"
      done <<< "$PW_IDS"
    fi
  fi

  # --- PulseAudio 互換 ---
  if command -v pactl >/dev/null 2>&1; then
    PA_IDXS=$(
      pactl list sink-inputs 2>/dev/null \
      | awk -v pid="$PID" '
          /^\s*Sink Input #/ {gsub(/[^0-9]/,"",$3); idx=$3}
          /application\.process\.id/ {gsub(/[^0-9]/,"",$0); if($0==pid){print idx}}
        ' | sed '/^$/d'
    )
    if [[ -n "$PA_IDXS" ]]; then
      while IFS= read -r idx; do
        [[ -z "$idx" ]] && continue
        pactl set-sink-input-mute "$idx" 1 || true
        ALL_PA_INDEXES+=" $idx"
      done <<< "$PA_IDXS"
    fi
  fi
done

# 4) 解除用の記録を保存
PW_JSON="$(printf '%s\n' $ALL_PW_IDS | sed '/^$/d' | to_json_array)"
PA_JSON="$(printf '%s\n' $ALL_PA_INDEXES | sed '/^$/d' | to_json_array)"
PIDS_JSON="$(printf '%s\n' $BGM_PIDS | sed '/^$/d' | to_json_array)"

jq -n \
  --arg ap "${ACTIVE_PID:-}" \
  --arg app "${ACTIVE_APP:-}" \
  --arg title "${ACTIVE_TITLE:-}" \
  --arg pattern "$BGM_PATTERN" \
  --argjson bgm_pids "${PIDS_JSON:-[]}" \
  --argjson pipewire_ids "${PW_JSON:-[]}" \
  --argjson pulse_indexes "${PA_JSON:-[]}" \
  '{ts: now, active:{pid:$ap, app:$app, title:$title}, pattern:$pattern,
    bgm_pids:$bgm_pids, pipewire_ids:$pipewire_ids, pulse_indexes:$pulse_indexes}' \
  > "$STATE_FILE"

notify "TTS: BGMをミュート" "対象PID=$(echo "$BGM_PIDS" | tr '\n' ' ')"
