"""
Database Schemas for Calendar SMS Reminders

Each Pydantic model corresponds to a MongoDB collection (lowercased name).
"""

from pydantic import BaseModel, Field
from typing import Optional, List

class User(BaseModel):
    """Stores a user with Google OAuth linkage and SMS number"""
    email: str = Field(..., description="Email address")
    phone_e164: str = Field(..., description="SMS number in E.164 format, e.g., +15551234567")
    timezone: str = Field(..., description="IANA timezone, e.g., America/New_York")
    # Minimal token storage (encrypted at rest in real app); here we store refresh token only
    google_refresh_token: Optional[str] = Field(None, description="Google OAuth refresh token")

class Subscription(BaseModel):
    """Stores a Google Calendar watch channel for push notifications (optional enhancement)"""
    user_email: str = Field(...)
    channel_id: str = Field(...)
    resource_id: str = Field(...)
    expiration: int = Field(..., description="Unix ms expiration of the channel")

class ReminderRule(BaseModel):
    """User preference for how long before events to text"""
    user_email: str = Field(...)
    minutes_before: int = Field(10, ge=1, le=10080, description="How many minutes before start to notify")
    active: bool = Field(True)

class OutboundLog(BaseModel):
    """Keeps a log of SMS messages we sent, to dedupe"""
    user_email: str = Field(...)
    event_id: str = Field(...)
    sent_at_ts: int = Field(..., description="UTC epoch seconds when sent")
    message: str = Field(...)
