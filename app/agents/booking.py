"""BookingAgent - goal: sell tickets fairly without overselling.

Decision rules
  B1  refuse quantity < 1 or > 6 per request.
  B2  refuse unless the event is published.
  B3  refuse once the event has started (sales close at start time).
  B4  refuse if the customer would hold more than 10 active tickets for the event.
  B5  seats are taken with ONE atomic conditional UPDATE
        (sold + held + qty <= quantity); if 0 rows change -> REFUSE (sold out / not enough).
  B6  refund on cancellation: >=72 h before start 100%, 24-72 h 50%, <24 h 0%.
  B7  cancellation refused after the event has started or after check-in of any ticket.
  B8  when an event is cancelled every confirmed booking is refunded 100%.
"""
import secrets

from sqlalchemy import func, update

from ..bus import bus, refuse
from ..config import MAX_PER_REQUEST, MAX_PER_USER_EVENT, now
from ..models import Booking, Event, TicketCategory
from .base import incr_state

AGENT = "BookingAgent"


def refund_policy(hours_before: float, total: float):
    pct = 1.0 if hours_before >= 72 else 0.5 if hours_before >= 24 else 0.0
    return pct, round(total * pct, 2)


def book(db, msg):
    c, conv = msg.content, msg.conversation_id
    uid, qty = c.get("user_id"), int(c.get("quantity", 0))
    release = int(c.get("release_hold", 0))
    cat = db.get(TicketCategory, c.get("category_id"))
    if cat is None:
        return refuse("Unknown ticket category")
    ev = db.get(Event, cat.event_id)
    if qty < 1 or qty > MAX_PER_REQUEST:
        return refuse(f"You can book between 1 and {MAX_PER_REQUEST} tickets per request")
    if ev.status != "published":
        return refuse("This event is not open for booking")
    if now() >= ev.start:
        return refuse("Sales are closed: the event has already started")
    mine = db.query(func.coalesce(func.sum(Booking.quantity), 0)).filter(
        Booking.user_id == uid, Booking.event_id == ev.id, Booking.status == "confirmed").scalar()
    if mine + qty > MAX_PER_USER_EVENT:
        return refuse(f"Limit of {MAX_PER_USER_EVENT} tickets per customer for this event "
                      f"(you already hold {mine})")
    res = db.execute(
        update(TicketCategory)
        .where(TicketCategory.id == cat.id,
               TicketCategory.sold + TicketCategory.held - release + qty <= TicketCategory.quantity,
               TicketCategory.held >= release)
        .values(sold=TicketCategory.sold + qty, held=TicketCategory.held - release)
        .execution_options(synchronize_session=False))
    if res.rowcount != 1:
        db.refresh(cat)
        incr_state(db, AGENT, "oversell_prevented")
        return refuse(f"Only {max(cat.available, 0)} ticket(s) left in '{cat.name}'",
                      left=max(cat.available, 0), waitlist_available=True)
    db.refresh(cat)
    total = round(cat.price * qty, 2)
    ref = "SIM-" + secrets.token_hex(5).upper()  # simulated payment reference
    b = Booking(user_id=uid, event_id=ev.id, category_id=cat.id, quantity=qty, total=total,
                status="confirmed", payment_ref=ref, conversation_id=conv)
    db.add(b)
    db.flush()
    r = bus.send(db, AGENT, "TicketAgent", "REQUEST", {"action": "issue", "booking_id": b.id}, conv)
    if r.performative != "AGREE":
        raise RuntimeError("TicketAgent failed to issue tickets")
    incr_state(db, AGENT, "bookings_confirmed")
    bus.broadcast(db, AGENT, {"event": "booking_confirmed", "user_id": uid, "booking_id": b.id,
                              "event_title": ev.title, "quantity": qty, "total": total}, conv)
    return "AGREE", {"booking_id": b.id, "payment_ref": ref, "total": total}


def cancel(db, msg):
    c, conv = msg.content, msg.conversation_id
    b = db.get(Booking, c.get("booking_id"))
    if b is None or (b.user_id != c.get("user_id") and not c.get("by_admin")):
        return refuse("Booking not found")
    if b.status != "confirmed":
        return refuse("Booking is already cancelled")
    ev = db.get(Event, b.event_id)
    n = now()
    if n >= ev.start:
        return refuse("The event has already started; cancellation is no longer possible")
    r = bus.send(db, AGENT, "TicketAgent", "REQUEST", {"action": "void", "booking_id": b.id}, conv)
    if r.performative != "AGREE":
        return refuse(r.content.get("reason", "Tickets cannot be voided"))
    hours = (ev.start - n).total_seconds() / 3600
    pct, refund = refund_policy(hours, b.total)
    db.execute(update(TicketCategory).where(TicketCategory.id == b.category_id)
               .values(sold=TicketCategory.sold - b.quantity).execution_options(synchronize_session=False))
    b.status, b.refund_amount, b.cancelled_at = "cancelled", refund, n
    db.flush()
    incr_state(db, AGENT, "cancellations")
    bus.broadcast(db, AGENT, {"event": "booking_cancelled", "user_id": b.user_id, "booking_id": b.id,
                              "event_title": ev.title, "refund": refund, "percent": int(pct * 100)}, conv)
    bus.broadcast(db, AGENT, {"event": "seats_freed", "event_id": ev.id,
                              "category_id": b.category_id, "quantity": b.quantity}, conv)
    return "AGREE", {"refund": refund, "percent": int(pct * 100)}


def cancel_event(db, msg):
    c, conv = msg.content, msg.conversation_id
    ev = db.get(Event, c.get("event_id"))
    if ev is None:
        return refuse("Event not found")
    total, count = 0.0, 0
    for b in db.query(Booking).filter(Booking.event_id == ev.id, Booking.status == "confirmed").all():
        bus.send(db, AGENT, "TicketAgent", "REQUEST", {"action": "void", "booking_id": b.id, "force": True}, conv)
        b.status, b.refund_amount, b.cancelled_at = "cancelled", b.total, now()
        total += b.total
        count += 1
        bus.broadcast(db, AGENT, {"event": "event_cancelled_refund", "user_id": b.user_id, "booking_id": b.id,
                                  "event_title": ev.title, "refund": b.total}, conv)
    for cat in ev.categories:
        cat.sold = 0
    db.flush()
    incr_state(db, AGENT, "events_cancelled")
    bus.broadcast(db, AGENT, {"event": "event_cancelled", "event_id": ev.id, "event_title": ev.title}, conv)
    return "AGREE", {"refunded_bookings": count, "total_refunded": round(total, 2)}


def handle(db, msg):
    a = msg.content.get("action")
    if a == "book":
        return book(db, msg)
    if a == "cancel":
        return cancel(db, msg)
    if a == "cancel_event":
        return cancel_event(db, msg)
    return "FAILURE", {"reason": f"BookingAgent does not understand action '{a}'"}
