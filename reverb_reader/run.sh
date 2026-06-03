#!/usr/bin/env bash
set -euo pipefail

OPTIONS=/data/options.json
UPSTREAM=""
RECOMMENDER=""
if [ -f "$OPTIONS" ]; then
    UPSTREAM=$(jq -r '.freshrss_upstream // ""' "$OPTIONS")
    RECOMMENDER=$(jq -r '.recommender_upstream // ""' "$OPTIONS")
fi
UPSTREAM="${UPSTREAM%/}" # strip any trailing slash
RECOMMENDER="${RECOMMENDER%/}" # strip any trailing slash

if [ -z "$UPSTREAM" ]; then
    echo "[reverb-reader] ERROR: set 'freshrss_upstream' in the add-on Configuration tab."
    echo "[reverb-reader]   Use the SAME host:port you point Reverb at, WITHOUT /api/greader.php"
    echo "[reverb-reader]   e.g.  http://192.168.1.50:7077"
    exit 1
fi

# When unset, point the recommender proxy at a dead address so requests fail fast
# and the reader simply shows no recommendations instead of breaking.
if [ -z "$RECOMMENDER" ]; then
    RECOMMENDER="http://127.0.0.1:1"
fi

# Only these vars are substituted; nginx's own \$uri/\$host vars are preserved.
export FRESHRSS_UPSTREAM="$UPSTREAM"
export RECOMMENDER_UPSTREAM="$RECOMMENDER"
envsubst '${FRESHRSS_UPSTREAM} ${RECOMMENDER_UPSTREAM}' \
    < /etc/nginx/reader.conf.template \
    > /etc/nginx/conf.d/default.conf

echo "[reverb-reader] web reader on ingress port 8099"
echo "[reverb-reader] proxying /api/  ->  ${FRESHRSS_UPSTREAM}/api/"
echo "[reverb-reader] proxying /recs/ ->  ${RECOMMENDER_UPSTREAM}/"
exec nginx -g 'daemon off;'
