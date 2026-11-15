"""VenueAgent - goal: keep every venue calendar free of clashes.

Decision rules
  V1  check_slot: refuse if end <= start or venue unknown.
  V2  check_slot: refuse if the slot overlaps another non-cancelled event or
      confirmed/pending hire at the venue, INCLUDING a 30-minute turnaround buffer.
  H1  hire: refuse if venue is not open for private hire.
  H2  hire: refuse if duration < 1 h or > 12 h.
  H3  hire: refuse if the start is less than 1 hour from now.
  H4  hire: guests must be 1..capacity; otherwise PROPOSE larger venues (or refuse).
  H5  hire: calendar clash -> PROPOSE other times (same venue) + other venues (same time).
  H6  price = hourly_rate x hours; x1.25 on Sat/Sun; x1.15 for time after 18:00.
  H7  routine request (<=4 h, guests <=80% capacity, >=48 h notice) -> AGREE (auto-confirm),
      anything else -> INFORM decision=escalated (admin must approve).
"""
from datetime import datetime, timedelta

from ..bus import refuse
from ..config import (EVENING_MULT, HIRE_MAX_HOURS, HIRE_MIN_HOURS, ROUTINE_MAX_FILL,
                      ROUTINE_MAX_HOURS, ROUTINE_MIN_LEAD_HOURS, TURNAROUND_MIN, WEEKEND_MULT, now)
from ..models import Event, HireRequest, Venue
from .base import incr_state

AGENT = "VenueAgent"


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="minutes")


def busy(db, venue_id, exclude_event=None, exclude_hire=None):
    out = []
    for e in db.query(Event).filter(Event.venue_id == venue_id, Event.status != "cancelled"):
        if e.id != exclude_event:
            out.append((e.start, e.end, f"event '{e.title}'"))
    for h in db.query(HireRequest).filter(HireRequest.venue_id == venue_id,
                                          HireRequest.status.in_(("confirmed", "pending"))):
        if h.id != exclude_hire:
            out.append((h.start, h.end, "a private hire"))
    return out


def clash(slots, start, end):
    buf = timedelta(minutes=TURNAROUND_MIN)
    for s, e, label in slots:
        if start < e + buf and end > s - buf:
            return label
    return None


def multiplier(dt: datetime) -> float:
    m = 1.0
    if dt.weekday() >= 5:
        m *= WEEKEND_MULT
    if dt.hour >= 18:
        m *= EVENING_MULT
    return m


def quote(venue: Venue, start: datetime, end: datetime) -> float:
    total, cur = 0.0, start
    while cur < end:
        step = min(timedelta(minutes=30), end - cur)
        total += venue.hourly_rate * (step.total_seconds() / 3600) * multiplier(cur)
        cur += step
    return round(total, 2)


def alt_slots(db, venue, start, end, limit=3):
    slots, dur, out = busy(db, venue.id), end - start, []
    cands = []
    for k in range(1, 49):
        cands += [start + timedelta(hours=k), start - timedelta(hours=k)]
    for s in cands:
        e = s + dur
        if s < now() + timedelta(hours=1):
            continue
        if s.hour < 8 or e.date() != s.date() or (e.hour, e.minute) > (22, 0):
            continue
        if clash(slots, s, e) is None:
            out.append({"venue_id": venue.id, "venue": venue.name, "start": _iso(s), "end": _iso(e),
                        "price": quote(venue, s, e), "note": "same venue, different time"})
            if len(out) >= limit:
                break
    return out


def alt_venues(db, venue_id, start, end, guests, hire_only=True, limit=3):
    out = []
    q = db.query(Venue).filter(Venue.id != venue_id, Venue.capacity >= max(guests, 1))
    for v in q:
        if hire_only and not v.open_for_hire:
            continue
        if clash(busy(db, v.id), start, end) is None:
            out.append({"venue_id": v.id, "venue": v.name, "start": _iso(start), "end": _iso(end),
                        "price": quote(v, start, end), "note": "different venue, same time"})
            if len(out) >= limit:
                break
    return out


def check_slot(db, c):
    v = db.get(Venue, c.get("venue_id"))
    if v is None:
        return refuse("Venue does not exist")
    s, e = _dt(c["start"]), _dt(c["end"])
    if e <= s:
        return refuse("End time must be after start time")
    label = clash(busy(db, v.id, c.get("exclude_event_id"), c.get("exclude_hire_id")), s, e)
    if label:
        incr_state(db, AGENT, "slots_refused")
        return refuse(f"Venue is not free: clashes with {label} (a {TURNAROUND_MIN}-minute turnaround buffer applies)",
                      alternatives=alt_slots(db, v, s, e))
    incr_state(db, AGENT, "slots_accepted")
    return "AGREE", {"free": True, "venue": v.name}


def hire(db, c):
    v = db.get(Venue, c.get("venue_id"))
    if v is None:
        return refuse("Venue does not exist")
    if not v.open_for_hire:
        incr_state(db, AGENT, "hires_refused")
        return refuse(f"{v.name} is not open for private hire")
    s, e, guests = _dt(c["start"]), _dt(c["end"]), int(c.get("guests", 0))
    hours = (e - s).total_seconds() / 3600
    if hours < HIRE_MIN_HOURS or hours > HIRE_MAX_HOURS:
        incr_state(db, AGENT, "hires_refused")
        return refuse(f"Hire duration must be between {HIRE_MIN_HOURS} and {HIRE_MAX_HOURS} hours")
    if s < now() + timedelta(hours=1):
        incr_state(db, AGENT, "hires_refused")
        return refuse("Start time must be at least 1 hour from now")
    if guests < 1 or guests > v.capacity:
        alts = alt_venues(db, v.id, s, e, guests)
        incr_state(db, AGENT, "hires_countered" if alts else "hires_refused")
        reason = f"Guest count must be between 1 and {v.capacity} for {v.name}"
        return ("PROPOSE", {"reason": reason, "alternatives": alts}) if alts else refuse(reason)
    label = clash(busy(db, v.id), s, e)
    if label:
        alts = alt_slots(db, v, s, e) + alt_venues(db, v.id, s, e, guests)
        reason = f"{v.name} is not free then (clashes with {label})"
        if alts:
            incr_state(db, AGENT, "hires_countered")
            return "PROPOSE", {"reason": reason, "alternatives": alts}
        incr_state(db, AGENT, "hires_refused")
        return refuse(reason)
    price = quote(v, s, e)
    reasons = []
    if hours > ROUTINE_MAX_HOURS:
        reasons.append(f"longer than {ROUTINE_MAX_HOURS} hours")
    if guests > v.capacity * ROUTINE_MAX_FILL:
        reasons.append(f"more than {int(ROUTINE_MAX_FILL * 100)}% of capacity")
    if s < now() + timedelta(hours=ROUTINE_MIN_LEAD_HOURS):
        reasons.append(f"less than {ROUTINE_MIN_LEAD_HOURS} hours notice")
    if reasons:
        incr_state(db, AGENT, "hires_escalated")
        return "INFORM", {"decision": "escalated", "price": price,
                          "reason": "Needs admin approval: " + "; ".join(reasons)}
    incr_state(db, AGENT, "hires_confirmed")
    return "AGREE", {"decision": "confirmed", "price": price, "reason": "Routine request - auto-confirmed"}


def handle(db, msg):
    c = msg.content
    a = c.get("action")
    if a == "check_slot":
        return check_slot(db, c)
    if a == "hire":
        return hire(db, c)
    return "FAILURE", {"reason": f"VenueAgent does not understand action '{a}'"}
