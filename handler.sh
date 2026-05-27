#!/system/bin/sh
# Handler — invoked per TCP connection. stdin/stdout is the socket.
# Reads one line "CMD args..." and writes a response.

IFS= read -r line
cmd=${line%% *}
args=${line#"$cmd"}
args=${args# }

case "$cmd" in
  PING)
    echo PONG
    ;;
  TAP)
    # TAP x y
    input tap $args
    echo OK
    ;;
  SWIPE)
    # SWIPE x1 y1 x2 y2 dur_ms
    input swipe $args
    echo OK
    ;;
  KEY)
    # KEY KEYCODE  (HOME, BACK, APP_SWITCH, POWER, VOLUME_UP, VOLUME_DOWN, MENU, ENTER)
    input keyevent $args
    echo OK
    ;;
  TEXT)
    # ASCII text via `input text` (fast). Spaces → %s.
    t=$(printf '%s' "$args" | sed "s/'/\\\\'/g; s/ /%s/g")
    input text "$t"
    echo OK
    ;;
  IME_TEXT)
    # Unicode text via ADBKeyboard broadcast (works for Korean/emoji/etc).
    # Requires com.android.adbkeyboard set as default IME.
    am broadcast -a ADB_INPUT_TEXT --es msg "$args" >/dev/null 2>&1
    echo OK
    ;;
  APP)
    # APP com.package
    monkey -p "$args" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
    echo OK
    ;;
  STOP)
    # STOP com.package
    am force-stop "$args"
    echo OK
    ;;
  SHOT)
    # Raw PNG bytes to stdout
    screencap -p
    ;;
  SIZE)
    wm size
    ;;
  FOCUS)
    dumpsys window 2>/dev/null | grep -E "mCurrentFocus|mFocusedApp" | head -2
    ;;
  PKGS)
    pm list packages -3 | sed 's/^package://'
    ;;
  STATS)
    # Structured key/value system metrics. Client parses for live monitor.
    echo "TIME $(date +%s%N)"
    awk '/^cpu / {print "CPU",$2,$3,$4,$5,$6,$7,$8; exit}' /proc/stat
    awk '/^MemTotal:/ {t=$2} /^MemAvailable:/ {a=$2} END {print "MEM",t,a}' /proc/meminfo
    # thermal zones — read those readable
    for z in 20 21 22 23 24 25 26 27; do
      t=$(cat /sys/class/thermal/thermal_zone${z}/temp 2>/dev/null)
      n=$(cat /sys/class/thermal/thermal_zone${z}/type 2>/dev/null)
      [ -n "$t" ] && echo "TZ $z $n $t"
    done
    # cpufreq per core
    for i in 0 1 2 3 4 5 6 7; do
      f=$(cat /sys/devices/system/cpu/cpu$i/cpufreq/scaling_cur_freq 2>/dev/null)
      [ -n "$f" ] && echo "CPUFREQ $i $f"
    done
    # battery — only the static state block (early lines), match anchored fields
    dumpsys battery 2>/dev/null | head -25 | awk '
      /^  level:/         {print "BAT_LEVEL "$2}
      /^  temperature:/   {print "BAT_TEMP "$2}
      /^  voltage:/       {print "BAT_VOLT "$2}
      /^  USB powered:/   {print "BAT_USB "$3}
      /^  AC powered:/    {print "BAT_AC "$3}
      /^  status:/        {print "BAT_STATUS "$2}'
    # gpu (Adreno) — total mem if readable
    dumpsys gpu 2>/dev/null | awk '/^Global total:/ {print "GPU_MEM "$3; exit}'
    echo "GPU_MODEL $(cat /sys/class/kgsl/kgsl-3d0/gpu_model 2>/dev/null)"
    echo "EOF"
    ;;
  PROCS)
    # top by CPU%, 25 procs
    top -b -n 1 -m 25 -s 9 2>/dev/null | sed -n '4,40p'
    ;;
  APPS)
    # Foreground app + user-installed app processes sorted by CPU.
    # Format:
    #   FG <package>
    #   APP <pid> <cpu%> <mem%> <res> <pkg/name>
    fg=$(dumpsys window 2>/dev/null | awk '/mCurrentFocus/ {
      if (match($0, /[a-z][a-zA-Z0-9_.]+\/[a-zA-Z0-9_.$]+/)) {
        s = substr($0, RSTART, RLENGTH); split(s, a, "/"); print a[1]; exit
      }}')
    # If no focused app (notification shade / system UI), fall back to top resumed activity
    if [ -z "$fg" ]; then
      fg=$(dumpsys activity activities 2>/dev/null | awk '/topResumedActivity|mResumedActivity/ {
        if (match($0, /[a-z][a-zA-Z0-9_.]+\/[a-zA-Z0-9_.$]+/)) {
          s = substr($0, RSTART, RLENGTH); split(s, a, "/"); print a[1]; exit
        }}' | head -1)
    fi
    echo "FG $fg"
    # top: pick rows where USER starts with u0_a (user apps).
    # Columns: PID USER PR NI VIRT RES SHR S %CPU %MEM TIME+ ARGS...
    top -b -n 1 -m 80 -s 9 2>/dev/null | awk '
      NR > 4 && $2 ~ /^u0_a/ {
        pid=$1; res=$6; cpu=$9; mem=$10
        cmd=""; for (i=12; i<=NF; i++) cmd = cmd (i>12?" ":"") $i
        printf "APP %s %s %s %s %s\n", pid, cpu, mem, res, cmd
      }'
    ;;
  STREAM)
    # Pipe H.264 stream directly into the socket via screenrecord's stdout.
    # Optional args from client: "STREAM WxH bitrate seconds"  (defaults below)
    set -- $args
    SIZE=${1:-540x1200}
    BR=${2:-6M}
    SEC=${3:-175}
    exec screenrecord --output-format=h264 --size "$SIZE" --bit-rate "$BR" --time-limit "$SEC" -
    ;;
  SH)
    # Raw shell — runs the rest of the line
    sh -c "$args" 2>&1
    ;;
  *)
    echo "ERR unknown: $cmd"
    ;;
esac
