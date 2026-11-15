# IT4123 ICA 02 - EventSphere Report

**Live URL:** `<paste Render HTTPS URL>`  **GitHub:** `<paste repo URL>`

**Demo accounts** - Admin `admin@eventsphere.test` / `Admin@12345`; Staff `staff@eventsphere.test` / `Staff@12345`;
Attendee `attendee@eventsphere.test` / `Attendee@12345`.

## 1. Architecture
Browser (Bootstrap, 375 px+) -> FastAPI routes (`WebUI` gateway) -> **message bus** -> agents -> SQLAlchemy
(PostgreSQL in production, SQLite locally). Routes never contain business decisions: they translate a user action
into a JSON message (`REQUEST`) and show the agent's reply (`AGREE`, `REFUSE`, `PROPOSE`, `INFORM`, `FAILURE`).

Message = `{sender, receiver, performative, content, conversation_id}` (validated with Pydantic). Every message
(including each reply and each broadcast) is saved in `message_log` and shown on **Admin -> Agent log**; clicking a
conversation id shows one user action across all agents. Agents never import or call each other; they only call
`bus.send()` (point to point) or `bus.broadcast()` (INFORM to listeners).

Persistent beliefs are stored in `agent_state` (e.g. `repeat_scans:<code>`, `offers_expired`, `oversell_prevented`)
and survive restarts; the dashboard displays them.

**Example conversation - booking:** `WebUI -> BookingAgent REQUEST book` -> `BookingAgent -> TicketAgent REQUEST issue`
-> `TicketAgent -> BookingAgent AGREE codes` -> `BookingAgent -> ALL INFORM booking_confirmed` ->
(NotificationAgent creates the customer notification) -> `BookingAgent -> WebUI AGREE`.

**Reactive behaviour:** WaitlistAgent and NotificationAgent listen to broadcast INFORMs (`seats_freed`,
`booking_cancelled`, `event_cancelled`, ...). **Proactive behaviour:** a timer inside the application calls
`WaitlistAgent.tick()` every `SWEEP_SECONDS` (30 s) with no user action.

## 2. Agents and decision rules
| Agent | Goal | Rules |
|---|---|---|
| **VenueAgent** | Calendar free of clashes | V1 refuse invalid/unknown slot. V2 refuse overlap with any non-cancelled event or confirmed/pending hire, including a **30-min turnaround buffer** (REFUSE carries alternative free slots). H1 refuse if venue not open for hire. H2 refuse duration <1 h or >12 h. H3 refuse start <1 h from now. H4 guests must be 1..capacity, else PROPOSE larger venues. H5 clash -> PROPOSE alternative times at the same venue and other venues at the same time. H6 price = rate x hours, x1.25 Sat/Sun, x1.15 after 18:00. H7 **auto-confirm** if <=4 h, guests <=80% capacity and >=48 h notice; otherwise INFORM `escalated` (admin decides). |
| **BookingAgent** | Sell fairly, never oversell | B1 refuse <1 or >6 tickets per request. B2 refuse unless published. B3 refuse after start time. B4 refuse if customer would hold >10 tickets for the event. B5 take seats with one atomic conditional UPDATE; refuse with remaining count if it fails. B6 refund: >=72 h before start 100%, 24-72 h 50%, <24 h 0%. B7 refuse cancellation after start or after any ticket was checked in. B8 event cancelled -> every confirmed booking refunded 100%. |
| **TicketAgent** | Admit each ticket once | T1 issue unique `secrets.token_urlsafe(18)` codes. T2 unknown code -> REFUSE. T3 cancelled ticket -> REFUSE. T4 wrong event -> REFUSE with the real event name. T5 check-in window: opens 2 h before start, closes at event end. T6 already used -> REFUSE and count the repeat in its beliefs; from the 2nd repeat broadcast `suspicious_scan`. T7 void refused if a ticket was already used (unless event cancelled). |
| **WaitlistAgent** | Pass freed seats on | W1 join refused if seats are available, duplicate entry, or event closed. W2 FIFO; an entry too big to fit is skipped but keeps its place. W3 offer **holds** seats 10 min (`held` column, invisible to other buyers). W4 timer sweep expires unclaimed offers, releases seats, re-offers to the next in line. W5 claim only by owner before expiry; books through BookingAgent releasing the hold. W6 event cancelled -> entries cancelled and holds released. |
| **NotificationAgent** | Tell the right person | N1 listens to all broadcasts. N2 customer events -> that customer. N3 hire escalations and suspicious scans -> admins. N4 other events ignored. |

## 3. Concurrency (no overselling)
Seats are taken with a **single atomic SQL statement**:
`UPDATE ticket_categories SET sold = sold + :q WHERE id = :id AND sold + held + :q <= quantity`.
The database serialises writers on that row (row lock in PostgreSQL); only a statement that still satisfies the
condition changes a row. If `rowcount != 1` the agent REFUSEs. There is no read-then-write gap, so two people
booking the last seat cannot both succeed. The booking, ticket issue and log rows are in one transaction: any failure
rolls the seat back. Waitlist holds use the same condition, so held seats can't be sold to others.
Test: `test_never_oversells_concurrent_last_seat` (5 threads, 1 seat).

## 4. Security
PBKDF2-SHA256 (200 000 iterations, per-user salt) password hashing; signed session cookies (Secure on HTTPS);
role dependency on every admin/staff route (303 to login if anonymous, 403 if wrong role); CSRF token on every
POST form (also on the scanner's fetch call); secrets only from environment variables; Pydantic validation;
payments are simulated - a reference `SIM-XXXXXXXXXX` is stored, no card data is ever collected.
Agent HTTP inbox requires an admin session or `X-Agent-Key`.

## 5. Advanced features implemented
1. Waitlist with timed holds, visible countdown, automatic re-offer (timer).
2. Weekend and evening/peak pricing for hire (VenueAgent H6).
3. Suspicious repeated-scan detection (TicketAgent T6) with admin alert.
4. Agents reachable over HTTP: `POST /agents/{name}/inbox` with a JSON message (documented in `/docs`).
5. Dashboard charts (tickets per event, check-ins over time) with Chart.js.
6. Advanced search: keyword, category, venue, date range, max price.
Not implemented: e-mail notifications, cart holds.

## 6. Testing
`pytest -v` - 24 tests: overselling (sequential + concurrent), refund tiers, event-cancel refunds, double check-in,
wrong event/cancelled/unknown, check-in window, suspicious scans, venue buffer, hire pricing/escalation/counter-proposal,
waitlist offer/expiry/claim, notification, message traceability, password hashing, CSRF, role checks.

## 7. Deployment
Render blueprint (`render.yaml`): web service + PostgreSQL, HTTPS, env vars. Free tier sleeps - open before the viva.
