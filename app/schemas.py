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


MenuChoice = Literal["", "STANDARD", "VEGETARIANO", "VEGANO", "SENZA_GLUTINE", "ALTRO"]


class CompanionIn(BaseModel):
    name: str = Field(default="", max_length=200)
    menu: MenuChoice = ""
    notes: str = Field(default="", max_length=300)


def _email_ok(v: str) -> str:
    v = _clean(v).lower()
    if "@" not in v or "." not in v.split("@")[-1] or " " in v:
        raise ValueError("Indirizzo email non valido")
    return v


class ProfileLinkRequest(BaseModel):
    """Richiesta del link personale: con l'email di un invitato già in lista, oppure email + nome per crearsi."""
    email: str = Field(min_length=3, max_length=200)
    fullName: str = Field(default="", max_length=200)

    @field_validator("email")
    @classmethod
    def email_ok(cls, v: str) -> str:
        return _email_ok(v)


class ProfileBusClaimIn(BaseModel):
    """Prenotazione navetta senza profilo da fare propria ("È la mia")."""
    bookingId: int


class ProfileCodeVerify(BaseModel):
    """Accesso con il codice a 6 cifre ricevuto nell'email del link (app installata su iPhone):
    invitati (/api/profile), festeggiati (/api/graduate) e cassieri (/api/gifts/collector)."""
    email: str = Field(min_length=3, max_length=200)
    code: str = Field(min_length=1, max_length=20)

    @field_validator("email")
    @classmethod
    def email_ok(cls, v: str) -> str:
        return _email_ok(v)

    @field_validator("code")
    @classmethod
    def code_ok(cls, v: str) -> str:
        return "".join(ch for ch in v if ch.isdigit())


class ProfileIn(BaseModel):
    """Il profilo dell'invitato: dati personali, partecipazione, accompagnatori e menu."""
    fullName: str = Field(min_length=1, max_length=200)
    category: str = Field(default="Invitato", max_length=100)
    rsvpStatus: RsvpStatus = "CONFIRMED"
    email: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=100)
    menu: MenuChoice = ""
    dietaryNotes: str = Field(default="", max_length=500)
    companions: list[CompanionIn] = Field(default_factory=list, max_length=20)
    consent: bool = False

    @field_validator("fullName", "category", "email", "phone", "dietaryNotes")
    @classmethod
    def strip(cls, v: str, info) -> str:
        v = _clean(v)
        if info.field_name == "fullName" and not v:
            raise ValueError("Il nome è obbligatorio")
        if info.field_name == "email":
            v = v.lower()
            if v and ("@" not in v or " " in v):
                raise ValueError("Indirizzo email non valido")
        return v


class ProfileBusIn(BaseModel):
    """Navetta dal profilo: posti (default = persone del profilo), fermata, ritorno, telefono."""
    seatsCount: int | None = Field(default=None, ge=1, le=40)
    pickupStop: str = Field(default="", max_length=200)
    returnTripWanted: bool = True
    contactPhone: str = Field(default="", max_length=100)
    notes: str = Field(default="", max_length=500)


class ImportRowIn(BaseModel):
    """Una riga dell'import invitati (CSV incollato o caricato dagli organizzatori)."""
    fullName: str = Field(default="", max_length=200)
    category: str = Field(default="", max_length=100)
    email: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=100)
    guestsCount: int | None = Field(default=None, ge=0, le=50)
    rsvpStatus: RsvpStatus | None = None

    @field_validator("fullName", "category", "email", "phone")
    @classmethod
    def strip(cls, v: str, info) -> str:
        v = _clean(v)
        return v.lower() if info.field_name == "email" else v


class ImportIn(BaseModel):
    rows: list[ImportRowIn] = Field(default_factory=list, max_length=1000)
    sendLinks: bool = False  # manda il link personale a chi ha l'email
    dryRun: bool = False  # solo anteprima: niente scritture né email


class GuestUpdate(BaseModel):
    fullName: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=100)
    rsvpStatus: RsvpStatus | None = None
    guestsCount: int | None = Field(default=None, ge=0, le=50)
    dietaryNotes: str | None = Field(default=None, max_length=500)
    contactInfo: str | None = Field(default=None, max_length=200)


class BookingIn(BaseModel):
    passengerName: str = Field(min_length=1, max_length=200)
    seatsCount: int = Field(default=1, ge=1, le=40)
    pickupStop: str = Field(default="", max_length=200)
    returnTripWanted: bool = True
    contactPhone: str = Field(default="", max_length=100)
    notes: str = Field(default="", max_length=500)
    allowWaitlist: bool = False  # a posti esauriti: in lista d'attesa invece di errore

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


PoolPaymentMethod = Literal["IBAN", "PayPal", "Contanti"]
PoolSplitMode = Literal["EQUAL", "CUSTOM"]
PoolStatus = Literal["PENDING", "RECEIVED"]


class PoolAllocationIn(BaseModel):
    graduateId: str = Field(min_length=1, max_length=64)
    amount: float = Field(gt=0, le=100000)


class PoolContributionIn(BaseModel):
    """Quota unica versata al cassiere e ripartita fra i neo-specialisti.
    - splitMode EQUAL: totalAmount diviso in parti uguali fra graduateIds (vuoto = tutti);
    - splitMode CUSTOM: allocations con un importo per neo-specialista (il totale è la somma)."""
    collectorId: str = Field(default="", max_length=64)  # vuoto = primo cassiere
    donorName: str = Field(min_length=1, max_length=200)
    contact: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=200)  # facoltativa: conferma e promemoria al donatore
    paymentMethod: PoolPaymentMethod = "IBAN"
    splitMode: PoolSplitMode = "EQUAL"
    totalAmount: float | None = Field(default=None, gt=0, le=100000)
    graduateIds: list[str] = Field(default_factory=list, max_length=100)
    allocations: list[PoolAllocationIn] = Field(default_factory=list, max_length=100)
    note: str = Field(default="", max_length=1000)

    @field_validator("donorName", "contact", "note", "email")
    @classmethod
    def strip(cls, v: str, info) -> str:
        v = _clean(v)
        if info.field_name == "donorName" and not v:
            raise ValueError("Il nome è obbligatorio: serve al cassiere per riconoscere il versamento")
        if info.field_name == "email":
            v = v.lower()
            if v and ("@" not in v or " " in v):
                raise ValueError("Indirizzo email non valido")
        return v


class PoolStatusIn(BaseModel):
    status: PoolStatus


class PoolBatchStatusIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    status: PoolStatus


class PoolNoteIn(BaseModel):
    treasurerNote: str = Field(default="", max_length=1000)


class NotificationIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    message: str = Field(min_length=1, max_length=2000)
    category: str = Field(default="Organizzazione", max_length=50)
    # Timestamp (ms) a cui pubblicare la notifica; assente o nel passato = invio immediato.
    sendAt: int | None = Field(default=None, ge=0)

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
