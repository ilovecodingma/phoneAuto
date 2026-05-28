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
    # gpu busy — probe Adreno (cumulative busy total) and Mali (instantaneous %)
    # Adreno: gpubusy file has "busy total" cumulative jiffies → client computes delta
    if [ -r /sys/class/kgsl/kgsl-3d0/gpubusy ]; then
      echo "GPU_BUSY adreno $(cat /sys/class/kgsl/kgsl-3d0/gpubusy)"
    else
      for p in /sys/class/misc/mali0/device/utilization \
               /sys/kernel/gpu/gpu_busy \
               /sys/devices/platform/mali/utilization; do
        if [ -r "$p" ]; then
          echo "GPU_BUSY mali $(cat "$p")"
          break
        fi
      done
      # devfreq style — load is often "<busy> <total>" or just %
      for p in /sys/class/devfreq/*.mali*/load \
               /sys/class/devfreq/gpufreq/load; do
        [ -r "$p" ] || continue
        echo "GPU_BUSY devfreq $(cat "$p")"
        break
      done
    fi
    # gpu freq
    for p in /sys/class/kgsl/kgsl-3d0/gpu_freq \
             /sys/class/kgsl/kgsl-3d0/clock_mhz \
             /sys/class/devfreq/*.mali*/cur_freq \
             /sys/class/devfreq/gpufreq/cur_freq \
             /sys/kernel/gpu/gpu_clock; do
      [ -r "$p" ] || continue
      echo "GPU_FREQ $(cat "$p")"
      break
    done
    # power — current_now (µA, may be negative for drain), voltage_now (µV)
    cn=$(cat /sys/class/power_supply/battery/current_now 2>/dev/null)
    vn=$(cat /sys/class/power_supply/battery/voltage_now 2>/dev/null)
    [ -n "$cn" ] && echo "PWR_CUR $cn"
    [ -n "$vn" ] && echo "PWR_VOLT $vn"
    pn=$(cat /sys/class/power_supply/battery/power_now 2>/dev/null)
    [ -n "$pn" ] && echo "PWR_NOW $pn"
    echo "EOF"
    ;;
  MEMDETAIL)
    # MEMDETAIL <pkg>  — full meminfo + smaps_rollup + status snapshot.
    pkg=$args
    [ -z "$pkg" ] && { echo "ERR missing pkg"; exit 0; }
    echo "===MEMINFO==="
    dumpsys meminfo "$pkg" 2>/dev/null
    pid=$(pidof "$pkg" 2>/dev/null | awk '{print $1}')
    if [ -n "$pid" ] && [ -d "/proc/$pid" ]; then
      echo "===PID==="
      echo "$pid"
      echo "===SMAPS_ROLLUP==="
      cat "/proc/$pid/smaps_rollup" 2>/dev/null
      echo "===STATUS==="
      cat "/proc/$pid/status" 2>/dev/null
      echo "===OOM==="
      cat "/proc/$pid/oom_score" 2>/dev/null
      cat "/proc/$pid/oom_score_adj" 2>/dev/null
    fi
    echo "===EOF==="
    ;;
  IO_PID)
    # IO_PID <pid>  — /proc/<pid>/io snapshot (client computes deltas).
    pid=$args
    if [ -z "$pid" ] || [ ! -d "/proc/$pid" ]; then
      echo "ERR bad pid: $pid"
    else
      echo "TIME $(date +%s%N)"
      cat "/proc/$pid/io" 2>/dev/null
      echo "===EOF==="
    fi
    ;;
  SCHED_PID)
    # SCHED_PID <pid>  — cpuset, sched, schedstat, cgroup info for one PID.
    pid=$args
    if [ -z "$pid" ] || [ ! -d "/proc/$pid" ]; then
      echo "ERR bad pid: $pid"
    else
      echo "===CPUSET==="
      cat "/proc/$pid/cpuset" 2>/dev/null
      echo "===CGROUP==="
      cat "/proc/$pid/cgroup" 2>/dev/null
      echo "===SCHEDSTAT==="
      cat "/proc/$pid/schedstat" 2>/dev/null
      echo "===SCHED==="
      head -30 "/proc/$pid/sched" 2>/dev/null
      echo "===GOVERNOR==="
      for i in 0 1 2 3 4 5 6 7; do
        g=$(cat /sys/devices/system/cpu/cpu$i/cpufreq/scaling_governor 2>/dev/null)
        mx=$(cat /sys/devices/system/cpu/cpu$i/cpufreq/scaling_max_freq 2>/dev/null)
        mn=$(cat /sys/devices/system/cpu/cpu$i/cpufreq/scaling_min_freq 2>/dev/null)
        [ -n "$g" ] && echo "CPU$i $g $mn $mx"
      done
      echo "===EOF==="
    fi
    ;;
  DISPLAY)
    # Refresh rates + active display info — used to set jank baseline.
    dumpsys display 2>/dev/null | awk '
      /mActiveModeId|mDefaultModeId|mActiveDisplayModeId/ {print "MODE", $0}
      /fps=/ {print "FPS", $0}
      /refresh-rate=/ {print "REFRESH", $0}
      /mAppliedDeviceConfig/ {print "APPLIED", $0}
    ' | head -30
    echo "===EOF==="
    ;;
  CRASH_RECENT)
    # Last 300 lines of crash buffer.
    logcat -b crash -v time -d 2>/dev/null | tail -300
    echo "===EOF==="
    ;;
  ANR_LS)
    # /data/anr is only readable on userdebug/root; otherwise empty.
    ls -al /data/anr 2>/dev/null
    echo "===DROPBOX==="
    dumpsys dropbox --print 2>/dev/null | head -200
    echo "===EOF==="
    ;;
  TOMBSTONE_LS)
    ls -al /data/tombstones 2>/dev/null
    echo "===EOF==="
    ;;
  DEVICE_INFO)
    # One-shot summary used at session start.
    echo "===PROPS==="
    for p in ro.product.manufacturer ro.product.model ro.product.brand \
             ro.product.cpu.abi ro.product.cpu.abilist \
             ro.build.version.release ro.build.version.sdk ro.build.id \
             ro.soc.manufacturer ro.soc.model ro.board.platform \
             ro.hardware ro.hardware.chipname; do
      v=$(getprop "$p" 2>/dev/null)
      [ -n "$v" ] && echo "$p=$v"
    done
    echo "===MEMTOTAL==="
    awk '/^MemTotal:/ {print $2}' /proc/meminfo
    echo "===CPU_INFO==="
    grep -E "^(processor|model name|Hardware|CPU implementer|CPU part)" /proc/cpuinfo 2>/dev/null | head -50
    echo "===CPU_COUNT==="
    nproc
    echo "===GPU==="
    cat /sys/class/kgsl/kgsl-3d0/gpu_model 2>/dev/null
    cat /sys/class/kgsl/kgsl-3d0/gpu_freq 2>/dev/null
    echo "===DISPLAY==="
    wm size 2>/dev/null
    wm density 2>/dev/null
    echo "===KERNEL==="
    uname -a
    echo "===EOF==="
    ;;
  APPOPS)
    pkg=$args
    [ -z "$pkg" ] && { echo "ERR missing pkg"; exit 0; }
    appops get "$pkg" 2>/dev/null
    echo "===EOF==="
    ;;
  GETENFORCE)
    getenforce
    echo "ID $(id)"
    echo "===EOF==="
    ;;
  BINDER_DUMP)
    dumpsys binder 2>/dev/null | head -300
    echo "===EOF==="
    ;;
  ACTIVITY_PROCS)
    dumpsys activity processes 2>/dev/null | head -200
    echo "===EOF==="
    ;;
  SF_LIST)
    dumpsys SurfaceFlinger --list 2>/dev/null
    echo "===EOF==="
    ;;
  SF_LATENCY)
    layer=$args
    [ -z "$layer" ] && { echo "ERR missing layer"; exit 0; }
    dumpsys SurfaceFlinger --latency "$layer" 2>/dev/null
    echo "===EOF==="
    ;;
  CLEANUP_TMP)
    # Remove old macro_*.sh / .log / .pid files (older than 1 day OR all if arg=all)
    if [ "$args" = "all" ]; then
      n_sh=$(ls /data/local/tmp/macro_*.sh 2>/dev/null | wc -l)
      n_log=$(ls /data/local/tmp/macro_*.log 2>/dev/null | wc -l)
      rm -f /data/local/tmp/macro_*.sh /data/local/tmp/macro_*.log /data/local/tmp/macro_*.pid
      echo "removed all: sh=$n_sh log=$n_log"
    else
      # toybox find supports -mtime
      n=$(find /data/local/tmp -maxdepth 1 -name 'macro_*' -mtime +1 2>/dev/null | wc -l)
      find /data/local/tmp -maxdepth 1 -name 'macro_*' -mtime +1 -delete 2>/dev/null
      echo "removed older-than-1day: $n"
    fi
    echo "===EOF==="
    ;;
  THREADS)
    # THREADS <pid> — dump per-thread stat + schedstat for client-side delta.
    pid=$args
    if [ -z "$pid" ] || [ ! -d "/proc/$pid" ]; then
      echo "ERR bad pid: $pid"
    else
      echo "TIME $(date +%s%N)"
      for stat in /proc/$pid/task/*/stat; do
        [ -r "$stat" ] || continue
        tid=${stat%/stat}; tid=${tid##*/}
        w=$(awk '{print $2}' "/proc/$pid/task/$tid/schedstat" 2>/dev/null)
        [ -z "$w" ] && w=0
        # raw stat line: pid (comm) state ... (52+ fields)
        s=$(cat "$stat" 2>/dev/null)
        [ -n "$s" ] && echo "TS $tid $w $s"
      done
      echo "EOF"
    fi
    ;;
  JANK)
    # JANK <pkg> — gfxinfo framestats (frame timing ns columns).
    pkg=$args
    if [ -z "$pkg" ]; then
      echo "ERR missing pkg"
    else
      dumpsys gfxinfo "$pkg" framestats 2>/dev/null
      echo "EOF"
    fi
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
