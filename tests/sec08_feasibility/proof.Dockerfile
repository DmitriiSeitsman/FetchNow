# Disposable image for the SEC-08 container cgroup proof.
# The tool is compiled here so it matches this image's libc.
# This is not a production service image.
FROM python:3.12-slim-bookworm

COPY sec08tool.c /tmp/sec08tool.c
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && mkdir -p /opt/sec08-trusted/bin \
    && gcc -O2 -Wall -Wextra -Werror -o /opt/sec08-trusted/bin/sec08tool /tmp/sec08tool.c \
    && rm /tmp/sec08tool.c \
    && apt-get purge -y gcc libc6-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
