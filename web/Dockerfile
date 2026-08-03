# syntax=docker/dockerfile:1

FROM node:26.5.1-alpine AS build

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci

COPY . .
ARG PUBLIC_SITE_URL=http://localhost
ENV PUBLIC_SITE_URL=$PUBLIC_SITE_URL
RUN npm run build

FROM nginx:1.29.0-alpine AS runtime

RUN addgroup -g 10001 -S fetchnow \
    && adduser -S -D -H -u 10001 -G fetchnow fetchnow \
    && mkdir -p /var/cache/nginx /var/run \
    && chown -R fetchnow:fetchnow /var/cache/nginx /var/run /usr/share/nginx/html

COPY --from=build /app/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/nginx.conf

USER fetchnow

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1:8080/ >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]
