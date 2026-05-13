import logging
from datetime import datetime, timedelta, date
from typing import Optional

import requests

logger = logging.getLogger(__name__)

FINNHUB_CALENDAR_URL = "https://finnhub.io/api/v1/calendar/economic"

# USD events move gold the most
GOLD_RELEVANT_COUNTRIES = {"US"}

# Only block on high/medium impact events
BLOCK_IMPACTS = {"high", "medium"}

# Keywords that always block regardless of impact level
HIGH_SENSITIVITY_KEYWORDS = {
    "nfp", "non-farm", "fomc", "fed", "interest rate", "cpi", "inflation",
    "pce", "gdp", "jobs", "employment", "powell", "treasury", "fed funds",
}


class NewsFilter:
    def __init__(self, config):
        self.config = config
        self._cache: list = []
        self._cache_date: Optional[date] = None
        self._cache_fetched: bool = False  # True once fetched for _cache_date, even when 0 events

    # ── Fetch ─────────────────────────────────────────────────────────────────

    def _fetch_finnhub(self, from_date: str, to_date: str) -> list:
        if not self.config.finnhub_api_key:
            logger.warning("FINNHUB_API_KEY not set — news filter disabled")
            return []
        try:
            resp = requests.get(
                FINNHUB_CALENDAR_URL,
                params={
                    "from": from_date,
                    "to": to_date,
                    "token": self.config.finnhub_api_key,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("economicCalendar", [])
        except Exception as e:
            logger.warning(f"Finnhub fetch failed: {e} — allowing trading")
            return []

    def fetch_news(self) -> list:
        today = date.today()
        if self._cache_date == today and self._cache_fetched:
            logger.debug(f"News cache hit: {len(self._cache)} events for {today}")
            return self._cache

        # Fetch today + tomorrow to catch events at day boundaries
        from_str = today.strftime("%Y-%m-%d")
        to_str = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        raw = self._fetch_finnhub(from_str, to_str)

        events = []
        for e in raw:
            country = (e.get("country") or "").upper()
            impact = (e.get("impact") or "").lower()
            title = (e.get("event") or "").lower()
            time_str = e.get("time") or ""          # ISO 8601 e.g. "2026-05-12T12:30:00"

            if country not in GOLD_RELEVANT_COUNTRIES:
                continue

            # Include if impact is high/medium OR title matches sensitive keywords
            is_sensitive = any(kw in title for kw in HIGH_SENSITIVITY_KEYWORDS)
            if impact not in BLOCK_IMPACTS and not is_sensitive:
                continue

            try:
                event_dt = datetime.strptime(time_str[:19], "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                logger.debug(f"Could not parse event time: {time_str}")
                continue

            events.append({
                "title": e.get("event", ""),
                "country": country,
                "impact": impact,
                "event_time": event_dt,
                "actual": e.get("actual"),
                "estimate": e.get("estimate"),
                "prev": e.get("prev"),
            })

        self._cache = events
        self._cache_date = today
        self._cache_fetched = True
        logger.info(f"Finnhub: fresh fetch — {len(events)} gold-relevant events for {today}")
        return events

    # ── Checks ────────────────────────────────────────────────────────────────

    def is_news_time(self) -> tuple[bool, str]:
        now = datetime.utcnow()
        before = timedelta(minutes=self.config.news_filter_before_minutes)
        after = timedelta(minutes=self.config.news_filter_after_minutes)

        try:
            for event in self.fetch_news():
                event_dt = event["event_time"]
                if event_dt - before <= now <= event_dt + after:
                    msg = (
                        f"{event['impact'].upper()} | {event['title']} "
                        f"@ {event_dt.strftime('%H:%M UTC')}"
                    )
                    logger.info(f"News block active: {msg}")
                    return True, msg
        except Exception as e:
            logger.warning(f"News check error: {e}")

        return False, ""

    def get_upcoming_events(self, hours_ahead: int = 4) -> list:
        now = datetime.utcnow()
        cutoff = now + timedelta(hours=hours_ahead)
        upcoming = []
        try:
            for event in self.fetch_news():
                if now <= event["event_time"] <= cutoff:
                    upcoming.append(event)
        except Exception:
            pass
        return upcoming
