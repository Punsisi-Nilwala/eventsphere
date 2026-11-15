"""WaitlistAgent - goal: pass freed seats to waiting customers.

Decision rules
  W1  join: refuse if seats are available (book directly), if the customer is already
      waiting/offered for that category, or if the event is not open / already started.
  W2  FIFO: when seats are freed (reacts to INFORM 'seats_freed'), offer to the oldest waiting
      entries whose quantity fits; an entry too large to fit is skipped but keeps its place.
  W3  an offer holds the seats (category.held) for HOLD_MINUTES (default 10).
  W4  TIMER: every SWEEP_SECONDS the agent expires unclaimed offers, releases the held seats and
      re-offers them to the next customers - no user action needed (proactive behaviour).
  W5  claim: refused if the offer is not yours / expired; otherwise asks BookingAgent to book,
      releasing the hold.
  W6  event cancelled -> all waiting/offered entries are cancelled and holds released.
"""
from datetime import timedelta

from sqlalchemy import update

from ..bus import bus, new_conversation, refuse
from ..config import HOLD_MINUTES, MAX_PER_REQUEST, now
from ..models import Event, TicketCategory, WaitlistEntry
from .base import incr_state

AGENT = "WaitlistAgent"


def _hold(db, cat_id, qty):
    r = db.execute(update(TicketCategory)
                   .where(TicketCategory.id == cat_id,
                          TicketCategory.sold + TicketCategory.held + qty <= TicketCategory.quantity)
                   .values(held=TicketCategory.held + qty).execution_options(synchronize_session=False))
    return r.rowcount == 1


def _release(db, cat_id, qty):
    db.execute(update(TicketCategory).where(TicketCategory.id == cat_id, TicketCategory.held >= qty)
               .values(held=TicketCategory.held - qty).execution_options(synchronize_session=False))


def process_queue(db, category_id, conv):
    cat = db.get(TicketCategory, category_id)
    db.refresh(cat)
    free, offered = cat.quantity - cat.sold - cat.held, 0
    waiting = (db.query(WaitlistEntry).filter(WaitlistEntry.category_id == category_id,
                                              WaitlistEntry.status == "waiting")
               .order_by(WaitlistEntry.created_at, WaitlistEntry.id).all())
    for e in waiting:
        if e.quantity <= free and _hold(db, category_id, e.quantity):
            e.status = "offered"
            e.offer_expires = now() + timedelta(minutes=HOLD_MINUTES)
            free -= e.quantity
            offered += 1
            incr_state(db, AGENT, "offers_made")
            bus.broadcast(db, AGENT, {"event": "waitlist_offer", "user_id": e.user_id, "entry_id": e.id,
                                      "event_title": e.event.title, "quantity": e.quantity,
                                      "minutes": HOLD_MINUTES}, conv)
    db.flush()
    return offered


def sweep(db, conv=None):
    """Proactive timer behaviour: expire unclaimed offers, then re-offer the seats."""
    conv = conv or new_conversation("sweep")
    expired = db.query(WaitlistEntry).filter(WaitlistEntry.status == "offered",
                                             WaitlistEntry.offer_expires < now()).all()
    cats = set()
    for e in expired:
        _release(db, e.category_id, e.quantity)
        e.status = "expired"
        cats.add(e.category_id)
        incr_state(db, AGENT, "offers_expired")
        bus.broadcast(db, AGENT, {"event": "waitlist_expired", "user_id": e.user_id,
                                  "event_title": e.event.title}, conv)
    db.flush()
    reoffered = sum(process_queue(db, cid, conv) for cid in cats)
    return {"expired": len(expired), "reoffered": reoffered}


def tick(db):
    """Called by the agent's own timer (background task). Silent when there is nothing to do."""
    return sweep(db)


def join(db, c, conv):
    cat = db.get(TicketCategory, c.get("category_id"))
    qty = int(c.get("quantity", 1))
    if cat is None:
        return refuse("Unknown ticket category")
    ev = db.get(Event, cat.event_id)
    if ev.status != "published" or now() >= ev.start:
        return refuse("This event is not open for booking")
    if qty < 1 or qty > MAX_PER_REQUEST:
        return refuse(f"You can wait for between 1 and {MAX_PER_REQUEST} tickets")
    if cat.available >= qty:
        return refuse("Tickets are still available - please book directly")
    dup = db.query(WaitlistEntry).filter(WaitlistEntry.user_id == c["user_id"],
                                         WaitlistEntry.category_id == cat.id,
                                         WaitlistEntry.status.in_(("waiting", "offered"))).first()
    if dup:
        return refuse("You are already on the waitlist for this ticket type")
    e = WaitlistEntry(event_id=ev.id, category_id=cat.id, user_id=c["user_id"], quantity=qty)
    db.add(e)
    db.flush()
    pos = db.query(WaitlistEntry).filter(WaitlistEntry.category_id == cat.id,
                                         WaitlistEntry.status == "waiting",
                                         WaitlistEntry.id <= e.id).count()
    incr_state(db, AGENT, "joined")
    return "AGREE", {"entry_id": e.id, "position": pos}


def claim(db, c, conv):
    e = db.get(WaitlistEntry, c.get("entry_id"))
    if e is None or e.user_id != c.get("user_id"):
        return refuse("Offer not found")
    if e.status != "offered":
        return refuse("This offer is no longer available")
    if e.offer_expires < now():
        return refuse("This offer has expired")
    r = bus.send(db, AGENT, "BookingAgent", "REQUEST",
                 {"action": "book", "user_id": e.user_id, "category_id": e.category_id,
                  "quantity": e.quantity, "release_hold": e.quantity}, conv)
    if r.performative != "AGREE":
        return refuse(r.content.get("reason", "Booking failed"))
    e.status = "claimed"
    db.flush()
    incr_state(db, AGENT, "claimed")
    return "AGREE", {"booking_id": r.content["booking_id"]}


def on_inform(db, msg):
    c, conv = msg.content, msg.conversation_id
    kind = c.get("event")
    if kind == "seats_freed":                      # reactive behaviour
        process_queue(db, c["category_id"], conv)
    elif kind == "event_cancelled":
        entries = db.query(WaitlistEntry).filter(WaitlistEntry.event_id == c["event_id"],
                                                 WaitlistEntry.status.in_(("waiting", "offered"))).all()
        for e in entries:
            e.status = "cancelled"
            bus.broadcast(db, AGENT, {"event": "waitlist_event_cancelled", "user_id": e.user_id,
                                      "event_title": c.get("event_title", "")}, conv)
        for cat in db.query(TicketCategory).filter(TicketCategory.event_id == c["event_id"]):
            cat.held = 0
        db.flush()


def handle(db, msg):
    if msg.performative == "INFORM":
        on_inform(db, msg)
        return None
    c, a = msg.content, msg.content.get("action")
    if a == "join":
        return join(db, c, msg.conversation_id)
    if a == "claim":
        return claim(db, c, msg.conversation_id)
    if a == "sweep":
        return "INFORM", sweep(db, msg.conversation_id)
    return "FAILURE", {"reason": f"WaitlistAgent does not understand action '{a}'"}
