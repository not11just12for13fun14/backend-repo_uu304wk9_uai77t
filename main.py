import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from database import db, create_document, get_documents

# Google Calendar
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest

# Twilio
from twilio.rest import Client as TwilioClient

# Timezones
import pytz
from dateutil import parser as dateparser

# Scheduler
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

app = FastAPI(title="Calendar SMS Reminder API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")

scheduler = BackgroundScheduler()
_job = None


# -------- Models (requests) --------
class UserIn(BaseModel):
    email: str
    phone_e164: str
    timezone: str

class RuleIn(BaseModel):
    user_email: str
    minutes_before: int = Field(10, ge=1, le=10080)
    active: bool = True

class SaveRefreshTokenIn(BaseModel):
    user_email: str
    refresh_token: str


# -------- Utilities --------

def _collection(name: str):
    return db[name]


def get_user(email: str) -> Optional[dict]:
    return _collection("user").find_one({"email": email})


def upsert_user(user: UserIn):
    _collection("user").update_one(
        {"email": user.email},
        {"$set": {"email": user.email, "phone_e164": user.phone_e164, "timezone": user.timezone}},
        upsert=True,
    )


def upsert_rule(rule: RuleIn):
    _collection("reminderrule").update_one(
        {"user_email": rule.user_email},
        {"$set": {"user_email": rule.user_email, "minutes_before": rule.minutes_before, "active": rule.active}},
        upsert=True,
    )


def save_refresh_token(user_email: str, refresh_token: str):
    _collection("user").update_one(
        {"email": user_email},
        {"$set": {"google_refresh_token": refresh_token}},
        upsert=True,
    )


def get_rule(user_email: str) -> Optional[dict]:
    return _collection("reminderrule").find_one({"user_email": user_email, "active": True})


def get_google_service(refresh_token: str):
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise RuntimeError("Google client credentials not configured")
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/calendar.readonly"],
    )
    # Force refresh to obtain access token
    creds.refresh(GoogleRequest())
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return service


def get_twilio_client():
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER):
        raise RuntimeError("Twilio credentials not configured")
    return TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def _has_sent(user_email: str, event_id: str) -> bool:
    found = _collection("outboundlog").find_one({"user_email": user_email, "event_id": event_id})
    return found is not None


def log_sent(user_email: str, event_id: str, message: str):
    _collection("outboundlog").insert_one(
        {
            "user_email": user_email,
            "event_id": event_id,
            "message": message,
            "sent_at_ts": int(time.time()),
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
    )


def human_time(dt_utc: datetime, tz_name: str) -> str:
    tz = pytz.timezone(tz_name)
    local_dt = dt_utc.astimezone(tz)
    return local_dt.strftime("%a %b %d, %I:%M %p %Z")


def parse_event_start(event: dict) -> Optional[datetime]:
    start = event.get("start") or {}
    # dateTime preferred; if only date, treat as all-day (ignore for reminders)
    if "dateTime" in start:
        dt = dateparser.isoparse(start["dateTime"])  # may include tzinfo
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def poll_and_send_once() -> dict:
    """Poll Google Calendar for all users and send SMS reminders when within window."""
    processed = []
    now_utc = datetime.now(timezone.utc)
    users = list(_collection("user").find({"google_refresh_token": {"$exists": True, "$ne": None}}))
    for user in users:
        email = user["email"]
        tz_name = user.get("timezone", "UTC")
        phone = user.get("phone_e164")
        rule = get_rule(email)
        if not rule or not rule.get("active", True):
            continue
        minutes_before = int(rule.get("minutes_before", 10))
        window_end = now_utc + timedelta(minutes=max(minutes_before, 15))
        try:
            service = get_google_service(user["google_refresh_token"])
            events_result = (
                service.events()
                .list(
                    calendarId="primary",
                    timeMin=now_utc.isoformat(),
                    timeMax=window_end.isoformat(),
                    maxResults=25,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            events = events_result.get("items", [])
        except Exception as e:
            processed.append({"user": email, "error": f"Google API error: {str(e)[:120]}"})
            continue

        to_send = []
        for ev in events:
            ev_id = ev.get("id")
            start_utc = parse_event_start(ev)
            if start_utc is None:
                continue  # skip all-day or unknown
            diff = (start_utc - now_utc).total_seconds() / 60.0
            if 0 <= diff <= minutes_before:
                # Dedupe by event instance id (for recurring events Google includes recurringEventId; id is unique per instance)
                if not _has_sent(email, ev_id):
                    to_send.append((ev, start_utc))

        if not to_send:
            processed.append({"user": email, "sent": 0})
            continue

        # Prepare Twilio client once per user
        try:
            twilio = get_twilio_client()
        except Exception as e:
            processed.append({"user": email, "error": f"Twilio config error: {str(e)}"})
            continue

        count = 0
        for ev, start_utc in to_send:
            title = ev.get("summary", "Untitled Event")
            location = ev.get("location")
            meeting_link = None
            # Find meet link if present
            hangout = ev.get("hangoutLink")
            if hangout:
                meeting_link = hangout
            else:
                # conferenceData if enabled
                conf = (ev.get("conferenceData") or {}).get("entryPoints") or []
                for ep in conf:
                    if ep.get("entryPointType") == "video" and ep.get("uri"):
                        meeting_link = ep["uri"]
                        break

            local_str = human_time(start_utc, tz_name)
            parts = [f"Reminder: '{title}' starts at {local_str}"]
            if location:
                parts.append(f"Location: {location}")
            if meeting_link:
                parts.append(f"Link: {meeting_link}")
            msg = "\n".join(parts)

            try:
                twilio.messages.create(to=phone, from_=TWILIO_FROM_NUMBER, body=msg)
                log_sent(email, ev.get("id"), msg)
                count += 1
            except Exception as e:
                processed.append({"user": email, "error": f"SMS error: {str(e)[:120]}"})
        processed.append({"user": email, "sent": count})

    return {"now": now_utc.isoformat(), "results": processed}


def ensure_scheduler():
    global _job
    if not scheduler.running:
        scheduler.start()
    if _job is None:
        _job = scheduler.add_job(poll_and_send_once, IntervalTrigger(minutes=5), id="poll-job", replace_existing=True)


# -------- Routes --------
@app.get("/")
def index():
    return {"message": "Calendar SMS Reminder API running"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": [],
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Set"
            response["database_name"] = db.name
            response["connection_status"] = "Connected"
            response["collections"] = db.list_collection_names()[:10]
    except Exception as e:
        response["database"] = f"⚠️ Error: {str(e)[:80]}"
    return response


@app.post("/api/user")
def api_upsert_user(payload: UserIn):
    try:
        # Validate timezone
        _ = pytz.timezone(payload.timezone)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid IANA timezone")
    upsert_user(payload)
    ensure_scheduler()
    return {"status": "ok"}


@app.post("/api/reminder-rule")
def api_set_rule(payload: RuleIn):
    upsert_rule(payload)
    ensure_scheduler()
    return {"status": "ok"}


@app.post("/api/google/token")
def api_save_refresh_token(payload: SaveRefreshTokenIn):
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Google OAuth env vars missing")
    save_refresh_token(payload.user_email, payload.refresh_token)
    ensure_scheduler()
    return {"status": "ok"}


@app.post("/api/trigger")
def api_trigger_now():
    ensure_scheduler()
    result = poll_and_send_once()
    return result


@app.get("/api/status")
def api_status():
    ensure_scheduler()
    users = list(_collection("user").find({}, {"_id": 0, "email": 1, "timezone": 1, "phone_e164": 1, "google_refresh_token": {"$cond": [{"$ifNull": ["$google_refresh_token", False]}, True, False]}}))
    rules = list(_collection("reminderrule").find({}, {"_id": 0}))
    next_run = None
    try:
        job = scheduler.get_job("poll-job")
        if job and job.next_run_time:
            next_run = job.next_run_time.astimezone(timezone.utc).isoformat()
    except Exception:
        next_run = None
    return {"users": users, "rules": rules, "next_run": next_run}


if __name__ == "__main__":
    ensure_scheduler()
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
