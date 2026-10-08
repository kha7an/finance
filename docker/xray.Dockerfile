FROM ghcr.io/xtls/xray-core:26.3.27 AS xray
FROM alpine:3.23

RUN apk add --no-cache ca-certificates
COPY --from=xray /usr/local/bin/xray /usr/local/bin/xray
USER 65532:65532
ENTRYPOINT ["/bin/sh", "-c", "umask 077; printf '%s' \"$XRAY_CONFIG_JSON\" > /tmp/xray.json && exec xray run -config /tmp/xray.json"]
