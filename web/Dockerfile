# syntax=docker/dockerfile:1

FROM node:26.5.1-alpine AS build

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci

COPY . .
ARG PUBLIC_SITE_URL=http://localhost
ENV PUBLIC_SITE_URL=$PUBLIC_SITE_URL
ARG PUBLIC_MEDIA_FLOW_ENABLED=false
RUN test "$PUBLIC_MEDIA_FLOW_ENABLED" = "true" -o "$PUBLIC_MEDIA_FLOW_ENABLED" = "false"
ENV PUBLIC_MEDIA_FLOW_ENABLED=$PUBLIC_MEDIA_FLOW_ENABLED
ARG PUBLIC_SEARCH_INDEXING_ENABLED=false
RUN test "$PUBLIC_SEARCH_INDEXING_ENABLED" = "true" -o "$PUBLIC_SEARCH_INDEXING_ENABLED" = "false"
ENV PUBLIC_SEARCH_INDEXING_ENABLED=$PUBLIC_SEARCH_INDEXING_ENABLED
RUN npm run build

FROM nginx:1.31.6-alpine3.24 AS runtime

# SEC-03C: pin Alpine v3.24 main fixes (same branch; no edge/distro switch).
# libexpat: CVE-2026-93990 fixed in >=2.8.5-r0; v3.24 main now ships 2.9.0-r0
# (also includes CVE-2026-77214 / CVE-2026-102633). pcre2: CVE-2026-103111;
# tiff: CVE-2026-4775. Sources: pkgs.alpinelinux.org + security.alpinelinux.org
# + dl-cdn.alpinelinux.org APKINDEX (v3.24/main/x86_64).
# Run 40031834156: CVE-2026-111497 (brotli), CVE-2026-88806 (libx11).
# Exact fixes verified in v3.24/main APKINDEX for x86_64 and aarch64.
RUN apk add --no-cache \
        'libexpat=2.9.0-r0' \
        'pcre2=10.49-r0' \
        'tiff=4.7.2-r0' \
        'brotli-libs=1.2.1-r0' \
        'libx11=1.8.14-r0'

RUN addgroup -g 10001 -S fetchnow \
    && adduser -S -D -H -u 10001 -G fetchnow fetchnow \
    && mkdir -p /var/cache/nginx /var/run \
    && chown -R fetchnow:fetchnow /var/cache/nginx /var/run /usr/share/nginx/html

COPY --from=build /app/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/nginx.conf

# Fail-closed X-Robots-Tag: only exact "true" omits the header.
ARG PUBLIC_SEARCH_INDEXING_ENABLED=false
RUN test "$PUBLIC_SEARCH_INDEXING_ENABLED" = "true" -o "$PUBLIC_SEARCH_INDEXING_ENABLED" = "false" \
    && if [ "$PUBLIC_SEARCH_INDEXING_ENABLED" = "true" ]; then \
         printf '%s\n' '# Search indexing enabled for this image build.' > /etc/nginx/robots-headers.inc; \
       else \
         printf '%s\n' 'add_header X-Robots-Tag "noindex, nofollow" always;' > /etc/nginx/robots-headers.inc; \
       fi \
    && chown fetchnow:fetchnow /etc/nginx/robots-headers.inc

USER fetchnow

# Late ARG so revision changes cannot reuse a stale LABEL from an earlier layer.
ARG FETCHNOW_RELEASE_REVISION=local
LABEL org.opencontainers.image.revision="${FETCHNOW_RELEASE_REVISION}"

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1:8080/ >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]
