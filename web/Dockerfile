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

FROM nginx:1.31.3-alpine AS runtime

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
