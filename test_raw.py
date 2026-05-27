"""Pure TCP recv throughput from daemon STREAM (no decode)."""
import socket, time, sys
s = socket.socket()
s.settimeout(20)
s.connect(("127.0.0.1", 8889))
s.sendall(b"STREAM 540x1200 6M 8\n")
total = 0
t0 = time.time()
last = t0
while True:
    try:
        chunk = s.recv(65536)
    except socket.timeout:
        break
    if not chunk: break
    total += len(chunk)
    now = time.time()
    if now - last >= 0.5:
        elapsed = now - t0
        print(f"t={elapsed:5.2f}s  total={total/1024:.1f}KB  rate={total/elapsed/1024:.1f}KB/s", flush=True)
        last = now
s.close()
print(f"FINAL: {total/1024:.1f}KB in {time.time()-t0:.2f}s = {total/(time.time()-t0)/1024:.1f}KB/s")
