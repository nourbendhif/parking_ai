"""
Smart Parking System - SQLAlchemy Data Models
Updated: Reservation now has reference + notes columns.
"""
from __future__ import annotations

from datetime import datetime

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


# ─── User ─────────────────────────────────────────────────────────────────────

class User(UserMixin, db.Model):
    __tablename__ = "users"

    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80),  unique=True, nullable=False, index=True)
    email         = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role          = db.Column(db.String(20),  nullable=False, default="user")
    is_active     = db.Column(db.Boolean, default=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    last_login    = db.Column(db.DateTime)

    vehicles     = db.relationship("Vehicle",      backref="owner", lazy="dynamic",
                                   cascade="all, delete-orphan")
    reservations = db.relationship("Reservation",  backref="user",  lazy="dynamic")
    events       = db.relationship("ParkingEvent", backref="user",  lazy="dynamic")

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def to_dict(self) -> dict:
        return {
            "id"        : self.id,
            "username"  : self.username,
            "email"     : self.email,
            "role"      : self.role,
            "is_active" : self.is_active,
            "created_at": self.created_at.isoformat(),
        }


# ─── Vehicle ──────────────────────────────────────────────────────────────────

class Vehicle(db.Model):
    __tablename__ = "vehicles"

    id            = db.Column(db.Integer, primary_key=True)
    license_plate = db.Column(db.String(20), unique=True, nullable=False, index=True)
    owner_name    = db.Column(db.String(100))
    vehicle_type  = db.Column(db.String(50), default="car")
    color         = db.Column(db.String(30))
    notes         = db.Column(db.Text)
    is_authorized = db.Column(db.Boolean, default=True)
    user_id       = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    reservations = db.relationship("Reservation",  backref="vehicle", lazy="dynamic")
    events       = db.relationship("ParkingEvent", backref="vehicle", lazy="dynamic")

    def to_dict(self) -> dict:
        return {
            "id"           : self.id,
            "license_plate": self.license_plate,
            "owner_name"   : self.owner_name,
            "vehicle_type" : self.vehicle_type,
            "color"        : self.color,
            "is_authorized": self.is_authorized,
            "created_at"   : self.created_at.isoformat(),
        }


# ─── Reservation ──────────────────────────────────────────────────────────────

class Reservation(db.Model):
    __tablename__ = "reservations"

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"), nullable=False)
    slot_id    = db.Column(db.String(10), default="A1")
    start_time = db.Column(db.DateTime, nullable=False)
    end_time   = db.Column(db.DateTime)
    status     = db.Column(db.String(20), default="active")  # active | completed | cancelled
    reference  = db.Column(db.String(60))   # ← matricule / personal reference
    notes      = db.Column(db.Text)         # ← optional booking notes
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id"        : self.id,
            "slot_id"   : self.slot_id,
            "start_time": self.start_time.isoformat(),
            "end_time"  : self.end_time.isoformat() if self.end_time else None,
            "status"    : self.status,
            "reference" : self.reference,
            "notes"     : self.notes,
        }


# ─── Parking Event ────────────────────────────────────────────────────────────

class ParkingEvent(db.Model):
    __tablename__ = "parking_events"

    id              = db.Column(db.Integer, primary_key=True)
    vehicle_id      = db.Column(db.Integer, db.ForeignKey("vehicles.id"))
    user_id         = db.Column(db.Integer, db.ForeignKey("users.id"))
    license_plate   = db.Column(db.String(20), index=True)
    event_type      = db.Column(db.String(10))
    timestamp       = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    success         = db.Column(db.Boolean, default=True)
    confidence      = db.Column(db.Float)
    processing_ms   = db.Column(db.Integer)
    gate_opened     = db.Column(db.Boolean, default=False)
    annotated_image = db.Column(db.Text)
    notes           = db.Column(db.Text)
    simulated       = db.Column(db.Boolean, default=False)

    def to_dict(self) -> dict:
        return {
            "id"           : self.id,
            "license_plate": self.license_plate,
            "event_type"   : self.event_type,
            "timestamp"    : self.timestamp.isoformat(),
            "success"      : self.success,
            "confidence"   : self.confidence,
            "processing_ms": self.processing_ms,
            "gate_opened"  : self.gate_opened,
            "simulated"    : self.simulated,
            "notes"        : self.notes,
        }


# ─── System Log ───────────────────────────────────────────────────────────────

class SystemLog(db.Model):
    __tablename__ = "system_logs"

    id        = db.Column(db.Integer, primary_key=True)
    level     = db.Column(db.String(10))
    component = db.Column(db.String(50))
    message   = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    @classmethod
    def info(cls, component: str, message: str):
        return cls(level="INFO", component=component, message=message)

    @classmethod
    def warning(cls, component: str, message: str):
        return cls(level="WARNING", component=component, message=message)

    @classmethod
    def error(cls, component: str, message: str):
        return cls(level="ERROR", component=component, message=message)

    def to_dict(self) -> dict:
        return {
            "id"       : self.id,
            "level"    : self.level,
            "component": self.component,
            "message"  : self.message,
            "timestamp": self.timestamp.isoformat(),
        }


# ─── System Settings ──────────────────────────────────────────────────────────

class SystemSettings(db.Model):
    __tablename__ = "system_settings"

    id    = db.Column(db.Integer, primary_key=True)
    key   = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text)

    @classmethod
    def get(cls, key: str, default: str = None) -> str | None:
        row = cls.query.filter_by(key=key).first()
        return row.value if row else default

    @classmethod
    def set(cls, key: str, value: str):
        row = cls.query.filter_by(key=key).first()
        if row:
            row.value = value
        else:
            db.session.add(cls(key=key, value=value))
        db.session.commit()
