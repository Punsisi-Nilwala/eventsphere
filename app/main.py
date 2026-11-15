import asyncio
import hmac
import io
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import qrcode
import qrcode.image.svg
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ValidationError, field_validator
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import agents  # noqa: F401  (registers the agents on the bus)
from .agents import waitlist as waitlist_agent
from .agents.base import get_state
from .bus import Message, bus, new_conversation
from .config import AGENT_API_KEY, HTTPS_ONLY, SECRET_KEY, SEED_DEMO, SWEEP_SECONDS, now
from .db import Base, SessionLocal, engine, get_db
from .models import (Booking, Event, HireRequest, MessageLog, Notification, Ticket, TicketCategory,
                     User, Venue, WaitlistEntry)
from .security import hash_password, verify_password
from .seed import seed

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
UI = "WebUI"  # the web gateway that turns user actions into agent messages


# ------------------------------------------------------------------ timer (proactive agent)
def run_tick():
    db = SessionLocal()
    try:
        waitlist_agent.tick(db)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


async def sweeper():
    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        await asyncio.to_thread(run_tick)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    if SEED_DEMO:
        seed()
    task = asyncio.create_task(sweeper())
    yield
    task.cancel()


app = FastAPI(title="EventSphere", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", https_only=HTTPS_ONLY)


# ------------------------------------------------------------------ helpers
def current_user(request: Request, db: Session) -> Optional[User]:
    uid = request.session.get("uid")
    return db.get(User, uid) if uid else None


def need(*roles):
    def dep(request: Request, db: Session = Depends(get_db)) -> User:
        u = current_user(request, db)
        if u is None:
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        if roles and u.role not in roles:
            raise HTTPException(status_code=403, detail="You do not have permission to view this page")
        return u
    return dep


def get_csrf(request: Request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(24)
    return request.session["csrf"]


async def verify_csrf(request: Request):
    form = await request.form()
    token = str(form.get("csrf_token", ""))
    if not token or not hmac.compare_digest(token, request.session.get("csrf", "")):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")


def flash(request: Request, category: str, message: str):
    request.session.setdefault("flash", []).append([category, message])


def render(request: Request, db: Session, name: str, status_code: int = 200, **ctx):
    user = current_user(request, db)
    unread = 0
    if user:
        unread = db.query(Notification).filter(Notification.is_read == False,  # noqa: E712
                                               (Notification.user_id == user.id) |
                                               (Notification.role == user.role)).count()
    ctx.update(user=user, csrf=get_csrf(request), flashes=request.session.pop("flash", []),
               now=now(), unread=unread)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def go(url: str):
    return RedirectResponse(url, status_code=303)


def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.strip())


class RegisterData(BaseModel):
    name: str
    email: str
    password: str

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Name is too short")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        v = v.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            raise ValueError("Invalid email address")
        return v

    @field_validator("password")
    @classmethod
    def _pw(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


@app.get("/health")
def health():
    return {"status": "ok"}


# ------------------------------------------------------------------ accounts
@app.get("/register")
def register_page(request: Request, db: Session = Depends(get_db)):
    return render(request, db, "register.html")


@app.post("/register", dependencies=[Depends(verify_csrf)])
def register(request: Request, name: str = Form(""), email: str = Form(""), password: str = Form(""),
             db: Session = Depends(get_db)):
    try:
        d = RegisterData(name=name, email=email, password=password)
    except ValidationError as e:
        flash(request, "danger", "; ".join(x["msg"].replace("Value error, ", "") for x in e.errors()))
        return go("/register")
    if db.query(User).filter(User.email == d.email).first():
        flash(request, "danger", "That email is already registered")
        return go("/register")
    u = User(name=d.name, email=d.email, password_hash=hash_password(d.password), role="attendee")
    db.add(u)
    db.commit()
    request.session.clear()
    request.session["uid"] = u.id
    return go("/")


@app.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    return render(request, db, "login.html")


@app.post("/login", dependencies=[Depends(verify_csrf)])
def login(request: Request, email: str = Form(""), password: str = Form(""), db: Session = Depends(get_db)):
    u = db.query(User).filter(User.email == email.strip().lower()).first()
    if not u or not verify_password(password, u.password_hash):
        flash(request, "danger", "Wrong email or password")
        return go("/login")
    request.session.clear()
    request.session["uid"] = u.id
    return go("/admin" if u.role == "admin" else "/staff/scan" if u.role == "staff" else "/")


@app.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    request.session.clear()
    return go("/")


# ------------------------------------------------------------------ browsing
@app.get("/")
def index(request: Request, q: str = "", category: str = "", venue: str = "", date_from: str = "",
          date_to: str = "", max_price: str = "", db: Session = Depends(get_db)):
    qs = db.query(Event).filter(Event.status == "published", Event.end >= now())
    if q.strip():
        like = f"%{q.strip()}%"
        qs = qs.filter(Event.title.ilike(like) | Event.description.ilike(like))
    if category:
        qs = qs.filter(Event.category == category)
    if venue.isdigit():
        qs = qs.filter(Event.venue_id == int(venue))
    try:
        if date_from:
            qs = qs.filter(Event.start >= parse_dt(date_from))
        if date_to:
            qs = qs.filter(Event.start < parse_dt(date_to) + timedelta(days=1))
        if max_price:
            qs = qs.filter(Event.categories.any(TicketCategory.price <= float(max_price)))
    except ValueError:
        flash(request, "warning", "Ignored an invalid filter value")
    events = qs.order_by(Event.start).all()
    cats = [r[0] for r in db.query(Event.category).filter(Event.status == "published").distinct()]
    return render(request, db, "index.html", events=events, cats=cats, venues=db.query(Venue).all(),
                  f=dict(q=q, category=category, venue=venue, date_from=date_from, date_to=date_to,
                         max_price=max_price))


@app.get("/events/{event_id}")
def event_page(event_id: int, request: Request, db: Session = Depends(get_db)):
    ev = db.get(Event, event_id)
    user = current_user(request, db)
    if ev is None or (ev.status == "draft" and (not user or user.role != "admin")):
        raise HTTPException(404, "Event not found")
    return render(request, db, "event.html", ev=ev)


@app.post("/events/{event_id}/book", dependencies=[Depends(verify_csrf)])
def book(event_id: int, request: Request, category_id: int = Form(...), quantity: int = Form(1),
         user: User = Depends(need()), db: Session = Depends(get_db)):
    r = bus.send(db, UI, "BookingAgent", "REQUEST",
                 {"action": "book", "user_id": user.id, "category_id": category_id, "quantity": quantity})
    if r.performative == "AGREE":
        db.commit()
        flash(request, "success", f"Payment successful (simulated). Reference {r.content['payment_ref']}")
        return go(f"/bookings/{r.content['booking_id']}")
    db.commit()  # keep the message log of the refusal
    flash(request, "danger", r.content.get("reason", "Booking failed"))
    return go(f"/events/{event_id}")


@app.post("/events/{event_id}/waitlist", dependencies=[Depends(verify_csrf)])
def join_waitlist(event_id: int, request: Request, category_id: int = Form(...), quantity: int = Form(1),
                  user: User = Depends(need()), db: Session = Depends(get_db)):
    r = bus.send(db, UI, "WaitlistAgent", "REQUEST",
                 {"action": "join", "user_id": user.id, "category_id": category_id, "quantity": quantity})
    db.commit()
    if r.performative == "AGREE":
        flash(request, "success", f"You are on the waitlist (position {r.content['position']}).")
    else:
        flash(request, "warning", r.content.get("reason", "Could not join waitlist"))
    return go(f"/events/{event_id}")


@app.post("/waitlist/{entry_id}/claim", dependencies=[Depends(verify_csrf)])
def claim(entry_id: int, request: Request, user: User = Depends(need()), db: Session = Depends(get_db)):
    r = bus.send(db, UI, "WaitlistAgent", "REQUEST", {"action": "claim", "entry_id": entry_id, "user_id": user.id})
    db.commit()
    if r.performative == "AGREE":
        flash(request, "success", "Seats claimed - your tickets are ready.")
        return go(f"/bookings/{r.content['booking_id']}")
    flash(request, "danger", r.content.get("reason", "Could not claim"))
    return go("/bookings")


@app.get("/bookings")
def my_bookings(request: Request, user: User = Depends(need()), db: Session = Depends(get_db)):
    bookings = db.query(Booking).filter(Booking.user_id == user.id).order_by(Booking.id.desc()).all()
    entries = db.query(WaitlistEntry).filter(WaitlistEntry.user_id == user.id).order_by(WaitlistEntry.id.desc()).all()
    return render(request, db, "bookings.html", bookings=bookings, entries=entries)


@app.get("/bookings/{booking_id}")
def booking_detail(booking_id: int, request: Request, user: User = Depends(need()), db: Session = Depends(get_db)):
    b = db.get(Booking, booking_id)
    if b is None or (b.user_id != user.id and user.role != "admin"):
        raise HTTPException(404, "Booking not found")
    return render(request, db, "booking.html", b=b)


@app.post("/bookings/{booking_id}/cancel", dependencies=[Depends(verify_csrf)])
def cancel_booking(booking_id: int, request: Request, user: User = Depends(need()), db: Session = Depends(get_db)):
    r = bus.send(db, UI, "BookingAgent", "REQUEST", {"action": "cancel", "booking_id": booking_id, "user_id": user.id})
    db.commit()
    if r.performative == "AGREE":
        flash(request, "success", f"Booking cancelled. Refund: LKR {r.content['refund']:.2f} ({r.content['percent']}%).")
    else:
        flash(request, "danger", r.content.get("reason", "Could not cancel"))
    return go(f"/bookings/{booking_id}")


@app.get("/qr/{code}.svg")
def qr(code: str, db: Session = Depends(get_db)):
    if db.query(Ticket).filter(Ticket.code == code).first() is None:
        raise HTTPException(404)
    img = qrcode.make(code, image_factory=qrcode.image.svg.SvgPathImage, box_size=10)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml")


@app.get("/notifications")
def notifications(request: Request, user: User = Depends(need()), db: Session = Depends(get_db)):
    items = (db.query(Notification).filter((Notification.user_id == user.id) | (Notification.role == user.role))
             .order_by(Notification.id.desc()).limit(100).all())
    page = render(request, db, "notifications.html", items=items)
    for n in items:
        n.is_read = True
    db.commit()
    return page


# ------------------------------------------------------------------ private venue hire
def _hire_ctx(db, user, **extra):
    mine = db.query(HireRequest).filter(HireRequest.user_id == user.id).order_by(HireRequest.id.desc()).all()
    return dict(venues=db.query(Venue).filter(Venue.open_for_hire == True).all(), mine=mine, **extra)  # noqa: E712


@app.get("/hire")
def hire_page(request: Request, venue_id: str = "", start: str = "", end: str = "", guests: str = "",
              purpose: str = "", user: User = Depends(need()), db: Session = Depends(get_db)):
    return render(request, db, "hire.html", **_hire_ctx(
        db, user, form=dict(venue_id=venue_id, start=start, end=end, guests=guests, purpose=purpose), result=None))


@app.post("/hire", dependencies=[Depends(verify_csrf)])
def hire_submit(request: Request, venue_id: int = Form(...), start: str = Form(...), end: str = Form(...),
                guests: int = Form(...), purpose: str = Form(""), user: User = Depends(need()),
                db: Session = Depends(get_db)):
    form = dict(venue_id=venue_id, start=start, end=end, guests=guests, purpose=purpose)
    try:
        s, e = parse_dt(start), parse_dt(end)
    except ValueError:
        flash(request, "danger", "Please enter valid start and end times")
        return go("/hire")
    conv = new_conversation()
    r = bus.send(db, UI, "VenueAgent", "REQUEST", {"action": "hire", "venue_id": venue_id,
                                                  "start": s.isoformat(), "end": e.isoformat(), "guests": guests}, conv)
    venue = db.get(Venue, venue_id)
    c = r.content
    result = {"kind": r.performative, **c}
    if r.performative in ("AGREE", "INFORM") and c.get("decision") in ("confirmed", "escalated"):
        status = "confirmed" if c["decision"] == "confirmed" else "pending"
        h = HireRequest(user_id=user.id, venue_id=venue_id, start=s, end=e, purpose=purpose[:300], guests=guests,
                        price=c["price"], status=status, reason=c.get("reason", ""))
        db.add(h)
        db.flush()
        bus.broadcast(db, UI, {"event": "hire_decision", "user_id": user.id, "venue": venue.name,
                               "start": s.strftime("%d %b %H:%M"), "status": status, "reason": c.get("reason", "")}, conv)
        if status == "pending":
            bus.broadcast(db, UI, {"event": "hire_escalated", "request_id": h.id, "venue": venue.name,
                                   "reason": c.get("reason", "")}, conv)
    db.commit()
    return render(request, db, "hire.html", **_hire_ctx(db, user, form=form, result=result))


# ------------------------------------------------------------------ staff scanner
@app.get("/staff/scan")
def scanner(request: Request, user: User = Depends(need("staff", "admin")), db: Session = Depends(get_db)):
    events = db.query(Event).filter(Event.status == "published", Event.end >= now() - timedelta(hours=6)).order_by(Event.start).all()
    return render(request, db, "scanner.html", events=events)


@app.post("/staff/scan", dependencies=[Depends(verify_csrf)])
def scan(code: str = Form(""), event_id: int = Form(...), user: User = Depends(need("staff", "admin")),
         db: Session = Depends(get_db)):
    r = bus.send(db, UI, "TicketAgent", "REQUEST", {"action": "scan", "code": code, "event_id": event_id,
                                                   "staff_id": user.id})
    db.commit()
    if r.performative == "AGREE":
        return {"ok": True, "message": f"ADMIT - {r.content['holder']} ({r.content['category']})"}
    return JSONResponse({"ok": False, "message": "REJECT - " + r.content.get("reason", "Invalid")})


# ------------------------------------------------------------------ admin: venues
@app.get("/admin")
def dashboard(request: Request, user: User = Depends(need("admin")), db: Session = Depends(get_db)):
    rows, hourly = [], {}
    for ev in db.query(Event).filter(Event.status != "draft").order_by(Event.start).all():
        bookings = db.query(Booking).filter(Booking.event_id == ev.id).all()
        used = db.query(Ticket).filter(Ticket.event_id == ev.id, Ticket.status == "used").all()
        for t in used:
            key = t.used_at.strftime("%d %b %H:00")
            hourly[key] = hourly.get(key, 0) + 1
        rows.append(dict(ev=ev, sold=sum(c.sold for c in ev.categories), capacity=ev.venue.capacity,
                         revenue=round(sum(b.total - b.refund_amount for b in bookings), 2), checkins=len(used)))
    state = {a: {k: get_state(db, a, k) for k in keys} for a, keys in {
        "BookingAgent": ["bookings_confirmed", "cancellations", "oversell_prevented"],
        "WaitlistAgent": ["offers_made", "offers_expired", "claimed"],
        "TicketAgent": ["tickets_issued", "scans_total"],
        "VenueAgent": ["slots_accepted", "slots_refused", "hires_confirmed", "hires_escalated", "hires_countered"]}.items()}
    return render(request, db, "admin_dashboard.html", rows=rows, hourly=hourly, state=state,
                  pending=db.query(HireRequest).filter(HireRequest.status == "pending").count())


@app.get("/admin/venues")
def venues(request: Request, user: User = Depends(need("admin")), db: Session = Depends(get_db)):
    return render(request, db, "admin_venues.html", venues=db.query(Venue).order_by(Venue.id).all())


@app.get("/admin/venues/new")
def venue_new(request: Request, user: User = Depends(need("admin")), db: Session = Depends(get_db)):
    return render(request, db, "venue_form.html", v=None)


@app.get("/admin/venues/{venue_id}/edit")
def venue_edit(venue_id: int, request: Request, user: User = Depends(need("admin")), db: Session = Depends(get_db)):
    v = db.get(Venue, venue_id)
    if v is None:
        raise HTTPException(404)
    return render(request, db, "venue_form.html", v=v)


@app.post("/admin/venues/save", dependencies=[Depends(verify_csrf)])
def venue_save(request: Request, venue_id: str = Form(""), name: str = Form(...), location: str = Form(""),
               capacity: int = Form(...), description: str = Form(""), image_url: str = Form(""),
               hourly_rate: float = Form(0), open_for_hire: str = Form(""),
               user: User = Depends(need("admin")), db: Session = Depends(get_db)):
    if capacity < 1 or hourly_rate < 0:
        flash(request, "danger", "Capacity must be positive and rate cannot be negative")
        return go("/admin/venues")
    v = db.get(Venue, int(venue_id)) if venue_id.isdigit() else Venue()
    v.name, v.location, v.capacity, v.description = name.strip(), location.strip(), capacity, description
    v.image_url, v.hourly_rate, v.open_for_hire = image_url.strip(), hourly_rate, bool(open_for_hire)
    db.add(v)
    db.commit()
    flash(request, "success", "Venue saved")
    return go("/admin/venues")


# ------------------------------------------------------------------ admin: events
@app.get("/admin/events")
def admin_events(request: Request, user: User = Depends(need("admin")), db: Session = Depends(get_db)):
    return render(request, db, "admin_events.html", events=db.query(Event).order_by(Event.start.desc()).all())


@app.get("/admin/events/new")
def event_new(request: Request, user: User = Depends(need("admin")), db: Session = Depends(get_db)):
    return render(request, db, "event_form.html", ev=None, venues=db.query(Venue).all())


@app.get("/admin/events/{event_id}/edit")
def event_edit(event_id: int, request: Request, user: User = Depends(need("admin")), db: Session = Depends(get_db)):
    ev = db.get(Event, event_id)
    if ev is None:
        raise HTTPException(404)
    return render(request, db, "event_form.html", ev=ev, venues=db.query(Venue).all())


@app.post("/admin/events/save", dependencies=[Depends(verify_csrf)])
def event_save(request: Request, event_id: str = Form(""), title: str = Form(...), category: str = Form("Other"),
               venue_id: int = Form(...), start: str = Form(...), end: str = Form(...), description: str = Form(""),
               action: str = Form("draft"), cat_id: List[str] = Form([]), cat_name: List[str] = Form([]),
               cat_price: List[str] = Form([]), cat_qty: List[str] = Form([]),
               user: User = Depends(need("admin")), db: Session = Depends(get_db)):
    back = f"/admin/events/{event_id}/edit" if event_id.isdigit() else "/admin/events/new"
    try:
        s, e = parse_dt(start), parse_dt(end)
        rows = [(i, n.strip(), float(p), int(q)) for i, n, p, q in zip(cat_id, cat_name, cat_price, cat_qty) if n.strip()]
    except ValueError:
        flash(request, "danger", "Invalid date, price or quantity")
        return go(back)
    venue = db.get(Venue, venue_id)
    ev = db.get(Event, int(event_id)) if event_id.isdigit() else None
    if venue is None or e <= s:
        flash(request, "danger", "Choose a venue and make sure the end time is after the start time")
        return go(back)
    if not rows or any(p < 0 or q < 1 for _, _, p, q in rows):
        flash(request, "danger", "Add at least one ticket category with a valid price and quantity")
        return go(back)
    if sum(q for *_, q in rows) > venue.capacity:
        flash(request, "danger", f"Total tickets ({sum(q for *_, q in rows)}) exceed the venue capacity ({venue.capacity})")
        return go(back)
    conv = new_conversation()
    r = bus.send(db, UI, "VenueAgent", "REQUEST", {"action": "check_slot", "venue_id": venue_id, "start": s.isoformat(),
                                                  "end": e.isoformat(), "exclude_event_id": ev.id if ev else None}, conv)
    if r.performative != "AGREE":
        db.commit()
        alts = "; ".join(f"{a['start'].replace('T', ' ')}" for a in r.content.get("alternatives", []))
        flash(request, "danger", r.content["reason"] + (f". Free alternatives: {alts}" if alts else ""))
        return go(back)
    if ev is None:
        ev = Event(status="draft")
        db.add(ev)
    ev.title, ev.category, ev.venue_id, ev.start, ev.end, ev.description = title.strip(), category.strip(), venue_id, s, e, description
    if ev.status != "cancelled" and action == "publish":
        ev.status = "published"
    db.flush()
    existing = {str(c.id): c for c in ev.categories}
    for i, n, p, q in rows:
        c = existing.get(i)
        if c is None:
            ev.categories.append(TicketCategory(name=n, price=p, quantity=q))
        else:
            if q < c.sold + c.held:
                flash(request, "warning", f"'{n}': quantity cannot go below tickets already sold/held ({c.sold + c.held})")
                q = c.sold + c.held
            c.name, c.price, c.quantity = n, p, q
    db.commit()
    flash(request, "success", f"Event saved as {ev.status}")
    return go("/admin/events")


@app.post("/admin/events/{event_id}/cancel", dependencies=[Depends(verify_csrf)])
def event_cancel(event_id: int, request: Request, user: User = Depends(need("admin")), db: Session = Depends(get_db)):
    ev = db.get(Event, event_id)
    if ev is None or ev.status == "cancelled":
        flash(request, "warning", "Event not found or already cancelled")
        return go("/admin/events")
    ev.status = "cancelled"
    db.flush()
    r = bus.send(db, UI, "BookingAgent", "REQUEST", {"action": "cancel_event", "event_id": event_id})
    db.commit()
    flash(request, "success", f"Event cancelled; {r.content.get('refunded_bookings', 0)} booking(s) refunded "
                              f"(LKR {r.content.get('total_refunded', 0):.2f})")
    return go("/admin/events")


@app.post("/admin/events/{event_id}/publish", dependencies=[Depends(verify_csrf)])
def event_publish(event_id: int, request: Request, user: User = Depends(need("admin")), db: Session = Depends(get_db)):
    ev = db.get(Event, event_id)
    if ev and ev.status == "draft":
        ev.status = "published"
        db.commit()
        flash(request, "success", "Event published")
    return go("/admin/events")


# ------------------------------------------------------------------ admin: hire approvals, logs
@app.get("/admin/hire")
def admin_hire(request: Request, user: User = Depends(need("admin")), db: Session = Depends(get_db)):
    reqs = db.query(HireRequest).order_by((HireRequest.status == "pending").desc(), HireRequest.id.desc()).all()
    return render(request, db, "admin_hire.html", reqs=reqs)


@app.post("/admin/hire/{req_id}/decide", dependencies=[Depends(verify_csrf)])
def hire_decide(req_id: int, request: Request, decision: str = Form(...), note: str = Form(""),
                user: User = Depends(need("admin")), db: Session = Depends(get_db)):
    h = db.get(HireRequest, req_id)
    if h is None or h.status != "pending":
        flash(request, "warning", "Request not found or already decided")
        return go("/admin/hire")
    conv = new_conversation()
    if decision == "approve":
        r = bus.send(db, UI, "VenueAgent", "REQUEST", {"action": "check_slot", "venue_id": h.venue_id,
                     "start": h.start.isoformat(), "end": h.end.isoformat(), "exclude_hire_id": h.id}, conv)
        if r.performative != "AGREE":
            db.commit()
            flash(request, "danger", "Cannot approve: " + r.content["reason"])
            return go("/admin/hire")
        h.status, h.reason = "confirmed", note or "Approved by admin"
    else:
        h.status, h.reason = "rejected", note or "Rejected by admin"
    bus.broadcast(db, UI, {"event": "hire_decision", "user_id": h.user_id, "venue": h.venue.name,
                           "start": h.start.strftime("%d %b %H:%M"), "status": h.status, "reason": h.reason}, conv)
    db.commit()
    flash(request, "success", f"Request {h.status}")
    return go("/admin/hire")


@app.get("/admin/messages")
def message_log(request: Request, conv: str = "", user: User = Depends(need("admin")), db: Session = Depends(get_db)):
    q = db.query(MessageLog)
    if conv:
        q = q.filter(MessageLog.conversation_id == conv).order_by(MessageLog.id)
    else:
        q = q.order_by(MessageLog.id.desc()).limit(300)
    return render(request, db, "admin_messages.html", msgs=q.all(), conv=conv)


@app.post("/admin/sweep", dependencies=[Depends(verify_csrf)])
def manual_sweep(request: Request, user: User = Depends(need("admin")), db: Session = Depends(get_db)):
    r = bus.send(db, UI, "WaitlistAgent", "REQUEST", {"action": "sweep"})
    db.commit()
    flash(request, "info", f"Waitlist sweep: {r.content.get('expired', 0)} expired, {r.content.get('reoffered', 0)} re-offered")
    return go("/admin")


# ------------------------------------------------------------------ agents over HTTP (advanced)
@app.post("/agents/{name}/inbox")
def agent_inbox(name: str, msg: Message, request: Request, db: Session = Depends(get_db)):
    """HTTP inbox for an agent. Needs an admin session or the X-Agent-Key header."""
    user = current_user(request, db)
    key_ok = bool(AGENT_API_KEY) and hmac.compare_digest(request.headers.get("x-agent-key", ""), AGENT_API_KEY)
    if not key_ok and not (user and user.role == "admin"):
        raise HTTPException(403, "Admin session or X-Agent-Key required")
    if name not in bus.handlers:
        raise HTTPException(404, f"Unknown agent '{name}'")
    if msg.performative == "INFORM" and msg.receiver == "ALL":
        bus.broadcast(db, msg.sender, msg.content, msg.conversation_id)
        db.commit()
        return {"delivered": "broadcast"}
    reply = bus.send(db, msg.sender, name, msg.performative, msg.content, msg.conversation_id)
    db.commit()
    return reply.model_dump()
