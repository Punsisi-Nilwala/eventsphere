from datetime import timedelta

from app.agents import waitlist
from app.bus import bus
from app.config import now
from app.models import WaitlistEntry
from tests.helpers import book, cancel, mk_event, mk_user, mk_venue


def join(db, user, cat, qty=1):
    r = bus.send(db, "test", "WaitlistAgent", "REQUEST",
                 {"action": "join", "user_id": user.id, "category_id": cat.id, "quantity": qty})
    db.commit()
    return r


def test_freed_seat_is_offered_to_first_in_line_and_held(db):
    ev, cat = mk_event(db, mk_venue(db), qty=1)
    a, b, c = mk_user(db), mk_user(db), mk_user(db)
    bk = book(db, a, cat)
    assert join(db, b, cat).content["position"] == 1
    assert join(db, c, cat).content["position"] == 2
    assert join(db, b, cat).performative == "REFUSE"                 # duplicate
    cancel(db, a, bk.content["booking_id"])                           # INFORM seats_freed -> reactive offer
    eb = db.query(WaitlistEntry).filter_by(user_id=b.id).first()
    ec = db.query(WaitlistEntry).filter_by(user_id=c.id).first()
    db.refresh(cat)
    assert eb.status == "offered" and ec.status == "waiting" and cat.held == 1
    assert book(db, mk_user(db), cat).performative == "REFUSE"       # held seat cannot be sold to others


def test_timer_sweep_expires_unclaimed_offer_and_reoffers(db):
    ev, cat = mk_event(db, mk_venue(db), qty=1)
    a, b, c = mk_user(db), mk_user(db), mk_user(db)
    bk = book(db, a, cat)
    join(db, b, cat)
    join(db, c, cat)
    cancel(db, a, bk.content["booking_id"])
    eb = db.query(WaitlistEntry).filter_by(user_id=b.id).first()
    eb.offer_expires = now() - timedelta(minutes=1)                   # simulate time passing
    db.commit()
    result = waitlist.tick(db)                                        # the agent's own timer
    db.commit()
    ec = db.query(WaitlistEntry).filter_by(user_id=c.id).first()
    db.refresh(eb)
    db.refresh(cat)
    assert result == {"expired": 1, "reoffered": 1}
    assert eb.status == "expired" and ec.status == "offered" and cat.held == 1


def test_claiming_an_offer_books_the_seat(db):
    ev, cat = mk_event(db, mk_venue(db), qty=1)
    a, b = mk_user(db), mk_user(db)
    bk = book(db, a, cat)
    join(db, b, cat)
    cancel(db, a, bk.content["booking_id"])
    e = db.query(WaitlistEntry).filter_by(user_id=b.id).first()
    r = bus.send(db, "test", "WaitlistAgent", "REQUEST", {"action": "claim", "entry_id": e.id, "user_id": b.id})
    db.commit()
    db.refresh(cat)
    db.refresh(e)
    assert r.performative == "AGREE" and e.status == "claimed"
    assert cat.sold == 1 and cat.held == 0


def test_cannot_join_waitlist_when_seats_available(db):
    ev, cat = mk_event(db, mk_venue(db), qty=5)
    r = join(db, mk_user(db), cat)
    assert r.performative == "REFUSE" and "book directly" in r.content["reason"]
