"""Новостной домен без I/O: дедупликация в сюжеты, связывание по алиасам, детерминированный фон (§8.2–8.3)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from investbot.core.models import AssetClass, InstrumentMeta
from investbot.news.background import build_background, buzz_z, daily_story_counts, msk_date
from investbot.news.dedup import StoryIndex, content_sha1, normalize_for_match
from investbot.news.linking import DEFAULT_AMBIGUOUS, AliasIndex, load_aliases
from investbot.news.models import NewsLink, RawNewsItem, StoredNews

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)  # 15:00 по Москве

# ---------------------------------------------------------------- дедупликация

IFX = (
    "Набсовет «Сбербанка» рекомендовал дивиденды 34,84 руб. на акцию",
    "Москва. 15 апреля. ИНТЕРФАКС - Наблюдательный совет Сбербанка рекомендовал годовому собранию акционеров "
    "утвердить дивиденды за 2024 год в размере 34,84 рубля на обыкновенную и привилегированную акцию, "
    "говорится в сообщении банка.",
)
TASS = (
    "Набсовет Сбербанка рекомендовал дивиденды в 34,84 руб. на акцию",
    "МОСКВА, 15 апреля. /ТАСС/. Наблюдательный совет Сбербанка рекомендовал годовому собранию акционеров "
    "утвердить дивиденды за 2024 год в размере 34,84 рубля на обыкновенную и привилегированную акцию, "
    "говорится в сообщении банка.",
)
OTHER = (
    "Набсовет «Газпром нефти» рекомендовал дивиденды 51,96 руб. на акцию",
    "Москва. 15 апреля. ИНТЕРФАКС - Совет директоров Газпром нефти рекомендовал акционерам утвердить "
    "финальные дивиденды за 2024 год в размере 51,96 рубля на акцию, говорится в сообщении компании.",
)

_TEXT = st.text(alphabet=st.sampled_from(list("абвгдеёжзклмнАБВЁЖКabcxyzXYZ0129 .,!?«»\"'-—\t\n­")), max_size=60)


def test_normalize_for_match() -> None:
    assert normalize_for_match("  «Газпром нефть» — ЁЛКИ!  2025… ") == "газпром нефть елки 2025"
    assert normalize_for_match("Сбер­банк, ПАО") == "сбербанк пао"
    assert normalize_for_match("") == ""


@settings(max_examples=60, deadline=None)
@given(_TEXT)
def test_normalize_is_idempotent(s: str) -> None:
    n = normalize_for_match(s)
    assert normalize_for_match(n) == n
    assert n == n.strip() and "  " not in n


@settings(max_examples=60, deadline=None)
@given(_TEXT, _TEXT)
def test_sha1_ignores_case_and_punctuation(title: str, lead: str) -> None:
    assert content_sha1(title.upper() + "!!", f"«{lead}»") == content_sha1(title, lead)


def test_sha1_uses_lead_prefix_and_strips_dateline() -> None:
    body = "а" * 300
    assert content_sha1("T", body + " хвост один") == content_sha1("T", body + " другой хвост")
    assert content_sha1("T", "начало один " + body) != content_sha1("T", "начало два " + body)
    # одинаковый текст с разными агентскими «шапками» — точный дубль
    text = "Наблюдательный совет банка рекомендовал дивиденды."
    assert content_sha1("Заголовок", f"Москва. 15 апреля. ИНТЕРФАКС - {text}") == content_sha1(
        "Заголовок", f"МОСКВА, 15 апреля. /ТАСС/. {text}")
    # «шапка» срезается только в начале и только с датой
    assert content_sha1("З", "Москва и область: " + text) != content_sha1("З", text)


def test_near_duplicates_across_agencies_form_one_story() -> None:
    idx = StoryIndex()
    assert idx.assign(1, *IFX) == 1
    assert idx.assign(2, *TASS) == 1  # перепечатка с другой шапкой и слегка другим заголовком
    assert idx.assign(3, *OTHER) == 3  # та же структура, другая компания — другой сюжет
    assert idx.find_story("Погода в Москве: ожидается дождь и сильный ветер", "") is None
    assert len(idx) == 3 and 2 in idx


def test_exact_duplicate_and_formatting_noise() -> None:
    idx = StoryIndex()
    idx.add(10, 10, *IFX)
    assert idx.find_story(IFX[0].upper(), IFX[1].replace(",", " ,")) == 10


def test_earliest_story_wins() -> None:
    idx = StoryIndex()
    idx.add(7, 7, *IFX)
    idx.add(3, 3, *IFX)  # тот же текст в двух сюжетах (например, после пересборки окна)
    assert idx.find_story(*IFX) == 3
    fuzzy = StoryIndex()
    fuzzy.add(20, 20, *IFX)
    fuzzy.add(4, 4, *TASS)
    assert fuzzy.find_story(IFX[0] + " (обновлено)", IFX[1]) == 4


def test_short_texts_match_only_exactly() -> None:
    idx = StoryIndex()
    idx.add(1, 1, "Сбер +2%")
    assert idx.find_story("СБЕР: +2 %") == 1
    assert idx.find_story("Сбер +3%") is None
    idx.add(2, 2, "Сбербанк повысил ставки по вкладам", "")
    assert idx.find_story("Сбербанк повысил ставки") is None


def test_index_is_deterministic_and_removable() -> None:
    texts = [IFX, TASS, OTHER, ("Набсовет Газпром нефти рекомендовал дивиденды в 51,96 руб.", OTHER[1])]

    def run() -> list[int]:
        idx = StoryIndex()
        return [idx.assign(i + 1, *t) for i, t in enumerate(texts)]

    assert run() == run() == [1, 1, 3, 3]
    idx = StoryIndex()
    idx.add(1, 1, *IFX)
    with pytest.raises(ValueError, match="уже в индексе"):
        idx.add(1, 1, *IFX)
    idx.remove(1)
    idx.remove(1)  # повторное снятие — без ошибки
    assert idx.find_story(*IFX) is None and idx.find_story(*TASS) is None


# ---------------------------------------------------------------- связывание

E, B, F = AssetClass.EQUITY, AssetClass.BOND, AssetClass.FUND
INSTRUMENTS = [
    InstrumentMeta(ticker="SBER", name="Сбербанк России", asset_class=E, isin="RU0009029540"),
    InstrumentMeta(ticker="SBERP", name="Сбербанк России - привилегированные акции", asset_class=E,
                   isin="RU0009029557"),
    InstrumentMeta(ticker="GAZP", name="ПАО «Газпром»", asset_class=E, isin="RU0007661625"),
    InstrumentMeta(ticker="SIBN", name="Газпром нефть", asset_class=E),
    InstrumentMeta(ticker="GMKN", name="ГМК Норильский никель", asset_class=E),
    InstrumentMeta(ticker="ROSN", name="НК Роснефть", asset_class=E),
    InstrumentMeta(ticker="MGNT", name="Магнит", asset_class=E),
    InstrumentMeta(ticker="AFKS", name="АФК Система", asset_class=E),
    InstrumentMeta(ticker="IRAO", name="Интер РАО", asset_class=E),
    InstrumentMeta(ticker="PIKK", name="ПИК-специализированный застройщик", asset_class=E),
    InstrumentMeta(ticker="MTSS", name="МТС", asset_class=E),
    InstrumentMeta(ticker="MBNK", name="МТС-Банк", asset_class=E),
    InstrumentMeta(ticker="AFLT", name="Аэрофлот - Российские авиалинии", asset_class=E),
    InstrumentMeta(ticker="DATA", name="Группа Аренадата", asset_class=E),
    InstrumentMeta(ticker="RU000A105TU7", name="Сбербанк ПАО БО-001Р-SBER44", asset_class=B, isin="RU000A105TU7"),
    InstrumentMeta(ticker="SU26238RMFS4", name="ОФЗ 26238", asset_class=B, isin="RU000A1038V6", is_government=True),
    InstrumentMeta(ticker="TGLD", name="Золото.Биржевой", asset_class=F),
]
INDEX = AliasIndex.build(INSTRUMENTS)


def news(title: str, lead: str = "", tickers: tuple[str, ...] = ()) -> RawNewsItem:
    return RawNewsItem(source="interfax", external_id=title, url=None, title=title, lead=lead, published_at=NOW,
                       fetched_at=NOW, instrument_tickers=tickers)


def linked(title: str, lead: str = "", index: AliasIndex = INDEX) -> dict[str, str]:
    return {lk.ticker: lk.method for lk in index.link(news(title, lead))}


def test_ticker_is_whole_uppercase_token() -> None:
    assert linked("SBER обновил максимум") == {"SBER": "alias_ticker"}
    assert linked("Акции #SBER и $GAZP растут") == {"SBER": "alias_ticker", "GAZP": "alias_ticker"}
    assert linked("SBERBANK и sber не тикеры") == {}
    assert linked("SBERP дороже") == {"SBERP": "alias_ticker"}  # не SBER


def test_ticker_stoplist() -> None:
    assert linked("DATA и MOEX:SBER") == {"SBER": "alias_ticker"}
    assert linked("Аренадаты отчиталась") == {"DATA": "alias_name"}  # по названию — можно


def test_isin_any_case() -> None:
    assert linked("Минфин разместил выпуск", "ISIN RU000A1038V6, спрос высокий") == {"SU26238RMFS4": "alias_isin"}
    assert linked("ru000a1038v6") == {"SU26238RMFS4": "alias_isin"}
    assert linked("Выпуск ОФЗ 26238 доразмещён") == {"SU26238RMFS4": "alias_name"}
    assert linked("Минфин разместил ОФЗ") == {}


def test_name_with_case_endings() -> None:
    assert linked("Акции Газпрома выросли") == {"GAZP": "alias_name"}
    assert linked("Сбербанку разрешили") == {"SBER": "alias_name", "SBERP": "alias_name",
                                              "RU000A105TU7": "alias_name"}  # облигация эмитента
    assert linked("Капитализация «Норильского никеля»") == {"GMKN": "alias_name"}
    assert linked("Роснефти и Аэрофлоту") == {"ROSN": "alias_name", "AFLT": "alias_name"}
    assert linked("Газпромбанк выпустил бонды") == {}  # больше 3 букв окончания — другое слово


def test_longest_match_wins() -> None:
    assert linked("Дивиденды «Газпром нефти»") == {"SIBN": "alias_name"}
    assert linked("МТС-Банка и МТС") == {"MBNK": "alias_name", "MTSS": "alias_name"}
    assert linked("МТС-Банк") == {"MBNK": "alias_name"}


def test_case_rules_block_common_words() -> None:
    assert linked("Интерес инвесторов к рынку") == {}
    assert linked("интер рао") == {}
    assert linked("Интер РАО отчиталась") == {"IRAO": "alias_name"}
    assert linked("Пик продаж пришёлся на март") == {}  # «ПИК» — только капсом
    assert linked("ПИК увеличил продажи") == {"PIKK": "alias_name"}
    assert linked("Золото дорожает") == {}  # у фондов нет алиасов по названию


def test_ambiguous_names_not_linked() -> None:
    assert linked("Магнит открыл магазины", "Ритейлер «Магнит» сообщил") == {}
    assert linked("MGNT: Магнит открыл магазины") == {"MGNT": "alias_ticker"}
    assert linked("Система управления") == {}
    assert linked("АФК «Система» продала актив") == {"AFKS": "alias_name"}  # фраза однозначна
    assert INDEX.name_aliases("MGNT") == []
    relaxed = AliasIndex.build(INSTRUMENTS, ambiguous=())
    assert linked("Магнит открыл магазины", index=relaxed) == {"MGNT": "alias_name"}


def test_head_word_only_when_unique_and_descriptive() -> None:
    assert "аэрофлот" in INDEX.name_aliases("AFLT")
    assert INDEX.name_aliases("SIBN") == ["газпром нефть"]  # «Газпром» — другая компания
    assert INDEX.name_aliases("SBER") == ["сбербанк"]


def test_api_links_kept_and_strongest_method_wins() -> None:
    item = news("Газпром и SBER: Сбербанк", tickers=("GAZP", "NEWCO"))
    links = INDEX.link(item)
    assert links == [
        NewsLink(ticker="GAZP", method="api", confidence=Decimal(1)),
        NewsLink(ticker="NEWCO", method="api", confidence=Decimal(1)),  # нет в справочнике — всё равно связь
        NewsLink(ticker="SBER", method="alias_ticker", confidence=Decimal("0.9")),
        NewsLink(ticker="RU000A105TU7", method="alias_name", confidence=Decimal("0.7")),
        NewsLink(ticker="SBERP", method="alias_name", confidence=Decimal("0.7")),
    ]
    isin_and_ticker = INDEX.link(news("RU000A105TU7"))
    assert [(lk.ticker, lk.method) for lk in isin_and_ticker] == [("RU000A105TU7", "alias_isin")]


def test_load_aliases_example_file() -> None:
    extra, ambiguous = load_aliases(ROOT / "config" / "aliases.example.yaml")
    assert "Норникель" in extra["GMKN"]
    assert ambiguous >= DEFAULT_AMBIGUOUS and "Черкизово" in ambiguous
    idx = AliasIndex.build(INSTRUMENTS, extra, ambiguous)
    assert linked("Акции Норникеля", index=idx) == {"GMKN": "alias_name"}
    assert linked("Т-Банк повысил ставки", index=idx) == {"T": "alias_name"}
    assert linked("т банк", index=idx) == {}


def test_load_aliases_missing_and_invalid(tmp_path: Path) -> None:
    assert load_aliases(tmp_path / "nope.yaml") == ({}, DEFAULT_AMBIGUOUS)
    p = tmp_path / "a.yaml"
    p.write_text("aliases:\n  SBER: Сбер\nambiguous: [Мечел]\nambiguous_defaults: false\n", encoding="utf-8")
    assert load_aliases(p) == ({"SBER": ("Сбер",)}, frozenset({"Мечел"}))
    p.write_text("aliases:\n  ON: [Он]\n", encoding="utf-8")  # YAML 1.1: ON → True
    with pytest.raises(ValueError, match="строкой"):
        load_aliases(p)
    p.write_text("alias: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="неизвестные ключи"):
        load_aliases(p)


# ---------------------------------------------------------------- новостной фон


def stored(nid: int, story: int, at: datetime, tickers: tuple[str, ...] = ("SBER",), source: str = "interfax",
           title: str | None = None, published: bool = True) -> StoredNews:
    return StoredNews(
        id=nid, source=source, external_id=str(nid), url=f"https://{source}.example/{nid}", title=title or f"N{nid}",
        published_at=at if published else None, first_seen_at=at if not published else at + timedelta(minutes=5),
        sha1=str(nid), story_id=story,
        links=tuple(NewsLink(ticker=t, method="alias_name", confidence=Decimal("0.7")) for t in tickers),
    )


def test_daily_counts_by_moscow_day() -> None:
    late = datetime(2026, 10, 8, 20, 30, tzinfo=UTC)  # 23:30 МСК 8 октября
    after_midnight = datetime(2026, 10, 8, 21, 30, tzinfo=UTC)  # 00:30 МСК 9 октября
    items = [
        stored(1, 1, late),
        stored(2, 1, late + timedelta(minutes=10), source="tass"),  # дубль того же сюжета
        stored(3, 3, after_midnight, published=False),  # без published_at — по first_seen_at
        stored(4, 4, after_midnight, tickers=("GAZP",)),
        stored(5, 5, datetime(2026, 10, 6, 10, tzinfo=UTC)),
    ]
    assert msk_date(after_midnight) == date(2026, 10, 9)
    assert daily_story_counts(items, "SBER", date(2026, 10, 9), 4) == [1, 0, 1, 1]
    assert daily_story_counts(items, "GAZP", date(2026, 10, 9), 1) == [1]
    assert daily_story_counts(items, "SBER", date(2026, 10, 9), 0) == []


def test_buzz_z() -> None:
    assert buzz_z([1] * 19, 5) is None  # меньше 20 наблюдений
    assert buzz_z([0] * 30, 0) == Decimal("0.00")
    assert buzz_z([0] * 30, 3) == Decimal("3.00")  # нулевой разброс: знаменатель не меньше 1 сюжета
    calm = [2, 3] * 15
    z = buzz_z(calm, 12)
    assert z is not None and z > Decimal(5)
    assert buzz_z(calm, 2) == buzz_z(calm, 3) * -1  # симметрично вокруг среднего 2,5


@settings(max_examples=50, deadline=None)
@given(st.lists(st.integers(0, 30), min_size=20, max_size=60), st.integers(0, 40))
def test_buzz_z_monotone(history: list[int], current: int) -> None:
    lo, hi = buzz_z(history, current), buzz_z(history, current + 1)
    assert lo is not None and hi is not None and hi > lo


def test_build_background_windows_and_headlines() -> None:
    items = [
        stored(1, 1, NOW - timedelta(hours=2), title="Сбербанк: прибыль выросла"),
        stored(2, 1, NOW - timedelta(hours=1), source="tass", title="Сбербанк увеличил прибыль"),
        stored(3, 1, NOW - timedelta(minutes=30), tickers=(), source="rbc"),  # дубль без связи — в story_size
        stored(4, 4, NOW - timedelta(days=3), source="tass", title="Сбербанк снизил ставки"),
        stored(5, 5, NOW - timedelta(days=10)),  # вне 7 суток
        stored(6, 6, NOW + timedelta(hours=1)),  # из будущего — не видна
        stored(7, 7, NOW - timedelta(hours=3), tickers=("GAZP",)),
    ]
    bg = build_background("SBER", items, NOW)
    assert (bg.ticker, bg.stories_24h, bg.stories_7d) == ("SBER", 1, 2)
    assert [(h.title, h.source, h.story_size) for h in bg.headlines] == [
        ("Сбербанк: прибыль выросла", "interfax", 3),  # самая ранняя новость сюжета, новые сверху
        ("Сбербанк снизил ставки", "tass", 1),
    ]
    assert bg.headlines[0].at == NOW - timedelta(hours=2)
    assert bg.buzz_z is None  # архиву 10 дней — меньше 20 наблюдений
    assert len(build_background("SBER", items, NOW, max_headlines=1).headlines) == 1


def test_build_background_buzz_spike_and_history_start() -> None:
    daily = [stored(100 + d, 100 + d, NOW - timedelta(days=d, hours=3)) for d in range(1, 31)]  # 1 сюжет в день
    spike = [stored(k, k, NOW - timedelta(hours=k)) for k in range(1, 7)]  # 6 сюжетов за сутки
    bg = build_background("SBER", daily + spike, NOW)
    assert bg.stories_24h == 6
    assert bg.buzz_z == Decimal("5.00")  # история 30 дней по 1 сюжету, разброс 0 → знаменатель 1
    # тот же архив, если сбор шёл 60 дней (раньше новостей по бумаге не было): нули — тоже наблюдения
    longer = build_background("SBER", daily + spike, NOW, history_start=date(2026, 8, 10))
    assert longer.buzz_z is not None and longer.buzz_z > bg.buzz_z
    # история меньше 20 дней — None
    assert build_background("SBER", daily[:10] + spike, NOW).buzz_z is None
