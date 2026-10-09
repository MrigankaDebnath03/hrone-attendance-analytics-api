# REVIEW.md

This review covers the defects in the starter implementation. Each item below has a concrete failure mode and the fix that was implemented in the completed version.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health` | The endpoint always returns 200 and never pings MongoDB. | Start the app without a database or with the Mongo service down; `/health` still reports OK. | The final version runs `client.admin.command("ping")` and returns 503 if Mongo is unavailable. |
| 2 | `list_employees` | `skip = page * page_size` is off by one; page 1 skips the first page and page 0 is accepted. | Call `GET /employees?page=1&page_size=20`, then compare the first item: the first row is skipped. | The final logic uses `(page - 1) * page_size` and validates `page >= 1` and `page_size` in `1..100`. |
| 3 | `list_employees` | `total` counts the whole collection instead of the filtered set. | Query with `department=Engineering` and observe `total` still includes all employees. | The final version uses `count_documents(q)` against the active filter. |
| 4 | `list_employees` | Employees are returned in insertion order, not by `emp_code` ascending as required. | Create employees in a non-sorted order and compare ordering. | The final version adds `.sort("emp_code", ASCENDING)` and a `dept_emp` index to support it. |
| 5 | `create_employee` | No validation of `emp_code`, `email`, `joined_on`, `shift_start`, and `shift_end`. | Submit malformed codes, invalid dates, or `shift_start == shift_end`. | The final app uses Pydantic validation plus regex and date checks, returning 422. |
| 6 | `create_employee` | Check-then-insert race allows duplicate employees. There is no unique index. | Create the same employee concurrently; both requests can succeed. | A unique index on `employees.emp_code` plus `insert_one` in a try/except catches DuplicateKeyError and returns 409. |
| 7 | `create_employee` | `created_at` is naive local time and returned as a datetime instead of epoch milliseconds. | Inspect the API response; it returns a Python datetime object, not an integer ms value. | The final version stores UTC-aware datetimes and serializes with `dt_to_ms()`. |
| 8 | `punch_in` | Unknown employee causes a `TypeError` when the code reads `emp["shift_start"]`; the correct status is 404. | Punch in for an unknown `emp_code` and hit a 500. | The final code checks `if not emp: raise HTTPException(404, ...)` before any access. |
| 9 | `punch_in` | `punched_at` is interpreted as local naive time, not UTC/IST, and `0` is treated like “missing”. | Use a timestamp near midnight or `0` and compare the computed attendance date to R1. | The final code converts ms to a UTC-aware datetime via `ms_to_dt()` and treats `None` as “use now”. |
| 10 | `punch_in` | No seconds truncation, bad numeric validation, and floats/strings can slip in. | Send `punched_at=1.0e12`, `"123"`, or a value like `1700000000000` and see a bad record created. | The final request models use `StrictInt` with ge/le validation and truncate to whole seconds by `ms // 1000`. |
| 11 | `punch_in` | It does not guard the daily unique punch-in record atomically; concurrent punches can both succeed. | Send two identical punch-ins together; both can get 201. | The final version adds a unique `(emp_code, date)` index and catches `DuplicateKeyError` for 409. |
| 12 | `compute_late_minutes` | It truncates to minutes before comparing against 10 minutes, so `09:40:01` is miscounted as zero. | Use an input around the boundary and compare to the rule: 09:40:01 must be 10 late minutes. | The final logic computes exact seconds, then floors the difference only after the `> 600` comparison. |
| 13 | `compute_work_hours` | It uses Python `round()`, which is banker's rounding and returns the wrong result for 54 seconds. | Check 54 s / 3600: must be 0.02, not 0.01. | The final code uses `Decimal(...).quantize(..., ROUND_HALF_UP)`. |
| 14 | `compute_overtime` | The algorithm ignores the 30-minute minimum and overnight shift end calculations. | Punch out at 19:10 after an overnight shift or check a short overtime window; values are wrong. | The final version uses `compute_overtime()` with the record date and shift bounds, which accounts for overnight shifts and the 30-minute threshold. |
| 15 | `list_attendance` | The function loads the entire collection into memory and sorts in Python. | Run against ~100k rows and hit slow response times. | The final app uses Mongo `.find(...).sort(...).hint(...)` and pagination directly in the database. |
| 16 | `list_attendance` | It validates nothing: bad dates, bad status, and `date_from > date_to` are accepted. | Query with an invalid or reversed date range. | The final code validates date strings and checks the ordering before hitting the query. |
| 17 | `list_attendance` | It leaks raw Mongo datetime values and `_id` in the response, and legacy docs without defaults are returned unsafely. | Fetch a record missing `history` or `half_day` and inspect the response. | The final serializers normalize to epoch ms and default missing fields to `[]`, `False`, `0`, or `null` as required. |

## Items reviewed and judged not to be defects

- `load_dotenv()` with default `override=False` is correct; real environment variables should win over a local `.env` file.
- Using `409 Conflict` for duplicate employee codes and duplicate daily punch-ins is consistent with the contract.
- The route paths themselves match the contract; the main issue was the internal logic behind them, not the URL design.
- The API's use of unique natural keys (`emp_code`, `date`) is the correct model for this assignment and not a defect.
- The presence of `history` as an audit trail, rather than a write-on-every-event log, is consistent with the specification for manual corrections only.

