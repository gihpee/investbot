# Развёртывание investbot на VPS

> Порядок: выбрать локацию → арендовать сервер с почасовой оплатой → за 10 минут проверить сеть (`doctor`) →
> только потом настраивать всё остальное. Бот не открывает входящих портов (long polling), снаружи нужен только SSH.

## 0. Где размещать сервер

Боту нужны две группы внешних сервисов, и они «живут» в разных частях интернета:

| Сервис | Из РФ | Из-за рубежа (проверено 11.10.2026 с зарубежного IP) |
|---|---|---|
| API Т-Инвестиций | ✅ | ✅ отвечает (нужен сертификат Минцифры) — но зависит от сети хостера |
| MOEX ISS (история, облигации, кривая ОФЗ, индексы) | ✅ | ❌ таймаут |
| Часть новостных лент (MOEX, Smart-lab, Финмаркет) | ✅ | ❌ таймаут |
| Индекс SDK Т-Банка (opensource.tbank.ru) | ✅ | ❌ таймаут |
| Telegram Bot API | ❔ возможны ограничения — проверяется `doctor` | ✅ |
| Зарубежный LLM-провайдер | ❔ часть провайдеров блокирует российские IP | ✅ |

Без MOEX ISS не работают идеи по облигациям, конструктор аллокации (история индексов), оценка исходов на бумаге и
календарь для облигаций у других брокеров. Поэтому **рекомендуемая схема — VPS в РФ**, а Telegram и LLM, если они
недоступны с него напрямую, идут через прокси: VPN-подписку, готовый SOCKS5 или свой зарубежный сервер (раздел 9).
Боту для этого нужны две переменные — `INVESTBOT_TELEGRAM_PROXY` и `INVESTBOT_LLM_PROXY`. Обратная схема (сервер за рубежом,
прокси в РФ) хуже: проксировать пришлось бы gRPC Т-Инвестиций и все запросы к MOEX, а база с портфелем жила бы
за рубежом.

Если хочется обойтись одним сервером — попробуйте VPS в стране, откуда доступны обе стороны (например, Казахстан):
`doctor` за несколько минут покажет, работает ли там MOEX ISS. Заранее это не гарантировано.

## 1. Сервер

- Ubuntu 24.04 LTS, 2 vCPU, 4 ГБ RAM, 40 ГБ SSD — с запасом для бота, Postgres и сборки образа.
- В РФ: любой облачный провайдер с почасовой оплатой (Timeweb Cloud, Selectel, Yandex Cloud, VK Cloud и т. п.).
- Для туннеля (если понадобится): самый маленький сервер за рубежом, 1 vCPU / 0,5–1 ГБ.

## 2. Первичная настройка (на сервере, от root)

```bash
adduser investbot && usermod -aG sudo investbot
```

```bash
rsync --archive --chown=investbot:investbot ~/.ssh /home/investbot
```

Дальше — под пользователем `investbot`. Вход только по ключу (файл в `sshd_config.d` с префиксом `00-` перекрывает
настройки облачного образа), файрвол — только SSH:

```bash
echo "PasswordAuthentication no" | sudo tee /etc/ssh/sshd_config.d/00-investbot.conf && sudo systemctl restart ssh
```

Не закрывайте текущую сессию, пока не проверите вход по ключу в новом окне.

```bash
sudo ufw allow OpenSSH && sudo ufw --force enable
```

Docker (официальный установщик) и git:

```bash
curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker investbot && sudo apt-get install -y git
```

Перелогиньтесь, чтобы группа docker применилась. Если `docker pull` с Docker Hub с российского сервера не
проходит, включите зеркало реестра, которое даёт ваш хостер (настройка `registry-mirrors` в
`/etc/docker/daemon.json`, адрес — в документации хостера).

## 3. Код на сервер

Каталог проекта — `/opt/investbot`, владелец — пользователь `investbot`:

```bash
sudo mkdir -p /opt/investbot && sudo chown investbot:investbot /opt/investbot
```

Вариант А — приватный репозиторий (GitHub/GitLab): `git clone <url> /opt/investbot`.

Вариант Б — копия с вашего компьютера (из каталога проекта):

```bash
rsync -az --delete --exclude .venv --exclude data/local --exclude secrets --exclude .env ./ investbot@SERVER:/opt/investbot/
```

## 4. Сертификаты Минцифры

API Т-Инвестиций и индекс SDK используют сертификаты Russian Trusted Root CA. В систему их ставить не нужно —
только в bundle для бота. Корневой и выпускающие сертификаты скачиваются с сервера Госуслуг (gu-st.ru, раздел
«Сертификаты Минцифры» на https://www.gosuslugi.ru/crt); для opensource.tbank.ru нужен выпускающий 2024 года:

```bash
cd /opt/investbot && mkdir -p docker/certs data/local && for f in russian_trusted_root_ca_pem.crt russian_trusted_sub_ca_pem.crt russian_trusted_sub_ca_2024_pem.crt; do curl -fsSo docker/certs/$f https://gu-st.ru/content/lending/$f; done
```

Файлы Госуслуг заканчиваются без перевода строки, поэтому при склейке он добавляется после каждого файла (иначе
`-----END CERTIFICATE-----` и следующий `-----BEGIN` окажутся в одной строке и bundle не прочитается):

```bash
for f in /etc/ssl/certs/ca-certificates.crt docker/certs/*.crt; do cat "$f"; echo; done > data/local/ca-bundle.pem
```

## 5. Секреты и .env

```bash
mkdir -p secrets && openssl rand -base64 32 > secrets/pg_password && chmod 600 secrets/pg_password
```

```bash
cp .env.example .env && chmod 600 .env && printf 'INVESTBOT_UID=%s\nINVESTBOT_GID=%s\n' "$(id -u)" "$(id -g)" >> .env
```

Последняя часть команды запускает контейнер бота от вашего пользователя, чтобы он мог писать в `data/local`.
Откройте файл на редактирование (`nano .env`). Комментарии в нём — только на отдельных строках: после `ЗНАЧЕНИЕ  # …`
docker compose считает комментарий частью значения.

Заполните:
- `INVESTBOT_TELEGRAM_BOT_TOKEN` — от @BotFather;
- `INVESTBOT_TINVEST_TOKEN` — токен Т-Инвестиций **«Только для чтения»**;
- `INVESTBOT_LLM_BASE_URL`, `INVESTBOT_LLM_API_KEY`, `INVESTBOT_LLM_MODEL` — ваш OpenAI-совместимый провайдер
  (адрес до `/v1`, точный id модели), цены и лимит — по тарифу провайдера;
- `INVESTBOT_LLM_DATA_IN_RU=true`, если провайдер обрабатывает данные в РФ. Если он за рубежом, портфельные данные
  (доли, без сумм) ему не отправляются, пока вы явно не поставите `INVESTBOT_LLM_ALLOW_PORTFOLIO_ABROAD=true`.

`INVESTBOT_OWNER_TELEGRAM_ID` пока оставьте пустым — бот сообщит его сам (шаг 7).

## 6. Сборка и проверка сети

```bash
ops/compose.sh build
```

```bash
ops/compose.sh run --rm bot doctor
```

Команда проверяет конфигурацию (токен «только чтение», сертификаты, SDK) и сеть с этого сервера. В конце — вердикт. Если Telegram или LLM недоступны напрямую — раздел 9, затем повторите `doctor`.

## 7. Запуск

```bash
ops/compose.sh up -d
```

```bash
ops/compose.sh logs -f bot
```

Напишите боту `/start` — он пришлёт ваш Telegram ID. Впишите его в `.env` (`INVESTBOT_OWNER_TELEGRAM_ID=…`) и
перезапустите:

```bash
ops/compose.sh up -d
```

После этого бот отвечает только вам, расписание (отчёт по субботам в 10:00 МСК, новости, проверка токена) включено.

## 8. Первые шаги в боте

1. `/status` — что подключено (Т-Инвестиции, LLM, ленты).
2. `/edit` — счета у других брокеров и карточки ЦФА.
3. `/allocation` — анкета → варианты → обсуждение → «Утвердить».
4. `/portfolio`, `/report`, `/ideas`, `/news`, вопросы обычным текстом.

## 9. Доступ к Telegram, если он закрыт с сервера

Bot API — это HTTPS к `api.telegram.org`, поэтому MTProto-прокси (ключи для приложения Telegram) не подходят: нужен
SOCKS5 или HTTP. Три варианта, от простого к сложному:

- **готовый SOCKS5/HTTP-прокси** — одна строка `INVESTBOT_TELEGRAM_PROXY=socks5://логин:пароль@хост:порт` в `.env`;
- **VPN-подписка с ключами VLESS** (9.1) — контейнер `vpn` из этого проекта;
- **свой зарубежный сервер** (9.2) — SSH-туннель, контейнер `tunnel`.

### 9.1. VPN-подписка (VLESS)

Контейнер `vpn` (sing-box) берёт серверы из подписки, сам выбирает живой и быстрый по задержке до Telegram,
раз в 6 часов перечитывает подписку и пропускает через VPN только домены Telegram — остальное отклоняет.
Поддерживаются ключи `vless://` с Reality или TLS (транспорт TCP, WebSocket, gRPC); xhttp и серверы с выходом в РФ
пропускаются.

Ссылку на подписку (или ключи `vless://` по одному на строку) положите в секрет — не в `.env` и не в чат:

```bash
nano secrets/vpn_subscription && chmod 600 secrets/vpn_subscription
```

В `.env`:

```
COMPOSE_PROFILES=vpn
INVESTBOT_TELEGRAM_PROXY=socks5://vpn:1080
```

```bash
ops/compose.sh up -d --build
```

Проверка: `ops/compose.sh logs vpn` — «серверов: N»; `ops/compose.sh run --rm bot doctor` — «Telegram Bot API через
прокси ✅». Если через VPN нужен и LLM-провайдер — `VPN_ALLOW_DOMAINS=telegram.org,домен-провайдера` и
`INVESTBOT_LLM_PROXY=socks5://vpn:1080` в `.env`.

### 9.2. Свой зарубежный сервер (SSH-туннель)

На **зарубежном** сервере создайте пользователя без shell, которому разрешена только переадресация на нужные хосты:

```bash
sudo adduser --disabled-password --gecos "" --shell /usr/sbin/nologin tunnel && sudo mkdir -p /home/tunnel/.ssh
```

На **российском** сервере создайте ключ и сохраните отпечаток зарубежного сервера:

```bash
cd /opt/investbot && ssh-keygen -t ed25519 -N "" -f secrets/tunnel_key && ssh-keyscan FOREIGN_HOST > secrets/tunnel_known_hosts
```

Публичный ключ (`secrets/tunnel_key.pub`) добавьте на зарубежном сервере в `/home/tunnel/.ssh/authorized_keys` с
ограничениями (подставьте хост вашего LLM-провайдера):

```
restrict,port-forwarding,permitopen="api.telegram.org:443",permitopen="LLM_HOST:443" ssh-ed25519 AAAA… investbot
```

В `.env` на российском сервере:

```
TUNNEL_HOST=FOREIGN_HOST
TUNNEL_USER=tunnel
INVESTBOT_TELEGRAM_PROXY=socks5://tunnel:1080
INVESTBOT_LLM_PROXY=socks5://tunnel:1080
```

```bash
ops/compose.sh --profile tunnel up -d --build
```

Проверка: `ops/compose.sh run --rm bot doctor` — строки «через прокси» должны быть зелёными.

## 10. Обновления

Новая версия кода (git pull или rsync из шага 3), затем:

```bash
ops/compose.sh up -d --build
```

Миграции базы применяются автоматически при старте бота.

## 11. Бэкапы

Ежедневный дамп базы в `data/backups` (хранится 14 дней):

```bash
(crontab -l 2>/dev/null; echo "15 4 * * * /opt/investbot/ops/backup.sh >> /opt/investbot/data/backups/backup.log 2>&1") | crontab -
```

Копируйте `data/backups` и `secrets/` куда-нибудь вне сервера. Восстановление:

```bash
ops/compose.sh exec -T postgres pg_restore -U investbot -d investbot --clean < data/backups/investbot-ДАТА.dump
```

## 12. Безопасность

- Токен Т-Инвестиций — только «чтение»; с полным доступом бот не запустится и проверяет это ежедневно.
- `.env` и `secrets/` — права 600, не в git. Токены не отправляйте в чат.
- Входящие порты закрыты, кроме SSH по ключу. Бот сам ходит к Telegram (long polling).
- Контейнер бота — с корнем только для чтения и без повышения привилегий; изменяемые данные — в `data/local`.
