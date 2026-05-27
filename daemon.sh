#!/system/bin/sh
# Listen on PORT loopback, fork handler per connection.
PORT=8889
DIR=/data/local/tmp

# -L = listen forever, fork handler per connection. If bind keeps failing,
# give up after a few tries instead of busy-looping forever (would otherwise
# leave a zombie daemon stealing CPU/encoder resources).
TRIES=0
while [ $TRIES -lt 5 ]; do
  toybox nc -L -p "$PORT" -s 127.0.0.1 sh "$DIR/handler.sh" 2>>"$DIR/daemon.log"
  rc=$?
  TRIES=$((TRIES + 1))
  echo "[`date`] nc exit rc=$rc try=$TRIES" >> "$DIR/daemon.log"
  sleep 2
done
echo "[`date`] giving up after $TRIES tries" >> "$DIR/daemon.log"
