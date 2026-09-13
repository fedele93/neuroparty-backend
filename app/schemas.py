"""Schemi Pydantic per la validazione delle richieste (nomi campo = camelCase come su Android)."""
from typing import Literal

from pydantic import BaseModel, Field, field_validator

RsvpStatus = Literal["CONFIRMED", "PENDING", "DECLINED"]


def _clean(value: str) -> str:
    return (value or "").strip()


class GuestIn(BaseModel):
    fullName: str = Field(min_length=1, max_length=200)
    category: str = Field(default="Invitato", max_length=100)
    rsvpStatus: RsvpStatus = "CONFIRMED"
    guestsCount: int = Field(default=1, ge=0, le=50)
    dietaryNotes: str = Field(default="", max_length=500)
    contactInfo: str = Field(default="", max_length=200)

    @field_validator("fullName", "category", "dietaryNotes", "contactInfo")
    @classmethod
    def strip(cls, v: str, info) -> str:
        v = _clean(v)
        if info.field_name == "fullName" and not v:
            raise ValueError("Il nome è obbligatorio")
        return v


class GuestUpdate(BaseModel):
    fullName: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=100)
    rsvpStatus: RsvpStatus | None = None
    guestsCount: int | None = Field(default=None, ge=0, le=50)
    dietaryNotes: str | None = Field(default=None, max_length=500)
    contactInfo: str | None = Field(default=None, max_length=200)


class BookingIn(BaseModel):
    passengerName: str = Field(min_length=1, max_length=200)
    seatsCount: int = Field(default=1, ge=1, le=54)
    pickupStop: str = Field(default="", max_length=200)
    returnTripWanted: bool = True
    contactPhone: str = Field(default="", max_length=100)
    notes: str = Field(default="", max_length=500)

    @field_validator("passengerName", "pickupStop", "contactPhone", "notes")
    @classmethod
    def strip(cls, v: str, info) -> str:
        v = _clean(v)
        if info.field_name == "passengerName" and not v:
            raise ValueError("Il nome del passeggero è obbligatorio")
        return v


class WishIn(BaseModel):
    authorName: str = Field(default="Amico/a", max_length=200)
    targetGraduate: str = Field(default="Tutti i Laureandi", max_length=200)
    message: str = Field(min_length=1, max_length=2000)
    emojiBadge: str = Field(default="🎓", max_length=16)

    @field_validator("authorName")
    @classmethod
    def default_author(cls, v: str) -> str:
        return _clean(v) or "Amico/a"

    @field_validator("message")
    @classmethod
    def strip_message(cls, v: str) -> str:
        v = _clean(v)
        if not v:
            raise ValueError("Il messaggio non può essere vuoto")
        return v


class ContributionIn(BaseModel):
    donorName: str = Field(default="Invitato", max_length=200)
    targetGraduateId: str = Field(min_length=1, max_length=64)
    amount: float = Field(gt=0, le=100000)
    paymentMethod: str = Field(default="IBAN", max_length=50)
    note: str = Field(default="", max_length=1000)
    isAnonymous: bool = False


class NotificationIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    message: str = Field(min_length=1, max_length=2000)
    category: str = Field(default="Organizzazione", max_length=50)

    @field_validator("title", "message")
    @classmethod
    def strip(cls, v: str) -> str:
        v = _clean(v)
        if not v:
            raise ValueError("Campo obbligatorio")
        return v


class PushKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscriptionIn(BaseModel):
    endpoint: str = Field(min_length=10)
    keys: PushKeys
    expirationTime: int | None = None


class PushUnsubscribeIn(BaseModel):
    endpoint: str
