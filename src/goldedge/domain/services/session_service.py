from datetime import datetime, time
import pytz
from ..models.session import SessionState, TradingSession

class SessionService:
    """
    DST-aware trading session classifier for XAU/USD.

    Sessions are defined in their LOCAL market timezones and converted to UTC
    at runtime — this is the only correct way to be DST-aware.

    DO NOT use hardcoded UTC hour ranges. UTC offsets change when
    Europe/London or America/New_York observes DST transitions.

    Local market open/close times:
        Asia    — 00:00 Tokyo (JST/UTC+9)
        London  — 08:00 Europe/London (BST in summer, GMT in winter)
        New York— 08:00 America/New_York (EDT in summer, EST in winter)

    The resulting UTC windows will shift by 1 hour twice a year.
    """

    def __init__(self):
        self.london_tz = pytz.timezone('Europe/London')
        self.ny_tz = pytz.timezone('America/New_York')
        self.tokyo_tz = pytz.timezone('Asia/Tokyo')
        self.utc_tz = pytz.UTC

    def _london_open_utc(self, dt: datetime) -> time:
        """London session opens at 08:00 local — convert to UTC."""
        local_open = self.london_tz.localize(
            dt.replace(hour=8, minute=0, second=0, microsecond=0)
        )
        return local_open.astimezone(self.utc_tz).time()

    def _london_close_utc(self, dt: datetime) -> time:
        """London session closes at 17:00 local — convert to UTC."""
        local_close = self.london_tz.localize(
            dt.replace(hour=17, minute=0, second=0, microsecond=0)
        )
        return local_close.astimezone(self.utc_tz).time()

    def _ny_open_utc(self, dt: datetime) -> time:
        """NY session opens at 08:00 local — convert to UTC."""
        local_open = self.ny_tz.localize(
            dt.replace(hour=8, minute=0, second=0, microsecond=0)
        )
        return local_open.astimezone(self.utc_tz).time()

    def _ny_close_utc(self, dt: datetime) -> time:
        """NY session closes at 17:00 local — convert to UTC."""
        local_close = self.ny_tz.localize(
            dt.replace(hour=17, minute=0, second=0, microsecond=0)
        )
        return local_close.astimezone(self.utc_tz).time()

    def get_session(self, dt: datetime) -> SessionState:
        """
        Returns the current TradingSession for a given UTC timestamp.
        All boundaries are computed dynamically to account for DST.
        """
        if dt.tzinfo is None:
            dt = pytz.UTC.localize(dt)
        else:
            dt = dt.astimezone(self.utc_tz)

        ny_time = dt.astimezone(self.ny_tz)

        # Weekend closure: Friday 17:00 NY → Sunday 18:00 NY
        if ny_time.weekday() == 4 and ny_time.time() >= time(17, 0):
            return SessionState(TradingSession.CLOSED, False, 0.0)
        if ny_time.weekday() == 5:
            return SessionState(TradingSession.CLOSED, False, 0.0)
        if ny_time.weekday() == 6 and ny_time.time() < time(18, 0):
            return SessionState(TradingSession.CLOSED, False, 0.0)

        # Compute DST-correct UTC boundaries for this specific date
        naive_dt = dt.replace(tzinfo=None)
        london_open = self._london_open_utc(naive_dt)
        london_close = self._london_close_utc(naive_dt)
        ny_open = self._ny_open_utc(naive_dt)
        ny_close = self._ny_close_utc(naive_dt)

        utc_time = dt.time()
        is_london = london_open <= utc_time < london_close
        is_ny = ny_open <= utc_time < ny_close

        # Asia: 00:00 – 09:00 UTC (Tokyo 09:00–18:00 JST) — no DST in Japan
        is_asia = time(0, 0) <= utc_time < time(9, 0)

        if is_london and is_ny:
            return SessionState(TradingSession.OVERLAP, True, 0.0)
        elif is_ny:
            return SessionState(TradingSession.NY, True, 0.0)
        elif is_london:
            return SessionState(TradingSession.LONDON, True, 0.0)
        elif is_asia:
            return SessionState(TradingSession.ASIA, True, 0.0)

        return SessionState(TradingSession.CLOSED, False, 0.0)
