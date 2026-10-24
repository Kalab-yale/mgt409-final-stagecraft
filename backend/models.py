"""Venue-agnostic Pydantic schemas for Stagecraft AI.

Every record the agents read or write passes through one of these models, so bad data
(an impossible time, a negative capacity, an unknown status) is rejected at the edge.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

# "HH:MM" on a 24-hour clock, e.g. "08:30" or "17:05".
HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", examples=["13:00"])]

RoomStatus = Literal["Operational", "AV Degraded", "Closed"]
VipTier = Literal["Tier 1 - Keynote", "Tier 2 - Featured", "Tier 3 - Panelist"]
SpeakerStatus = Literal["Confirmed On-Site", "In Transit", "Delayed"]
SessionTrack = Literal["Keynote", "Venture & Capital", "Tech & AI", "Founder Workshop", "Catering & Networking"]
SessionStatus = Literal["Scheduled", "Conflict Flagged", "Rescheduled"]
CrisisCategory = Literal["Flight/Transit Delay", "AV/Room Failure", "Catering/Overrun", "Red-Team Prompt Injection"]
Severity = Literal["Low", "Medium", "High", "Critical"]
EventStatus = Literal["Live War-Room", "Planning Mode"]


def minutes(hhmm: str) -> int:
    """'13:05' -> 785 minutes after midnight."""
    hours, mins = hhmm.split(":")
    return int(hours) * 60 + int(mins)


class Venue(BaseModel):
    id: str
    name: str
    city: str
    state_or_country: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    airport_corridor: str = ""
    notes: str = ""


class Room(BaseModel):
    id: str
    venue_id: str
    name: str
    floor_or_zone: str
    capacity: int = Field(gt=0)
    av_specs: list[str] = Field(default_factory=list)
    status: RoomStatus = "Operational"
    is_catering_hub: bool = False


class Speaker(BaseModel):
    id: str
    event_id: str
    name: str
    title_org: str
    vip_tier: VipTier
    travel_origin: str
    arrival_buffer_mins: int = Field(ge=0)
    status: SpeakerStatus = "Confirmed On-Site"
    email: str
    dietary_or_av_notes: str = ""
    bio: str = ""


class Session(BaseModel):
    id: str
    event_id: str
    title: str
    track: SessionTrack
    room_id: str
    start_time: HHMM
    end_time: HHMM
    expected_attendance: int = Field(ge=0)
    required_av: list[str] = Field(default_factory=list)
    speaker_ids: list[str] = Field(default_factory=list)
    is_locked_keynote: bool = False
    status: SessionStatus = "Scheduled"

    @model_validator(mode="after")
    def _ends_after_start(self) -> Session:
        if minutes(self.end_time) <= minutes(self.start_time):
            raise ValueError(f"Session {self.id}: end_time {self.end_time} must be after start_time {self.start_time}")
        return self


class CateringSLA(BaseModel):
    id: str
    event_id: str
    meal_name: str
    room_id: str
    scheduled_time: HHMM
    hard_cutoff_time: HHMM
    late_penalty_usd: float = Field(ge=0)
    headcount: int = Field(ge=0)
    vendor_name: str

    @model_validator(mode="after")
    def _cutoff_after_service(self) -> CateringSLA:
        if minutes(self.hard_cutoff_time) < minutes(self.scheduled_time):
            raise ValueError(f"Catering {self.id}: hard_cutoff_time cannot be before scheduled_time")
        return self


class CrisisScenarioPreset(BaseModel):
    id: str
    event_id: str
    title: str
    category: CrisisCategory
    prompt_text: str
    severity: Severity


class EventSummary(BaseModel):
    id: str
    slug: str
    name: str
    date: date
    venue_id: str
    attendee_count: int = Field(ge=0)
    status: EventStatus = "Planning Mode"
    description: str = ""


class EventFullState(BaseModel):
    """Everything the war-room needs about one event, in a single payload."""

    event: EventSummary
    venue: Venue
    rooms: list[Room]
    sessions: list[Session]
    speakers: list[Speaker]
    catering_slas: list[CateringSLA]
    crisis_presets: list[CrisisScenarioPreset]


# ===================================================================================================
# Planning layer (Phase 1 of the redesign): the event as a project - deadlines, initiatives, tasks,
# attendees, budget, vendors/sponsors, the inbox, and red flags. Grouped by dimension so each
# workspace tab (and its assistant) owns one slice.
# ===================================================================================================

Dimension = Literal["Program", "Venue", "Food", "Attendees", "Budget", "General"]
DIMENSION_LABELS: dict[str, str] = {
    "Program": "Program and speakers",
    "Venue": "Venue and logistics",
    "Food": "Food and catering",
    "Attendees": "Attendees",
    "Budget": "Tasks and budget",
    "General": "General",
}
Source = Literal["seed", "inbox", "assistant", "user", "system", "template"]


class Deadline(BaseModel):
    id: str
    event_id: str
    title: str
    dimension: Dimension
    due_date: date
    owner: str = ""
    done: bool = False
    notes: str = ""
    source: Source = "seed"


class Initiative(BaseModel):
    """A workstream with an owner and a progress bar (progress is computed from its tasks)."""

    id: str
    event_id: str
    title: str
    dimension: Dimension
    owner: str = ""
    target_date: date
    description: str = ""
    blocked: bool = False


class Task(BaseModel):
    id: str
    event_id: str
    title: str
    dimension: Dimension
    initiative_id: str | None = None
    owner: str = ""
    due_date: date | None = None
    status: Literal["To Do", "In Progress", "Done", "Blocked"] = "To Do"
    priority: Literal["High", "Medium", "Low"] = "Medium"
    source: Source = "seed"


class Attendee(BaseModel):
    id: str
    event_id: str
    name: str
    email: str
    organization: str = ""
    attendee_type: Literal["General", "VIP", "Student", "Sponsor", "Press", "Staff"] = "General"
    rsvp_status: Literal["Registered", "Confirmed", "Waitlist", "Cancelled", "Checked In"] = "Registered"
    dietary: Literal["None", "Vegetarian", "Vegan", "Halal", "Kosher", "Gluten-Free", "Nut Allergy"] = "None"
    registered_on: date


class Vendor(BaseModel):
    """Vendors we pay and sponsors who pay us."""

    id: str
    event_id: str
    name: str
    relationship: Literal["Vendor", "Sponsor"] = "Vendor"
    category: str
    contact_name: str = ""
    contact_email: str = ""
    contract_status: Literal["Prospect", "Negotiating", "Signed", "Invoiced", "Paid"] = "Prospect"
    contract_value_usd: float = Field(default=0.0, ge=0)
    notes: str = ""


class BudgetItem(BaseModel):
    id: str
    event_id: str
    category: Literal["Venue", "Catering", "AV and Production", "Travel", "Marketing", "Speakers", "Staffing", "Misc"]
    description: str
    vendor_id: str | None = None
    planned_usd: float = Field(ge=0)
    actual_usd: float = Field(default=0.0, ge=0)
    status: Literal["Estimated", "Committed", "Paid"] = "Estimated"


class InboxMessage(BaseModel):
    """An email the organizer received (demo inbox, Gmail, or pasted). The Inbox Watcher fills the AI fields."""

    id: str
    event_id: str | None = None
    source: Literal["demo", "gmail", "pasted"] = "demo"
    external_id: str = ""
    from_name: str
    from_email: str
    subject: str
    body: str
    received_at: str  # ISO 8601 timestamp
    status: Literal["New", "Triaged", "Needs Reply", "Done", "Quarantined"] = "New"
    dimension: Dimension | None = None
    ai_summary: str = ""
    # Filled by the Inbox Watcher
    category: Literal["Action needed", "Reply needed", "FYI", "Noise", "Suspicious"] | None = None
    urgency: Literal["Critical", "High", "Medium", "Low"] | None = None
    key_facts: list[str] = Field(default_factory=list)
    reply_draft: str = ""
    processed_at: str | None = None


class Flag(BaseModel):
    """A red flag stored by the Inbox Watcher, an assistant, or a person (system rules compute theirs live)."""

    id: str
    event_id: str
    title: str
    detail: str = ""
    dimension: Dimension
    severity: Literal["Critical", "High", "Medium", "Low"] = "Medium"
    status: Literal["Open", "Resolved", "Dismissed"] = "Open"
    source: Source = "user"
    source_ref: str = ""
    created_at: str
