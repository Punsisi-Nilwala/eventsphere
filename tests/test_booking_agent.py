import threading

from app.bus import bus
from app.config import now
from app.db import SessionLocal
from app.models import Booking, MessageLog, Notification, Ticket
from tests.helpers import book, cancel, day_at, mk_event, mk_user, mk_venue


def test_refuses_more_than_six_tickets(db):
    ev, cat = mk_event(db, mk_venue(db), qty=50)
    r = book(db, mk_user(db), cat, qty=7)
    assert r.performative == "REFUSE" and "between 1 and 6" in r.content["reason"]


def test_never_oversells_sequential(db):
    ev, cat = mk_event(db, mk_venue(db), qty=1)
    a, b = mk_user(db), mk_user(db)
    assert book(db, a, cat).performative == "AGREE"
    r = book(db, b, cat)
    assert r.performative == "REFUSE" and r.content["waitlist_available"] is True
    db.refresh(cat)
    assert cat.sold == 1


def test_never_oversells_concurrent_last_seat(db):
    ev, cat = mk_event(db, mk_venue(db), qty=1)
    users = [mk_user(db) for _ in range(5)]
    results = []

    def worker(uid):
        s = SessionLocal()
        try:
            r = bus.send(s, "t", "BookingAgent", "REQUEST",
                         {"action": "book", "user_id": uid, "category_id": cat.id, "quantity": 1})
            s.commit()
            results.append(r.performative)
        except Exception:
            s.rollback()
            results.append("ERR")
        finally:
            s.close()

    threads = [threading.Thread(target=worker, args=(u.id,)) for u in users]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("AGREE") == 1
    db.refresh(cat)
    assert cat.sold == 1


def test_refund_policy_tiers(db):
    v = mk_venue(db)
    expected = [(day_at(10, 10), 500.0), (now() + __import__("datetime").timedelta(hours=48), 250.0),
                (now() + __import__("datetime").timedelta(hours=10), 0.0)]
    for start, refund in expected:
        ev, cat = mk_event(db, v, start=start, price=500.0, title=f"E{refund}")
        u = mk_user(db)
        b = book(db, u, cat)
        r = cancel(db, u, b.content["booking_id"])
        assert r.performative == "AGREE" and r.content["refund"] == refund
        db.refresh(cat)
        assert cat.sold == 0
        assert all(t.status == "cancelled" for t in db.query(Ticket).filter_by(booking_id=b.content["booking_id"]))


def test_event_cancellation_refunds_everyone_fully(db):
    ev, cat = mk_event(db, mk_venue(db), qty=10, price=300.0)
    users = [mk_user(db) for _ in range(3)]
    for u in users:
        book(db, u, cat, qty=2)
    ev.status = "cancelled"
    db.commit()
    r = bus.send(db, "test", "BookingAgent", "REQUEST", {"action": "cancel_event", "event_id": ev.id})
    db.commit()
    assert r.performative == "AGREE" and r.content["refunded_bookings"] == 3
    assert r.content["total_refunded"] == 1800.0
    assert all(b.refund_amount == b.total for b in db.query(Booking))
    assert db.query(Notification).filter(Notification.message.like("%refunded in full%")).count() == 3


def test_messages_are_traceable_by_conversation_id(db):
    ev, cat = mk_event(db, mk_venue(db))
    book(db, mk_user(db), cat, conv="conv-trace-1")
    rows = db.query(MessageLog).filter_by(conversation_id="conv-trace-1").all()
    pairs = {(m.sender, m.receiver, m.performative) for m in rows}
    assert ("test", "BookingAgent", "REQUEST") in pairs
    assert ("BookingAgent", "TicketAgent", "REQUEST") in pairs
    assert ("TicketAgent", "BookingAgent", "AGREE") in pairs
    assert any(m.receiver == "ALL" for m in rows)


def test_notification_agent_tells_customer(db):
    ev, cat = mk_event(db, mk_venue(db), title="Gala")
    u = mk_user(db)
    book(db, u, cat, qty=2)
    n = db.query(Notification).filter_by(user_id=u.id).first()
    assert n and "Gala" in n.message and "confirmed" in n.message
