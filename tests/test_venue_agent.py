from datetime import timedelta

from app.bus import bus
from tests.helpers import day_at, mk_event, mk_user, mk_venue


def ask(db, **c):
    r = bus.send(db, "test", "VenueAgent", "REQUEST", c)
    db.commit()
    return r


def iso(d):
    return d.isoformat()


def test_slot_clash_with_turnaround_buffer(db):
    v = mk_venue(db)
    mk_event(db, v, start=day_at(10, 10), hours=2)          # 10:00-12:00
    s = day_at(10, 12) + timedelta(minutes=10)               # 12:10 -> inside 30 min buffer
    r = ask(db, action="check_slot", venue_id=v.id, start=iso(s), end=iso(s + timedelta(hours=1)))
    assert r.performative == "REFUSE" and "turnaround" in r.content["reason"]
    assert r.content["alternatives"]
    s2 = day_at(10, 12) + timedelta(minutes=30)              # 12:30 is fine
    r = ask(db, action="check_slot", venue_id=v.id, start=iso(s2), end=iso(s2 + timedelta(hours=1)))
    assert r.performative == "AGREE"


def test_routine_hire_is_auto_confirmed_with_weekend_pricing(db):
    v = mk_venue(db, capacity=100, rate=1000.0)
    sat = day_at(10, 10)
    while sat.weekday() != 5:
        sat += timedelta(days=1)
    r = ask(db, action="hire", venue_id=v.id, start=iso(sat), end=iso(sat + timedelta(hours=2)), guests=20)
    assert r.performative == "AGREE" and r.content["decision"] == "confirmed"
    assert r.content["price"] == 2500.0                      # 1000 x 2h x 1.25 weekend


def test_large_or_long_hire_is_escalated(db):
    v = mk_venue(db, capacity=100)
    wed = day_at(10, 10)
    while wed.weekday() != 2:
        wed += timedelta(days=1)
    r = ask(db, action="hire", venue_id=v.id, start=iso(wed), end=iso(wed + timedelta(hours=6)), guests=90)
    assert r.performative == "INFORM" and r.content["decision"] == "escalated"
    assert "admin" in r.content["reason"]


def test_clashing_hire_gets_counter_proposal_with_alternatives(db):
    v = mk_venue(db, capacity=100, name="Hall A")
    mk_venue(db, capacity=100, name="Hall B")
    mk_event(db, v, start=day_at(10, 10), hours=2)
    s = day_at(10, 10)
    r = ask(db, action="hire", venue_id=v.id, start=iso(s), end=iso(s + timedelta(hours=2)), guests=10)
    assert r.performative == "PROPOSE" and r.content["reason"]
    notes = {a["note"] for a in r.content["alternatives"]}
    assert "same venue, different time" in notes and "different venue, same time" in notes


def test_hire_refused_when_venue_not_for_hire_or_too_many_guests(db):
    closed = mk_venue(db, hire=False, name="Closed")
    s = day_at(10, 10)
    r = ask(db, action="hire", venue_id=closed.id, start=iso(s), end=iso(s + timedelta(hours=2)), guests=5)
    assert r.performative == "REFUSE" and "not open for private hire" in r.content["reason"]
