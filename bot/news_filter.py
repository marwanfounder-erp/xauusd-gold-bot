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
    # ── Class-level cache — one Finnhub call per day across all instances ─────
    _last_fetch_date: Optional[date] = None
    _cached_events: list = []

    def __init__(self, config):
        self.config = config

    # ── Fetch ─────────────────────────────────────────────────────────────────

    def _fetch_finnhub(self, from_date: str, to_date: str) -> Optional[list]:
        """
        Returns raw economicCalendar list on success, None on any failure.
        Returning None (not []) lets fetch_news distinguish API failure from
        a genuinely empty calendar day.
        """
        if not self.config.finnhub_api_key:
            logger.warning("FINNHUB_API_KEY not set — news filter disabled")
            return None
        try:
            resp = requests.get(
                FINNHUB_CALENDAR_URL,
                params={
                    "from":  from_date,
                    "to":    to_date,
                    "token": self.config.finnhub_api_key,
                },
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("economicCalendar", [])
        except Exception as e:
            logger.warning(f"Finnhub fetch failed: {e} — allowing trading")
            return None

    def _parse_events(self, raw: list) -> list:
        """Filter and parse raw Finnhub calendar entries into event dicts."""
        events = []
        for e in raw:
            country  = (e.get("country") or "").upper()
            impact   = (e.get("impact")  or "").lower()
            title    = (e.get("event")   or "").lower()
            time_str =  e.get("time")    or ""

            if country not in GOLD_RELEVANT_COUNTRIES:
                continue

            is_sensitive = any(kw in title for kw in HIGH_SENSITIVITY_KEYWORDS)
            if impact not in BLOCK_IMPACTS and not is_sensitive:
                continue

            try:
                event_dt = datetime.strptime(time_str[:19], "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                logger.debug(f"Could not parse event time: {time_str!r}")
                continue

            events.append({
                "title":      e.get("event", ""),
                "country":    country,
                "impact":     impact,
                "event_time": event_dt,
                "actual":     e.get("actual"),
                "estimate":   e.get("estimate"),
                "prev":       e.get("prev"),
            })
        return events

    def fetch_news(self) -> list:
        """
        Returns today's gold-relevant events.
        Hits Finnhub exactly once per UTC day; all subsequent calls return
        the class-level cache.
        """
        today = datetime.utcnow().date()

        # ── Cache hit ──────────────────────────────────────────────────────
        if NewsFilter._last_fetch_date == today:
            logger.info(
                f"News filter status: CACHE HIT — "
                f"Using cached news events ({len(NewsFilter._cached_events)} events)"
            )
            return NewsFilter._cached_events

        # ── Fresh fetch ────────────────────────────────────────────────────
        logger.info("News filter status: FRESH FETCH — Fetching fresh news from Finnhub")
        from_str = today.strftime("%Y-%m-%d")
        to_str   = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        raw = self._fetch_finnhub(from_str, to_str)

        if raw is None:
            # API failed — cache empty list so we don't hammer the API every 60s
            logger.warning(
                "News filter status: API FAILED — "
                "trading with no news filter today"
            )
            NewsFilter._last_fetch_date = today
            NewsFilter._cached_events   = []
            return []

        events = self._parse_events(raw)

        if not events:
            logger.warning(
                "Finnhub returned 0 events — trading with no news filter today"
            )

        NewsFilter._last_fetch_date = today
        NewsFilter._cached_events   = events
        logger.info(
            f"News filter status: FRESH FETCH — "
            f"{len(events)} gold-relevant events cached for {today}"
        )
        return events

    # ── Window helpers ────────────────────────────────────────────────────────

    def _get_window(self) -> tuple[timedelta, timedelta]:
        """Return (before, after) timedeltas based on active mode."""
        if getattr(self.config, "the5ers_mode", False):
            mins = self.config.the5ers_news_block_minutes
            return timedelta(minutes=mins), timedelta(minutes=mins)
        return (
            timedelta(minutes=self.config.news_filter_before_minutes),
            timedelta(minutes=self.config.news_filter_after_minutes),
        )

    def _blocked_by_event(self, now: datetime, event: dict) -> bool:
        before, after = self._get_window()
        event_dt = event["event_time"]
        return event_dt - before <= now <= event_dt + after

    # ── Single entry point for the main loop ──────────────────────────────────

    def check(self, hours_ahead: int = 4) -> tuple[bool, str, list]:
        """
        Call this ONCE per 60s loop cycle.
        Returns (is_blocked, block_msg, upcoming_events).

        - is_blocked    : True if current time falls inside a news window
        - block_msg     : human-readable description of the blocking event
        - upcoming_events: events within the next `hours_ahead` hours (for dashboard)
        """
        events = self.fetch_news()
        logger.info(f"News check: {len(events)} events cached for today")

        now    = datetime.utcnow()
        cutoff = now + timedelta(hours=hours_ahead)

        # Upcoming events for dashboard (computed regardless of blocking)
        upcoming = [e for e in events if now <= e["event_time"] <= cutoff]

        the5ers = getattr(self.config, "the5ers_mode", False)

        # Check if we are currently inside a news blackout window
        for event in events:
            if self._blocked_by_event(now, event):
                if the5ers:
                    block_msg = (
                        f"ORDER BLOCKED — within {self.config.the5ers_news_block_minutes}min "
                        f"of news event: {event['title']}"
                    )
                else:
                    block_msg = (
                        f"{event['impact'].upper()} | {event['title']} "
                        f"@ {event['event_time'].strftime('%H:%M UTC')}"
                    )
                logger.info(f"News block active: {block_msg}")
                return True, block_msg, upcoming

        return False, "", upcoming

    # ── Legacy helpers — kept for api/index.py compatibility ──────────────────

    def is_news_time(self) -> tuple[bool, str]:
        """Thin wrapper used by api/index.py. Hits the class-level cache."""
        now = datetime.utcnow()
        the5ers = getattr(self.config, "the5ers_mode", False)
        try:
            for event in self.fetch_news():
                if self._blocked_by_event(now, event):
                    if the5ers:
                        msg = (
                            f"ORDER BLOCKED — within {self.config.the5ers_news_block_minutes}min "
                            f"of news event: {event['title']}"
                        )
                    else:
                        msg = (
                            f"{event['impact'].upper()} | {event['title']} "
                            f"@ {event['event_time'].strftime('%H:%M UTC')}"
                        )
                    logger.info(f"News block active: {msg}")
                    return True, msg
        except Exception as e:
            logger.warning(f"News check error: {e}")
        return False, ""

    def get_upcoming_events(self, hours_ahead: int = 4) -> list:
        """Thin wrapper used by api/index.py. Hits the class-level cache."""
        now    = datetime.utcnow()
        cutoff = now + timedelta(hours=hours_ahead)
        try:
            return [e for e in self.fetch_news()
                    if now <= e["event_time"] <= cutoff]
        except Exception:
            return []
