"""Isolated test: adb exec-out screenrecord → PyAV decode → count frames."""
import os
import subprocess
import sys
import time

import av

ADB = os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe")

args = [ADB, "exec-out", "screenrecord", "--output-format=h264",
        "--size", "540x1200", "--bit-rate", "4M",
        "--time-limit", "8", "-"]
print("spawning:", " ".join(args), flush=True)

p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     creationflags=subprocess.CREATE_NO_WINDOW)

codec = av.CodecContext.create("h264", "r")
total_bytes = 0
total_frames = 0
chunks = 0
t0 = time.time()

try:
    while True:
        chunk = p.stdout.read(8192)
        if not chunk:
            break
        total_bytes += len(chunk)
        chunks += 1
        if chunks <= 3:
            print(f"chunk #{chunks}: {len(chunk)}B, first10=", chunk[:10].hex(), flush=True)
        try:
            packets = codec.parse(chunk)
        except av.error.AVError as e:
            print("parse err:", e, flush=True)
            continue
        for pkt in packets:
            try:
                frames = codec.decode(pkt)
                for f in frames:
                    total_frames += 1
                    if total_frames <= 3:
                        print(f"frame #{total_frames}: {f.width}x{f.height} pts={f.pts}", flush=True)
            except av.error.AVError as e:
                print("decode err:", e, flush=True)
finally:
    err = p.stderr.read().decode("utf-8", "replace")
    p.wait()

elapsed = time.time() - t0
print(f"\n=== summary ===")
print(f"elapsed: {elapsed:.2f}s")
print(f"bytes: {total_bytes} ({total_bytes/elapsed/1024:.1f} KB/s)")
print(f"frames: {total_frames}  ({total_frames/elapsed:.2f} fps)")
print(f"exit code: {p.returncode}")
if err:
    print("stderr:", err)
