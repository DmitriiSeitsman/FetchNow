# syntax=docker/dockerfile:1

FROM nginx:1.29.0-alpine

RUN addgroup -g 10001 -S fetchnow \
    && adduser -S -D -H -u 10001 -G fetchnow fetchnow \
    && mkdir -p /var/cache/nginx /var/run /tmp \
    && chown -R fetchnow:fetchnow /var/cache/nginx /var/run /tmp \
    && touch /tmp/nginx.pid \
    && chown fetchnow:fetchnow /tmp/nginx.pid

COPY nginx.conf /etc/nginx/nginx.conf

USER fetchnow

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1:8080/api/v1/health/live >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]
