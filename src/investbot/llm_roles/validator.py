"""Валидатор текста LLM (§9.6 G4/G9, §17.3): числа — только через плейсхолдеры, проверка после рендера.

LLM пишет `{{f:ID}}` вместо чисел и дат и `{{i:ID}}` вместо названий бумаг и классов активов; рендерер
подставляет значения, отформатированные кодом. Проверки:

1. `validate_draft` — до рендера: все плейсхолдеры известны; вне плейсхолдеров нет цифр (кроме узкого allowlist:
   «39-ФЗ», «7176-У», «ИИС-3», «T+1», маркер списка в начале строки), числительных словами, римских чисел,
   числовых символов («%», «½», «²»); нет запрещённых формулировок (`compliance_errors`).
2. `validate_rendered` — после рендера (numeric grounding): каждое число итогового текста совпадает с одним из
   отрендеренных фактов числовых единиц, каждая дата — с фактом `unit="date"`. Фрагменты, подставленные из
   `{{i:ID}}` и текстовых фактов, маскируются: цифры в названиях бумаг («ОФЗ 26238», «БО-02», «001Р-05») и в
   готовых формулировках кода не сверяются.

Морфологического анализатора нет (pymorphy3 не используется): числительные ловятся курируемыми списками форм
и основ. Списки пополняются golden-тестами; «один», «оба», «первый», «второй» разрешены как устойчивые обороты.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from investbot.core.models import Fact
from investbot.core.money import round_money

NBSP = " "
MINUS = "−"
SENTINEL = "￼"  # замена плейсхолдера при проверке черновика: не цифра, не буква, не пробел

PH = re.compile(r"\{\{([fi]):([A-Za-z0-9_:.\-]+)\}\}")
DIGIT = re.compile(r"\d")
# Разделитель тысяч — только NBSP (U+00A0) и узкий NBSP (U+202F); обычный пробел соседние числа не склеивает
NUM = re.compile(r"[-−+]?\d{1,3}(?:[  ]\d{3})+(?:[.,]\d+)?|[-−+]?\d+(?:[.,]\d+)?")
# Регуляторные идентификаторы и маркеры списков (§9.6 G4). «Т+1» с кириллической «Т» — тот же идентификатор.
ALLOW = re.compile(r"\b\d{1,3}-ФЗ\b|\b\d{4}-У\b|\bИИС-3\b|\b[TТ]\+1\b|\bНК РФ\b|^\s*\d{1,2}[.)]\s", re.M)
# Даты — отдельный класс токенов. Рендерер пишет десятичные числа только через запятую,
# поэтому «ДД.ММ» и «ДД.ММ.ГГГГ» с точкой — всегда дата, а не число
DATE = re.compile(r"\b\d{1,2}\.\d{1,2}(?:\.\d{4})?\b")
NUMERIC_UNITS = frozenset({"pct", "rub", "bp", "years", "pp", "count"})
MASKED_UNITS = frozenset({"text"})  # готовые формулировки кода: подставляются как есть и в сверку не входят

ROMAN = re.compile(r"\b(?=[IVX]{2,}\b)X{0,3}(?:IX|IV|V?I{0,3})\b|\b[IVX](?=\s+(?:квартал|полугоди|век))")
UNIT_AFTER_PH = re.compile(r"\{\{f:[^{}]+\}\}\s*(?:п\.\s*п\.|б\.\s*п\.|₽|руб\.?(?!\w)|рубл\w*)")
WORD = re.compile(r"[А-Яа-яЁё]+")
FRAGMENT = re.compile(r"\S*\d\S*")

# --- числительные словами -------------------------------------------------------------------------------------

_NUMERAL_FORMS = frozenset({
    # количественные 2–9, сорок, сто и их падежные формы
    "два", "две", "двух", "двум", "двумя",
    "три", "трех", "трем", "тремя",
    "четыре", "четырех", "четырем", "четырьмя",
    "пять", "пяти", "пятью",
    "шесть", "шести", "шестью",
    "семь", "семи", "семью",
    "восемь", "восьми", "восемью", "восьмью",
    "девять", "девяти", "девятью",
    "сорок", "сорока", "сто", "ста", "сотен",
    "двести", "двумстам", "двумястами", "двухстах",
    "триста", "тремстам", "тремястами", "трехстах",
    "четыреста", "четырьмстам", "четырьмястами", "четырехстах",
    "ноль", "нуль", "ноля", "нуля", "нолю", "нулю", "нолем", "нулем", "ноле", "нуле", "нули", "нулей",
    "тыс", "млн", "млрд", "трлн",
    # собирательные
    "двое", "двоих", "двоим", "двоими", "трое", "троих", "троим", "троими",
    "четверо", "четверых", "четверым", "четверыми", "пятеро", "пятерых", "пятерым", "шестеро", "шестерых",
    "семеро", "семерых", "восьмеро", "восьмерых", "девятеро", "девятерых",
    # кратности
    "вдвое", "втрое", "вчетверо", "впятеро", "вшестеро", "всемеро", "ввосьмеро", "вдевятеро", "вдесятеро",
    "дважды", "трижды", "четырежды",
    # доли и половины
    "полтора", "полторы", "полутора", "полгода", "полугода",
    # «процент» как существительное («процентный» разрешено; «проценты» — это выплаты, тоже разрешено)
    "процент", "процента", "проценту", "процентом", "проценте", "процентов", "процентах",
})
_NUMERAL_PREFIXES = (
    "одиннадцат", "двенадцат", "тринадцат", "четырнадцат", "пятнадцат", "шестнадцат", "семнадцат",
    "восемнадцат", "девятнадцат", "двадцат", "тридцат", "сороков", "пятьдесят", "пятидесят", "пятьюдесят",
    "шестьдесят", "шестидесят", "шестьюдесят", "семьдесят", "семидесят", "семьюдесят", "восемьдесят",
    "восьмидесят", "восемьюдесят", "восьмьюдесят", "девяност", "десят", "сотн", "двухсот", "трехсот",
    "четырехсот", "тысяч", "миллион", "миллиард", "триллион",
    "трет", "четверт", "нулев", "половин", "удво",  # третий/треть, четвёртый/четверть, нулевой, удвоить
    "двойк", "тройк", "четверк", "пятерк", "шестерк", "семерк", "восьмерк", "девятк",
    "полсотн", "полтысяч", "полмиллион", "полмиллиард", "полпроцент", "дюжин",
)
_NUMERAL_EXCEPT_PREFIXES = ("третейск",)
_ADJ = r"(?:ый|ий|ая|ое|ые|ого|ой|ому|ым|ом|ых|ыми|ую)"
_NUMERAL_RE = re.compile(
    r"(?:пят|шест|сем|восем|восьм|девят)(?:ь|и|ью)?(?:сот\w*|стам|стами|стах)"  # пятьсот…девятьсот
    rf"|(?:пят|шест|седьм|восьм|девят|сот){_ADJ}"  # пятый…девятый, сотый
    r"|(?:дву|двух|трех|четырех|пяти|шести|семи|восьми|девяти|ста|сто|тысяче)"
    r"(?:летн|лет|месячн|недельн|дневн|кратн|часов|значн|процентн|годичн|годов|минутн)\w*"  # двухлетний
    r"|утро(?:ить|ение|ения|ению|ился|илась|илось|ились|ится|ятся|ит|ят)"
)
# «в-третьих», «в-четвёртых» — маркеры перечисления, а не количество
_DISCOURSE = re.compile(r"\bв-(?:третьих|четв[её]ртых|пятых)\b", re.I)


def _is_numeral(word: str) -> bool:
    w = word.lower().replace("ё", "е")
    if w in _NUMERAL_FORMS:
        return True
    if w.startswith(_NUMERAL_PREFIXES) and not w.startswith(_NUMERAL_EXCEPT_PREFIXES):
        return True
    return _NUMERAL_RE.fullmatch(w) is not None


def numeral_words(text: str) -> list[str]:
    """Числительные словами, кратности, доли и «процент» как существительное («пятнадцать процентов»)."""
    cleaned = _DISCOURSE.sub(" ", text)
    return [m.group() for m in WORD.finditer(cleaned) if _is_numeral(m.group())]


# --- compliance (G9) -------------------------------------------------------------------------------------------

BANNED = re.compile(
    r"гарантир\w*"
    r"|гаранти[яиюей]\w*\s+(?:доход|прибыл)\w*"
    r"|без\s+(?:всякого\s+|какого-либо\s+|какого-то\s+)?риск(?:а|ов)\b"
    r"|точно\s+(?:вырастет|вырастут|подорожает|подорожают|принесет|принесут|окупится|окупятся)"
    r"|(?:срочно|обязательно|немедленно)\s+(?:купи|прода|покупа|продава|приобре)\w*"
    r"|инсайд\w*|беспроигрыш\w*|верняк\w*"
    r"|не\s+может\s+(?:упасть|подешеветь|обесцениться)"
    r"|\bзаработа(?:ете|ешь)\b"
    r"|(?:прибыль|доход|доходность)\s+(?:обеспечен\w*|гарантир\w*)"
    r"|\b(?:купите|покупайте|продайте|продавайте|купи|продай|покупай|продавай|приобретите)\b"
    r"|(?:рекомендуем|советуем)\s+(?:купить|продать|покупать|продавать|приобрести)",
    re.I,
)
_NEGATED_GUARANTEE = re.compile(r"\bне\s+гарантир\w*", re.I)  # «доходность не гарантирована» — допустимо


def compliance_errors(text: str) -> list[str]:
    """Запрещённые формулировки: гарантии, обещания доходности, призывы «купите»/«срочно продайте»."""
    norm = _NEGATED_GUARANTEE.sub(" ", text.replace("ё", "е").replace("Ё", "Е"))
    hits = list(dict.fromkeys(m.group().lower() for m in BANNED.finditer(norm)))
    return [f"запрещённая формулировка: «{h}» — пиши нейтрально: «действие по IPS: …», «рассмотреть …»"
            for h in hits]


# --- форматирование фактов ---------------------------------------------------------------------------------------

def _group(int_digits: str) -> str:
    if len(int_digits) < 4:
        return int_digits
    head = len(int_digits) % 3 or 3
    parts = [int_digits[:head], *(int_digits[i:i + 3] for i in range(head, len(int_digits), 3))]
    return NBSP.join(parts)


def fmt_number(x: Decimal, places: int, *, plus: bool = False) -> str:
    """Decimal → «1 234,5» (NBSP между тысячами, запятая, «−»); минус ноль не пишется."""
    q = round_money(x, places)
    if q == 0:
        q = abs(q)
    sign = MINUS if q < 0 else ("+" if plus and q > 0 else "")
    int_part, _, frac = format(abs(q), "f").partition(".")
    return sign + _group(int_part) + ("," + frac if frac else "")


def _to_decimal(v: Decimal | str | date) -> Decimal | None:
    if isinstance(v, Decimal):
        return v
    if isinstance(v, date):
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _to_date(v: Decimal | str | date) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v)
        except ValueError:
            return None
    return None


def _pct_places(percent: Decimal) -> int:
    return 2 if 0 < abs(round_money(percent, 2)) < 1 else 1


def _years_word(n: Decimal) -> str:
    if n != n.to_integral_value():
        return "года"
    k = int(abs(n))
    if k % 10 == 1 and k % 100 != 11:
        return "год"
    if k % 10 in (2, 3, 4) and k % 100 not in (12, 13, 14):
        return "года"
    return "лет"


def format_fact(f: Fact) -> str:
    """Значение факта так, как его увидит пользователь.

    pct: доля 0.123 → «12,3%» (меньше 1% — два знака: «0,15%»); pp: 0.023 → «+2,3 п.п.»; rub: «1 234 ₽»;
    years: «3,4 года»; bp: «40 б.п.»; count: «3»; date: «16.10.2026»; text — как есть.
    """
    if f.unit == "text":
        return str(f.value)
    if f.unit == "date":
        d = _to_date(f.value)
        return d.strftime("%d.%m.%Y") if d is not None else str(f.value)
    x = _to_decimal(f.value)
    if x is None:
        return str(f.value)
    if f.unit == "pct":
        p = x * 100
        return fmt_number(p, _pct_places(p)) + "%"
    if f.unit == "pp":
        p = x * 100
        return fmt_number(p, _pct_places(p), plus=True) + NBSP + "п.п."
    if f.unit == "rub":
        return fmt_number(x, 0) + NBSP + "₽"
    if f.unit == "bp":
        return fmt_number(x, 0) + NBSP + "б.п."
    if f.unit == "years":
        q = round_money(x, 1)
        places = 0 if q == q.to_integral_value() else 1
        shown = round_money(x, places)
        return fmt_number(x, places) + NBSP + _years_word(shown)
    # count
    places = 0 if x == x.to_integral_value() else 2
    return fmt_number(x, places)


def normalize(s: str) -> str:
    """Канонический вид числа: без разделителей тысяч, ASCII-минус, точка как десятичный знак."""
    s = s.replace(NBSP, "").replace(" ", "").replace(MINUS, "-").replace(",", ".")
    try:
        d = Decimal(s).normalize()
    except InvalidOperation:
        return s
    if d == 0:
        d = abs(d)
    return format(d, "f")  # "1 950,00" → "1950"; "−5,20" → "-5.2"


# --- проверки ----------------------------------------------------------------------------------------------------

def _fragments(text: str, limit: int = 5) -> str:
    found = list(dict.fromkeys(m.group().replace(SENTINEL, "").strip(".,;:!?()«»\"'") for m in FRAGMENT.finditer(text)))
    return ", ".join(f"«{x}»" for x in found[:limit])


def validate_draft(text: str, facts: Mapping[str, Fact], instruments: Collection[str]) -> list[str]:
    """Черновик LLM до рендера. Пустой список — черновик допустим."""
    errs: list[str] = []
    unknown = [m.group() for m in PH.finditer(text)
               if m.group(2) not in (facts if m.group(1) == "f" else instruments)]
    if unknown:
        errs.append("неизвестные плейсхолдеры (их нет в facts/names): " + ", ".join(dict.fromkeys(unknown)))
    stripped = PH.sub(SENTINEL, text)
    if "{" in stripped or "}" in stripped:
        errs.append("фигурные скобки вне плейсхолдеров: формат строго {{f:ID}} или {{i:ID}}")
    if "}}{{" in text:
        errs.append("плейсхолдеры подряд без разделителя: значения склеятся")
    if UNIT_AFTER_PH.search(text):
        errs.append("после {{f:ID}} не нужна единица (п.п., б.п., ₽): значение подставляется вместе с ней")
    residue = ALLOW.sub(" ", stripped)
    if DIGIT.search(residue):
        errs.append(f"цифры вне плейсхолдеров: {_fragments(residue)} — числа и даты только через {{{{f:ID}}}}, "
                    "названия бумаг с цифрами — через {{i:ID}}")
    symbols = sorted({c for c in residue if c in "%‰" or unicodedata.category(c) in ("No", "Nl")})
    if symbols:
        errs.append("числовые символы вне плейсхолдеров: " + ", ".join(f"«{c}»" for c in symbols))
    if romans := [m.group() for m in ROMAN.finditer(residue) if m.group()]:
        errs.append("римские числа вне плейсхолдеров: " + ", ".join(f"«{r}»" for r in dict.fromkeys(romans)))
    if words := numeral_words(residue):
        errs.append("числительные словами вне плейсхолдеров: " + ", ".join(f"«{w}»" for w in dict.fromkeys(words)))
    errs.extend(compliance_errors(text))
    return errs


def render(text: str, facts: Mapping[str, Fact], instruments: Mapping[str, str]) -> tuple[str, list[tuple[int, int]]]:
    """Подставляет значения. Возвращает текст и фрагменты для маскирования: названия ({{i:ID}}) и текстовые факты."""
    out: list[str] = []
    spans: list[tuple[int, int]] = []
    pos = length = 0
    for m in PH.finditer(text):
        chunk = text[pos:m.start()]
        out.append(chunk)
        length += len(chunk)
        kind, ref = m.group(1), m.group(2)
        if kind == "i":
            if ref not in instruments:
                raise ValueError(f"неизвестный плейсхолдер {m.group()}")
            value, masked = instruments[ref], True
        else:
            if ref not in facts:
                raise ValueError(f"неизвестный плейсхолдер {m.group()}")
            value, masked = format_fact(facts[ref]), facts[ref].unit in MASKED_UNITS
        out.append(value)
        if masked and value:
            spans.append((length, length + len(value)))
        length += len(value)
        pos = m.end()
    out.append(text[pos:])
    return "".join(out), spans


def mask(text: str, spans: list[tuple[int, int]]) -> str:
    """Заменяет фрагменты пробелами той же длины: позиции остальных токенов не сдвигаются."""
    chars = list(text)
    for a, b in spans:
        chars[a:b] = " " * (b - a)
    return "".join(chars)


def fact_numbers(facts: Mapping[str, Fact]) -> set[str]:
    """Нормализованные числа из отрендеренных фактов числовых единиц."""
    return {normalize(m.group()) for f in facts.values() if f.unit in NUMERIC_UNITS
            for m in NUM.finditer(format_fact(f))}


def fact_dates(facts: Mapping[str, Fact]) -> set[str]:
    out: set[str] = set()
    for f in facts.values():
        if f.unit == "date" and (d := _to_date(f.value)) is not None:
            out.update(d.strftime(fmt) for fmt in ("%d.%m", "%d.%m.%Y"))
    return out


def validate_rendered(text: str, masked_spans: list[tuple[int, int]], facts: Mapping[str, Fact]) -> list[str]:
    """Второй проход по итоговому тексту (numeric grounding).

    1) masked_spans возвращает рендерер: фрагменты из {{i:ID}} и текстовых фактов в сверку не входят.
    2) Даты сверяются только с фактами unit="date" в обоих форматах («16.10» и «16.10.2026»).
    3) Остальные числа нормализуются и сверяются только с фактами числовых единиц. Дефис сразу после
       значения («17,6%-17,9%») считается знаком диапазона, а не минусом.
    """
    residue = ALLOW.sub(" ", mask(text, masked_spans))
    dates = fact_dates(facts)
    errs = [f"дата без факта: «{m.group()}»" for m in DATE.finditer(residue) if m.group() not in dates]
    residue = DATE.sub(" ", residue)
    numbers = fact_numbers(facts)
    for m in NUM.finditer(residue):
        tok = m.group()
        n = normalize(tok)
        if n in numbers:
            continue
        prev = residue[m.start() - 1] if m.start() > 0 else " "
        if tok[0] == "-" and not prev.isspace() and prev not in "(«\"" and normalize(tok[1:]) in numbers:
            continue
        errs.append(f"число без факта: «{tok}»")
    return errs
