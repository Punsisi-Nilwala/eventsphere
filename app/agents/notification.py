"""NotificationAgent - goal: tell the right person what affects them.

Rules
  N1  listens to every broadcast INFORM and turns the ones that matter into notifications.
  N2  customer events (booking, refund, waitlist offer, hire decision) go to that customer.
  N3  operational events (hire needs approval, suspicious scan) go to all admins.
  N4  seats_freed and unknown events are ignored.
"""
from ..models import Notification
from .base import incr_state

AGENT = "NotificationAgent"


def _fmt(d) -> str:
    return d.strftime("%d %b %H:%M") if hasattr(d, "strftime") else str(d)


def handle(db, msg):
    if msg.performative != "INFORM":
        return "REFUSE", {"reason": "NotificationAgent only accepts INFORM messages"}
    c = msg.content
    k = c.get("event")
    uid, text, role = c.get("user_id"), None, None
    if k == "booking_confirmed":
        text = f"Booking #{c['booking_id']} confirmed: {c['quantity']} x {c['event_title']} (LKR {c['total']:.2f})."
    elif k == "booking_cancelled":
        text = f"Booking #{c['booking_id']} cancelled. Refund: LKR {c['refund']:.2f} ({c['percent']}%)."
    elif k == "event_cancelled_refund":
        text = f"'{c['event_title']}' was cancelled. Booking #{c['booking_id']} refunded in full (LKR {c['refund']:.2f})."
    elif k == "waitlist_offer":
        text = (f"Good news! {c['quantity']} seat(s) for '{c['event_title']}' are held for you for "
                f"{c['minutes']} minutes. Claim them under My Bookings.")
    elif k == "waitlist_expired":
        text = f"Your waitlist offer for '{c['event_title']}' expired because it was not claimed in time."
    elif k == "waitlist_event_cancelled":
        text = f"'{c['event_title']}' was cancelled, so your waitlist entry was removed."
    elif k == "hire_decision":
        text = f"Venue hire at {c['venue']} ({c['start']}): {c['status']}. {c.get('reason', '')}".strip()
    elif k == "hire_escalated":
        uid, role = None, "admin"
        text = f"Hire request #{c['request_id']} for {c['venue']} needs approval. {c.get('reason', '')}"
    elif k == "suspicious_scan":
        uid, role = None, "admin"
        text = (f"Suspicious: ticket ...{c['code_tail']} for '{c['event_title']}' was scanned again "
                f"{c['repeats']} times after admission (possible shared screenshot).")
    if text is None:
        return None
    db.add(Notification(user_id=uid, role=role, message=text[:500]))
    db.flush()
    incr_state(db, AGENT, "notifications_created")
    return None
