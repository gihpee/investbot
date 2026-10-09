# investbot

Личный ИИ-аналитик инвестиций для рынка РФ: читает портфель из Т-Инвестиций (только чтение),
учитывает ручные счета и ЦФА, сравнивает портфель с целевой аллокацией и общается через Telegram.
**Бот не совершает сделок** — только рекомендации, решения принимаете вы.

Исследование и архитектура: [docs/README.md](docs/README.md) → [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Что уже работает (Ф0 → Ф1)

| Компонент | Где | Статус |
|---|---|---|
| Доменное ядро: счета, позиции, снимок, факты | `src/investbot/core` | ✅ |
| Адаптер Т-Инвестиций, только чтение: проверка прав токена (fail-closed), блокировка торговых сервисов, лимиты API | `src/investbot/adapters/tinvest` | ✅ код и тесты на фейковом SDK; живая проверка — после установки SDK |
| Ручные счета и карточки ЦФА (YAML), запрет двойного учёта | `src/investbot/portfolio/manual.py` | ✅ |
| Отнесение бумаг к бакетам, шкала рейтингов, оценка в ₽ | `src/investbot/portfolio` | ✅ |
| IPS (целевая аллокация), коридоры, дрейф | `src/investbot/rebalance` | ✅ |
| Клиент MOEX ISS (история, облигации, КБД, индексы) | `src/investbot/adapters/moex_iss` | ✅ код и тесты на фикстурах; живая проверка — с российского IP |
| Конструктор аллокации: анкета, варианты A/B/C, правки кнопками, утверждение IPS (§10.8) | `src/investbot/allocation`, `services/allocation.py`, `channels/telegram/allocation.py` | ✅ |
| Журнал решений с хэш-цепочкой (JSONL до появления Postgres) | `src/investbot/journal` | ✅ |
| Telegram-бот: только владелец, `/portfolio`, `/allocation`, `/status`, только доли в чате | `src/investbot/channels/telegram` | ✅ |
| `investbot doctor` / `certs` / `portfolio` / `bot` | `src/investbot/cli.py` | ✅ |

## Быстрый старт (локально)

```bash
uv sync
```

```bash
uv run pytest
```

```bash
cp .env.example .env
```

Заполните `.env` (токены в чат не отправляйте). Затем:

```bash
uv run investbot doctor
```

```bash
uv run investbot portfolio
```

```bash
uv run investbot bot
```

При первом `/start` бот пришлёт ваш Telegram ID — впишите его в `INVESTBOT_OWNER_TELEGRAM_ID` и перезапустите.
После этого бот отвечает только вам.

## Токены

- **Telegram:** создайте бота у @BotFather, токен — в `INVESTBOT_TELEGRAM_BOT_TOKEN`.
- **Т-Инвестиции:** в приложении «Настройки → Токены T-Invest API» выпустите токен **«Только для чтения»**
  (на все счета или по одному на счёт). Токен с полным доступом бот отвергнет при старте и при ежедневной проверке.

## Сертификаты Минцифры

API Т-Инвестиций использует сертификаты Russian Trusted Root CA. В систему их ставить не нужно:
положите их в `docker/certs/` и соберите bundle для процесса бота — инструкция в
[docker/certs/README.md](docker/certs/README.md).

## SDK Т-Инвестиций

Официальный SDK `t-tech-investments` (1.51.0) публикуется только в GitLab-индексе Т-Банка
(PyPI-проект в карантине, старый `tinkoff-investments` удалён). Для доступа к индексу нужен тот же CA bundle:

```bash
uv pip install "t-tech-investments==1.51.0" --index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple --cert data/local/ca-bundle.pem
```

Для Docker-образа скачайте wheel в `vendor/wheels/` (`uv pip download` или `pip download` с теми же параметрами).

## Ручной портфель и IPS

- `config/manual_portfolio.yaml` — счета у других брокеров и ЦФА (пример: `config/manual_portfolio.example.yaml`).
- `config/ips.yaml` — утверждённая целевая аллокация (пример: `config/ips.example.yaml`).
  Её будет создавать конструктор аллокации в боте; до этого бот работает в режиме «только мониторинг».

Оба файла не попадают в git.

## Проверки качества

```bash
uv run ruff check src tests
```

```bash
uv run mypy
```

```bash
uv run lint-imports
```

`tests/security` проверяет, что в коде нет торговых вызовов SDK, а секреты не попадают в repr настроек.
