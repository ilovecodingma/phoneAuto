"""Test STREAM command via daemon socket (no adb exec-out)."""
import socket
import time
import av

HOST = "127.0.0.1"
PORT = 8889

s = socket.socket()
s.settimeout(20)
s.connect((HOST, PORT))
s.sendall(b"STREAM 540x1200 6M 8\n")

codec = av.CodecContext.create("h264", "r")
t0 = time.time()
total_bytes = 0
total_frames = 0
last_print = t0
while True:
    try:
        chunk = s.recv(65536)
    except socket.timeout:
        break
    if not chunk:
        break
    total_bytes += len(chunk)
    try:
        packets = codec.parse(chunk)
    except av.error.AVError:
        continue
    for pkt in packets:
        try:
            for f in codec.decode(pkt):
                total_frames += 1
        except av.error.AVError:
            pass
    now = time.time()
    if now - last_print >= 1.0:
        elapsed = now - t0
        print(f"t={elapsed:5.2f}s  bytes={total_bytes/1024:.1f}KB  frames={total_frames}  fps~{total_frames/elapsed:.1f}  rate={total_bytes/elapsed/1024:.1f}KB/s", flush=True)
        last_print = now
s.close()

elapsed = time.time() - t0
print(f"\n=== final ===")
print(f"elapsed: {elapsed:.2f}s")
print(f"bytes: {total_bytes} ({total_bytes/elapsed/1024:.1f} KB/s)")
print(f"frames: {total_frames} ({total_frames/elapsed:.2f} fps)")
