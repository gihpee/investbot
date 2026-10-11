# SOCKS5-туннель через SSH к небольшому зарубежному серверу: для Telegram и LLM, если они недоступны с VPS в РФ.
FROM alpine:3.20
RUN apk add --no-cache openssh-client autossh && adduser -D -u 10002 tunnel
USER tunnel
# -D 0.0.0.0:1080 — SOCKS5 для контейнеров сети compose; ключ и known_hosts монтируются как секреты
ENTRYPOINT ["sh", "-c", "exec autossh -M 0 -N -D 0.0.0.0:1080 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/run/secrets/tunnel_known_hosts -i /run/secrets/tunnel_key -p ${TUNNEL_PORT:-22} ${TUNNEL_USER}@${TUNNEL_HOST}"]
