import os
from datetime import datetime, timedelta, timezone

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./eventsphere.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
HTTPS_ONLY = os.getenv("HTTPS_ONLY", "0") == "1"
SEED_DEMO = os.getenv("SEED_DEMO", "1") == "1"
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")
TZ_OFFSET_MIN = int(os.getenv("TZ_OFFSET_MIN", "330"))  # Asia/Colombo = UTC+5:30
HOLD_MINUTES = int(os.getenv("HOLD_MINUTES", "10"))
SWEEP_SECONDS = int(os.getenv("SWEEP_SECONDS", "30"))

# ---- Business rules used by the agents (documented in docs/REPORT.md) ----
MAX_PER_REQUEST = 6          # BookingAgent B1
MAX_PER_USER_EVENT = 10      # BookingAgent B4
TURNAROUND_MIN = 30          # VenueAgent V2 buffer between activities
HIRE_MIN_HOURS = 1
HIRE_MAX_HOURS = 12
ROUTINE_MAX_HOURS = 4
ROUTINE_MAX_FILL = 0.8
ROUTINE_MIN_LEAD_HOURS = 48
WEEKEND_MULT = 1.25
EVENING_MULT = 1.15          # from 18:00
CHECKIN_OPENS_HOURS = 2      # TicketAgent T5
SUSPICIOUS_REPEATS = 2       # TicketAgent T6


def now() -> datetime:
    """Naive local time (Asia/Colombo by default) so form input and server agree."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=TZ_OFFSET_MIN)
