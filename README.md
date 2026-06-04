# Badminton Booking Manager

The project is now centered on `web_platform`, a FastAPI backend with a single-page Web UI for account management, scheduled booking, court queries, slider matching, and booking submission.

## Structure

```text
badminton_book_system/
├── web_platform/
│   ├── main.py            # FastAPI entrypoint, port 8000
│   ├── booking_engine.py  # booking workflow
│   ├── scheduler.py       # scheduled jobs
│   ├── database.py        # SQLite access
│   ├── static/index.html  # Web UI
│   └── bookings.db        # local account/task data
├── cas_http/
│   ├── booking_api.py     # booking-site HTTP API helpers
│   └── cas_login.py       # CAS login
└── slider_match/
    ├── run_slider_match.py
    └── slider_server.py   # optional standalone Flask debug service
```

## Start

Run:

```bat
web_platform\start.bat
```

Then open:

```text
http://localhost:8000
```

The Web backend calls `slider_match/run_slider_match.py` directly. Deprecated mailbox-based secondary-code automation has been removed.

## Runtime Dependencies

Install via `web_platform/requirements.txt`:

- fastapi
- uvicorn
- aiosqlite
- apscheduler
- httpx
- cryptography
- opencv-python
- numpy

## Sensitive Data

`web_platform/bookings.db` still stores local account/task data, including saved profile credentials. Do not share it unless you intentionally want to share those records.
