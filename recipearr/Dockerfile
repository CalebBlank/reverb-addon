# Ships the prebuilt aarch64 binary so the build on the Yellow is trivial (copy into a distroless
# image — no Go toolchain, no compile on the Pi). Rebuild the binary from the recipearr repo with:
#   CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -ldflags="-s -w" -o recipearr-arm64 .
FROM gcr.io/distroless/static-debian12
COPY recipearr-arm64 /recipearr
# /data is the add-on's Supervisor-persisted volume (survives restarts + updates).
ENV RECIPEARR_DATA_DIR=/data \
    RECIPEARR_ADDR=0.0.0.0:8585
EXPOSE 8585
ENTRYPOINT ["/recipearr"]
