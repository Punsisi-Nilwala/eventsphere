"""TicketAgent - goal: admit each ticket exactly once.

Decision rules
  T1  issue: one unique unguessable code (secrets.token_urlsafe) per ticket.
  T2  scan: unknown code -> REFUSE.
  T3  scan: cancelled ticket -> REFUSE.
  T4  scan: ticket belongs to another event -> REFUSE (wrong event).
  T5  scan: check-in opens 2 hours before start and closes at event end.
  T6  scan: already used -> REFUSE; every repeat attempt is counted in the agent's
      beliefs; from the 2nd repeat an INFORM 'suspicious_scan' is broadcast.
  T7  void: refuse if any ticket of the booking is already used (unless forced).
"""
import secrets
from datetime import timedelta

from ..bus import bus, refuse
from ..config import CHECKIN_OPENS_HOURS, SUSPICIOUS_REPEATS, now
from ..models import Booking, Event, Ticket
from .base import get_state, incr_state, set_state

AGENT = "TicketAgent"


def issue(db, c):
    b = db.get(Booking, c["booking_id"])
    codes = []
    for _ in range(b.quantity):
        code = secrets.token_urlsafe(18)
        db.add(Ticket(booking_id=b.id, event_id=b.event_id, code=code))
        codes.append(code)
    db.flush()
    incr_state(db, AGENT, "tickets_issued", len(codes))
    return "AGREE", {"codes": codes}


def void(db, c):
    tickets = db.query(Ticket).filter(Ticket.booking_id == c["booking_id"]).all()
    if not c.get("force") and any(t.status == "used" for t in tickets):
        return refuse("Some tickets in this booking were already checked in")
    for t in tickets:
        if t.status == "valid":
            t.status = "cancelled"
    db.flush()
    return "AGREE", {"voided": len(tickets)}


def scan(db, c, conv):
    code = (c.get("code") or "").strip()
    t = db.query(Ticket).filter(Ticket.code == code).first()
    incr_state(db, AGENT, "scans_total")
    if t is None:
        return refuse("Unknown ticket code")
    ev = db.get(Event, t.event_id)
    if t.status == "cancelled":
        return refuse("Ticket was cancelled (refunded)")
    if t.event_id != int(c.get("event_id", 0)):
        return refuse(f"Wrong event: this ticket is for '{ev.title}'")
    if t.status == "used":
        repeats = incr_state(db, AGENT, f"repeat_scans:{code}")
        if repeats >= SUSPICIOUS_REPEATS:
            bus.broadcast(db, AGENT, {"event": "suspicious_scan", "event_title": ev.title,
                                      "code_tail": code[-6:], "repeats": repeats}, conv)
        return refuse(f"Already used at {t.used_at.strftime('%H:%M:%S')}", repeats=repeats)
    if ev.status == "cancelled":
        return refuse("Event was cancelled")
    n = now()
    if n < ev.start - timedelta(hours=CHECKIN_OPENS_HOURS):
        return refuse(f"Check-in opens at {(ev.start - timedelta(hours=CHECKIN_OPENS_HOURS)).strftime('%d %b %H:%M')}")
    if n > ev.end:
        return refuse("Event has ended")
    t.status = "used"
    t.used_at = n
    db.flush()
    set_state(db, AGENT, "last_admit", {"code_tail": code[-6:], "at": n.isoformat(timespec="seconds")})
    b = t.booking
    return "AGREE", {"admitted": True, "holder": b.user.name, "category": b.category.name, "booking_id": b.id}


def handle(db, msg):
    c, a = msg.content, msg.content.get("action")
    if a == "issue":
        return issue(db, c)
    if a == "void":
        return void(db, c)
    if a == "scan":
        return scan(db, c, msg.conversation_id)
    return "FAILURE", {"reason": f"TicketAgent does not understand action '{a}'"}
