FROM nginx:alpine

# jq: read add-on options; gettext: envsubst for the nginx template; bash: run.sh;
# python3: the recommender (pure stdlib — no pip install needed).
RUN apk add --no-cache jq gettext bash python3

# Web reader (static), recommender, nginx proxy template, entrypoint.
COPY www/ /usr/share/nginx/html/
COPY app.py /app.py
COPY reader.conf.template /etc/nginx/reader.conf.template
COPY run.sh /run.sh
RUN chmod +x /run.sh

# Recommender API for the Android app.
EXPOSE 8100

# Bypass the stock nginx entrypoint (it would envsubst before our option is read).
ENTRYPOINT ["/run.sh"]
