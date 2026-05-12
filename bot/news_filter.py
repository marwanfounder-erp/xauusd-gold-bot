import logging
from datetime import datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Gold-relevant currencies and keywords
GOLD_RELEVANT_CURRENCIES = {"USD", "XAU"}
GOLD_RELEVANT_KEYWORDS = {
    "gold", "fed", "fomc", "nfp", "cpi", "inflation", "interest rate",
    "powell", "jobs", "employment", "gdp", "pce", "treasury",
}
HIGH_IMPACT_ONLY = True


class NewsFilter:
    def __init__(self, config):
        self.config = config
        self._cache: list = []
        self._cache_time: Optional[datetime] = None
        self._cache_ttl = timedelta(minutes=30)

    def fetch_news(self) -> list:
        if (
            self._cache_time
            and datetime.utcnow() - self._cache_time < self._cache_ttl
        ):
            return self._cache

        try:
            url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            events = resp.json()
            gold_events = []
            for e in events:
                currency = e.get("country", "").upper()
                impact = e.get("impact", "").lower()
                title = e.get("title", "").lower()

                if currency not in GOLD_RELEVANT_CURRENCIES:
                    continue
                if HIGH_IMPACT_ONLY and impact not in ("high", "medium"):
                    continue

                gold_events.append({
                    "title": e.get("title", ""),
                    "currency": currency,
                    "impact": impact,
                    "date": e.get("date", ""),
                    "time": e.get("time", ""),
                })

            self._cache = gold_events
            self._cache_time = datetime.utcnow()
            logger.info(f"News fetched: {len(gold_events)} gold-relevant events")
            return gold_events

        except Exception as e:
            logger.warning(f"News fetch failed: {e} — allowing trading")
            return self._cache

    def is_news_time(self) -> tuple[bool, str]:
        now = datetime.utcnow()
        try:
            events = self.fetch_news()
            for event in events:
                try:
                    event_dt_str = f"{event['date']} {event['time']}"
                    event_dt = datetime.strptime(event_dt_str, "%Y-%m-%d %I:%M%p")
                except ValueError:
                    continue

                before = timedelta(minutes=self.config.news_filter_before_minutes)
                after = timedelta(minutes=self.config.news_filter_after_minutes)

                if event_dt - before <= now <= event_dt + after:
                    msg = f"{event['impact'].upper()} news: {event['title']} @ {event_dt}"
                    logger.info(f"News block active: {msg}")
                    return True, msg

        except Exception as e:
            logger.warning(f"News check error: {e}")

        return False, ""

    def get_upcoming_events(self, hours_ahead: int = 4) -> list:
        now = datetime.utcnow()
        upcoming = []
        try:
            for event in self.fetch_news():
                try:
                    event_dt = datetime.strptime(
                        f"{event['date']} {event['time']}", "%Y-%m-%d %I:%M%p"
                    )
                except ValueError:
                    continue
                if now <= event_dt <= now + timedelta(hours=hours_ahead):
                    upcoming.append({**event, "event_time": event_dt})
        except Exception:
            pass
        return upcoming
