# T-Invest API (Т-Инвестиции): возможности для read-only агента

> Дата исследования: 2026-10-09
> Основной первоисточник: репозиторий контрактов и документации `invest/invest-contracts` на GitLab Т-Банка (тег **1.51** от 2026-09-11) и исходный код официального Python SDK `invest/invest-python` (версия **1.51.0** от 2026-09-15) [1][8][24]. Портал developer.tbank.ru, tbank.ru и habr.com из среды исследования были недоступны (таймаут TCP с IP хостинга в Нидерландах), поэтому документация читалась из исходников портала в GitLab, а сведения о MCP — из вторичных источников (помечено).

## Резюме

1. **API достаточно для read-only агента почти целиком.** Через один read-only токен доступны: все счета (включая ИИС и смарт-счета ЦФА), портфель с доходностями и НКД, позиции (бумаги и валюты), полная история операций курсором, брокерский отчёт, справочники всех классов активов, свечи до 10 лет назад (месячные) и 6 лет (дневные) на один запрос, купоны/оферты/погашения облигаций, дивиденды, ~55 фундаментальных показателей, консенсус-прогнозы и прогнозы инвестдомов, сделки инсайдеров, технические индикаторы, сигналы, кредитные рейтинги облигаций (с 1.51) и **лента новостей с привязкой к инструментам (метод `News`, с 1.49, май 2026)** [8][9][10][11][12][23].
2. **Read-only токен — штатный тип**, есть и токен с доступом к одному счёту (уровень прав настраивается). Уровень доступа видно в `GetAccounts.access_level`. Срок жизни — 3 месяца с даты последнего использования [3][11].
3. **Лимиты щадящие для недельного цикла:** инструменты 200/мин, операции 200/мин, котировки 600/мин, счета 100/мин, сигналы 100/мин, генерация отчётов 5/мин; суммарно рекомендовано ≤50 rps со всех токенов; бана нет, пока с IP ≤1000 запросов/мин [2].
4. **Официальный Python SDK сменил имя и площадку.** Актуален пакет `t-tech-investments` 1.51.0 (namespace `t_tech.invest.grpc`), устанавливается **только** с индекса GitLab Т-Банка; на PyPI проект в карантине (0 файлов), старый `tinkoff-investments` с PyPI удалён (simple-index 404), GitHub-организация RussianInvestments архивирована [24][25][28][29][30]. Версия 1.0.0 (2026-05-26) — ломающая: новые импорты из `t_tech.invest.grpc`, большинство методов принимают `request=...Request(...)` [26].
5. **ЦФА («смарт-активы») в API есть, но урезанно:** справочник `Dfas`/`DfaBy` (типы `credit_portfolio_dfa`, `debt_dfa`, YTM, купон, НКД, прогнозная доходность), смарт-счета в `GetAccounts` (`ACCOUNT_TYPE_DFA`), портфель (`total_amount_dfa`) и операции по ним. Котировки, позиции и торговля ЦФА через API **не поддерживаются** [8][14].
6. **Сентимент:** официального API Пульса нет. Ближайшие официальные сигналы «толпы» — `GetMarketValues.statistic` (`interested_count` — сколько людей смотрят инструмент, `traded_count` — сколько торгуют; с 1.51) и новостной поток `News`. Официальный T-Invest MCP (запущен летом–осенью 2026) по официальной странице умеет «сводку новостей по эмитентам с оценкой рыночного сентимента» и «поиск инвестиционных идей», но через gRPC-контракт сентимент-оценки не отдаются [9][33][37].
7. **Есть официальный MCP-сервер** `https://invest-public-api.tbank.ru/mcp` (песочница `https://sandbox-invest-public-api.tbank.ru/mcp`), транспорт streamable HTTP, авторизация `Authorization: Bearer <токен>`; первое официальное упоминание — июнь 2026, широкий анонс — ~10 сентября 2026 [33][35][37][38][44]. На портале есть официальные инструкции подключения, в том числе к Telegram через агент-шлюз Hermes Agent и к n8n [33]. Для серверного Telegram-бота его можно использовать как дополнительный источник, но не как основной интерфейс данных.
8. **Сетевой риск:** документация утверждает, что «чёрных списков нет», но требует сертификаты НУЦ Минцифры; эмпирически из зарубежного хостинга (NL, 2026-10-09) TCP к `invest-public-api.tbank.ru:443` не устанавливается. Сборщик данных из T-Invest следует размещать на российском IP [4][21].
9. **Цены облигаций — в процентах от номинала**, фьючерсов — в пунктах; деньги — `MoneyValue`/`Quotation` (units+nano), считать надо в `Decimal` [18][27].
10. **Пробелы, которые придётся закрывать внешними источниками:** нет свечей и сделок для внебиржевых ЗПИФ Т-Банка (`ZPIF_OTC`), нет котировок ЦФА, нет числового сентимента, нет истории операций для бумаг после корпоративных действий в полном виде (рекомендуется брокерский отчёт), портфель Инвесткопилки недоступен [16][17][19].

## Детальные находки

### 1. Общая картина: протоколы, хосты, версии

| Параметр | Значение (на 2026-10-09) | Источник |
|---|---|---|
| Основной протокол | gRPC (+ gRPC-web) | [1][21] |
| gRPC prod / sandbox | `invest-public-api.tbank.ru:443` / `sandbox-invest-public-api.tbank.ru:443` | [21] |
| REST-прокси | `https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1.<Service>/<Method>` (POST, JSON), sandbox — `https://sandbox-invest-public-api.tbank.ru/rest` | [22] |
| WebSocket стримы | `wss://invest-public-api.tbank.ru/ws/` (отдельная лимитная политика) | [52] |
| Архивы истории | `https://invest-public-api.tbank.ru/history-data` (минутки за год, ZIP), `.../history-trades/<date>?instrumentId=<ticker_classcode>` (обезличенные сделки за день) | [6][7] |
| MCP (официальный) | `https://invest-public-api.tbank.ru/mcp`, sandbox `https://sandbox-invest-public-api.tbank.ru/mcp` | [33][38] |
| Версия контрактов | 1.51 (тег от 2026-09-11), `openapi.yaml` `info.version: "1.51"` | [22][23] |
| Пакет proto | `tinkoff.public.invest.api.contract.v1` (имя пакета не менялось после ребрендинга) | [8] |
| Авторизация | `Authorization: Bearer <token>` в metadata каждого unary-запроса и при открытии стрима | [3][21] |
| Служебные заголовки | ответ: `x-tracking-id`, `x-ratelimit-limit`, `x-ratelimit-remaining`, `x-ratelimit-reset`; запрос: `x-app-name` (рекомендован формат `<ник>.<репозиторий>`) | [21][24] |
| Официальные SDK | Python, Go, Java, Kotlin, C#, JS — все на `opensource.tbank.ru/invest/*` | [1] |

**Хронология контрактов (по тегам и диффам proto в invest-contracts) [23]:**

| Релиз | Дата тега | Что добавлено (существенное для аналитика) |
|---|---|---|
| 1.43 | 2025-12-15 | — (первый тег на GitLab) |
| 1.44 | 2025-12-26 | `OperationsStream` (стрим операций), `PayIn` (пополнение счёта) |
| 1.48 | 2026-02-18 (обновление 2026-04-16) | `DfaBy`, `Dfas` (ЦФА), `GetAccountValues` |
| 1.49 | 2026-05-22 | **`News`** (лента новостей с инструментами), `IndexInstrument` (веса индексов) |
| 1.51 | 2026-09-11 | **`Bond.ratings`** (рейтинги агентств), **`MarketValueStatistic`** (сколько людей смотрят/торгуют инструмент) |

Вывод: API активно развивается в сторону «аналитических» данных (новости, рейтинги, внимание толпы). Это прямо полезно агенту и снижает потребность во внешних источниках.

**Миграция репозиториев.** Исторически: `github.com/Tinkoff/*` → `github.com/RussianInvestments/*` → `opensource.tbank.ru/invest/*`. Все 9 репозиториев RussianInvestments на GitHub помечены `archived` (последние push — от мая 2024 (`invest-history-index-ui`) до ноября 2025 (`investAPI`, `invest-js`); у `invest-python` — 2025-10-09) [30][31]. README на GitHub всё ещё советуют `pip install tinkoff-investments` — это устаревшая инструкция.

### 2. Сервисы и методы, релевантные read-only агенту

Ниже — полный перечень RPC из proto 1.51 с пометкой полезности. Торговые методы (`OrdersService`, `StopOrdersService`, `PostOrder*`, `CurrencyTransfer`, `PayIn`, `EditFavorites` и т.п.) агенту не нужны и с read-only токеном вернут ошибку прав [3].

#### 2.1. UsersService (лимит 100/мин) [11][15]

| Метод | Что даёт агенту |
|---|---|
| `GetAccounts(status)` | Список счетов: `id`, `type`, `name`, `status`, `opened_date`, **`access_level`** (FULL / READ_ONLY / NO_ACCESS — определяется токеном). Типы: `ACCOUNT_TYPE_TINKOFF` (брокерский), `ACCOUNT_TYPE_TINKOFF_IIS` (ИИС), `ACCOUNT_TYPE_INVEST_BOX` (Инвесткопилка), `ACCOUNT_TYPE_INVEST_FUND` (фонд денежного рынка), `ACCOUNT_TYPE_DFA` = 7 (**смарт-счёт, ЦФА**), а также банковские типы 5, 6 и 8–11 (дебет, накопительный, совместный накопительный, вклад, ОМС, ОФП-вклад). С токеном на один счёт возвращается только он. Новый счёт появляется в API в течение 1–2 часов. |
| `GetInfo()` | `qual_status` (квал. инвестор), `qualified_for_work_with` (пройденные тесты: `derivative`, `structured_bonds`, `closed_fund`, `bond` — низкорейтинговые облигации, `structured_income_bonds`, `foreign_shares`, `foreign_etf`, `foreign_bond`, `russian_shares` — бумаги вне котировальных списков, `leverage`, `repo`), `tariff`, `prem_status`, `risk_level_code`, `user_id`. Нужен, чтобы **не рекомендовать недоступные пользователю бумаги**. |
| `GetUserTariff()` | Фактические лимиты пользователя по каждому методу (unary: `limit_per_minute`, `limit_per_second`; стримы: `limit`, `open`). Используйте для самонастройки rate-limiter. |
| `GetMarginAttributes(account_id)` | Маржинальные показатели (для неторгующего агента — разве что контроль плеча). |
| `GetAccountValues(accounts, values)` | Доп. показатели счёта (комиссия маржинального кредитования, остаток лимита) — с 1.48. |
| `GetBankAccounts()` | Банковские счета пользователя (вне задачи, лучше не запрашивать — минимизация данных). |

#### 2.2. OperationsService и OperationsStreamService (лимит 200/мин; отчёты 5/мин) [10][16][17]

| Метод | Что даёт агенту | Нюансы |
|---|---|---|
| `GetPortfolio(account_id, currency=RUB/USD/EUR)` | Суммы по классам: `total_amount_shares/bonds/etf/currencies/futures/options/sp/dfa`, `total_amount_portfolio`, `expected_yield`, `daily_yield(_relative)`; по позициям: `quantity`, `average_position_price` (средневзвешенная), `average_position_price_fifo`, `expected_yield(_fifo)`, `current_price`, **`current_nkd`**, `blocked`, `blocked_lots`, `ticker`, `class_code`, `instrument_uid`, `position_uid`, `instrument_type`; плюс `virtual_positions` (подарочные бумаги со сроком сгорания). | Главный метод синхронизации. Для Инвесткопилки (`INVEST_BOX`) не работает. Цены вне торгов — по закрытию последней сессии. Средние цены пересчитываются асинхронно (до 1 с). |
| `GetPositions(account_id)` | `money` (валютные остатки по всем валютам), `blocked` (заблокированные деньги), `securities` (`balance`, `blocked`, `exchange_blocked`, `instrument_type`), `futures`, `options`, `limits_loading_in_progress`. | Не показывает ГО по фьючерсам (для этого `GetWithdrawLimits`). Валютные позиции — именно отсюда. |
| `GetOperationsByCursor(account_id, instrument_id?, from, to, cursor, limit≤1000, operation_types, state, without_commissions, without_trades, without_overnights)` | Полная история: покупки/продажи, купоны, дивиденды, налоги, комиссии, пополнения, с `payment`, `price`, `commission`, `yield`, `accrued_int`, `quantity` (в штуках), `trades_info`, `child_operations`. | Рекомендуемый метод. Использовать `limit > 2`. ID операций **могут меняться** — не использовать как первичный ключ. Для бумаг после корпоративных действий история может быть неполной. По купонам/дивидендам/налогам количество бумаг не заполняется. |
| `GetOperations(account_id, from, to, state, figi)` | Старый вариант | Только последние 1000 операций, некорректен для опционов. Не использовать. |
| `GetBrokerReport(generate → get по task_id)` | Брокерский отчёт постранично | Асинхронно: сначала `GenerateBrokerReportRequest(account_id, from, to)`, затем `GetBrokerReportRequest(task_id, page)`. Лимит генерации — 5/мин. Источник истины для сверки. |
| `GetDividendsForeignIssuer` | Справка о доходах за пределами РФ | Корректна, если правая граница периода не позже чем за 2 рабочих дня до сегодня. |
| `GetWithdrawLimits(account_id)` | Свободные к выводу деньги, заблокированное, ГО | — |
| `PortfolioStream`, `PositionsStream`, `OperationsStream` (server-side) | Push-обновления портфеля/позиций/операций | Лимит 11 стримов каждого типа. Для недельного ребаланса не нужны; полезны для «живых» алертов. В `PortfolioStream` не видны бумаги, заблокированные биржей. |

#### 2.3. InstrumentsService (лимит 200/мин) [8][14]

**Справочники и поиск**

| Метод | Назначение / ключевые поля |
|---|---|
| `Shares`, `Bonds`, `Etfs`, `Currencies`, `Futures`, `OptionsBy`, `StructuredNotes`, `Dfas`, `Indicatives` | Полные списки. Параметр `InstrumentsRequest.instrument_status`: `INSTRUMENT_STATUS_BASE` (по умолчанию — торгуемые через API) или `INSTRUMENT_STATUS_ALL`; `instrument_exchange=INSTRUMENT_EXCHANGE_DEALER` — внебиржевые/дилерские. `Indicatives` — индексы и товары (для бенчмарков). |
| `ShareBy`, `BondBy`, `EtfBy`, `CurrencyBy`, `FutureBy`, `OptionBy`, `StructuredNoteBy`, `DfaBy`, `GetInstrumentBy` | Карточка по `id_type`: FIGI, TICKER (+`class_code`), UID, POSITION_UID. |
| `FindInstrument(query, instrument_kind, api_trade_available_flag)` | Поиск по тикеру/названию/ISIN/FIGI; в ответе `uid`, `position_uid`, `first_1min_candle_date`, `for_iis_flag`. |
| `GetAssets`, `GetAssetBy` | Иерархия «актив → позиция → инструмент». `AssetEtf`: `total_expense`, `fixed_commission`, `performance_fee`, `hurdle_rate`, `primary_index`, `primary_index_tracking_error`, `rebalancing_dates`, `management_type`, `tax_rate`, `description`. `AssetBond`: `borrow_name`, `collateral_flag`, `tax_free_flag`, `indexed_nominal_flag`, `interest_kind`, `return_condition`. `AssetShare`: `total_float`, `preferred_share_type`, `dividend_rate`. Не работает для фьючерсов/опционов. |
| `GetBrands`, `GetBrandBy` | Бренд/эмитент, логотипы (`https://invest-brands.cdn-tinkoff.ru/<logoName>x160.png` и т.п.) — для UI бота. |
| `GetCountries`, `TradingSchedules(exchange, from, to)` | Справочник стран; расписания площадок (важно для «рынок закрыт → цена = close»). |
| `GetFavorites`, `GetFavoriteGroups` | Избранное пользователя в приложении — можно использовать как «вотчлист» без ручного ввода. |
| `GetRiskRates(instrument_id[])` | Ставки риска (лонг/шорт) по уровню риска клиента — косвенная мера ликвидности/риска бумаги. |

**Общие поля карточек `Share`/`Bond`/`Etf` (полезные агенту):** `uid`, `asset_uid`, `position_uid`, `ticker`, `class_code`, `isin`, `lot`, `currency`, `sector`, `country_of_risk`, `trading_status`, `buy_available_flag`, `sell_available_flag`, `api_trade_available_flag`, **`for_iis_flag`**, **`for_qual_investor_flag`**, **`required_tests`**, **`liquidity_flag`** (пересчитывается раз в 30 минут из среднедневной цены × объёма), `weekend_flag`, `blocked_tca_flag`, `real_exchange` (`REAL_EXCHANGE_MOEX`, `REAL_EXCHANGE_RTS`, `REAL_EXCHANGE_OTC`), `first_1min_candle_date`, `first_1day_candle_date`, `brand`, `dlong/dshort/dlong_client` [8][14].

Специфика: `Share.div_yield_flag`, `share_type`, `issue_size`, `ipo_date`; `Etf.focus_type` (`equity`, `fixed_income`, `mixed_allocation`, `money_market`, `real_estate`, `commodity`, …), `fixed_commission`, `rebalancing_freq`, `num_shares`; `Bond.risk_level` (LOW/MODERATE/HIGH), `coupon_quantity_per_year`, `maturity_date`, `nominal`, `initial_nominal`, `aci_value`, `floating_coupon_flag`, `amortization_flag`, `perpetual_flag`, `subordinated_flag`, `call_date` (оферта), `bond_type` (`BOND_TYPE_REPLACED` — замещающие), **`ratings[]`** (`agency_name`, `rating_level`, `rating_date`, `forecast`, `is_under_watch`; с 1.51) [8][23].

**Облигационная аналитика**

| Метод | Поля |
|---|---|
| `GetBondCoupons(instrument_id, from, to)` | `coupon_date`, `coupon_number`, `fix_date`, `pay_one_bond`, `coupon_type` (CONSTANT, FLOATING, DISCOUNT, MORTGAGE, FIX, VARIABLE, OTHER), `coupon_start_date/end_date`, `coupon_period` |
| `GetBondEvents(instrument_id, from, to, type)` | Типы `EVENT_TYPE_CPN` (купон), `EVENT_TYPE_CALL` (оферта), `EVENT_TYPE_MTY` (погашение), `EVENT_TYPE_CONV` (конвертация); поля `event_date`, `pay_one_bond`, `value` (ставка/доля номинала/цена выкупа), `coupon_interest_rate`, **`default_date`**, `real_pay_date`, `execution`, `note` |
| `GetAccruedInterests(instrument_id, from, to)` | Динамика НКД: `value`, `value_percent`, `nominal` по датам |

Этого достаточно, чтобы агент сам считал YTM/дюрацию по денежным потокам (готового поля YTM у биржевых облигаций в контракте нет; оно есть только у ЦФА и в `GetMarketValues` как `INSTRUMENT_VALUE_YIELD`).

**Дивиденды, фундаментал, консенсус, события**

| Метод | Поля / ограничения |
|---|---|
| `GetDividends(instrument_id, from, to)` | `dividend_net`, `declared_date`, `last_buy_date`, `record_date`, `payment_date`, `dividend_type` (`Regular Cash`, `Cancelled`, …), `regularity`, `yield_value`, `close_price`. Фильтр по `record_date`. |
| `GetAssetFundamentals(assets[≤100])` | ~55 показателей по `asset_uid`: капитализация, 52w high/low, средние объёмы, beta, free float, форвардная див. доходность, revenue/EBITDA/net income/EPS/FCF TTM, рост выручки 1/3/5 лет, P/E, P/S, P/B, P/FCF, EV, EV/EBITDA, EV/Sales, маржинальность, ROE/ROA/ROIC, долг, Debt/Equity, Debt/EBITDA, NetDebt/EBITDA, current ratio, payout ratio, buyback TTM, ex-dividend date, границы фискального периода. Тип `double`. |
| `GetAssetReports(instrument_id, from, to)` | Календарь отчётности: `report_date`, `period_year`, `period_num`, `period_type` (квартал/полугодие/год). Для «событийного» планирования. |
| `GetForecastBy(instrument_id)` | `targets[]` — прогнозы инвестдомов (компания, рекомендация BUY/HOLD/SELL, дата, текущая и целевая цена, изменение) + `consensus` (рекомендация, консенсус-цена, min/max target, потенциал). |
| `GetConsensusForecasts(paging)` | Пакетно по всем бумагам: `best_target_price/low/high`, `total_buy/hold/sell_recommend`, `consensus`, `prognosis_date`, по `asset_uid`. Фильтра по инструменту на сервере нет — пагинация по всему списку [39]. |
| `GetInsiderDeals(instrument_id, limit≤100, cursor)` | Сделки инсайдеров: направление (BUY/SELL/INCREASE/DECREASE), дата, объём, цена, имя и должность, доля, `disclosure_date`. |
| **`News(cursor, limit)`** | (с 1.49) `items[]`: `id`, `source`, `title`, `content`, `summary` (опц.), `tables[]`, `instrument_id[]` (список `instrument_uid`/`ticker`/`class_code` упомянутых бумаг), `priority` (важная новость), `ts`; `has_next`, `next_cursor`; `limit` по умолчанию 1000. Фильтров по тикеру/дате в запросе **нет** — только курсор. Числового сентимента нет. В описании gRPC-методов портала (`methods.mdx`) метода на 2026-10-09 нет, но он есть в proto, в REST/OpenAPI-спецификации 1.51 (`POST .../InstrumentsService/News`, «получение актуальных новостей») и в SDK (`client.instruments.news`) [8][22][24]. |

#### 2.4. MarketDataService (лимит 600/мин) [5][9][18]

| Метод | Назначение / ограничения |
|---|---|
| `GetCandles(instrument_id, from, to, interval, candle_source_type, limit)` | OHLC + `volume` (лоты) + **`volume_buy`/`volume_sell`** + `is_complete`, `candle_source`. `candle_source_type`: `CANDLE_SOURCE_EXCHANGE` (только биржа) / `CANDLE_SOURCE_INCLUDE_WEEKEND` (с дилерскими сессиями выходного дня). Максимальные окна — таблица ниже. |
| `GetLastPrices(instrument_id[], last_price_type)` | Пакетно; `LAST_PRICE_EXCHANGE` или `LAST_PRICE_DEALER`. |
| `GetClosePrices(instruments[])` | Цена закрытия основной сессии + `evening_session_price`. |
| `GetOrderBook(instrument_id, depth)` | Стакан (для оценки спреда/ликвидности перед рекомендацией). |
| `GetTradingStatus(es)` | Режим торгов (в т.ч. `DEALER_NORMAL_TRADING`). |
| `GetLastTrades(instrument_id, from, to)` | Обезличенные сделки — гарантируется только последний час. |
| `GetTechAnalysis(indicator_type, instrument_id, from, to, interval, type_of_price, length, deviation, smoothing)` | Серверный расчёт **SMA, EMA, RSI, MACD, BB** на интервалах 1 мин … 1 месяц. Удобно, но лучше считать самим по свечам (контроль и воспроизводимость). |
| `GetMarketValues(instrument_id[], values[])` | `LAST_PRICE`, `LAST_PRICE_DEALER`, `CLOSE_PRICE`, `EVENING_SESSION_PRICE`, `OPEN_INTEREST`, `THEOR_PRICE`, **`YIELD`**; с 1.51 — **`statistic.interested_count`** (сколько человек просматривают инструмент) и **`statistic.traded_count`** (сколько торгуют) + время актуализации. Это единственный официальный «поведенческий» сигнал толпы в gRPC API. |

**Глубина истории `GetCandles` за один запрос** (значения «ориентировочные», источник — proto 1.51 и документация) [5][9]:

| Интервал | Окно запроса | max `limit` |
|---|---|---|
| 5 с / 10 с / 30 с | до 200 мин / 200 мин / 20 ч (только за последний месяц) | 2500 / 1250 / 2500 |
| 1 мин / 2 мин / 3 мин | до 1 дня | 2400 / 1200 / 750 |
| 5 мин / 10 мин | до недели | 2400 / 1200 |
| 15 мин / 30 мин | до 3 недель | 2400 / 1200 |
| 1 ч / 2 ч / 4 ч | до 3 месяцев | 2400 / 2400 / 700 |
| 1 день | до 6 лет | 2400 |
| 1 неделя | до 5 лет | 300 |
| 1 месяц | до 10 лет | 120 |

Дата начала истории различается по инструментам и интервалам — смотрите `first_1min_candle_date` / `first_1day_candle_date` в карточке [5]. Для длинной минутной истории — архивы `history-data` (ZIP по году на инструмент, CSV без заголовка `uid;utc;open;close;high;low;volume`, обновление раз в сутки ночью, текущего дня нет) и `history-trades` (сделки за день, CSV с `TRADE_SOURCE` = `EXCHANGE`/`DEALER`); лимит — 30 файлов в минуту с IP, REST `getHistory` — 30/мин [2][6][7].

**Единицы цен [18]:** акции — `price × lot`; **облигации — пункты = % номинала**, стоимость = `price / 100 × nominal`; валюта — `price × lot / nominal` (у JPY, AMD, KZT номинал ≠ 1); фьючерсы — `price / min_price_increment × min_price_increment_amount`.

#### 2.5. MarketDataStreamService [2][52]

Bidirectional `MarketDataStream` и server-side `MarketDataServerSideStream`: подписки на свечи, стаканы, ленту сделок, последние цены, торговые статусы (`Info`). Лимиты: 32 стрима на сервис котировок; ≤300 подписок (свечи+стаканы+сделки суммарно) на один стрим; ≤100 запросов на подписку в минуту; доставка стаканов и свечей не чаще 1 раз в 100 мс на подписку. Стрим живёт до недели и может быть закрыт сервером; пинг по умолчанию раз в 120 с (настраивается 5–180 с); переподключение — с экспоненциальной задержкой и добором пропусков unary-методами. Для недельного агента стримы опциональны (нужны только для real-time алертов «цена дошла до зоны входа»).

#### 2.6. SignalService — существует (лимит 100/мин) [12]

- `GetStrategies(strategy_id?)` → `strategy_name`, `strategy_description`, `strategy_url`, `strategy_type` (`STRATEGY_TYPE_TECHNICAL` / `STRATEGY_TYPE_FUNDAMENTAL`), `active_signals`, `total_signals`, `time_in_position`, `average_signal_yield(_year)`, `yield(_year)`.
- `GetSignals(signal_id, strategy_id, strategy_type, instrument_uid, from, to, direction, active, paging)` → `direction` (BUY/SELL), `initial_price`, `target_price`, `stoploss`, `probability`, `create_dt`, `end_dt`, `close_price`, `close_dt`, `info`, `name`. По умолчанию 20 элементов на страницу; архив закрытых сигналов — `SIGNAL_STATE_CLOSED`.
- Природа сигналов (по документации): «долгосрочные» — идеи аналитиков на основе фундаментального анализа; «краткосрочные» — автоматически по бэктесту технических индикаторов. Те же данные показываются в терминале, Пульсе (профиль `T-Trading`) и приложении. Брокер прямо пишет, что сигналы — не ИИР и прошлая доходность ничего не гарантирует [12].
- Для агента это готовый внешний «эксперт» и одновременно бенчмарк: можно логировать сигналы и измерять их реальную точность по архиву (`yield`, `close_price`) — полезно как признак в ранжировщике/RL.

#### 2.7. Sandbox [20]

Отдельный контур с теми же методами (`sandbox-invest-public-api.tbank.ru:443`) и отдельным sandbox-токеном. Ограничения: нет доходностей и средних цен в портфеле, нет ставок риска/ГО, фильтр операций только BUY/SELL, налоги не считаются, `GetDividendsForeignIssuer` всегда пустой, счета живут 3 месяца с последнего использования. Для read-only агента песочница полезна только для интеграционных тестов (можно наполнить виртуальный портфель и прогнать синхронизацию), но **не для бэктеста** (это прямо указано в AI-документации SDK [27]).

### 3. Токены: типы, read-only, привязка к счёту, безопасность [3][11]

| Тип токена | Права | Комментарий для агента |
|---|---|---|
| **Read-only** | Только чтение: портфель, операции, котировки, справочники, расписания, история | **Основной выбор.** Торговые поручения отклоняются на стороне брокера — это вторая линия защиты помимо кода. |
| Full-access | Все методы, включая заявки | Не нужен. |
| Transfer-access | Full-access + переводы между счетами; выпускается только на все счета | Никогда не использовать. |
| **Токен на конкретный счёт** | Доступ к одному счёту, уровень (read-only / full) настраивается | Хорош для принципа минимальных привилегий: например, отдельный токен на ИИС и на брокерский счёт. `GetAccounts` вернёт только этот счёт. |
| Sandbox | Только песочница | Для тестов; на боевом контуре даёт ошибку. |

**Выпуск:** веб-кабинет → настройки Т-Инвестиций (`https://www.tbank.ru/invest/settings/`) → «Токены T-Invest API» → выпустить для биржи и/или песочницы, выбрать права и (опционально) счёт. Документация рекомендует делать это с компьютера (в мобильном приложении раздела нет) и упоминает требование отключить «Подтверждение сделок кодом» (актуально для торговли; для read-only не проверено). Токен показывается **один раз**, банк его не хранит; токенов можно выпускать неограниченно [3].

**Жизненный цикл:** срок жизни — 3 месяца с даты последнего использования (еженедельный агент будет продлевать его автоматически). Ошибка `40003` — токен недействителен (отозван, завершены все сессии, блокировка карты/аккаунта, клиент ушёл, sandbox-токен на боевом контуре). Список активных токенов с датой выпуска, последнего использования и правами — в разделе «Безопасность» профиля (`https://www.tbank.ru/mybank/profile/security/`), там же отзыв [3].

**Проверка прав в рантайме:** `GetAccounts().accounts[i].access_level` — агент при старте должен убедиться, что все счета `ACCOUNT_ACCESS_LEVEL_READ_ONLY`, и отказаться работать с full-access токеном (защита от ошибки пользователя) [11].

**Практика хранения (вывод):** токен — секрет уровня «доступ к брокерскому счёту». Хранить в secret-store/переменных окружения сервера, шифровать at-rest, не логировать (официальный SDK при отправке ошибок в Sentry маскирует `Bearer …` регуляркой [24]), не передавать в LLM-контекст. Если бот мультипользовательский — токен вводится пользователем один раз и хранится зашифрованным (ключ вне БД); в Telegram сообщение с токеном нужно удалить после приёма.

### 4. Лимиты запросов (документация, master от 2026-09-11) [2]

| Сервис / метод | Протокол | Лимит |
|---|---|---|
| Инструменты (`InstrumentsService`, суммарно все методы) | gRPC/REST | **200/мин** |
| Счета (`UsersService`) | gRPC/REST | 100/мин |
| Операции (`OperationsService`) | gRPC/REST | 200/мин |
| Формирование отчётов (`GetBrokerReport`, `GetDividendsForeignIssuer`) | gRPC | **5/мин** |
| Котировки (`MarketDataService`) | gRPC/REST | **600/мин** |
| Сигналы (`SignalService`) | gRPC/REST | 100/мин |
| Песочница | gRPC/REST | 200/мин |
| Ордера / стоп-ордера / автоследование | gRPC/REST | 100 / 50 / 100 в мин (агенту не нужны) |
| REST `getHistory` (архив за год) | REST | 30/мин; архивы `history-data`/`history-trades` — 30 файлов/мин с IP |
| Стримы котировок / ордеров / операций | gRPC | 32 / 16 на тип / 11 на тип соединений |
| Подписки в одном стриме котировок | — | ≤300 (свечи+стаканы+сделки), ≤100 запросов подписки/мин |

Дополнительно: лимит сервиса считается **суммарно по всем его методам и всем счетам пользователя**; рекомендация — не более 50 запросов/с суммарно со всех токенов; превышения «не будет», пока с IP ≤1000 запросов/мин; при подозрении на бан IP — в поддержку. Счётчик стримов обновляется раз в 2 минуты. `GetUserTariff` отдаёт персональные лимиты (могут отличаться — «динамическое формирование лимитов») [2][11].

**Оценка бюджета недельного цикла (вывод).** Портфель 30 бумаг + вселенная 300 кандидатов: справочники `Shares/Bonds/Etfs` — 3 запроса (кешировать на сутки); `GetPortfolio`+`GetPositions` по 3 счетам — 6; операции курсором — единицы; дневные свечи за 6 лет на 330 бумаг — 330 запросов (≈33 с при 600/мин), инкрементально потом — 330 коротких; фундаментал — 4 запроса по 100 активов; `GetForecastBy` на 330 бумаг — 330 запросов к сервису инструментов (≈1,7 мин при 200/мин) либо ~4–10 страниц `GetConsensusForecasts`; купоны/события для 100 облигаций — 200 запросов (1 мин); дивиденды — 100–200. Итого полный прогон — порядка 5–10 минут даже с запасом, инкрементальный ежедневный — 1–2 минуты. Узкое место — **сервис инструментов (200/мин)**: нужен отдельный token-bucket на каждый сервис и кеш.

### 5. Официальные SDK и сообщество

#### 5.1. Python SDK: `t-tech-investments` [24][25][26][28][29]

| Факт | Значение (проверено 2026-10-09) |
|---|---|
| Имя пакета | `t-tech-investments`; импорт `t_tech.invest` |
| Актуальная версия | **1.51.0** (опубликована в реестре GitLab 2026-09-15); история: 0.3.3 (2026-01-26), 0.3.4 (03-06), 0.3.5 (03-27), **1.0.0 (2026-05-26)**, 1.49.0 (05-27), 1.49.1–1.49.3 (06–08), 1.51.0 (09-15). Версии ≥1.49 нумеруются по версии контрактов. |
| Установка | `pip install t-tech-investments --index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple` (официальный README). На практике удобнее `--extra-index-url`, чтобы зависимости тянулись с PyPI [41]. |
| PyPI | Проект `t-tech-investments` — статус **quarantined**, файлов 0; старый `tinkoff-investments` — simple-index отдаёт 404 (пакет недоступен для установки). |
| Сертификаты | Индекс `opensource.tbank.ru` подписан **Russian Trusted Sub CA** (Минцифры); без установленного корня pip упадёт с SSL-ошибкой. README допускает `--trusted-host opensource.tbank.ru` (снижает безопасность). Для gRPC-соединения можно выставить `SSL_TBANK_VERIFY=True` — тогда SDK использует встроенный сертификат Минцифры. |
| Python | `requires-python >=3.8`; для 3.11+ — `grpcio ^1.75`, `protobuf ^6.31`; доп. зависимости `cachetools`, `python-dateutil`, `deprecation`, `sentry-sdk ^2.47`, `iprotopy`, опционально pandas/matplotlib/mplfinance. |
| Клиенты | `Client`, `AsyncClient`, `SandboxClient`, `AsyncSandboxClient` из `t_tech.invest.grpc`; есть `RetryingClient`/`AsyncRetryingClient` (`t_tech.invest.retrying`) — **но они унаследованы от устаревшего `t_tech.invest.Client`** (старые сервисы и инициализация Sentry, см. «Телеметрия»), поэтому для нового стиля ретраи лучше делать своими или через `interceptors=[...]` у `t_tech.invest.grpc.Client`; кеш инструментов в клиенте (по умолчанию кешируются `shares/bonds/etfs/...By`), `MarketDataCache` для свечей на диске, хелпер `client.get_all_candles(...)` (автоматически режет период на допустимые окна и дедуплицирует). |
| Ломающие изменения 1.0.0 | Код генерируется из proto; старые сервисы `t_tech.invest.services` и импорты `from t_tech.invest import ...` помечены устаревшими и не развиваются. Новый стиль: `from t_tech.invest.grpc import Client`, `from t_tech.invest.grpc.schemas import ...`, параметры **большинства** методов — в `request=XxxRequest(...)` (BREAKING_CHANGES: «большая часть функций… обернута параметром request», часть методов принимает request с значением по умолчанию — например, `dfas()`, `news()`). Поле `from` в Python называется `from_`. Примеры в самом README SDK до сих пор используют устаревший `from t_tech.invest import Client` — ориентироваться на `docs/ai/sdk-map.md`. |
| Телеметрия | (1) При создании `Client` по умолчанию делается HTTP-запрос к `https://opensource.tbank.ru/api/v4/projects/238/releases` для проверки новой версии (`check_sdk_version=False` отключает). (2) `sentry_sdk.init` с DSN `https://invest-piapi-errorhub@error-hub.tbank.ru/00000000` (фильтр пропускает только `UNAVAILABLE`, `Bearer …` маскируется) вызывается в `__enter__` **устаревших** `t_tech.invest.Client/AsyncClient` (и наследующих их `RetryingClient`); новый `t_tech.invest.grpc.Client` Sentry сам не инициализирует, но его стрим-методы обёрнуты `sentry_sdk.capture_exception` — если в процессе уже инициализирован Sentry (самим ботом или legacy-клиентом), ошибки стримов уйдут в этот DSN. Legacy-клиент к тому же перезапишет собственный `sentry_sdk.init` бота. Для сервера с жёсткой egress-политикой это надо учитывать. |
| Таймауты по умолчанию | unary 30 с, stream 7 суток, connect 10 с; `app_name` → заголовок `x-app-name`. |
| Ориентация на LLM | В репозитории есть `AGENTS.md`, раздел `docs/ai/` (карта SDK, типичные ошибки, безопасность торговых программ) и skill `.agents/skills/t-invest-python-sdk-bot-builder/SKILL.md`. Ключевые правила: не использовать `float` для денег, только tz-aware UTC, не путать `figi`/`instrument_id`/`instrument_uid`, не мешать sync и async, не считать песочницу бэктестом [27]. |
| Лицензия | Apache-2.0. README содержит оговорку, что проект — «независимая разработка» и не является официальным выпуском какой-либо компании (при этом размещён на GitLab Т-Банка и указан как официальный SDK в README контрактов [1]). |

#### 5.2. Другие официальные SDK (GitLab `opensource.tbank.ru/invest`, теги на 2026-10-09) [1][47][48][49][50]

| Язык | Установка / координаты | Последний тег |
|---|---|---|
| Go | `go get opensource.tbank.ru/invest/invest-go` (модуль `opensource.tbank.ru/invest/invest-go`, go 1.20) | v1.51.0 (2026-09-21) |
| Java | Maven `ru.tinkoff.piapi:java-sdk` (по бейджу README) | v1.51 (2026-09-16) |
| Kotlin | `ru.t-technologies.invest.piapi.kotlin:kotlin-sdk-grpc-core:1.51.0` | 1.51 (2026-09-15) |
| C# | см. README `invest-csharp` | v0.6.25 (2026-09-17) |
| JS/TS | см. README `invest-js` | 1.61.0 (2026-09-23) |
| Python (автоследование) | `pip install ttech-autofollow-sdk --index-url https://opensource.tbank.ru/api/v4/projects/240/packages/pypi/simple` | v1.0.2 (2026-02-05) |

Неофициальные SDK (Node `vitalets/tinkoff-invest-api`, Go, PHP, Rust, Swift, Haskell, C++, Ruby) перечислены в README контрактов; их актуальность относительно 1.49–1.51 (News, Dfa, ratings) не проверялась [1].

#### 5.3. Полезные проекты сообщества (как референс архитектуры)

| Проект | Что это | Чем полезен |
|---|---|---|
| `Arroch/invest-portfolio-bot` [41] | Личный Telegram-бот: read-only, сравнивает портфель T-Invest с `target.yaml`, считает дрейф и что докупить (`/portfolio`, `/rebalance`, `/untracked`) | Почти готовый MVP «ребаланс по целевым долям» на `t_tech.invest.AsyncClient` |
| `Sashido01/tinvest-devkit` [42] | Read-only прослойка над SDK: свои модели, `Decimal`, полная пагинация операций, retry на `UNAVAILABLE`, `doctor`-диагностика | Шаблон анти-коррупционного слоя над SDK |
| `Sicness/t-bank-invest-mcp-read-only` [39] | MCP (stdio, PyPI) только с read-инструментами: счета, портфель, операции, облигации, фундаментал, консенсус, свечи, стакан, `get_stock_snapshot` | Готовый набор read-tools для LLM-агента |
| `plasm-one/tinvest-mcp` [36] | Локальный MCP (35 tools) с жёсткими лимитами, планом ребалансировки с учётом комиссий, НКД, налога FIFO и ЛДВ, вердиктом `WORTH_IT/NOT_WORTH_IT` | Лучшая найденная модель «ИИ — аналитик, ограничения — в коде» |
| `solohinlex/InvestHelper` [38] | CLI: портфель YAML + MOEX ISS + LLM через OpenAI-compatible API; синхронизация через **официальный MCP** | Пример вызова официального MCP из Python |
| `konstantinmk/market-mcp` | Read-only MCP поверх MOEX ISS + T-Invest с кешем и rate-limit | Идея комбинирования ISS и T-Invest |

### 6. Официальный T-Invest MCP и AI-функции Т-Инвестиций

#### 6.1. T-Invest MCP (официальный сервер)

- **Что это:** MCP-сервер на стороне Т-Банка, «мост» между LLM-агентами и брокерской инфраструктурой; документация — `https://developer.tbank.ru/invest/mcp` (при первичном исследовании страница не открылась; при верификации 2026-10-09 прочитана напрямую вместе с подстраницами `agents-connect/coding-agents`, `agents-connect/hermes-agent`, `platforms-connect/n8n-platform`) [33].
- **Даты:** пост официального аккаунта `T_InvestAPI` в Пульсе о подключении ИИ-агента через T-Invest MCP — 2026-06-10 (совет начинать с режима «только чтение») [44]; пресс-анонс «брокер подключил счета клиентов к ИИ-помощникам» — статья от 2026-09-11 («накануне») [35]; статья команды на Хабре «ИИ-агенты в инвестициях: собираем Telegram-ассистента и торгового робота на MCP Т-Инвестиций» [34].
- **Подключение:** URL `https://invest-public-api.tbank.ru/mcp` (prod) и `https://sandbox-invest-public-api.tbank.ru/mcp` (песочница), транспорт streamable HTTP, заголовок `Authorization: Bearer <токен T-Invest API>` — тот же токен и та же модель прав (read-only / full / transfer) [37][38]. Поддерживаемые клиенты (официальная страница): Claude Desktop, Claude Code, Cursor, Qwen Code, Gemini CLI, плагины VS Code и любые MCP-совместимые клиенты; в инструкции есть конфиги для Qwen Code, VS Code, Claude Code, ChatGPT Desktop и **Claude Desktop — через мост `npx mcp-remote`** (нативно по streamable HTTP Claude Desktop не подключается — так и следует понимать тезис сравнения сообщества [37]). Отдельные официальные гайды: **Hermes Agent** (open-source агент-шлюз Nous Research в Docker, с LLM-провайдером и **Telegram-ботом**, требует сертификаты Минцифры в контейнере) и **n8n** [33]. Песочница рекомендуется для старта торгового функционала.
- **Функции (по выдаче со страницы и обзорам):** портфель, баланс, свободные средства, доходность и риски; поиск инструментов; котировки, стаканы; операции и налоги; **сводка новостей по эмитентам с оценкой рыночного сентимента**; **инвестидеи и анализ прогнозов инвестдомов** (в т.ч. поиск аналитиков, чьи прогнозы чаще сбывались, и «необычных изменений» прогнозов); сигналы, сделки инсайдеров, фундаментал; заявки (рыночные, лимитные, стоп), отмена/замена, переводы между счетами, пополнение [33][34][37].
- **Имена инструментов** официально не опубликованы в доступных источниках; в коде InvestHelper вызываются `invest_list_broker_accounts` (параметр `status`) и `invest_get_portfolio` (`accountId`, `responseView: ["DETAILED"]`) — т.е. инструменты имеют префикс `invest_` и camelCase-параметры (не подтверждено официально; брать актуальный список через `tools/list`) [38].
- **Безопасность (оценка):** сервер закрытый, лимиты на заявки пользователь не настраивает; при full-access токене модель, читающая новости (внешний текст) в той же сессии, где доступны торговые tools, уязвима к prompt-injection — аргумент сообщества за read-only токен и разделение «аналитика ↔ исполнение» [37]. Для нашего агента (без сделок) это закрывается выдачей **только read-only токена**.
- **Применимость для бота:** официальный MCP интересен как источник «новостей с сентиментом» и «инвестидей», которых нет в gRPC-контракте. Официальный гайд Hermes Agent (LLM + T-Invest MCP + Telegram) — готовый референс «бот за вечер», но он отдаёт LLM прямой доступ к tools счёта; для нашего агента это допустимо только с read-only токеном. Но для серверного Telegram-бота основной канал данных — gRPC SDK (детерминированность, типы, лимиты, кеш, отсутствие LLM-зависимости в синхронизации). MCP-вызовы можно обернуть как отдельный «tool» LLM-аналитика с явной маркировкой «внешний текст».

#### 6.2. AI внутри приложения Т-Инвестиций

- **AI Инвест-ассистент** — чат на странице каждой акции в приложении; собирает динамику цены, прогнозы аналитиков, мультипликаторы, мнения и новости; инвестрекомендаций не даёт. В одном из постов анонсировалось, что он «научится анализировать контент Пульса и в обобщённом виде делиться мнениями и настроениями» — подтверждения запуска не найдено [45].
- **«Тая»** — общий финансовый ИИ-ассистент «Т-Технологий» (бета запущена ~2026-09-09, доступна и не-клиентам; первый навык — подбор товаров, инвестиционные функции — в планах) [46].
- Внешнего API у этих ассистентов нет; использовать их в архитектуре нельзя (только как ориентир UX).

### 7. Покрытие классов активов и особые случаи

| Класс / случай | Что видно через API | Чего нет / ограничения |
|---|---|---|
| **Акции** (MOEX, СПБ) | Справочник, свечи, цены, стакан, дивиденды, фундаментал, консенсус, прогнозы, инсайдеры, отчётный календарь, сигналы | Иностранные акции на `dealer_int_exchange` — особый внебиржевой режим [19] |
| **Облигации** | Справочник с флагами (`floating_coupon`, `amortization`, `perpetual`, `subordinated`, `risk_level`, `call_date`, `ratings[]`), купоны, события (оферты, погашения, **дефолты**), НКД, свечи (цена в % номинала), `YIELD` через `GetMarketValues` | YTM/дюрацию для обычных облигаций считать самостоятельно; рейтинги — только с 1.51 |
| **Фонды (биржевые БПИФ/ETF, в т.ч. УК «Т-Капитал»)** | `Etfs` (focus_type, комиссии, частота ребалансировки), `GetAssetBy` → `AssetEtf` (TER, tracking error, индекс, даты ребалансировок, налоговый режим), свечи | Состав фонда (холдинги) в API не найден |
| **Внебиржевые ЗПИФ Т-Банка** | Площадка `dealer_realestate`, class_code `ZPIF_OTC`: стакан, последняя цена, торговый статус `DEALER_NORMAL_TRADING`, торги по будням 10:00–19:00 МСК | **Свечей, цен закрытия, обезличенных сделок нет**; только лимитные заявки DAY [19] |
| **ЦФА («смарт-активы»)** | `Dfas`/`DfaBy` (`type`: `credit_portfolio_dfa`, `debt_dfa`; `basic_assets`, `forecast_yield` min/max %, `yield_to_maturity`, `coupon_value`, частота, дата купона, НКД, `for_iis_flag`, `for_qual_investor_flag`); смарт-счёт `ACCOUNT_TYPE_DFA`; `GetPortfolio` (поле `total_amount_dfa`); `GetOperationsByCursor`; тип `INSTRUMENT_TYPE_DFA` | **Котировки, торговля и просмотр позиций по ЦФА через API не поддерживаются** (официальная FAQ) [14]. Значит, состав ЦФА-позиции агенту, возможно, придётся вести вручную/по операциям. Какие именно ЦФА и операторы ИС стоят за «смарт-активами», в API не раскрыто (не проверено) |
| **Структурные ноты** | `StructuredNotes`/`StructuredNoteBy` (барьер сохранности, базовые активы, принцип наблюдений, `for_qual_investor_flag`), `total_amount_sp` в портфеле, `INSTRUMENT_TYPE_SP` | — |
| **OTC / дилерские сессии** | `real_exchange = REAL_EXCHANGE_OTC`, `instrument_exchange = INSTRUMENT_EXCHANGE_DEALER`, `LAST_PRICE_DEALER`, свечи с `CANDLE_SOURCE_INCLUDE_WEEKEND`, `weekend_flag`; `otc_flag` — устаревший | — |
| **Ограничения для неквалов** | `for_qual_investor_flag`, `required_tests[]` у инструмента + `qual_status` и `qualified_for_work_with[]` у пользователя | Нужно фильтровать вселенную рекомендаций по этим полям |
| **ИИС** | `ACCOUNT_TYPE_TINKOFF_IIS`; у инструментов `for_iis_flag` | Тип ИИС (А/Б/ИИС-3) и лимиты взносов через API не найдены |
| **Валютные позиции** | `GetPositions.money[]` (все валюты), `GetPortfolio.total_amount_currencies`, справочник `Currencies` | — |
| **Несколько счетов** | `GetAccounts` + `GetPortfolio` по каждому `account_id`; агрегация — на стороне агента | Лимиты считаются суммарно по всем счетам |
| **Инвесткопилка / фонд денежного рынка** | Видны в `GetAccounts` (`INVEST_BOX`, `INVEST_FUND`) | `GetPortfolio` для Инвесткопилки не работает [16] |
| **Корпоративные действия** | Позиция переносится на новый инструмент (новые FIGI/UID); старый помечается удалённым | **История свечей старого инструмента становится недоступной**; история операций может быть неполной [14][17] |

### 8. Доступ из-за рубежа, сеть, условия использования

- **Документация:** «Со стороны брокера нет чёрных списков адресов… Доступ открыт для всех»; проблемы связывают с нестабильной сетью, отсутствием сертификатов НУЦ Минцифры и ограничениями провайдеров/VPN; для диагностики — `telnet invest-public-api.tbank.ru 443` [4].
- **Сертификаты:** API и индекс пакетов используют цепочку Russian Trusted Root/Sub CA (Минцифры); сертификаты ставятся с `https://www.gosuslugi.ru/crt`; для Node — переменная `NODE_EXTRA_CA_CERTS`, для Java — `keytool`, для Python SDK — `SSL_TBANK_VERIFY=True` [4][24][25].
- **Эмпирическая проверка (2026-10-09, IP хостинга в Амстердаме, AS216139):** TCP-соединение с `invest-public-api.tbank.ru:443` (178.130.128.33) — таймаут; `www.tbank.ru`, `developer.tbank.ru`, `ya.ru`, `gosuslugi.ru` — тоже недоступны; при этом `opensource.tbank.ru`, `iss.moex.com`, `cbr.ru` отвечают. Это согласуется с геофильтрацией части российских ресурсов для зарубежных/хостинговых сетей. Вывод: **компонент, ходящий в T-Invest API, должен работать на российском IP** (VPS в РФ), а LLM-провайдер, если он зарубежный, — вызываться из отдельного компонента/через прокси.
- **Публичное ПО:** если вы публикуете ПО для широкого круга пользователей, нужно написать на `invest-public-api@tbank.ru` для регистрации выделенного `appname` [1]. Для личного бота это не требуется, но заголовок `x-app-name` рекомендуется [21].
- **Условия:** `openapi.yaml` ссылается на `termsOfService: https://www.tbank.ru/invest/disclaimers/basic-information/` [22]. Явных ограничений на «перепродажу/переиспользование данных» в документации API не найдено (страницу условий открыть не удалось — открытый вопрос). Сигналы и прогнозы сопровождаются дисклеймером «не является индивидуальной инвестиционной рекомендацией» [12].
- **Телеметрия SDK:** см. раздел 5.1 (проверка версии через GitLab API, Sentry error-hub Т-Банка для ошибок стримов).

### 9. Пульс и сентимент

- **Официального API Пульса нет**: ни в proto 1.51, ни в документации методов соцсети нет [8].
- **Неофициально:** `meanother/tpulse-py` (PyPI `tpulse` 0.1.9) обращается к внутреннему эндпоинту сайта (`/api/invest-gw/social/v1/post/instrument/<тикер>`, ~30 постов на страницу); последний релиз — 2023-01-06, домен с тех пор сменился на tbank.ru — работоспособность не гарантирована [43]. Есть и Go-скрейпер публичных профилей (`suenot/tbank-pulse-auto`). Скрейпинг закрытого API — юридический и технический риск (условия сервиса, антибот-защита, смена формата).
- **Модерация и «качество» авторов:** с октября 2026 Пульс выделяет авторов с подтверждённым инвестиционным опытом (статус «Легенда Пульса» с требованиями к портфелю, подпискам на автоследование и стажу) — потенциальный вес для взвешивания мнений, если данные будут доступны [53].
- **Официальные заменители сентимента внутри T-Invest:**
  1. `GetMarketValues(...).statistic.interested_count / traded_count` — «внимание толпы» к бумаге (с 1.51) — пригодно как признак (рост интереса, аномалии) [9];
  2. `News` — поток новостей с привязкой к инструментам и флагом `priority` — сентимент считать своей моделью (RuBERT/LLM) [8];
  3. `GetSignals` (технические и фундаментальные идеи), `GetForecastBy`/`GetConsensusForecasts` (распределение BUY/HOLD/SELL), `GetInsiderDeals` — «мнение профессионалов» [8][12];
  4. Официальный MCP — «новости с оценкой сентимента» (только через MCP, формат не документирован публично) [33][37];
  5. `GetCandles.volume_buy/volume_sell` — дисбаланс покупок/продаж как микроструктурный прокси сентимента [9].

### 10. Примеры кода на актуальном SDK (`t-tech-investments` 1.51.0, стиль `t_tech.invest.grpc`)

> Все имена классов/методов/полей сверены с исходниками SDK 1.51.0 и proto 1.51 [8][9][10][11][24]. Код **не запускался** против боевого API (из среды исследования хост недоступен). Идентификаторы вида `SBER_TQBR` используются только как пример формата `ticker_classcode`, а не как рекомендация.

**Установка**

```bash
# Python 3.11+; нужен корневой сертификат Минцифры в системе (или --trusted-host, что хуже)
python -m venv .venv && source .venv/bin/activate
pip install "t-tech-investments==1.51.0" \
  --extra-index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple
export INVEST_TOKEN="t.***"          # read-only токен
export SSL_TBANK_VERIFY=True         # если gRPC ругается на сертификат: использовать встроенный CA Минцифры
```

**10.1. Подключение, проверка read-only, счета и профиль пользователя**

```python
import os
from t_tech.invest.grpc import Client
from t_tech.invest.grpc.schemas import (
    AccessLevel, AccountStatus, GetAccountsRequest, GetInfoRequest, GetUserTariffRequest,
)

TOKEN = os.environ["INVEST_TOKEN"]

with Client(TOKEN, app_name="myname.investbot", check_sdk_version=False) as c:
    accounts = c.users.get_accounts(
        request=GetAccountsRequest(status=AccountStatus.ACCOUNT_STATUS_OPEN)
    ).accounts
    # Защита: агент отказывается работать с токеном, который может торговать
    if any(a.access_level != AccessLevel.ACCOUNT_ACCESS_LEVEL_READ_ONLY for a in accounts):
        raise RuntimeError("Нужен read-only токен")
    for a in accounts:
        print(a.id, a.type.name, a.name, a.opened_date)   # ACCOUNT_TYPE_TINKOFF_IIS, ACCOUNT_TYPE_DFA, ...

    info = c.users.get_info(request=GetInfoRequest())
    print("квал:", info.qual_status, "тесты:", info.qualified_for_work_with,
          "тариф:", info.tariff, "риск:", info.risk_level_code)

    tariff = c.users.get_user_tariff(request=GetUserTariffRequest())
    for lim in tariff.unary_limits:                       # фактические лимиты → в rate-limiter
        print(lim.limit_per_minute, lim.methods[:3])
```

**10.2. Портфель и позиции по всем счетам (Decimal, без float)**

```python
from decimal import Decimal
from t_tech.invest.grpc.schemas import PortfolioRequest, PositionsRequest, AccountType
from t_tech.invest.utils import money_to_decimal, quotation_to_decimal

def snapshot(c, accounts):
    rows = []
    for a in accounts:
        if a.type == AccountType.ACCOUNT_TYPE_INVEST_BOX:      # портфель копилки API не отдаёт
            continue
        p = c.operations.get_portfolio(
            request=PortfolioRequest(account_id=a.id, currency=PortfolioRequest.CurrencyRequest.RUB)
        )
        for pos in p.positions:
            qty = quotation_to_decimal(pos.quantity)              # в штуках
            price = money_to_decimal(pos.current_price)           # для облигаций — в валюте (уже пересчитано)
            nkd = money_to_decimal(pos.current_nkd)
            rows.append({
                "account": a.id, "ticker": pos.ticker, "class_code": pos.class_code,
                "uid": pos.instrument_uid, "type": pos.instrument_type,
                "qty": qty, "price": price, "nkd": nkd,
                "value": qty * (price + nkd),
                "avg_price": money_to_decimal(pos.average_position_price),
                "yield": quotation_to_decimal(pos.expected_yield),
                "blocked": pos.blocked,
            })
        total = money_to_decimal(p.total_amount_portfolio)
        print(a.id, "итого:", total, "в т.ч. ЦФА:", money_to_decimal(p.total_amount_dfa))

        cash = c.operations.get_positions(request=PositionsRequest(account_id=a.id))
        for m in cash.money:                                       # валютные остатки
            print("  cash", m.currency, money_to_decimal(m))
    return rows
```

> Проверьте на своём счёте, в каких единицах приходит `current_price` облигаций в `GetPortfolio` (документация описывает перевод пунктов в валюту для котировок `MarketData`; для портфеля поле типа `MoneyValue`). Это пункт обязательного интеграционного теста.

**10.3. Полная история операций (курсор)**

```python
from datetime import datetime, timezone
from t_tech.invest.grpc.schemas import GetOperationsByCursorRequest, OperationType

def all_operations(c, account_id, since=datetime(2018, 1, 1, tzinfo=timezone.utc)):
    cursor, out = "", []
    while True:
        r = c.operations.get_operations_by_cursor(request=GetOperationsByCursorRequest(
            account_id=account_id, from_=since, to=datetime.now(timezone.utc),
            cursor=cursor, limit=1000,                     # max 1000; документация советует limit > 2
            operation_types=[OperationType.OPERATION_TYPE_BUY, OperationType.OPERATION_TYPE_SELL,
                             OperationType.OPERATION_TYPE_COUPON, OperationType.OPERATION_TYPE_DIVIDEND],
        ))
        out.extend(r.items)
        if not r.has_next:
            return out
        cursor = r.next_cursor
# Ключ дедупликации — не op.id (может меняться), а (date, type, instrument_uid, payment, quantity)
```

**10.4. Свечи: длинная история и инкремент**

```python
from datetime import timedelta
from t_tech.invest.grpc.schemas import CandleInterval, CandleSource, GetCandlesRequest
from t_tech.invest.utils import now, quotation_to_decimal

# (а) длинная история: хелпер сам режет период на допустимые окна и убирает дубли
daily = list(c.get_all_candles(
    instrument_id="SBER_TQBR", from_=now() - timedelta(days=365 * 10),
    interval=CandleInterval.CANDLE_INTERVAL_DAY,
    candle_source_type=CandleSource.CANDLE_SOURCE_EXCHANGE,
))
# (б) инкремент последних N свечей одним запросом
r = c.market_data.get_candles(request=GetCandlesRequest(
    instrument_id="SBER_TQBR", from_=now() - timedelta(days=30), to=now(),
    interval=CandleInterval.CANDLE_INTERVAL_DAY, limit=30))
bars = [(x.time, quotation_to_decimal(x.close), x.volume, x.volume_buy, x.volume_sell, x.is_complete)
        for x in r.candles]
```

**10.5. Облигации: вселенная, купоны, события, НКД, рейтинги**

```python
from t_tech.invest.grpc.schemas import (
    InstrumentsRequest, InstrumentStatus, GetBondCouponsRequest, GetBondEventsRequest,
    GetAccruedInterestsRequest, RiskLevel,
)

bonds = c.instruments.bonds(request=InstrumentsRequest(
    instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE)).instruments
universe = [b for b in bonds
            if not b.for_qual_investor_flag and b.liquidity_flag and b.buy_available_flag
            and b.currency == "rub" and b.risk_level != RiskLevel.RISK_LEVEL_HIGH]

b = universe[0]
coupons = c.instruments.get_bond_coupons(request=GetBondCouponsRequest(
    instrument_id=b.uid, from_=now(), to=b.maturity_date)).events
calls = c.instruments.get_bond_events(request=GetBondEventsRequest(
    instrument_id=b.uid, type=GetBondEventsRequest.EventType.EVENT_TYPE_CALL)).events
aci = c.instruments.get_accrued_interests(request=GetAccruedInterestsRequest(
    instrument_id=b.uid, from_=now() - timedelta(days=7), to=now())).accrued_interests
ratings = [(r.agency_name, r.rating_level, r.forecast, r.is_under_watch) for r in b.ratings]  # с 1.51
cashflows = [(cp.coupon_date, money_to_decimal(cp.pay_one_bond), cp.coupon_type.name) for cp in coupons]
# YTM/дюрацию агент считает сам по cashflows + номиналу + цене (цена котировки = % номинала!)
```

**10.6. Дивиденды, фундаментал, прогнозы, консенсус**

```python
from t_tech.invest.grpc.schemas import (
    GetDividendsRequest, GetAssetFundamentalsRequest, GetForecastRequest,
    GetConsensusForecastsRequest, Page, InstrumentsRequest,
)

shares = c.instruments.shares(request=InstrumentsRequest()).instruments
s = next(x for x in shares if x.ticker == "SBER" and x.class_code == "TQBR")

divs = c.instruments.get_dividends(request=GetDividendsRequest(
    instrument_id=s.uid, from_=now() - timedelta(days=365 * 5), to=now() + timedelta(days=365))).dividends

# фундаментал — по asset_uid, до 100 активов за запрос
asset_uids = list({x.asset_uid for x in shares})[:100]
fund = c.instruments.get_asset_fundamentals(
    request=GetAssetFundamentalsRequest(assets=asset_uids)).fundamentals
pe = {f.asset_uid: f.pe_ratio_ttm for f in fund}            # double; 0 может означать «нет данных»

fc = c.instruments.get_forecast_by(request=GetForecastRequest(instrument_id=s.uid))
print(fc.consensus.recommendation.name, quotation_to_decimal(fc.consensus.consensus),
      [(t.company, t.recommendation.name, t.recommendation_date) for t in fc.targets])

page, items = 0, []
while True:                                                  # консенсус по всему рынку постранично
    r = c.instruments.get_consensus_forecasts(request=GetConsensusForecastsRequest(
        paging=Page(limit=100, page_number=page)))
    items += r.items
    if (page + 1) * r.page.limit >= r.page.total_count:
        break
    page += 1
```

**10.7. Сигналы, новости, «внимание толпы», ЦФА**

```python
from t_tech.invest.grpc.schemas import (
    GetSignalsRequest, SignalState, NewsRequest, GetMarketValuesRequest, MarketValueType,
)

sig = c.signals.get_signals(request=GetSignalsRequest(
    instrument_uid=s.uid, active=SignalState.SIGNAL_STATE_ACTIVE, paging=Page(limit=50, page_number=0)))
for x in sig.signals:
    print(x.strategy_name, x.direction.name, quotation_to_decimal(x.target_price), x.end_dt, x.probability)

news, cursor = [], None
for _ in range(5):                                           # поток новостей, без фильтра по тикеру
    r = c.instruments.news(request=NewsRequest(limit=200, cursor=cursor))
    news += r.items
    if not r.has_next:
        break
    cursor = r.next_cursor
by_ticker = {}
for n in news:
    for ni in n.instrument_id:
        by_ticker.setdefault(ni.instrument.ticker, []).append((n.ts, n.source, n.title, n.priority))

mv = c.market_data.get_market_values(request=GetMarketValuesRequest(
    instrument_id=[s.uid], values=[MarketValueType.INSTRUMENT_VALUE_LAST_PRICE]))
for i in mv.instruments:
    print(i.ticker, i.statistic.interested_count, i.statistic.traded_count, i.statistic.time)  # с 1.51

for d in c.instruments.dfas().instruments:                  # ЦФА: только справочник
    print(d.ticker, d.type, quotation_to_decimal(d.yield_to_maturity), d.maturity_date, d.for_qual_investor_flag)
```

**10.8. Асинхронный клиент с бюджетом по сервисам (скелет для бота)**

```python
import asyncio, time
from t_tech.invest.grpc import AsyncClient
from t_tech.invest.grpc.schemas import GetForecastRequest

class Bucket:                                   # token-bucket на сервис (лимиты из GetUserTariff)
    def __init__(self, per_min: int):
        self.rate, self.tokens, self.ts = per_min / 60, per_min, time.monotonic()
        self.cap, self.lock = per_min, asyncio.Lock()
    async def take(self):
        async with self.lock:
            while True:
                now_ = time.monotonic()
                self.tokens = min(self.cap, self.tokens + (now_ - self.ts) * self.rate); self.ts = now_
                if self.tokens >= 1:
                    self.tokens -= 1; return
                await asyncio.sleep((1 - self.tokens) / self.rate)

LIMITS = {"instruments": Bucket(180), "market_data": Bucket(540), "operations": Bucket(180), "users": Bucket(90)}

async def fetch_forecasts(token, uids):
    async with AsyncClient(token, app_name="myname.investbot", check_sdk_version=False) as c:
        async def one(uid):
            await LIMITS["instruments"].take()
            return uid, await c.instruments.get_forecast_by(request=GetForecastRequest(instrument_id=uid))
        return dict(await asyncio.gather(*(one(u) for u in uids)))
```

### 11. Сопоставление методов и функций агента

| Функция агента | Методы T-Invest API | Частота / кеш | Примечания |
|---|---|---|---|
| **Синхронизация портфеля** | `GetAccounts`, `GetPortfolio`, `GetPositions`, `GetWithdrawLimits` (+ `PortfolioStream` для live) | по команде пользователя и перед каждым отчётом | Портфель «задаётся вручную» → API используется для автосверки; ручной ввод остаётся для ЦФА-позиций, внешних брокеров, копилки |
| **История сделок и P&L, налоговая база** | `GetOperationsByCursor`, `GetBrokerReport` | ежедневно инкрементально; отчёт — раз в месяц | Дедуп по составному ключу; FIFO/ЛДВ считать самим |
| **Профиль ограничений пользователя** | `GetInfo` (`qual_status`, `qualified_for_work_with`), флаги `for_qual_investor_flag`, `required_tests`, `for_iis_flag` | раз в сутки | Фильтр вселенной рекомендаций |
| **Вселенная инструментов** | `Shares`, `Bonds`, `Etfs`, `Currencies`, `StructuredNotes`, `Dfas`, `Indicatives`, `GetAssets/GetAssetBy`, `FindInstrument`, `GetBrands` | раз в сутки | Хранить по `uid`/`asset_uid`; учитывать корпоративные действия (смена UID) |
| **История цен / технический анализ** | `GetCandles` (+ `get_all_candles`), архивы `history-data`, `GetTechAnalysis` (опционально), `GetClosePrices` | дневки — ежедневно; минутки — по необходимости | Свои индикаторы по свечам; `volume_buy/sell` как признак |
| **Текущие цены и ликвидность для «зоны входа»** | `GetLastPrices`, `GetOrderBook`, `GetTradingStatus`, `GetMarketValues`, `TradingSchedules` | перед формированием рекомендаций | Спред и глубина стакана → фильтр неликвида |
| **Облигационная аналитика** | `Bonds`/`BondBy` (флаги, `ratings`, `call_date`), `GetBondCoupons`, `GetBondEvents`, `GetAccruedInterests`, `GetMarketValues(YIELD)` | раз в сутки | YTM, дюрация, лесенка погашений, риск оферт/дефолтов |
| **Дивиденды** | `GetDividends`, `GetAssetFundamentals` (div yield, payout, ex-date) | раз в сутки | Календарь отсечек для рекомендаций |
| **Фундаментал** | `GetAssetFundamentals` (≤100 активов/запрос), `GetAssetReports` | раз в неделю + по отчётному календарю | Мультипликаторы и качество бизнеса |
| **Консенсус и мнения аналитиков** | `GetForecastBy`, `GetConsensusForecasts`, `GetSignals`, `GetStrategies`, `GetInsiderDeals` | раз в сутки | Признаки для скоринга и «внешний эксперт» |
| **Новостной фон** | `News` (курсор), опционально официальный MCP (новости с сентиментом) | каждые 15–60 минут | Сентимент считать своей моделью; хранить сырой текст |
| **«Сентимент толпы»** | `GetMarketValues.statistic` (`interested_count`, `traded_count`), дисбаланс `volume_buy/sell` | ежедневно (снимки во времени) | Пульс — только неофициально |
| **Фонды** | `Etfs`, `GetAssetBy` → `AssetEtf` (TER, индекс, tracking error), свечи | раз в неделю | Для внебиржевых ЗПИФ — только стакан/последняя цена |
| **ЦФА** | `Dfas`, `DfaBy`, `GetAccounts(ACCOUNT_TYPE_DFA)`, `GetPortfolio.total_amount_dfa`, операции | раз в сутки | Котировок и позиций нет → ручной учёт |
| **Мониторинг лимитов/здоровья** | `GetUserTariff`, заголовки `x-ratelimit-*`, `x-tracking-id` | при старте + постоянно | Логировать tracking-id для поддержки |
| **Тестирование интеграции** | `SandboxClient` + `OpenSandboxAccount`, `SandboxPayIn`, `PostSandboxOrder` | CI | Не для бэктеста |

## Выводы для архитектуры агента

1. **Источник данных №1 — gRPC через официальный Python SDK `t-tech-investments` (async).** Он покрывает портфель, историю, справочники, облигации, дивиденды, фундаментал, консенсус, сигналы и новости. Зафиксировать версию (`==1.51.0`), ставить с индекса GitLab Т-Банка через `--extra-index-url`, обновлять осознанно (контракты меняются каждые 1–3 месяца). PyPI-зеркал официального пакета нет.
2. **Анти-коррупционный слой (Broker Adapter).** Собственные доменные модели (`Decimal`, tz-aware UTC, явные типы идентификаторов `uid/asset_uid/position_uid/figi`), конвертация `Quotation/MoneyValue` и единиц цен (облигации в % номинала) — в одном месте. Это изолирует бота от ломающих изменений SDK (как 1.0.0) и позволяет при необходимости переключиться на REST или MCP.
3. **Принцип «только чтение» на трёх уровнях:** (а) read-only токен, при желании — отдельный токен на каждый счёт; (б) проверка `access_level` при старте и отказ работать с full-access; (в) в адаптере физически отсутствуют торговые методы. LLM никогда не видит токен.
4. **Хранилище и кеш.** Справочники и свечи — в локальную БД (PostgreSQL/TimescaleDB или DuckDB/Parquet для аналитики), инкрементальная догрузка; учёт корпоративных действий (смена UID — история старого инструмента исчезает из API, значит **свою копию истории нужно хранить с первого дня**).
5. **Планировщик с бюджетом по сервисам.** Token-bucket на каждый сервис (инструменты 200/мин — узкое место; котировки 600/мин), лимиты подтягивать из `GetUserTariff`, ретраи с экспонентой на `UNAVAILABLE`/`RESOURCE_EXHAUSTED`. Полный недельный прогон по 300+ бумагам укладывается в 5–10 минут.
6. **Размещение.** Компонент «T-Invest connector» — на VPS в РФ (российский IP, сертификаты Минцифры в trust store). Если LLM-провайдер зарубежный и недоступен из РФ — разделить на два сервиса (RU-коннектор ↔ аналитический сервис) с минимизацией передаваемых данных (обезличенный портфель без номеров счетов).
7. **Сентимент — свой конвейер.** `News` (официальный поток) + `interested_count/traded_count` + консенсус/сигналы/инсайдеры + внешние источники (новостные агрегаторы, Telegram-каналы — отдельные исследования). Пульс — только как опциональный, неофициальный и отключаемый источник. Официальный MCP можно подключить к LLM-аналитику как дополнительный tool «новости с сентиментом», помечая его вывод как недоверенный текст.
8. **Портфель «задаётся вручную» + автосверка.** Ручной ввод (Telegram-команды/форма) остаётся источником целевых долей и позиций, которых нет в API (ЦФА-позиции, внешние брокеры, Инвесткопилка); API-снимок используется для сверки и подсказок «расхождение с ручным портфелем».
9. **Фильтр допустимости рекомендаций.** Перед выдачей идей отсекать бумаги с `for_qual_investor_flag` (если пользователь не квал), с непройденными `required_tests`, без `for_iis_flag` (для ИИС), с `liquidity_flag=false`, недоступные к покупке (`buy_available_flag=false`), внебиржевые без свечей.
10. **RL/бэктест — на собственных исторических данных** (свечи + архивы `history-data`), а не в песочнице; песочница — только для интеграционных тестов адаптера.

## Риски и подводные камни

- **Сетевая доступность:** из зарубежных хостингов API может быть недоступен (наш тест — таймаут TCP из NL), несмотря на тезис документации об отсутствии чёрных списков; плюс обязательные сертификаты НУЦ Минцифры (а это расширение доверия к национальному УЦ в процессе бота) [4].
- **Дистрибуция SDK:** пакет живёт только в GitLab-реестре Т-Банка; PyPI-проект в карантине, старый пакет удалён; сообщество фиксирует случаи «битых» wheel из индекса [36]. Нужен vendoring (локальный wheel/зеркало в своём реестре) и pin версии.
- **Ломающие изменения SDK** (1.0.0: request-модели, новые импорты) и устаревшие примеры в интернете/LLM-памяти (`tinkoff.invest`, позиционные аргументы). Генерировать код только по текущим сигнатурам [26][27].
- **Телеметрия SDK по умолчанию** (запрос версии к GitLab при каждом создании `t_tech.invest.grpc.Client`; Sentry-отчёты об ошибках стримов на `error-hub.tbank.ru` при использовании legacy-клиентов `t_tech.invest.Client`/`RetryingClient`, а с новым клиентом — в любой Sentry, инициализированный в процессе) — отключать `check_sdk_version=False`, не использовать legacy-клиенты; Sentry-поведение — проверить и при необходимости блокировать egress [24].
- **Нестабильные идентификаторы:** ID операций меняются; при корпоративных действиях меняются FIGI/UID, старый инструмент и его история пропадают из API [14][17].
- **Единицы и типы:** цены облигаций в % номинала, фьючерсов — в пунктах; `quantity` в операциях — в штуках, в заявках — в лотах; фундаментал — `double` (возможны нули как «нет данных»); `Quotation` нельзя вести через `float` [18][27].
- **Неполнота данных:** нет свечей по внебиржевым ЗПИФ, нет котировок/позиций ЦФА, нет портфеля Инвесткопилки, нет состава фондов, нет числового сентимента; `GetLastTrades` — только последний час; секундные свечи — только за месяц [5][14][16][19].
- **`News` без фильтров:** только курсорная лента «актуальных новостей» (без тикера/даты в запросе) — нужен собственный индекс; источники/лицензия контента не описаны; текстового описания метода в gRPC-документации портала нет (есть только proto и OpenAPI-схема) — поведение может измениться.
- **`GetConsensusForecasts` без фильтра по инструменту** — приходится выкачивать все страницы [39].
- **Дисклеймеры и регулирование:** сигналы и прогнозы брокера — «не ИИР»; бот, выдающий персонализированные рекомендации по портфелю, рискует попасть в определение индивидуальной инвестиционной рекомендации (вопрос для отдельного правового исследования).
- **Prompt-injection через новости/посты**, если LLM-агент получает и внешний текст, и любые инструменты действия; при read-only токене ущерб ограничен утечкой данных — но всё равно нужна изоляция инструментов [37].
- **Хранение токена в Telegram-боте:** токен, присланный в чат, остаётся в истории Telegram — удалять сообщение, лучше передавать через одноразовую web-форму/`/settoken` с немедленным удалением.
- **Лимиты по всем счетам суммарно:** несколько счетов и параллельные задачи делят один бюджет [2].
- **Песочница не моделирует** доходности, средние цены, налоги, риск-ставки — непригодна для оценки стратегий [20].
- **Неофициальный Пульс:** закрытый эндпоинт, библиотека не обновлялась с 2023, риск блокировки и нарушения условий использования [43].

## Открытые вопросы

1. Точный список и схемы инструментов официального T-Invest MCP (`tools/list`), формат «оценки сентимента» новостей, лимиты MCP и поддерживает ли он read-only токен на один счёт — требуется живой вызов с токеном с российского IP.
2. Источники и лицензия новостей в методе `News` (поле `source`), глубина истории ленты, можно ли хранить контент; есть ли (будет ли) фильтр по инструменту.
3. Единицы `current_price` облигаций в `GetPortfolio` (валюта vs % номинала) — проверить интеграционным тестом.
4. Что именно стоит за «смарт-активами» (ЦФА) Т-Банка: оператор ИС, перечень выпусков, как получить количество ЦФА в портфеле, если позиции через API не отдаются (возможно, только через `GetPortfolio` по смарт-счёту или операции).
5. Отдаёт ли `GetPortfolio` по смарт-счёту (`ACCOUNT_TYPE_DFA`) построчные позиции или только `total_amount_dfa`.
6. Условия использования данных (`https://www.tbank.ru/invest/disclaimers/basic-information/`) — допустимо ли хранение и производная аналитика для личного бота; что меняется, если бот станет публичным (регистрация appname).
7. Геодоступность: блокируется ли API для всех зарубежных IP или только для отдельных хостинг-сетей; нужен тест с нескольких локаций.
8. Отправляет ли SDK что-либо, кроме ошибок стримов, в Sentry error-hub; можно ли отключить без патча.
9. Насколько полны `GetAssetFundamentals` и `GetForecastBy` для облигаций/фондов и малых эмитентов (покрытие вселенной MOEX).
10. Будет ли открыт API Пульса или выдача сентимента/инвестидей в gRPC-контракте (сейчас только в MCP).
11. Требование «отключить подтверждение сделок кодом» при выпуске токена — относится ли к read-only токенам.

## Источники

1. invest-contracts (README, список SDK, appname) — https://opensource.tbank.ru/invest/invest-contracts
2. Лимиты — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/md/intro/intro/limits.md
3. Токен — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/md/intro/intro/token.mdx
4. Проблемы подключения (сеть, сертификаты) — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/md/intro/developer/network.mdx ; импорт сертификатов — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/md/intro/intro/cases.mdx
5. Исторические котировки (окна GetCandles) — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/md/intro/intro/load_history.md
6. Архив исторических данных (`history-data`) — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/md/services/quotes/get_history.md
7. Сервис исторических данных (`history-trades`, лимит 30 файлов/мин) — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/md/services/history-md/head-history-md.mdx
8. instruments.proto (1.51) — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/docs/contracts/instruments.proto
9. marketdata.proto (1.51) — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/docs/contracts/marketdata.proto
10. operations.proto — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/docs/contracts/operations.proto
11. users.proto — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/docs/contracts/users.proto
12. signals.proto и описание сервиса сигналов — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/docs/contracts/signals.proto ; https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/md/services/signals/head-signals.md
13. common.proto (InstrumentType, InstrumentStatus) — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/docs/contracts/common.proto
14. FAQ инструментов (ЦФА/смарт-активы, liquidity_flag, корпоративные действия) — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/md/services/instruments/faq_instruments.md
15. FAQ счетов (`qualified_for_work_with`) — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/md/services/accounts/faq_users.md ; сервис счетов — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/md/services/accounts/head-account.md
16. Сервис операций (портфель, Инвесткопилка) — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/md/services/operations/head-operations.md
17. Особенности методов операций — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/md/services/operations/operations_problems.md
18. FAQ котировок (пункты, формулы пересчёта) — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/md/services/quotes/faq_marketdata.md
19. Фонды Т-Банка (ZPIF_OTC) и внутриброкерский рынок — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/md/intro/useful-info/weekend-trading/tbank_fund.md ; https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/md/intro/useful-info/weekend-trading/index.md
20. Песочница — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/md/intro/developer/sandbox/index.md
21. gRPC: адреса, x-tracking-id, x-app-name — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/md/intro/developer/protocols/grpc.mdx
22. OpenAPI 1.51 (REST, servers, termsOfService) — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/docs/swagger-ui/openapi.yaml
23. Теги/релизы контрактов — https://opensource.tbank.ru/invest/invest-contracts/-/tags
24. Python SDK (исходники 1.51.0: clients, constants, error hub, version checker) — https://opensource.tbank.ru/invest/invest-python
25. README Python SDK (установка, SSL_TBANK_VERIFY) — https://opensource.tbank.ru/invest/invest-python/-/blob/master/README.md
26. BREAKING_CHANGES (1.0.0, 0.3.0) — https://opensource.tbank.ru/invest/invest-python/-/blob/master/BREAKING_CHANGES.md
27. AI-документация SDK: README, карта SDK, типичные ошибки — https://opensource.tbank.ru/invest/invest-python/-/blob/master/docs/ai/README.md ; https://opensource.tbank.ru/invest/invest-python/-/blob/master/docs/ai/sdk-map.md ; https://opensource.tbank.ru/invest/invest-python/-/blob/master/docs/ai/common-pitfalls.md
28. Реестр пакетов Python SDK — https://opensource.tbank.ru/invest/invest-python/-/packages
29. PyPI t-tech-investments (quarantined) — https://pypi.org/project/t-tech-investments/
30. Архивный GitHub invest-python — https://github.com/RussianInvestments/invest-python
31. Архивный GitHub investAPI — https://github.com/RussianInvestments/investAPI
32. Портал разработчика — https://developer.tbank.ru/invest/intro/intro
33. T-Invest MCP (официальная страница) — https://developer.tbank.ru/invest/mcp
34. Хабр, блог Т-Банка: «ИИ-агенты в инвестициях: собираем Telegram-ассистента и торгового робота на MCP Т-Инвестиций» — https://habr.com/ru/companies/tbank/articles/1086714/
35. Finance Magnates: «Брокер Т-Банка подключил счета клиентов к ИИ-помощникам» (2026-09-11) — https://rus.financemagnates.com/broker-t-banka-podklyuchil-scheta-klientov-k-ii-pomoshhnikam/
36. plasm-one/tinvest-mcp — https://github.com/plasm-one/tinvest-mcp
37. Сравнение T-Invest MCP и tinvest-mcp — https://github.com/plasm-one/tinvest-mcp/blob/main/docs/comparison.ru.md
38. solohinlex/InvestHelper (клиент официального MCP) — https://github.com/solohinlex/InvestHelper
39. Sicness/t-bank-invest-mcp-read-only — https://github.com/Sicness/t-bank-invest-mcp-read-only
40. konstantinmk/market-mcp — https://github.com/konstantinmk/market-mcp
41. Arroch/invest-portfolio-bot — https://github.com/Arroch/invest-portfolio-bot
42. Sashido01/tinvest-devkit — https://github.com/Sashido01/tinvest-devkit
43. meanother/tpulse-py (неофициальный клиент Пульса) — https://github.com/meanother/tpulse-py
44. Пост T_InvestAPI в Пульсе о T-InvestMCP (2026-06-10) — https://www.tbank.ru/invest/social/profile/T_InvestAPI/b142cf4a-576b-4ca9-84a1-8b53aa3075ec/
45. Пост T-Investments об AI Инвест-ассистенте (2025-06-21) — https://www.tbank.ru/invest/social/profile/T-Investments/78c9e0ab-a0ef-4cca-bf8b-eda6313356a5/
46. Ведомости: «Т-Банк запустил умного финансового ИИ-ассистента Таю» (2026-09-09) — https://www.vedomosti.ru/finance/news/2026/09/09/1227424-finansovogo-ii-assistenta
47. Go SDK — https://opensource.tbank.ru/invest/invest-go
48. Java SDK — https://opensource.tbank.ru/invest/invest-java
49. Kotlin SDK — https://opensource.tbank.ru/invest/invest-kotlin
50. C# и JS SDK — https://opensource.tbank.ru/invest/invest-csharp ; https://opensource.tbank.ru/invest/invest-js ; автоследование — https://opensource.tbank.ru/invest/invest-autofollow
51. Telegram-канал T-Invest API — https://t.me/s/tinkoffinvestopenapi
52. Стримы, WebSocket, время жизни стримов — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/md/intro/developer/stream.md ; https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/md/intro/developer/sla.md ; https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/md/intro/developer/protocols/ws.mdx
53. CNews: «Т-Банк выделит в Пульсе инвесторов с подтверждённым опытом» (2026-10-06) — https://www.cnews.ru/news/line/2026-10-06_t-bank_vydelit_v_sotsialnoj
54. Мониторинг ошибок (Sentry error-hub) — https://opensource.tbank.ru/invest/invest-contracts/-/blob/master/src/md/intro/developer/observability.md

## Верификация ключевых утверждений

> Проверка 2026-10-09: первоисточники читались напрямую (proto/md/openapi из `invest/invest-contracts` ветки `master` и тегов 1.48/1.49/1.51, исходники `invest/invest-python` master = 1.51.0, GitLab API тегов и реестра пакетов, PyPI JSON/simple API, GitHub API, страницы `developer.tbank.ru/invest/mcp*`, пост T_InvestAPI в Пульсе). Для доступа к `opensource.tbank.ru` и `developer.tbank.ru` понадобилась цепочка Russian Trusted Root CA + Sub CA (выпуск `subca_ssl_rsa2024`, старый Sub CA с gosuslugi не подходит).

| № | Утверждение | Вердикт | Комментарий | Источник |
|---|---|---|---|---|
| 1 | Контракты 1.51 (2026-09-11) покрывают все read-only потребности: счета, квал/тесты, портфель, позиции, операции курсором, брокерский отчёт, справочники, свечи, облигационные события, дивиденды, фундаментал, консенсус, сигналы, инсайдеры, новости | ✅ подтверждено | Все RPC присутствуют в proto master: `UsersService` (GetAccounts/GetInfo/GetUserTariff), `OperationsService` (GetPortfolio/GetPositions/GetOperationsByCursor/GetBrokerReport), `InstrumentsService` (Shares…Dfas, GetBondCoupons/Events, GetDividends, GetAssetFundamentals, GetForecastBy, GetConsensusForecasts, GetInsiderDeals, News), `MarketDataService` (GetCandles…GetMarketValues), `SignalService`. Тег 1.51 датирован 2026-09-11 | [8][10][11][12][23] |
| 2 | Read-only токен — штатный; токен на конкретный счёт с настраиваемым уровнем прав; `access_level` в GetAccounts; срок жизни 3 месяца с последнего использования; показывается один раз | ✅ подтверждено | token.mdx дословно описывает все пять видов токенов, срок жизни и одноразовый показ; `Account.access_level` (FULL_ACCESS/READ_ONLY/NO_ACCESS) есть в users.proto | [3][11] |
| 3 | SDK `t-tech-investments` 1.51.0 (2026-09-15) только с индекса GitLab; PyPI quarantined (0 файлов); `tinkoff-investments` недоступен; GitHub RussianInvestments архивирован | ✅ подтверждено | Реестр проекта 238: 1.51.0 от 2026-09-15; PyPI simple JSON: `project-status: quarantined`, `files: []`; `tinkoff-investments` — 404 и в simple, и в JSON API; все 9 репозиториев организации `archived: true`. Уточнено: последние push — с мая 2024 по ноябрь 2025, а не «июль–ноябрь 2025» | [24][28][29][30] |
| 4 | SDK 1.0.0 (май 2026) — импорты из `t_tech.invest.grpc`, все параметры в `request=XxxRequest(...)`; `t_tech.invest.services` устарели; `from` → `from_` | ✏️ исправлено | 1.0.0 опубликован 2026-05-26; импорты и устаревание подтверждены, `from_` есть в сгенерированных схемах. Но в request обёрнута «большая часть» функций, а не все (BREAKING_CHANGES, sdk-map: «большинство»). Дополнительно: `RetryingClient` и README-примеры всё ещё на legacy `t_tech.invest.Client` | [26][27] |
| 5 | Лимиты: инструменты 200, счета 100, операции 200, отчёты 5, котировки 600, сигналы 100 в минуту; лимит суммарный по методам и счетам; ≤50 rps; персональные лимиты в GetUserTariff | ✅ подтверждено | Совпадает с limits.md (master): также песочница 200, стоп-ордера 50, ордера 100, автоследование 100, getHistory 30; «превышения не будет» при ≤1000 запросов/мин с IP | [2] |
| 6 | Окна GetCandles: день — 6 лет (limit 2400), неделя — 5 лет, месяц — 10 лет, час — 3 месяца, минута — 1 день; секундные — только за месяц; `volume_buy/volume_sell`; архивы history-data/history-trades, 30 файлов/мин с IP | ✅ подтверждено | Значения совпадают с `enum CandleInterval` в marketdata.proto и load_history.md (документация называет их «ориентировочными»); `HistoricCandle.volume_buy/volume_sell` есть; лимит 30 файлов в минуту с IP — head-history-md.mdx | [5][7][9] |
| 7 | Метод `News` (с 1.49) — лента курсором без фильтров и сентимента; на портале не задокументирован | ✏️ исправлено | Состав полей, отсутствие фильтров (только `cursor`, `limit`=1000 по умолчанию) и появление в теге 1.49 подтверждены. Неточность: метод есть в REST/OpenAPI-спецификации 1.51 (`InstrumentsService/News`, «получение актуальных новостей»), отсутствует только в текстовом `methods.mdx` gRPC | [8][22][23] |
| 8 | С 1.51 `GetMarketValues.statistic.interested_count/traded_count`; `Bond.ratings[]` (агентство, рейтинг, прогноз, под наблюдением) | ✅ подтверждено | `MarketValueStatistic` (interested_count, traded_count, time) и `Rating` (agency_name, rating_level, rating_date, forecast, is_under_watch) есть в 1.51 и отсутствуют в 1.48/1.49 | [8][9][23] |
| 9 | ЦФА с ограничениями: Dfas/DfaBy, смарт-счёт `ACCOUNT_TYPE_DFA`, портфель (`total_amount_dfa`), операции; котировки, торговля и позиции — нет | ✅ подтверждено | FAQ инструментов, раздел «Смарт активы», дословно; `DfaResponse` содержит type, YTM, купон, НКД, forecast_yield; `ACCOUNT_TYPE_DFA = 7`. Нюанс: FAQ отдельно разрешает «портфель по смарт счетам», то есть `GetPortfolio` по смарт-счёту работает, а «просмотр позиции» не поддерживается. Есть ли построчные позиции в `GetPortfolio`, проверяется только живым вызовом (открытый вопрос 5) | [14][11] |
| 10 | Официальный MCP: `invest-public-api.tbank.ru/mcp` (+ sandbox), streamable HTTP, Bearer; новости с сентиментом, инвестидеи, прогнозы; июнь 2026 / ~10 сентября 2026; официального списка tools нет | ✏️ исправлено | URL, sandbox, `--transport http` и `Authorization: Bearer` подтверждены официальной инструкцией; функции «сводка новостей… с оценкой рыночного сентимента», «анализ прогнозов инвестдомов», «поиск рыночных аномалий» — на официальной странице; пост T_InvestAPI в Пульсе — 2026-06-10; Finance Magnates — 2026-09-11 («накануне»). Список tools не опубликован (появляется после подключения). Исправлено: Claude Desktop официально поддерживается через `npx mcp-remote`, есть ChatGPT Desktop, а также официальные гайды Hermes Agent (Telegram) и n8n | [33][35][44] |

**Прочие исправления в тексте:** типы банковских счетов (DFA = 7 внутри диапазона 5–11); хронология push архивных GitHub-репозиториев; поведение Sentry в SDK (инициализация только в legacy-клиентах) и то, что `RetryingClient` унаследован от legacy-клиента; формулировка «все методы → большинство методов» в резюме; отметка о документированности `News` в OpenAPI.

**Не удалось проверить в этой сессии:** имена tools официального MCP (`invest_*`, взятые из InvestHelper); точную дату пресс-анонса: в статье от 2026-09-11 сказано только «накануне», отсюда ~10 сентября (лимит веб-поиска в этой сессии исчерпан).
