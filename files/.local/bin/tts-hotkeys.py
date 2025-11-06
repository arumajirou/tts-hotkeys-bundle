#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, time, json, subprocess, glob, re, errno, sys, shutil, shlex
from evdev import InputDevice, ecodes
from select import select

# ===== 設定 =====
BGM_PATTERN      = os.environ.get("BGM_PATTERN", r"YouTube|YouTube Music|Spotify|SoundCloud|ニコニコ")
BGM_URL_PATTERN  = os.environ.get("BGM_URL_PATTERN", r"youtube\.com|music\.youtube\.com|open\.spotify\.com|soundcloud\.com|nicovideo\.jp|niconico\.jp|radiko\.jp|tunein\.com")
CONTROL_MODE     = os.environ.get("CONTROL_MODE", "mute").lower()  # 'pause' or 'mute'
STATE_FILE       = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "tts-muted.json")
DEBOUNCE_MS      = 250
LOG_PREFIX       = "[tts-hotkeys] "

ACTION_KEYS = {
    'mute_or_pause':   ecodes.KEY_A,  # Ctrl+Shift+A
    'unmute_or_play':  ecodes.KEY_S,  # Ctrl+Shift+S
    'toggle':          ecodes.KEY_M,  # Ctrl+Shift+M
    'stop':            ecodes.KEY_F,  # Ctrl+Shift+D  ← 追加：停止
}

# ===== kdotool の自動検出 =====
def find_kdotool():
    env_path = os.environ.get("KDOT_PATH")
    if env_path and os.path.exists(env_path) and os.access(env_path, os.X_OK):
        return env_path
    which = shutil.which("kdotool")
    if which:
        return which
    for c in [os.path.expanduser("~/.cargo/bin/kdotool"), "/usr/local/bin/kdotool", "/usr/bin/kdotool"]:
        if os.path.exists(c) and os.access(c, os.X_OK):
            return c
    return None

KDOT = find_kdotool()

# ===== 汎用ユーティリティ =====
def log(msg): print(LOG_PREFIX + msg, flush=True)

def run(cmd, check=False, text=True):
    try:
        out = subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL)
        return out.decode() if text else out
    except subprocess.CalledProcessError:
        if check: raise
        return None

def have(cmd):
    return subprocess.call(f"command -v {cmd}", shell=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0

def notify(title, body=""):
    if have("notify-send"):
        subprocess.Popen(["notify-send", title, body])

def run_kdotool(args):
    if not KDOT: return None
    return run(f"{shlex.quote(KDOT)} {args}")

# -----------------------------------------------------------------------------
#                      Pause/Resume/Stop（MPRIS / playerctl）
# -----------------------------------------------------------------------------
BGM_URL_RE = re.compile(BGM_URL_PATTERN, re.I)

def list_players():
    out = run("playerctl -l")
    if not out: return []
    return [x.strip() for x in out.splitlines() if x.strip()]

def player_status(name):
    out = run(f"playerctl -p {shlex.quote(name)} status")
    return out.strip() if out else None

def player_meta(name, fmt):
    return run(f"playerctl -p {shlex.quote(name)} metadata --format '{fmt}'") or ""

def player_url(name):
    return player_meta(name, "{{xesam:url}}").strip()

def player_pid(name):
    pid = player_meta(name, "{{mpris:pid}}").strip()
    if pid.isdigit(): return pid
    m = re.search(r'\.instance(\d+)$', name)  # chromium.instance8892 等
    if m: return m.group(1)
    return ""

def window_titles_for_pid(pid):
    if not KDOT: return []
    out = run_kdotool("search '.*'")
    if not out: return []
    titles = []
    for wid in out.strip().splitlines():
        p = run_kdotool(f"getwindowpid {wid}")
        if p and p.strip() == str(pid):
            t = run_kdotool(f"getwindowname {wid}") or ""
            t = t.strip()
            if t: titles.append(t)
    return titles

def get_active_window():
    wid = run_kdotool("getactivewindow")
    if not wid: return None
    wid = wid.strip()
    pid = (run_kdotool(f"getwindowpid {wid}") or "").strip()
    app = (run_kdotool(f"getwindowclassname {wid}") or "").strip()
    title = (run_kdotool(f"getwindowname {wid}") or "").strip()
    return (wid, pid, app, title)

def is_bgm_player(name, active_pid):
    """Playing かつ (URL or タイトル) が BGM と判定できれば True。active_pid は読み上げ側除外用。"""
    st = player_status(name)
    if st != "Playing": return False

    url = player_url(name)
    if url and BGM_URL_RE.search(url): return True

    pid = player_pid(name)
    if pid and str(active_pid or "") == pid:  # フォーカスは除外
        return False

    low = name.lower()
    if any(k in low for k in ["spotify", "youtube", "music", "vlc", "brave", "chrome", "chromium", "edge", "vivaldi", "firefox"]):
        titles = window_titles_for_pid(pid) if pid else []
        if titles and any(re.search(BGM_PATTERN, t, re.I) for t in titles):
            return True
    return False

def pause_candidates(active_pid):
    return [p for p in list_players() if is_bgm_player(p, active_pid)]

def do_pause():
    act = get_active_window()
    active_pid = act[1] if act else ""
    cands = pause_candidates(active_pid)
    if not cands:
        log("pause: no mpris candidates (url/title match failed)")
        notify("TTS: 一時停止対象なし", "")
        return
    for p in cands:
        run(f"playerctl -p {shlex.quote(p)} pause")
    st = load_state() or {}
    st["paused_players"] = sorted(set(st.get("paused_players", []) + cands))
    save_state_generic(st)
    notify("TTS: 再生を一時停止", "players: " + ", ".join(cands))
    log(f"pause: {cands}")

def do_resume():
    st = load_state() or {}
    names_paused = st.get("paused_players", [])
    names_stopped = st.get("stopped_players", [])
    played = []

    existing = set(list_players())

    if names_paused:
        for p in names_paused:
            if p in existing:
                run(f"playerctl -p {shlex.quote(p)} play"); played.append(p)
        st["paused_players"] = []

    if names_stopped:
        for p in names_stopped:
            if p in existing:
                run(f"playerctl -p {shlex.quote(p)} play"); played.append(p)
        st["stopped_players"] = []

    save_state_generic(st)
    notify("TTS: 再開", "players: " + (", ".join(played) if played else "なし"))
    log(f"resume: {played}")

def do_stop():
    """Ctrl+Shift+D：再生中のBGM候補を Stop（必要なら Pause にフォールバック）"""
    act = get_active_window()
    active_pid = act[1] if act else ""
    cands = pause_candidates(active_pid)  # Playing かつ BGM判定済み
    if not cands:
        notify("TTS: 停止対象なし", "")
        log("stop: no candidates"); return

    stopped = []
    for p in cands:
        # stop が不実装なプレーヤーもあるので、失敗したら pause にフォールバック
        ok = run(f"playerctl -p {shlex.quote(p)} stop")
        if ok is None:
            run(f"playerctl -p {shlex.quote(p)} pause")
        stopped.append(p)

    st = load_state() or {}
    st["stopped_players"] = sorted(set(st.get("stopped_players", []) + stopped))
    save_state_generic(st)

    notify("TTS: 再生を停止", "players: " + ", ".join(stopped))
    log(f"stop: {stopped}")

# -----------------------------------------------------------------------------
#                      Mute/Unmute（PipeWire / PulseAudio）
# -----------------------------------------------------------------------------
MEDIA_REGEX = re.compile(r"(Audio/Stream$)|(Stream/Output/Audio$)")

def pw_dump():
    if not have("pw-dump"): return None
    out = run("pw-dump")
    if not out: return None
    try:
        return json.loads(out)
    except Exception:
        return None

def pw_ids_for_pid_or_bin(pid, binname):
    if not have("wpctl"): return []
    dump = pw_dump()
    if not dump: return []
    ids = []
    for obj in dump:
        if not isinstance(obj, dict): continue
        if obj.get("type") != "PipeWire:Interface:Node": continue
        props = (obj.get("info") or {}).get("props") or {}
        media = props.get("media.class", "")
        if not MEDIA_REGEX.search(media or ""): continue
        apid = str(props.get("application.process.id", "") or "")
        abin = props.get("application.process.binary", "") or ""
        aname = props.get("application.name", "") or ""
        cond = (apid == str(pid)) or (abin.endswith(binname)) or (binname and re.search(re.escape(binname), aname, re.I))
        if cond: ids.append(str(obj.get("id")))
    return ids

def pactl_indexes_for_pid(pid):
    if not have("pactl"): return []
    out = run("pactl list sink-inputs")
    if not out: return []
    idxs, current = [], None
    for line in out.splitlines():
        t = line.strip()
        if t.startswith("Sink Input #"):
            current = ''.join(ch for ch in t if ch.isdigit())
        elif "application.process.id" in t and current:
            digits = ''.join(ch for ch in t if ch.isdigit())
            if digits == str(pid):
                idxs.append(current); current = None
    return idxs

def search_bgm_pids(pattern, exclude_pid=None):
    out = run_kdotool(f"search {shlex.quote(pattern)}")
    if not out: return set()
    pids = set()
    for wid in out.strip().splitlines():
        pid = run_kdotool(f"getwindowpid {wid}")
        if pid:
            pid = pid.strip()
            if pid and pid != (exclude_pid or ""):
                pids.add(pid)
    return pids

def bin_for_pid(pid):
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
        return os.path.basename(exe)
    except Exception:
        try:
            with open(f"/proc/{pid}/comm", "r") as f:
                return f.read().strip()
        except Exception:
            return ""

def set_mute(pipewire_ids, pulse_indexes, mute=True):
    if have("wpctl"):
        for nid in pipewire_ids:
            if nid: run(f"wpctl set-mute {nid} {1 if mute else 0}")
    if have("pactl"):
        for idx in pulse_indexes:
            if idx: run(f"pactl set-sink-input-mute {idx} {1 if mute else 0}")

def do_mute():
    act = get_active_window()
    active_pid = act[1] if act else ""
    bgm_pids = search_bgm_pids(BGM_PATTERN, exclude_pid=active_pid)
    if not bgm_pids:
        notify("TTS: ミュート対象なし", f'pattern="{BGM_PATTERN}"')
        log(f"mute: no bgm windows pattern={BGM_PATTERN}")
        return
    all_pw, all_pa = [], []
    for pid in bgm_pids:
        binname = bin_for_pid(pid)
        pw_ids = pw_ids_for_pid_or_bin(pid, binname)
        pa_ids = pactl_indexes_for_pid(pid)
        set_mute(pw_ids, pa_ids, True)
        all_pw.extend(pw_ids); all_pa.extend(pa_ids)
        log(f"mute: pid={pid} bin={binname} pw={pw_ids} pa={pa_ids}")
    st = load_state() or {}
    st["pipewire_ids"] = sorted(set(all_pw))
    st["pulse_indexes"] = sorted(set(all_pa))
    save_state_generic(st)
    notify("TTS: BGMをミュート", "")

def do_unmute():
    st = load_state()
    if not st:
        notify("TTS: 解除", "保存された対象がありません")
        log("unmute: nothing saved"); return
    pw_ids = st.get("pipewire_ids", [])
    pa_ids = st.get("pulse_indexes", [])
    set_mute(pw_ids, pa_ids, False)
    st["pipewire_ids"] = []; st["pulse_indexes"] = []
    save_state_generic(st)
    notify("TTS: 解除", "")
    log(f"unmute: pw={pw_ids} pa={pa_ids}")

# ===== 状態ファイル =====
def load_state():
    try:
        with open(STATE_FILE, "r") as f: return json.load(f)
    except Exception: return None

def save_state_generic(obj):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f: json.dump(obj, f)
    os.replace(tmp, STATE_FILE)

# ===== evdev 監視 =====
def iter_keyboard_devices():
    for path in glob.glob("/dev/input/event*"):
        try:
            dev = InputDevice(path)
            yield dev
        except Exception:
            continue

def main_loop():
    devices = list(iter_keyboard_devices())
    if not devices:
        log("no readable /dev/input/event* (権限: inputグループ/udevルール)")
        sys.exit(1)

    fds = {dev.fd: dev for dev in devices}
    mods = {"ctrl": False, "shift": False}
    last_ts = {k: 0.0 for k in ACTION_KEYS}

    CTRL_CODES = {ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL}
    SHIFT_CODES = {ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT}

    log(f"started. mode='{CONTROL_MODE}', kdotool='{KDOT or 'NOT FOUND'}', url_pattern='{BGM_URL_PATTERN}'")
    notify("TTSホットキー常駐中",
           ("Ctrl+Shift+A:一時停止 / S:再開 / D:停止 / M:トグル" if CONTROL_MODE=='pause'
            else "Ctrl+Shift+A:ミュート / S:解除 / M:トグル"))

    while True:
        r, _, _ = select(fds.keys(), [], [], 1.0)
        for fd in r:
            try:
                for e in fds[fd].read():
                    if e.type != ecodes.EV_KEY: continue
                    if e.code in CTRL_CODES:
                        mods['ctrl'] = (e.value != 0); continue
                    if e.code in SHIFT_CODES:
                        mods['shift'] = (e.value != 0); continue
                    if e.value != 1:  # 0:up 1:down 2:repeat
                        continue
                    if not (mods['ctrl'] and mods['shift']):
                        continue

                    now = time.time() * 1000
                    def hit(name):
                        if ACTION_KEYS.get(name) == e.code and (now - last_ts[name]) > DEBOUNCE_MS:
                            last_ts[name] = now
                            return True
                        return False

                    if hit('mute_or_pause'):
                        if CONTROL_MODE == 'pause': do_pause()
                        else: do_mute()
                    elif hit('unmute_or_play'):
                        if CONTROL_MODE == 'pause': do_resume()
                        else: do_unmute()
                    elif hit('stop') and CONTROL_MODE == 'pause':
                        o_stop()
                    elif hit('toggle'):
                        if CONTROL_MODE == 'pause':
                            st = load_state() or {}
                            if st.get("paused_players") or st.get("stopped_players"):
                                do_resume()
                            else:
                                do_pause()
                        else:
                            st = load_state() or {}
                            if st.get("pipewire_ids") or st.get("pulse_indexes"):
                                do_unmute()
                            else:
                                do_mute()
            except OSError as ex:
                if ex.errno != errno.EAGAIN:
                    log(f"read error: {ex}"); time.sleep(0.2)

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        pass
