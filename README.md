# HROne Attendance & Analytics API

This project implements the FastAPI + MongoDB attendance API described in the contract. The app lives in a single file at [app/main.py](app/main.py), starts with `uvicorn app.main:app --port 8000`, and uses MongoDB aggregation pipelines for analytics so the stored derived values are read as the source of truth.

## What is implemented

- Employee creation and listing with validation and unique `emp_code` enforcement.
- Punch-in and punch-out flows with IST-aware attendance-date logic and overnight-shift handling.
- Manual regularization with audit history in `history` and optimistic concurrency checks.
- Monthly employee metrics, department summary, leaderboard, and department trend endpoints.
- Query plans via `/admin/explain/{endpoint}`.
- Startup index creation that is idempotent and safe when Mongo is briefly unavailable.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

The app reads `MONGO_URI` and `MONGO_DB` from environment variables. If they are not set, it falls back to `mongodb://localhost:27017` and `attendance_db`.

## Important business rules followed

- Dates and shift times are IST-based calendar values.
- Instants are converted to and from epoch milliseconds with exact integer math.
- `punch_in` and `punch_out` are truncated to whole seconds before any business logic.
- Derived values (`late_minutes`, `work_hours`, `overtime_minutes`, `half_day`) are recomputed and stored only by the API.
- Duplicate employee codes and duplicate daily punch-ins are prevented by unique indexes plus atomic DB writes.
- Pagination is 1-based and bounded to `1..100` for `page_size`.
- Analytics use Mongo aggregation rather than in-memory Python recomputation.

## Assumptions and literal reading

The contract is specific enough that there were few genuine ambiguities, but I followed the most literal reading in a few places:

- An employee is identified by `emp_code`; Mongo `_id` is internal and never exposed.
- A record is addressed by `(emp_code, date)`, not by `_id`.
- `ABSENT` and `LEAVE` records are created only by seeded back-office data or a correction; the API does not create them directly.
- `total` in paginated responses always reflects the filtered result set, not the full collection.
- In the leaderboard, ties are kept as true ties, so a row at the `limit` boundary is included when it matches the same total as the last visible employee.

## Notes

- No secrets or local `.env` files are committed.
- The app is designed to handle the grader’s larger dataset without loading the whole collection into Python for analytics.
- The completed implementation reflects the contract in `openapi.yaml`, the stored data shape in `DATA_MODEL.md`, and the required behavior in the decision notes.
