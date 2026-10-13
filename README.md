# investbot

Личный ИИ-аналитик инвестиций для рынка РФ: читает портфель из Т-Инвестиций (только чтение),
учитывает ручные счета и ЦФА, сравнивает портфель с целевой аллокацией и общается через Telegram.
**Бот не совершает сделок** — только рекомендации, решения принимаете вы.

Исследование и архитектура: [docs/README.md](docs/README.md) → [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
**Развёртывание на VPS:** [docs/DEPLOY.md](docs/DEPLOY.md).

## Что уже работает (Ф0 → Ф1 → Ф2a)

| Компонент | Где | Статус |
|---|---|---|
| Доменное ядро: счета, позиции, снимок, факты | `src/investbot/core` | ✅ |
| Адаптер Т-Инвестиций, только чтение: проверка прав токена (fail-closed), блокировка торговых сервисов, лимиты API | `src/investbot/adapters/tinvest` | ✅ код и тесты на фейковом SDK; живая проверка — после установки SDK |
| Ручные счета и карточки ЦФА: ввод в боте (`/edit`, мастера бумаги, ЦФА по 7176-У, денег), хранение в БД, запрет двойного учёта | `portfolio/manual*.py`, `services/manual.py`, `channels/telegram/edit.py` | ✅ |
| Отнесение бумаг к бакетам, шкала рейтингов, оценка в ₽ | `src/investbot/portfolio` | ✅ |
| IPS (целевая аллокация), коридоры, дрейф | `src/investbot/rebalance` | ✅ |
| Клиент MOEX ISS (история, облигации, КБД, индексы) | `src/investbot/adapters/moex_iss` | ✅ код и тесты на фикстурах; живая проверка — с российского IP |
| Конструктор аллокации: анкета, варианты A/B/C, правки кнопками, утверждение IPS (§10.8) | `src/investbot/allocation`, `services/allocation.py`, `channels/telegram/allocation.py` | ✅ |
| Журнал решений с хэш-цепочкой в БД; изменение и удаление записей запрещены триггерами | `src/investbot/journal` | ✅ |
| Деградация L0–L4 и гейты данных: при недоступности брокера — план по снимку ≤ 7 дней без продаж; нет цен — частичный отчёт | `ops/degradation.py`, `data/quality.py` | ✅ |
| Telegram-бот: только владелец, `/portfolio`, `/allocation`, `/status`, только доли в чате | `src/investbot/channels/telegram` | ✅ |
| Движок ребалансировки: коридоры, cash-first, продажи до точки возврата, MILP по лотам, комиссии, налог по FIFO-лотам из операций с учётом ЛДВ, лимиты, проверка выгоды | `src/investbot/rebalance`, `portfolio/taxlots.py` | ✅ (валюта, стресс-режим — следующие шаги) |
| Недельный отчёт и `/report`, `/invest`, кнопки «принять / отложить / пропустить» с записью в журнал, количества по кнопке с автоудалением | `src/investbot/channels/telegram/report.py`, `services/rebalance.py` | ✅ |
| Календарь: купоны, оферты, погашения, дивиденды, ЦФА; `/calendar` и блок в отчёте | `src/investbot/calendar`, `services/calendar.py` | ✅ |
| Идеи для входа v1 (облигации): carry + roll-down − ожидаемые потери − издержки, зона входа, условия отмены, «за» и «против»; `/ideas` и блок «Идеи недели» | `analytics/bonds.py`, `ideas/`, `services/ideas.py` | ✅ |
| Бумажное исполнение рекомендаций и идей, исходы против бенчмарков на 1–26 неделях, `/journal` | `journal/paper.py` | ✅ |
| Кнопки «что если» (без продаж, пересчитать) и понедельничная актуализация плана | `channels/telegram/report.py` | ✅ |
| Расписание: отчёт по субботам 10:00 МСК, ежедневная проверка прав токена | `src/investbot/jobs` | ✅ |
| Архив новостей: 18 RSS-лент (СМИ, ЦБ, MOEX, Smart-lab) + лента News Т-Инвестиций, дедупликация сюжетов (SHA-1 + MinHash), привязка к бумагам по тикеру, ISIN и названию в падежах | `src/investbot/news`, `adapters/rss`, `services/news.py` | ✅ живой сбор проверен: 13 из 18 лент отвечают с зарубежного IP |
| Новостной фон без LLM: `/news`, блок в недельном отчёте, всплески внимания (buzz_z), здоровье источников в `/status` | `channels/telegram/news.py` | ✅ |
| БД: SQLAlchemy Core + alembic, Postgres 17 (прод) / SQLite (локально и в тестах) | `src/investbot/data`, `migrations/` | ✅ миграции проверены на Postgres 17 |
| LLM-слой (Cloud.ru / Yandex AI Studio, OpenAI-совместимый): роутер с резидентностью данных, бюджетом и circuit breaker; Writer (комментарий к отчёту, числа только из фактов), Tagger (тон новостей), вопросы аналитику (`/ask` и свободный текст), обсуждение аллокации текстом | `adapters/llm`, `llm_roles`, `services/qa.py` | ✅ на заглушке; живая проверка — с ключом провайдера |
| `investbot doctor` / `certs` / `db upgrade` / `import` / `portfolio` / `bot` | `src/investbot/cli.py` | ✅ |

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
uv run investbot db upgrade
```

```bash
uv run investbot portfolio
```

```bash
uv run investbot bot
```

При первом `/start` бот пришлёт ваш Telegram ID — впишите его в `INVESTBOT_OWNER_TELEGRAM_ID` и перезапустите.
После этого бот отвечает только вам.

## Токены и ключи

- **Telegram:** создайте бота у @BotFather, токен — в `INVESTBOT_TELEGRAM_BOT_TOKEN`.
- **Т-Инвестиции:** в приложении «Настройки → Токены T-Invest API» выпустите токен **«Только для чтения»**
  (на все счета или по одному на счёт). Токен с полным доступом бот отвергнет при старте и при ежедневной проверке.
- **LLM (необязательно):** ключ Cloud.ru Foundation Models (`CLOUDRU_API_KEY`) или Yandex AI Studio
  (`YC_API_KEY` + `YC_FOLDER_ID`). Без ключа бот работает без текстовых пояснений, тона новостей и вопросов аналитику.
  Модели, цены и месячные лимиты — `config/llm.yaml`; портфельные данные отправляются только моделям с размещением в РФ.

## Сертификаты Минцифры

API Т-Инвестиций использует сертификаты Russian Trusted Root CA. В систему их ставить не нужно:
положите их в `docker/certs/` и соберите bundle для процесса бота — инструкция в
[docker/certs/README.md](docker/certs/README.md).

## SDK Т-Инвестиций

Официальный SDK `t-tech-investments` 1.51.0 подключён как extra `tinvest` (PyPI-проект в карантине, старый
`tinkoff-investments` удалён). Пока площадка opensource.tbank.ru недоступна, Т-Банк выложил SDK на GitHub
(`Invest-SDK/invest-python`); wheel берётся по фиксированному коммиту, его sha256 записан в `uv.lock`:

```bash
uv sync --extra tinvest
```

Docker-образ ставит SDK так же. Перед закреплением wheel сверен с исходниками релиза 1.51.0 (история репозитория —
продолжение официального `RussianInvestments/invest-python`): весь код совпадает, отличается только встроенный
`certs/RussianTrustedRootCA.pem` — в нём дополнительно самоподписанный `TINKOFFBANK-ROOTCA`. Поэтому при заданном
`INVESTBOT_CA_BUNDLE` бот выключает встроенные корни SDK (`SSL_TBANK_VERIFY=false`) и проверяет TLS только по своему
bundle.

## Ручной портфель и IPS

- Счета у других брокеров, ЦФА и деньги на них вводятся в боте командой `/edit` и хранятся в БД.
  Если удобнее файлом — заполните YAML по образцу `config/manual_portfolio.example.yaml` и перенесите его:
  `uv run investbot import manual config/manual_portfolio.yaml`.
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

Тесты репозитория БД дополнительно гоняются на Postgres, если задан `INVESTBOT_TEST_PG_URL`
(база с применёнными миграциями), например во временном контейнере `postgres:17-alpine`.
