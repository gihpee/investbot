# SOCKS5-прокси для Telegram через VPN-подписку (VLESS): sing-box + скрипт, который собирает его конфиг (vpn.py).
FROM ghcr.io/sagernet/sing-box:v1.14.2
RUN apk add --no-cache python3
COPY vpn.py /usr/local/bin/vpn.py
ENTRYPOINT ["python3", "-u", "/usr/local/bin/vpn.py"]
