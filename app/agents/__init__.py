from ..bus import bus
from . import booking, notification, ticket, venue, waitlist


def register_all() -> None:
    bus.register("VenueAgent", venue.handle)
    bus.register("BookingAgent", booking.handle)
    bus.register("TicketAgent", ticket.handle)
    bus.register("WaitlistAgent", waitlist.handle, listener=True)       # reacts to seats_freed
    bus.register("NotificationAgent", notification.handle, listener=True)  # listens to everything


register_all()
