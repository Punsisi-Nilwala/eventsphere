from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import now
from .db import Base


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20), default="attendee")  # admin | staff | attendee


class Venue(Base):
    __tablename__ = "venues"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    location: Mapped[str] = mapped_column(String(200), default="")
    capacity: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str] = mapped_column(String(500), default="")
    hourly_rate: Mapped[float] = mapped_column(Float, default=0.0)
    open_for_hire: Mapped[bool] = mapped_column(Boolean, default=False)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(60), default="Other")
    venue_id: Mapped[int] = mapped_column(ForeignKey("venues.id"))
    start: Mapped[datetime] = mapped_column(DateTime)
    end: Mapped[datetime] = mapped_column(DateTime)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | published | cancelled
    venue = relationship("Venue")
    categories = relationship("TicketCategory", back_populates="event",
                              cascade="all, delete-orphan", order_by="TicketCategory.id")


class TicketCategory(Base):
    __tablename__ = "ticket_categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    name: Mapped[str] = mapped_column(String(80))
    price: Mapped[float] = mapped_column(Float, default=0.0)
    quantity: Mapped[int] = mapped_column(Integer)
    sold: Mapped[int] = mapped_column(Integer, default=0)
    held: Mapped[int] = mapped_column(Integer, default=0)  # seats held for waitlist offers
    event = relationship("Event", back_populates="categories")

    @property
    def available(self) -> int:
        return self.quantity - self.sold - self.held


class Booking(Base):
    __tablename__ = "bookings"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("ticket_categories.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    total: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default="confirmed")  # confirmed | cancelled
    payment_ref: Mapped[str] = mapped_column(String(40))
    refund_amount: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    conversation_id: Mapped[str] = mapped_column(String(40), default="")
    user = relationship("User")
    event = relationship("Event")
    category = relationship("TicketCategory")
    tickets = relationship("Ticket", order_by="Ticket.id", back_populates="booking")


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="valid")  # valid | used | cancelled
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    booking = relationship("Booking", back_populates="tickets")


class WaitlistEntry(Base):
    __tablename__ = "waitlist"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("ticket_categories.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="waiting")  # waiting|offered|claimed|expired|cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    offer_expires: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    event = relationship("Event")
    category = relationship("TicketCategory")


class HireRequest(Base):
    __tablename__ = "hire_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    venue_id: Mapped[int] = mapped_column(ForeignKey("venues.id"))
    start: Mapped[datetime] = mapped_column(DateTime)
    end: Mapped[datetime] = mapped_column(DateTime)
    purpose: Mapped[str] = mapped_column(String(300), default="")
    guests: Mapped[int] = mapped_column(Integer, default=1)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # confirmed | pending | rejected
    reason: Mapped[str] = mapped_column(String(400), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    venue = relationship("Venue")
    user = relationship("User")


class MessageLog(Base):
    __tablename__ = "message_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(40), index=True)
    sender: Mapped[str] = mapped_column(String(60))
    receiver: Mapped[str] = mapped_column(String(60))
    performative: Mapped[str] = mapped_column(String(20))
    content: Mapped[Any] = mapped_column(JSON)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now)


class AgentState(Base):
    """Persistent beliefs of each agent (survive restarts)."""
    __tablename__ = "agent_state"
    __table_args__ = (UniqueConstraint("agent", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str] = mapped_column(String(60))
    key: Mapped[str] = mapped_column(String(200))
    value: Mapped[Any] = mapped_column(JSON, nullable=True)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    role: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # e.g. 'admin' for staff-wide alerts
    message: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
