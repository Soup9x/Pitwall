"""ORM models. All datetimes are stored in UTC."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pitwall.db import Base, UTCDateTime, utcnow

SESSION_TYPES = ("FP1", "FP2", "FP3", "SQ", "SPRINT", "Q", "RACE")
# The first admin account. Created by migration 0005; single-user installs are this user.
OWNER_ID = 1
SESSION_LABELS = {
    "FP1": "Practice 1",
    "FP2": "Practice 2",
    "FP3": "Practice 3",
    "SQ": "Sprint Qualifying",
    "SPRINT": "Sprint",
    "Q": "Qualifying",
    "RACE": "Race",
}
SESSION_EMOJI = {
    "FP1": "🔧",
    "FP2": "🔧",
    "FP3": "🔧",
    "SQ": "⏱️",
    "SPRINT": "⚡",
    "Q": "⏱️",
    "RACE": "🏁",
}


class Season(Base):
    __tablename__ = "season"

    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    backfilled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Circuit(Base):
    __tablename__ = "circuit"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    locality: Mapped[str | None] = mapped_column(String(100), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    tz_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Curated facts merged from data/circuits.yaml
    length_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    laps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    turns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    drs_zones: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_gp_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lap_record_driver: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lap_record_time: Mapped[str | None] = mapped_column(String(20), nullable=True)
    lap_record_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    map_image: Mapped[str | None] = mapped_column(String(300), nullable=True)
    facts_verified: Mapped[bool] = mapped_column(Boolean, default=False)


class Event(Base):
    __tablename__ = "event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Stable identity across round renumbering: "{season}-{circuit_id}" (+ "-2" for a second
    # visit to the same circuit in one season). Used for ICS UIDs.
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    season: Mapped[int] = mapped_column(ForeignKey("season.year"), index=True)
    round: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(200))
    circuit_id: Mapped[str] = mapped_column(ForeignKey("circuit.id"), index=True)
    is_sprint_weekend: Mapped[bool] = mapped_column(Boolean, default=False)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="scheduled")
    url: Mapped[str | None] = mapped_column(String(300), nullable=True)

    circuit: Mapped[Circuit] = relationship(lazy="joined")
    sessions: Mapped[list[Session]] = relationship(
        back_populates="event", order_by="Session.start_utc", cascade="all, delete-orphan"
    )

    @property
    def short_name(self) -> str:
        return self.name.replace("Grand Prix", "GP")


class Session(Base):
    __tablename__ = "session"
    __table_args__ = (UniqueConstraint("event_id", "type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(10))
    start_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    end_utc_est: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="scheduled")
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    last_changed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    results_fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    event: Mapped[Event] = relationship(back_populates="sessions")

    @property
    def label(self) -> str:
        return SESSION_LABELS.get(self.type, self.type)


class Driver(Base):
    __tablename__ = "driver"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[str | None] = mapped_column(String(3), nullable=True)
    number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    given_name: Mapped[str] = mapped_column(String(100))
    family_name: Mapped[str] = mapped_column(String(100))
    dob: Mapped[date | None] = mapped_column(Date, nullable=True)
    nationality: Mapped[str | None] = mapped_column(String(50), nullable=True)
    url: Mapped[str | None] = mapped_column(String(300), nullable=True)

    @property
    def full_name(self) -> str:
        return f"{self.given_name} {self.family_name}"


class Constructor(Base):
    __tablename__ = "constructor"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    nationality: Mapped[str | None] = mapped_column(String(50), nullable=True)
    url: Mapped[str | None] = mapped_column(String(300), nullable=True)


class Result(Base):
    __tablename__ = "result"
    __table_args__ = (UniqueConstraint("session_id", "driver_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("session.id", ondelete="CASCADE"), index=True
    )
    driver_id: Mapped[str] = mapped_column(ForeignKey("driver.id"), index=True)
    constructor_id: Mapped[str | None] = mapped_column(ForeignKey("constructor.id"), nullable=True)
    number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    grid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position_text: Mapped[str | None] = mapped_column(String(5), nullable=True)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    points: Mapped[float] = mapped_column(Float, default=0.0)
    laps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    time_text: Mapped[str | None] = mapped_column(String(30), nullable=True)
    fastest_lap_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fastest_lap_time: Mapped[str | None] = mapped_column(String(20), nullable=True)
    q1: Mapped[str | None] = mapped_column(String(20), nullable=True)
    q2: Mapped[str | None] = mapped_column(String(20), nullable=True)
    q3: Mapped[str | None] = mapped_column(String(20), nullable=True)

    session: Mapped[Session] = relationship()
    driver: Mapped[Driver] = relationship(lazy="joined")
    constructor: Mapped[Constructor | None] = relationship(lazy="joined")


class StandingsSnapshot(Base):
    __tablename__ = "standings_snapshot"
    __table_args__ = (UniqueConstraint("season", "round", "type", "entity_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    season: Mapped[int] = mapped_column(Integer, index=True)
    round: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(12))  # driver / constructor
    entity_id: Mapped[str] = mapped_column(String(64))
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    points: Mapped[float] = mapped_column(Float, default=0.0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    taken_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(40), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200), default="!")  # "!" = can't log in
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    ics_token: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    # Bumped on password change/reset so existing login cookies stop working.
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    # Set when an admin chose the password (new account or temporary password).
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Invite(Base):
    """One-time link to create an account (user_id is None) or reset a password (user_id set).
    Only a hash of the token is stored."""

    __tablename__ = "invite"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=True
    )
    make_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class UserSetting(Base):
    __tablename__ = "user_setting"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)  # JSON-encoded


class FollowedDriver(Base):
    __tablename__ = "followed_driver"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), primary_key=True, default=OWNER_ID
    )
    driver_id: Mapped[str] = mapped_column(ForeignKey("driver.id"), primary_key=True)
    driver: Mapped[Driver] = relationship(lazy="joined")


class NotificationLog(Base):
    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True, default=OWNER_ID
    )
    type: Mapped[str] = mapped_column(String(40), index=True)
    channel: Mapped[str] = mapped_column(String(20))
    payload_hash: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    session_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    status: Mapped[str] = mapped_column(String(20))  # sent / failed / dropped / test
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class Outbox(Base):
    """Queued alerts. Holds alerts delayed by quiet hours or spoiler mode."""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True, default=OWNER_ID)  # recipient
    type: Mapped[str] = mapped_column(String(40), index=True)
    payload_hash: Mapped[str] = mapped_column(String(64), index=True)
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    session_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(300))
    text: Mapped[str] = mapped_column(Text)
    html: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_json: Mapped[str] = mapped_column(Text, default="{}")
    channels: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    spoiler: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    hold_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class SpoilerState(Base):
    __tablename__ = "spoiler_state"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), primary_key=True, default=OWNER_ID
    )
    event_id: Mapped[int] = mapped_column(
        ForeignKey("event.id", ondelete="CASCADE"), primary_key=True
    )
    watched: Mapped[bool] = mapped_column(Boolean, default=False)
    watched_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Setting(Base):
    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)  # JSON-encoded


class ApiCache(Base):
    __tablename__ = "api_cache"

    url: Mapped[str] = mapped_column(String(500), primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    body: Mapped[str] = mapped_column(Text)


class SourceStatus(Base):
    __tablename__ = "source_status"

    source: Mapped[str] = mapped_column(String(40), primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class NotificationTarget(Base):
    """An email or webhook destination added in the Settings UI (an Apprise URL).

    The URL holds credentials, so it is never rendered in full; the UI shows `label` and a
    masked form. Targets from APPRISE_URLS_* in .env are used as well, but aren't stored here."""

    __tablename__ = "notification_target"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True, default=OWNER_ID
    )
    channel: Mapped[str] = mapped_column(String(20), index=True)  # email / webhook
    kind: Mapped[str] = mapped_column(String(20))  # gmail, m365, smtp, ntfy, discord, json, apprise
    label: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DriverSessionDetail(Base):
    """Per-driver post-session detail from OpenF1 (2023+): best lap, tyre stints, pit stops."""

    __tablename__ = "driver_session_detail"
    __table_args__ = (UniqueConstraint("session_id", "driver_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("session.id", ondelete="CASCADE"), index=True
    )
    driver_id: Mapped[str] = mapped_column(ForeignKey("driver.id"), index=True)
    openf1_session_key: Mapped[int | None] = mapped_column(Integer, nullable=True)
    best_lap_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    pit_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stints_json: Mapped[str] = mapped_column(Text, default="[]")
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SessionWeather(Base):
    """Forecast for a session's time window (Open-Meteo), refreshed every few hours."""

    __tablename__ = "session_weather"

    session_id: Mapped[int] = mapped_column(
        ForeignKey("session.id", ondelete="CASCADE"), primary_key=True
    )
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    rain_chance: Mapped[int | None] = mapped_column(Integer, nullable=True)  # max %, window
    precip_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)
    weather_code: Mapped[int | None] = mapped_column(Integer, nullable=True)


# ---------------------------------------------------------------------------- tyres (Pirelli)

COMPOUNDS = ("C1", "C2", "C3", "C4", "C5", "C6")  # C1 hardest


class TyreNomination(Base):
    """Pirelli's hard/medium/soft choice for an event. One row per origin ("manual" from the
    admin form, "pirelli" parsed from a press release); a manual row always wins."""

    __tablename__ = "tyre_nomination"
    __table_args__ = (UniqueConstraint("event_id", "origin"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"), index=True)
    origin: Mapped[str] = mapped_column(String(20))  # manual / pirelli
    hard: Mapped[str] = mapped_column(String(2))
    medium: Mapped[str] = mapped_column(String(2))
    soft: Mapped[str] = mapped_column(String(2))
    source_title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    recorded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)  # user id (manual)


# ---------------------------------------------------------------------------- OpenF1 post-session


class SessionDataFetch(Base):
    """Bookkeeping for OpenF1 post-session data (race control + weather): which OpenF1 session
    a session matched and when it was fetched, so completed sessions aren't fetched again."""

    __tablename__ = "session_data_fetch"

    session_id: Mapped[int] = mapped_column(
        ForeignKey("session.id", ondelete="CASCADE"), primary_key=True
    )
    openf1_session_key: Mapped[int | None] = mapped_column(Integer, nullable=True)
    race_control_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    weather_samples: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class RaceControlMessage(Base):
    """A race control message (OpenF1 /race_control), stored after the session."""

    __tablename__ = "race_control_message"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("session.id", ondelete="CASCADE"), index=True
    )
    at: Mapped[datetime] = mapped_column(UTCDateTime)
    lap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category: Mapped[str | None] = mapped_column(String(30), nullable=True)
    flag: Mapped[str | None] = mapped_column(String(30), nullable=True)
    scope: Mapped[str | None] = mapped_column(String(20), nullable=True)
    sector: Mapped[int | None] = mapped_column(Integer, nullable=True)
    driver_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text)


class SessionConditions(Base):
    """Measured conditions for a completed session (OpenF1 /weather), summarised. `series_json`
    is a downsampled [[minutes from start, track °C], ...] list for the chart."""

    __tablename__ = "session_conditions"

    session_id: Mapped[int] = mapped_column(
        ForeignKey("session.id", ondelete="CASCADE"), primary_key=True
    )
    samples: Mapped[int] = mapped_column(Integer, default=0)
    track_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    track_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    track_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    air_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    air_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    air_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    humidity_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    humidity_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    humidity_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    rain: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    wind_max_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    series_json: Mapped[str] = mapped_column(Text, default="[]")
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# ---------------------------------------------------------------------------- penalty watch


class FiaDocument(Base):
    """An FIA event document seen by the poller, and what parsing it produced. Every parsed
    record points back here, so the UI can always link to its source."""

    __tablename__ = "fia_document"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(500), unique=True)
    title: Mapped[str] = mapped_column(String(300))
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("event.id", ondelete="SET NULL"), nullable=True, index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    kind: Mapped[str] = mapped_column(String(20))  # pu_usage / decision / other
    status: Mapped[str] = mapped_column(String(20))  # parsed / failed / ignored
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)  # why it failed
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PuAllocation(Base):
    """How many of each power unit element a driver may use in a season without a penalty.
    Entered by an admin (Pitwall doesn't guess regulations)."""

    __tablename__ = "pu_allocation"

    season: Mapped[int] = mapped_column(Integer, primary_key=True)
    element: Mapped[str] = mapped_column(String(10), primary_key=True)
    allowed: Mapped[int] = mapped_column(Integer)


class PuUsage(Base):
    """Cumulative count of one power unit element used by a driver as of an event."""

    __tablename__ = "pu_usage"
    __table_args__ = (UniqueConstraint("event_id", "driver_id", "element", "origin"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"), index=True)
    driver_id: Mapped[str] = mapped_column(ForeignKey("driver.id"), index=True)
    element: Mapped[str] = mapped_column(String(10))
    count: Mapped[int] = mapped_column(Integer)
    origin: Mapped[str] = mapped_column(String(10))  # manual / fia
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("fia_document.id", ondelete="SET NULL"), nullable=True
    )
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    recorded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)

    driver: Mapped[Driver] = relationship(lazy="joined")
    document: Mapped[FiaDocument | None] = relationship(lazy="joined")


class GridPenalty(Base):
    """A grid penalty for an event: `places` and/or `back_of_grid`. A manual row for an
    (event, driver) replaces all FIA-parsed rows for that pair; a manual row with no places and
    no back-of-grid records "no penalty" (to overrule a wrong parse)."""

    __tablename__ = "grid_penalty"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"), index=True)
    driver_id: Mapped[str] = mapped_column(ForeignKey("driver.id"), index=True)
    places: Mapped[int | None] = mapped_column(Integer, nullable=True)
    back_of_grid: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(300), default="")
    origin: Mapped[str] = mapped_column(String(10))  # manual / fia
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("fia_document.id", ondelete="SET NULL"), nullable=True
    )
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    recorded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)

    driver: Mapped[Driver] = relationship(lazy="joined")
    document: Mapped[FiaDocument | None] = relationship(lazy="joined")


class LicencePoints(Base):
    """Super licence penalty points. They count for 12 months from `awarded_on`. A manual row
    for a (driver, event) replaces FIA-parsed rows for that pair (points 0 = "none")."""

    __tablename__ = "licence_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    driver_id: Mapped[str] = mapped_column(ForeignKey("driver.id"), index=True)
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("event.id", ondelete="SET NULL"), nullable=True, index=True
    )
    points: Mapped[int] = mapped_column(Integer)
    awarded_on: Mapped[date] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(String(300), default="")
    origin: Mapped[str] = mapped_column(String(10))  # manual / fia
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("fia_document.id", ondelete="SET NULL"), nullable=True
    )
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    recorded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)

    driver: Mapped[Driver] = relationship(lazy="joined")
    document: Mapped[FiaDocument | None] = relationship(lazy="joined")


# ---------------------------------------------------------------------------- news (RSS)


class NewsFeed(Base):
    __tablename__ = "news_feed"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(500), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    etag: Mapped[str | None] = mapped_column(String(200), nullable=True)
    modified: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class NewsItem(Base):
    """A headline. Only the title, link, source, date and tags are stored; never article text."""

    __tablename__ = "news_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_id: Mapped[int | None] = mapped_column(
        ForeignKey("news_feed.id", ondelete="SET NULL"), nullable=True, index=True
    )
    link: Mapped[str] = mapped_column(String(500), unique=True)
    title: Mapped[str] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(100))
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    # JSON list like ["driver:norris", "team:mclaren", "circuit:monza", "track"]
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
