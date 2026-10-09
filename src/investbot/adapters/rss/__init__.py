"""RSS/Atom-коллектор новостей СМИ, ЦБ, MOEX и Smart-lab (ARCHITECTURE §5.7, §8.2)."""

from investbot.adapters.rss.collector import (
    CollectorError,
    FetchResult,
    RssCollector,
    map_entry,
    parse_feed,
    update_health,
)

__all__ = ["CollectorError", "FetchResult", "RssCollector", "map_entry", "parse_feed", "update_health"]
