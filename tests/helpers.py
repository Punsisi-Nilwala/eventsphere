import itertools
from datetime import timedelta

from app.bus import bus
from app.config import now
from app.models import Event, TicketCategory, User, Venue
from app.security import hash_password

_n = itertools.count(1)


def mk_user(db, role="attendee", name=None):
    i = next(_n)
    u = User(name=name or f"User{i}", email=f"u{i}@test.lk", password_hash=hash_password("Passw0rd!"), role=role)
    db.add(u)
    db.commit()
    return u


def mk_venue(db, capacity=100, rate=1000.0, hire=True, name="Hall"):
    v = Venue(name=name, location="Campus", capacity=capacity, hourly_rate=rate, open_for_hire=hire)
    db.add(v)
    db.commit()
    return v


def day_at(days, hour):
    return (now() + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)


def mk_event(db, venue, start=None, hours=2, qty=10, price=500.0, status="published", title="Show"):
    start = start or day_at(10, 10)
    ev = Event(title=title, category="Concert", venue_id=venue.id, start=start,
               end=start + timedelta(hours=hours), status=status)
    cat = TicketCategory(name="General", price=price, quantity=qty)
    ev.categories.append(cat)
    db.add(ev)
    db.commit()
    return ev, cat


def book(db, user, cat, qty=1, conv=None):
    r = bus.send(db, "test", "BookingAgent", "REQUEST",
                 {"action": "book", "user_id": user.id, "category_id": cat.id, "quantity": qty}, conv)
    db.commit()
    return r


def cancel(db, user, booking_id):
    r = bus.send(db, "test", "BookingAgent", "REQUEST",
                 {"action": "cancel", "user_id": user.id, "booking_id": booking_id})
    db.commit()
    return r


def scan(db, code, event_id):
    r = bus.send(db, "test", "TicketAgent", "REQUEST", {"action": "scan", "code": code, "event_id": event_id})
    db.commit()
    return r
