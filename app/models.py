"""Tabelle del database: rispecchiano 1:1 le entità Room dell'app Android."""
import time

from sqlalchemy import Boolean, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def now_ms() -> int:
    return int(time.time() * 1000)


class Guest(Base):
    __tablename__ = "guests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    full_name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(100), default="Invitato")
    rsvp_status: Mapped[str] = mapped_column(String(20), default="CONFIRMED")
    guests_count: Mapped[int] = mapped_column(Integer, default=1)
    dietary_notes: Mapped[str] = mapped_column(Text, default="")
    contact_info: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    owner_client_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "fullName": self.full_name,
            "category": self.category,
            "rsvpStatus": self.rsvp_status,
            "guestsCount": self.guests_count,
            "dietaryNotes": self.dietary_notes,
            "contactInfo": self.contact_info,
            "updatedAt": self.updated_at,
            "ownerClientId": self.owner_client_id,
        }


class BusBooking(Base):
    __tablename__ = "bus_bookings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    passenger_name: Mapped[str] = mapped_column(String(200))
    seats_count: Mapped[int] = mapped_column(Integer, default=1)
    pickup_stop: Mapped[str] = mapped_column(String(200), default="")
    return_trip_wanted: Mapped[bool] = mapped_column(Boolean, default=True)
    contact_phone: Mapped[str] = mapped_column(String(100), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    booked_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    owner_client_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "passengerName": self.passenger_name,
            "seatsCount": self.seats_count,
            "pickupStop": self.pickup_stop,
            "returnTripWanted": self.return_trip_wanted,
            "contactPhone": self.contact_phone,
            "notes": self.notes,
            "bookedAt": self.booked_at,
            "ownerClientId": self.owner_client_id,
        }


class Wish(Base):
    __tablename__ = "wishes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    author_name: Mapped[str] = mapped_column(String(200))
    target_graduate: Mapped[str] = mapped_column(String(200), default="Tutti i Laureandi")
    message: Mapped[str] = mapped_column(Text)
    emoji_badge: Mapped[str] = mapped_column(String(16), default="🎓")
    heart_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "authorName": self.author_name,
            "targetGraduate": self.target_graduate,
            "message": self.message,
            "emojiBadge": self.emoji_badge,
            "heartCount": self.heart_count,
            "createdAt": self.created_at,
        }


class SharedPhoto(Base):
    __tablename__ = "shared_photos"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    author_name: Mapped[str] = mapped_column(String(200))
    caption: Mapped[str] = mapped_column(Text, default="")
    image_res_id: Mapped[int] = mapped_column(Integer, default=0)
    # Percorso relativo del file caricato (es. /uploads/abc.jpg); vuoto per i segnaposto.
    image_path: Mapped[str] = mapped_column(String(300), default="")
    likes_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)

    def to_dict(self, base_url: str = "") -> dict:
        image_uri = f"{base_url}{self.image_path}" if self.image_path else ""
        return {
            "id": self.id,
            "authorName": self.author_name,
            "caption": self.caption,
            "imageResId": self.image_res_id,
            "imageUri": image_uri,
            "likesCount": self.likes_count,
            "createdAt": self.created_at,
        }


class GiftTarget(Base):
    __tablename__ = "gift_targets"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    specialization: Mapped[str] = mapped_column(String(200), default="")
    role_title: Mapped[str] = mapped_column(String(300), default="")
    gift_title: Mapped[str] = mapped_column(String(300), default="")
    gift_description: Mapped[str] = mapped_column(Text, default="")
    target_amount: Mapped[float] = mapped_column(Float, default=0.0)
    collected_amount: Mapped[float] = mapped_column(Float, default=0.0)
    iban: Mapped[str] = mapped_column(String(64), default="")
    iban_holder: Mapped[str] = mapped_column(String(200), default="")
    satispay_url: Mapped[str] = mapped_column(String(300), default="")
    paypal_me_url: Mapped[str] = mapped_column(String(300), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "specialization": self.specialization,
            "roleTitle": self.role_title,
            "giftTitle": self.gift_title,
            "giftDescription": self.gift_description,
            "targetAmount": self.target_amount,
            "collectedAmount": self.collected_amount,
            "iban": self.iban,
            "ibanHolder": self.iban_holder,
            "satispayUrl": self.satispay_url,
            "paypalMeUrl": self.paypal_me_url,
        }


class GiftContribution(Base):
    __tablename__ = "gift_contributions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    donor_name: Mapped[str] = mapped_column(String(200))
    target_graduate_id: Mapped[str] = mapped_column(String(64))
    target_graduate_name: Mapped[str] = mapped_column(String(200), default="")
    amount: Mapped[float] = mapped_column(Float)
    payment_method: Mapped[str] = mapped_column(String(50), default="IBAN")
    note: Mapped[str] = mapped_column(Text, default="")
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=False)
    contributed_at: Mapped[int] = mapped_column(Integer, default=now_ms)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "donorName": self.donor_name,
            "targetGraduateId": self.target_graduate_id,
            "targetGraduateName": self.target_graduate_name,
            "amount": self.amount,
            "paymentMethod": self.payment_method,
            "note": self.note,
            "isAnonymous": self.is_anonymous,
            "contributedAt": self.contributed_at,
        }


class EventNotification(Base):
    __tablename__ = "event_notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(300))
    message: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(50), default="Organizzazione")
    timestamp: Mapped[int] = mapped_column(Integer, default=now_ms)
    # Se valorizzato la notifica è programmata: invisibile agli invitati e non ancora inviata
    # in push finché lo scheduler non la pubblica (vedi app/scheduler.py).
    scheduled_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def to_dict(self) -> dict:
        # isRead è uno stato del singolo dispositivo: il server lo espone sempre a false.
        return {
            "id": self.id,
            "title": self.title,
            "message": self.message,
            "category": self.category,
            "timestamp": self.timestamp,
            "scheduledAt": self.scheduled_at,
            "isRead": False,
        }


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(Text)
    auth: Mapped[str] = mapped_column(Text)
    client_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class Meta(Base):
    """Coppie chiave/valore (versione dei dati, flag di seed...)."""
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
