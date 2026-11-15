from datetime import timedelta

from app.config import now
from app.models import Notification
from app.bus import bus
from app.agents.base import get_state
from tests.helpers import book, cancel, mk_event, mk_user, mk_venue, scan, day_at


def _live_ticket(db):
    v = mk_venue(db)
    ev, cat = mk_event(db, v, start=now() + timedelta(hours=1), qty=5)
    u = mk_user(db)
    b = book(db, u, cat)
    from app.models import Ticket
    t = db.query(Ticket).filter_by(booking_id=b.content["booking_id"]).first()
    return ev, cat, u, b, t


def test_codes_are_unique_and_unguessable(db):
    ev, cat, u, b, t = _live_ticket(db)
    b2 = book(db, mk_user(db), cat, qty=3)
    from app.models import Ticket
    codes = [x.code for x in db.query(Ticket).all()]
    assert len(codes) == len(set(codes)) == 4 and all(len(c) >= 20 for c in codes)


def test_double_checkin_is_rejected(db):
    ev, cat, u, b, t = _live_ticket(db)
    assert scan(db, t.code, ev.id).performative == "AGREE"
    r = scan(db, t.code, ev.id)
    assert r.performative == "REFUSE" and "Already used" in r.content["reason"]


def test_wrong_event_and_cancelled_and_unknown_are_rejected(db):
    ev, cat, u, b, t = _live_ticket(db)
    other, _ = mk_event(db, mk_venue(db, name="Other"), start=now() + timedelta(hours=1), title="Other")
    r = scan(db, t.code, other.id)
    assert r.performative == "REFUSE" and "Wrong event" in r.content["reason"]
    assert scan(db, "nonsense", ev.id).content["reason"] == "Unknown ticket code"
    cancel(db, u, b.content["booking_id"])
    assert "cancelled" in scan(db, t.code, ev.id).content["reason"]


def test_checkin_outside_window_is_rejected(db):
    v = mk_venue(db)
    ev, cat = mk_event(db, v, start=day_at(10, 10))
    b = book(db, mk_user(db), cat)
    from app.models import Ticket
    t = db.query(Ticket).filter_by(booking_id=b.content["booking_id"]).first()
    r = scan(db, t.code, ev.id)
    assert r.performative == "REFUSE" and "Check-in opens" in r.content["reason"]


def test_repeated_scans_raise_suspicious_alert_and_persist_belief(db):
    ev, cat, u, b, t = _live_ticket(db)
    for _ in range(3):          # 1 admit + 2 repeats
        scan(db, t.code, ev.id)
    assert get_state(db, "TicketAgent", f"repeat_scans:{t.code}") == 2
    alert = db.query(Notification).filter_by(role="admin").first()
    assert alert and "Suspicious" in alert.message
