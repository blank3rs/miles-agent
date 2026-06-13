"""Google Calendar: list, create, respond, find free slots."""
import asyncio
from datetime import datetime, timedelta, timezone

from agent.config import EMAIL_ADDRESS
from agent.tools.gmail import _get_google_service


async def list_calendar_events(days_ahead: int = 7, calendar_id: str = "primary") -> str:
    def _fetch():
        svc = _get_google_service("calendar", "v3")
        if not svc:
            return None
        now = datetime.now(timezone.utc)
        result = svc.events().list(
            calendarId=calendar_id,
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=days_ahead)).isoformat(),
            maxResults=20,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        return result.get("items", [])

    try:
        events = await asyncio.to_thread(_fetch)
        if events is None:
            return "[calendar] Google credentials not found or missing calendar scope."
        if not events:
            return f"(no events in the next {days_ahead} days)"
        lines = []
        for e in events:
            start = e.get("start", {})
            start_str = start.get("dateTime", start.get("date", "?"))
            title = e.get("summary", "(no title)")
            loc = f" @ {e['location']}" if e.get("location") else ""
            attendees = [a.get("email", "") for a in e.get("attendees", [])]
            att_str = f" | {', '.join(attendees)}" if attendees else ""
            lines.append(f"[{e.get('id', '')}] {start_str} — {title}{loc}{att_str} ({e.get('status', '')})")
        return "\n".join(lines)
    except Exception as e:
        return f"[calendar list failed] {e}"


async def create_calendar_event(
    title: str,
    start_iso: str,
    end_iso: str,
    attendees: list | None = None,
    description: str = "",
    location: str = "",
    calendar_id: str = "primary",
) -> str:
    def _create():
        svc = _get_google_service("calendar", "v3")
        if not svc:
            return None
        body = {
            "summary": title,
            "start": {"dateTime": start_iso, "timeZone": "UTC"},
            "end":   {"dateTime": end_iso,   "timeZone": "UTC"},
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]
            body["guestsCanSeeOtherGuests"] = True
        return svc.events().insert(
            calendarId=calendar_id,
            body=body,
            sendUpdates="all" if attendees else "none",
        ).execute()

    try:
        event = await asyncio.to_thread(_create)
        if event is None:
            return "[calendar] Google credentials not found or missing calendar scope."
        return f"Event created: {event.get('summary')} — {event.get('htmlLink', event.get('id'))}"
    except Exception as e:
        return f"[calendar create failed] {e}"


async def respond_to_calendar_event(event_id: str, response: str, calendar_id: str = "primary") -> str:
    """response: 'accepted', 'declined', or 'tentative'"""
    valid = {"accepted", "declined", "tentative"}
    if response not in valid:
        return f"[calendar] response must be one of: {', '.join(valid)}"

    def _respond():
        svc = _get_google_service("calendar", "v3")
        if not svc:
            return None
        event = svc.events().get(calendarId=calendar_id, eventId=event_id).execute()
        attendees = event.get("attendees", [])
        updated = False
        for a in attendees:
            if a.get("email", "").lower() == EMAIL_ADDRESS.lower():
                a["responseStatus"] = response
                updated = True
        if not updated:
            attendees.append({"email": EMAIL_ADDRESS, "responseStatus": response})
        return svc.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body={"attendees": attendees},
            sendUpdates="all",
        ).execute()

    try:
        result = await asyncio.to_thread(_respond)
        if result is None:
            return "[calendar] Google credentials not found or missing calendar scope."
        return f"Responded '{response}' to event: {result.get('summary', event_id)}"
    except Exception as e:
        return f"[calendar respond failed] {e}"


async def find_free_slots(date_iso: str, duration_minutes: int = 30, calendar_id: str = "primary") -> str:
    """Find available time slots on a given date (YYYY-MM-DD), 8am-6pm UTC."""
    def _fetch():
        svc = _get_google_service("calendar", "v3")
        if not svc:
            return None
        day_start = datetime.fromisoformat(f"{date_iso}T08:00:00+00:00")
        day_end   = datetime.fromisoformat(f"{date_iso}T18:00:00+00:00")
        result = svc.freebusy().query(body={
            "timeMin": day_start.isoformat(),
            "timeMax": day_end.isoformat(),
            "items": [{"id": calendar_id}],
        }).execute()
        busy = result.get("calendars", {}).get(calendar_id, {}).get("busy", [])
        return busy, day_start, day_end

    try:
        data = await asyncio.to_thread(_fetch)
        if data is None:
            return "[calendar] Google credentials not found or missing calendar scope."
        busy_blocks, day_start, day_end = data

        busy_ranges = [
            (datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"]))
            for b in busy_blocks
        ]

        slot_len = timedelta(minutes=duration_minutes)
        free_slots = []
        cursor = day_start
        while cursor + slot_len <= day_end:
            slot_end = cursor + slot_len
            conflict = any(s < slot_end and e > cursor for s, e in busy_ranges)
            if not conflict:
                free_slots.append(f"{cursor.strftime('%H:%M')}–{slot_end.strftime('%H:%M')} UTC")
            cursor += timedelta(minutes=30)

        if not free_slots:
            return f"No free {duration_minutes}-min slots on {date_iso} (8am–6pm UTC)."
        return f"Free {duration_minutes}-min slots on {date_iso}:\n" + "\n".join(free_slots[:12])
    except Exception as e:
        return f"[calendar free_slots failed] {e}"


HANDLERS = {
    "list_calendar_events":       list_calendar_events,
    "create_calendar_event":      create_calendar_event,
    "respond_to_calendar_event":  respond_to_calendar_event,
    "find_free_slots":            find_free_slots,
}

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "list_calendar_events",
            "description": "List upcoming calendar events. Shows event IDs, times, titles, attendees.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days_ahead":  {"type": "integer", "default": 7, "description": "How many days ahead to look"},
                    "calendar_id": {"type": "string", "default": "primary"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": "Create a calendar event and optionally invite attendees. Sends email invites automatically when attendees are specified.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title":       {"type": "string"},
                    "start_iso":   {"type": "string", "description": "Start time in ISO 8601 format, e.g. 2026-06-15T14:00:00+00:00"},
                    "end_iso":     {"type": "string", "description": "End time in ISO 8601 format"},
                    "attendees":   {"type": "array", "items": {"type": "string"}, "description": "List of email addresses to invite"},
                    "description": {"type": "string"},
                    "location":    {"type": "string"},
                    "calendar_id": {"type": "string", "default": "primary"},
                },
                "required": ["title", "start_iso", "end_iso"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "respond_to_calendar_event",
            "description": "Accept, decline, or mark tentative for a calendar event you were invited to.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id":    {"type": "string", "description": "Event ID from list_calendar_events"},
                    "response":    {"type": "string", "enum": ["accepted", "declined", "tentative"]},
                    "calendar_id": {"type": "string", "default": "primary"},
                },
                "required": ["event_id", "response"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_free_slots",
            "description": "Find available time slots on a given date for scheduling a meeting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date_iso":         {"type": "string", "description": "Date in YYYY-MM-DD format"},
                    "duration_minutes": {"type": "integer", "default": 30},
                    "calendar_id":      {"type": "string", "default": "primary"},
                },
                "required": ["date_iso"],
            },
        },
    },
]
