from datetime import timedelta

from .config import now
from .db import SessionLocal
from .models import Event, TicketCategory, User, Venue
from .security import hash_password


def seed() -> None:
    db = SessionLocal()
    try:
        if db.query(User).count() > 0:
            return
        db.add_all([
            User(name="Demo Admin", email="admin@eventsphere.test", password_hash=hash_password("Admin@12345"), role="admin"),
            User(name="Demo Staff", email="staff@eventsphere.test", password_hash=hash_password("Staff@12345"), role="staff"),
            User(name="Demo Attendee", email="attendee@eventsphere.test", password_hash=hash_password("Attendee@12345"), role="attendee"),
        ])
        v1 = Venue(name="Main Auditorium", location="Main Campus, Block A", capacity=300, hourly_rate=8000,
                   open_for_hire=False, description="Tiered auditorium with stage and AV.")
        v2 = Venue(name="Garden Pavilion", location="Main Campus, Lawn", capacity=120, hourly_rate=5000,
                   open_for_hire=True, description="Open-air pavilion for receptions and farewells.")
        v3 = Venue(name="Seminar Room 3", location="Science Block, 2nd floor", capacity=40, hourly_rate=2000,
                   open_for_hire=True, description="Small room for club meetings and workshops.")
        db.add_all([v1, v2, v3])
        db.flush()
        base = (now() + timedelta(days=10)).replace(hour=17, minute=0, second=0, microsecond=0)
        specs = [
            ("Campus Rock Night", "Concert", v1, base, 3, [("General", 1500, 200), ("VIP", 4000, 50)]),
            ("Intro to Agent-Based Computing", "Workshop", v3, base + timedelta(days=1, hours=-7), 2, [("Standard", 500, 30)]),
            ("Annual Tech Conference", "Conference", v1, base + timedelta(days=3, hours=-8), 6, [("Student", 1000, 150), ("Professional", 3500, 100)]),
        ]
        for title, cat, venue, start, hrs, cats in specs:
            ev = Event(title=title, category=cat, venue_id=venue.id, start=start, end=start + timedelta(hours=hrs),
                       description=f"{title} - a university event.", status="published")
            for n, p, q in cats:
                ev.categories.append(TicketCategory(name=n, price=p, quantity=q))
            db.add(ev)
        db.commit()
    finally:
        db.close()
