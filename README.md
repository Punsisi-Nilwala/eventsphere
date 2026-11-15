# EventSphere - agent-based venue booking & digital ticketing (IT4123 ICA 02)

FastAPI + SQLAlchemy + Jinja/Bootstrap. Every booking, hire, refund and check-in decision is made by five
cooperating agents that talk **only** through JSON messages on a message bus (`app/bus.py`).

## Run locally
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # optional; defaults work with SQLite
uvicorn app.main:app --reload
```
Open http://127.0.0.1:8000 . Demo data and accounts are created automatically on first start (`SEED_DEMO=1`).

## Environment variables
| Name | Purpose | Default |
|---|---|---|
| `DATABASE_URL` | PostgreSQL URL in production, SQLite locally | `sqlite:///./eventsphere.db` |
| `SECRET_KEY` | signs session cookies - **set a long random value in production** | dev value |
| `HTTPS_ONLY` | `1` marks the session cookie Secure (use on Render) | `0` |
| `SEED_DEMO` | create demo accounts, venues, events when DB is empty | `1` |
| `TZ_OFFSET_MIN` | local time offset from UTC in minutes | `330` (Sri Lanka) |
| `HOLD_MINUTES` / `SWEEP_SECONDS` | waitlist hold length / timer period | `10` / `30` |
| `AGENT_API_KEY` | lets external callers use `POST /agents/{name}/inbox` via `X-Agent-Key` | empty (disabled) |

Secrets are read from the environment only; `.env` is git-ignored.

## Demo accounts
| Role | Email | Password |
|---|---|---|
| Admin | admin@eventsphere.test | Admin@12345 |
| Staff | staff@eventsphere.test | Staff@12345 |
| Attendee | attendee@eventsphere.test | Attendee@12345 |

(Change or delete these on the live site if you do not want them public.)

## Tests
```bash
pytest -v
```
Tests use a temporary SQLite file and never touch your dev database.

## Deploy on Render
1. Push this repo to GitHub (add the lecturer as collaborator if private).
2. Render -> New -> Blueprint -> select the repo. `render.yaml` creates the web service and a free PostgreSQL DB.
3. After the first deploy open the HTTPS URL (free tier sleeps - open it before the viva).
Manual alternative: Build `pip install -r requirements.txt`, Start `uvicorn app.main:app --host 0.0.0.0 --port $PORT`,
env vars as in the table (`DATABASE_URL` = Render internal DB URL).

## Project layout
```
app/bus.py            message bus (pydantic Message, performatives, logging, broadcast)
app/agents/           venue.py booking.py ticket.py waitlist.py notification.py  (+ base.py persistent state)
app/main.py           routes, auth, CSRF, roles, background timer for WaitlistAgent
app/models.py         SQLAlchemy tables (incl. message_log, agent_state)
app/templates/        mobile-first Bootstrap pages
tests/                pytest suite
docs/REPORT.md        report text (export to PDF for submission)
```
