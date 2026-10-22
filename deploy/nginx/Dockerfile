# syntax=docker/dockerfile:1

FROM nginx:1.31.6-alpine3.24

# SEC-03C: pin Alpine v3.24 main fixes (same branch; no edge/distro switch).
# libexpat: CVE-2026-93990 fixed in >=2.8.5-r0; v3.24 main now ships 2.9.0-r0
# (also includes CVE-2026-77214 / CVE-2026-102633). pcre2: CVE-2026-103111;
# tiff: CVE-2026-4775. Sources: pkgs.alpinelinux.org + security.alpinelinux.org
# + dl-cdn.alpinelinux.org APKINDEX (v3.24/main/x86_64).
RUN apk add --no-cache \
        'libexpat=2.9.0-r0' \
        'pcre2=10.49-r0' \
        'tiff=4.7.2-r0'

RUN addgroup -g 10001 -S fetchnow \
    && adduser -S -D -H -u 10001 -G fetchnow fetchnow \
    && mkdir -p /var/cache/nginx /var/run /tmp \
    && chown -R fetchnow:fetchnow /var/cache/nginx /var/run /tmp \
    && touch /tmp/nginx.pid \
    && chown fetchnow:fetchnow /tmp/nginx.pid

COPY nginx.conf /etc/nginx/nginx.conf

USER fetchnow

# Late ARG so revision changes cannot reuse a stale LABEL from an earlier layer.
ARG FETCHNOW_RELEASE_REVISION=local
LABEL org.opencontainers.image.revision="${FETCHNOW_RELEASE_REVISION}"

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1:8080/api/v1/health/live >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]
