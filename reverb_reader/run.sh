#!/usr/bin/env bash
set -euo pipefail

OPTIONS=/data/options.json
UPSTREAM=""
if [ -f "$OPTIONS" ]; then
    UPSTREAM=$(jq -r '.freshrss_upstream // ""' "$OPTIONS")
fi
UPSTREAM="${UPSTREAM%/}" # strip any trailing slash

if [ -z "$UPSTREAM" ]; then
    echo "[reverb-reader] ERROR: set 'freshrss_upstream' in the add-on Configuration tab."
    echo "[reverb-reader]   Use the SAME host:port you point Reverb at, WITHOUT /api/greader.php"
    echo "[reverb-reader]   e.g.  http://192.168.1.50:7077"
    exit 1
fi

# Only FRESHRSS_UPSTREAM is substituted; nginx's own \$uri/\$host vars are preserved.
export FRESHRSS_UPSTREAM="$UPSTREAM"
envsubst '${FRESHRSS_UPSTREAM}' \
    < /etc/nginx/reader.conf.template \
    > /etc/nginx/conf.d/default.conf

echo "[reverb-reader] web reader on ingress port 8099"
echo "[reverb-reader] proxying /api/  ->  ${FRESHRSS_UPSTREAM}/api/"
exec nginx -g 'daemon off;'
