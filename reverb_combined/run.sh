#!/usr/bin/env bash
set -euo pipefail

OPTIONS=/data/options.json
UPSTREAM=""
if [ -f "$OPTIONS" ]; then
    UPSTREAM=$(jq -r '.freshrss_upstream // ""' "$OPTIONS")
fi
UPSTREAM="${UPSTREAM%/}" # strip any trailing slash

if [ -z "$UPSTREAM" ]; then
    echo "[reverb] ERROR: set 'freshrss_upstream' in the add-on Configuration tab."
    echo "[reverb]   Use the SAME host:port you point Reverb at, WITHOUT /api/greader.php"
    echo "[reverb]   e.g.  http://192.168.1.50:7077"
    exit 1
fi

# The recommender runs locally in THIS container; nginx proxies /recs/ to it. There is no
# recommender_upstream option anymore — that whole config footgun is gone.
export FRESHRSS_UPSTREAM="$UPSTREAM"
export RECOMMENDER_UPSTREAM="http://127.0.0.1:8100"
envsubst '${FRESHRSS_UPSTREAM} ${RECOMMENDER_UPSTREAM}' \
    < /etc/nginx/reader.conf.template \
    > /etc/nginx/conf.d/default.conf

# Start the recommender (it reads /data/options.json itself) in the background, then run nginx
# as PID 1 in the foreground. If the recommender dies the reader stays up (recs degrade to empty).
echo "[reverb] starting recommender on :8100"
python3 /app.py &

echo "[reverb] web reader on ingress :8099"
echo "[reverb]   /api/  -> ${FRESHRSS_UPSTREAM}/api/   (FreshRSS)"
echo "[reverb]   /recs/ -> ${RECOMMENDER_UPSTREAM}/    (local recommender)"
exec nginx -g 'daemon off;'
