FROM nginx:alpine

# jq: read add-on options; gettext: envsubst for the nginx template; bash: run.sh
RUN apk add --no-cache jq gettext bash

# Self-contained web reader + proxy template + entrypoint
COPY www/ /usr/share/nginx/html/
COPY reader.conf.template /etc/nginx/reader.conf.template
COPY run.sh /run.sh
RUN chmod +x /run.sh

# Bypass the stock nginx entrypoint (it would envsubst before our option is read).
ENTRYPOINT ["/run.sh"]
