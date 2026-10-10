"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import json
import logging
import os
import re
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

load_dotenv()

# --------------------------------------------------------------------------- #
# Config & client
# --------------------------------------------------------------------------- #
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
ALL_STATUSES = ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

logger = logging.getLogger("attendance")

uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
db_name = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(uri, tz_aware=True, tzinfo=UTC, serverSelectionTimeoutMS=5000)
db = client[db_name]

_indexes_ready = False


# --------------------------------------------------------------------------- #
# Index creation
# --------------------------------------------------------------------------- #
def ensure_indexes():
    """Create all indexes idempotently. Safe to call multiple times."""
    global _indexes_ready
    logs = db.attendance_logs
    emps = db.employees

    # attendance_logs indexes
    idx_defs_logs = [
        ("uniq_emp_date", [("emp_code", ASCENDING), ("date", ASCENDING)], True),
        ("date_emp", [("date", DESCENDING), ("emp_code", ASCENDING)], False),
        ("status_date_emp", [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], False),
        ("emp_punch_in", [("emp_code", ASCENDING), ("punch_in", DESCENDING)], False),
    ]
    for name, keys, unique in idx_defs_logs:
        try:
            logs.create_index(keys, name=name, unique=unique)
        except OperationFailure as e:
            logger.warning("Index %s on attendance_logs failed: %s", name, e)

    # employees indexes
    idx_defs_emps = [
        ("uniq_emp_code", [("emp_code", ASCENDING)], True),
        ("dept_emp", [("department", ASCENDING), ("emp_code", ASCENDING)], False),
        ("joined_on", [("joined_on", ASCENDING)], False),
    ]
    for name, keys, unique in idx_defs_emps:
        try:
            emps.create_index(keys, name=name, unique=unique)
        except OperationFailure as e:
            logger.warning("Index %s on employees failed: %s", name, e)

    _indexes_ready = True


# --------------------------------------------------------------------------- #
# Lifespan
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(application: FastAPI):
    try:
        ensure_indexes()
    except Exception as e:
        logger.error("Failed to create indexes at startup: %s", e)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Global exception handler for PyMongo errors -> 503
# --------------------------------------------------------------------------- #
@app.exception_handler(PyMongoError)
async def pymongo_exception_handler(request: Request, exc: PyMongoError):
    if isinstance(exc, DuplicateKeyError):
        raise exc  # let endpoint handle it
    return JSONResponse(status_code=503, content={"detail": "Database unavailable"})


# --------------------------------------------------------------------------- #
# Time / rule helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Convert epoch ms to UTC datetime, truncated to whole seconds."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt_val: datetime) -> int:
    """Convert datetime to epoch ms using exact integer math."""
    if dt_val.tzinfo is None:
        dt_val = dt_val.replace(tzinfo=UTC)
    return (dt_val - EPOCH) // timedelta(milliseconds=1)


def hhmm_to_min(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """Compute the attendance date (IST calendar date) from punch_in and shift times (R1)."""
    ist = punch_in.astimezone(IST)
    d = ist.date()
    s, e = hhmm_to_min(shift_start), hhmm_to_min(shift_end)
    # Overnight shift: if punch-in time is before shift_end, it belongs to previous day
    if e <= s and (ist.hour * 3600 + ist.minute * 60 + ist.second) < e * 60:
        d -= timedelta(days=1)
    return d.isoformat()


def shift_bounds(date_str: str, shift_start: str, shift_end: str):
    """Return (start_dt, end_dt) anchored to the record date in IST."""
    d = date.fromisoformat(date_str)
    s, e = hhmm_to_min(shift_start), hhmm_to_min(shift_end)
    start = datetime.combine(d, time(s // 60, s % 60), tzinfo=IST)
    end = datetime.combine(d, time(e // 60, e % 60), tzinfo=IST)
    if e <= s:
        end += timedelta(days=1)
    return start, end


def compute_late(punch_in: datetime, date_str: str, ss: str, se: str) -> int:
    """R2: late minutes. Grace = 10 min (600 sec). Floor division from shift start."""
    start, _ = shift_bounds(date_str, ss, se)
    delta = int((punch_in - start).total_seconds())
    return delta // 60 if delta > 600 else 0


def compute_overtime(punch_out: datetime, date_str: str, ss: str, se: str) -> int:
    """R3: overtime minutes. Only counted if >= 30."""
    _, end = shift_bounds(date_str, ss, se)
    mins = int((punch_out - end).total_seconds()) // 60
    return mins if mins >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: work hours, 2 dp half-up using Decimal."""
    secs = int((punch_out - punch_in).total_seconds())
    return float(
        (Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day = true when rounded work_hours < 4.50."""
    return work_hours < 4.5


def round_half_up(x, places: int) -> Decimal:
    """Round a value half-up to the given number of decimal places."""
    return Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def working_days_in_month(month: str, joined_on: str) -> int:
    """Count Mon-Fri days from max(first of month, joined_on) to end of month."""
    y, m = map(int, month.split("-"))
    first = date(y, m, 1)
    last = date(y, m, monthrange(y, m)[1])
    join_date = date.fromisoformat(joined_on)
    start = max(first, join_date)
    if start > last:
        return 0
    count = 0
    d = start
    while d <= last:
        if d.weekday() < 5:  # Mon=0 ... Fri=4
            count += 1
        d += timedelta(days=1)
    return count


def month_bounds(month: str):
    """Return (first_date_str, last_date_str) for a YYYY-MM month string."""
    validate_month_str(month, "month")
    y, m = map(int, month.split("-"))
    first = date(y, m, 1)
    last = date(y, m, monthrange(y, m)[1])
    return first.isoformat(), last.isoformat()


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
EMP_CODE_RE = re.compile(r"^EMP\d{4,6}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def unprocessable(msg: str, loc: list) -> HTTPException:
    return HTTPException(422, detail=[{"loc": loc, "msg": msg, "type": "value_error"}])


def validate_date_str(value: str, param_name: str) -> str:
    if not DATE_RE.match(value):
        raise unprocessable(f"Invalid date format: {value}", ["query", param_name])
    try:
        date.fromisoformat(value)
    except ValueError:
        raise unprocessable(f"Invalid date: {value}", ["query", param_name])
    return value


def validate_month_str(value: str, param_name: str = "month") -> str:
    if not MONTH_RE.match(value):
        raise unprocessable(f"Invalid month format: {value}", ["query", param_name])
    try:
        y, m = map(int, value.split("-"))
        date(y, m, 1)
    except ValueError:
        raise unprocessable(f"Invalid month: {value}", ["query", param_name])
    return value


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #
EpochMs = StrictInt  # validated by Field constraints


class EmployeeCreate(BaseModel):
    model_config = {"extra": "ignore"}

    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v):
        if not DATE_RE.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError(f"Invalid date: {v}")
        return v

    @model_validator(mode="after")
    def shift_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInRequest(BaseModel):
    model_config = {"extra": "ignore"}

    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    model_config = {"extra": "ignore"}

    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    model_config = {"extra": "ignore"}

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


def serialize_record(doc: dict) -> dict:
    pi = doc.get("punch_in")
    po = doc.get("punch_out")
    history = doc.get("history") or []
    serialized_history = []
    for entry in history:
        se = {
            "at": dt_to_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": {},
        }
        for key, change in entry.get("changes", {}).items():
            c = {}
            for direction in ("from", "to"):
                val = change.get(direction)
                if key in ("punch_in", "punch_out") and isinstance(val, datetime):
                    c[direction] = dt_to_ms(val)
                elif key in ("punch_in", "punch_out") and val is None:
                    c[direction] = None
                else:
                    c[direction] = val
            se["changes"][key] = c
        serialized_history.append(se)

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(pi) if isinstance(pi, datetime) else None,
        "punch_out": dt_to_ms(po) if isinstance(po, datetime) else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": doc.get("half_day") or False,
        "history": serialized_history,
    }


# --------------------------------------------------------------------------- #
# Query / pipeline builders (shared by endpoints and explain)
# --------------------------------------------------------------------------- #
def build_attendance_find(
    emp_code=None, date_from=None, date_to=None, status=None, page=1, page_size=20
):
    """Build filter, sort, skip, limit, hint for the attendance list query."""
    f: dict[str, Any] = {}
    if emp_code:
        f["emp_code"] = emp_code
    if date_from or date_to:
        f["date"] = {}
        if date_from:
            f["date"]["$gte"] = date_from
        if date_to:
            f["date"]["$lte"] = date_to
    if status:
        f["status"] = status

    sort = [("date", DESCENDING), ("emp_code", ASCENDING)]
    skip = (page - 1) * page_size
    limit = page_size

    # Choose hint based on the most selective filter present
    if emp_code:
        hint = "uniq_emp_date"
    elif status:
        hint = "status_date_emp"
    else:
        hint = "date_emp"

    return f, sort, skip, limit, hint


# Shared aggregation expression fragments
IS_PRESENT = {"$in": ["$status", PRESENCE]}
LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
OT_MIN = {"$ifNull": ["$overtime_minutes", 0]}
IS_LATE = {"$cond": [{"$gt": [LATE_MIN, 0]}, 1, 0]}
IS_WEEKDAY = {
    "$in": [
        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}},
        [2, 3, 4, 5, 6],
    ]
}
# Present days doubled as integers for exact arithmetic (full day = 2, half day = 1)
PRESENT2 = {
    "$cond": [
        {"$and": [IS_PRESENT, IS_WEEKDAY]},
        {"$cond": [{"$eq": ["$half_day", True]}, 1, 2]},
        0,
    ]
}


def build_monthly_pipeline(emp_code: str, first: str, last: str):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                "present2": {"$sum": PRESENT2},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": IS_LATE},
                "total_late_minutes": {"$sum": LATE_MIN},
                "total_overtime_minutes": {"$sum": OT_MIN},
            }
        },
    ]


def build_summary_pipeline(first: str, last: str, department=None):
    sub = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                "present2": {"$sum": PRESENT2},
                "hours_n": {
                    "$sum": {
                        "$cond": [
                            {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]},
                            1,
                            0,
                        ]
                    }
                },
                "hours_x100": {
                    "$sum": {
                        "$cond": [
                            {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]},
                            {"$round": [{"$multiply": ["$work_hours", 100]}, 0]},
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": IS_LATE},
                "total_late_minutes": {"$sum": LATE_MIN},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
    ]
    match: dict[str, Any] = {"joined_on": {"$lte": last}}
    if department:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": sub,
                "as": "m",
            }
        },
        {"$set": {"m": {"$ifNull": [{"$first": "$m"}, {}]}}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present2": {"$sum": "$m.present2"},
                "hours_n": {"$sum": "$m.hours_n"},
                "hours_x100": {"$sum": "$m.hours_x100"},
                "late_count": {"$sum": "$m.late_count"},
                "total_late_minutes": {"$sum": "$m.total_late_minutes"},
                "leave_count": {"$sum": "$m.leave_count"},
                "on_duty_count": {"$sum": "$m.on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def build_leaderboard_pipeline(first: str, last: str, limit: int, department=None):
    p = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": LATE_MIN},
                "late_count": {"$sum": IS_LATE},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department:
        p.append({"$match": {"emp.department": department}})
    p += [
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return p


def build_trend_pipeline(dept: str, frm: date, to: date, codes: list, joined: list):
    d0 = datetime(frm.year, frm.month, frm.day, tzinfo=UTC)
    d1 = datetime(to.year, to.month, to.day, tzinfo=UTC) + timedelta(days=1)  # exclusive upper
    return [
        {"$match": {"department": dept}},
        {"$limit": 1},
        {"$replaceRoot": {"newRoot": {"d": d0}}},
        {
            "$densify": {
                "field": "d",
                "range": {"step": 1, "unit": "day", "bounds": [d0, d1]},
            }
        },
        {
            "$set": {
                "day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                "dow": {"$dayOfWeek": "$d"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "day",
                "foreignField": "date",
                "pipeline": [
                    {"$match": {"emp_code": {"$in": codes}}},
                    {
                        "$group": {
                            "_id": None,
                            "present2": {
                                "$sum": {
                                    "$cond": [
                                        IS_PRESENT,
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                1,
                                                2,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {"$sum": IS_LATE},
                        }
                    },
                ],
                "as": "m",
            }
        },
        {"$set": {"m": {"$ifNull": [{"$first": "$m"}, {}]}}},
        {
            "$set": {
                "is_working_day": {
                    "$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": joined,
                            "as": "j",
                            "cond": {"$lte": ["$$j", "$day"]},
                        }
                    }
                },
                "present2": {"$ifNull": ["$m.present2", 0]},
                "late_count": {"$ifNull": ["$m.late_count", 0]},
            }
        },
        # attendance_rate in 1/10000 units with half-up rounding via floor((p2*10000+h)/(2h))
        {
            "$set": {
                "rate_e4": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$floor": {
                                "$divide": [
                                    {
                                        "$add": [
                                            {"$multiply": ["$present2", 10000]},
                                            "$headcount",
                                        ]
                                    },
                                    {"$multiply": [2, "$headcount"]},
                                ]
                            }
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$set": {
                "has_rate": {"$cond": [{"$eq": ["$rate_e4", None]}, 0, 1]}
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"d": 1},
                "output": {
                    "w_sum": {
                        "$sum": "$rate_e4",
                        "window": {"documents": [-6, 0]},
                    },
                    "w_n": {
                        "$sum": "$has_rate",
                        "window": {"documents": [-6, 0]},
                    },
                },
            }
        },
        {"$sort": {"d": 1}},
        {
            "$project": {
                "_id": 0,
                "date": "$day",
                "is_working_day": 1,
                "headcount": 1,
                "late_count": 1,
                "present_count": {"$divide": ["$present2", 2]},
                "attendance_rate": {
                    "$cond": [
                        {"$eq": ["$rate_e4", None]},
                        None,
                        {"$divide": ["$rate_e4", 10000]},
                    ]
                },
                "moving_avg_7d": {
                    "$cond": [
                        {"$gt": ["$w_n", 0]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$divide": [
                                            {
                                                "$add": [
                                                    {"$multiply": ["$w_sum", 2]},
                                                    "$w_n",
                                                ]
                                            },
                                            {"$multiply": ["$w_n", 2]},
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
    ]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

# --- Health ---------------------------------------------------------------- #
@app.get("/health")
def health():
    global _indexes_ready
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, detail="Database unavailable")
    if not _indexes_ready:
        try:
            ensure_indexes()
        except Exception:
            pass
    return {"status": "ok"}


# --- Employees ------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=(datetime.now(UTC).microsecond // 1000) * 1000)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    doc.pop("_id", None)
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict[str, Any] = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --- Attendance ------------------------------------------------------------ #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    if body.punched_at is not None:
        ts = ms_to_dt(body.punched_at)
    else:
        ts = datetime.now(UTC).replace(microsecond=0)

    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late = compute_late(ts, d, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="Already punched in for this date")
    doc.pop("_id", None)
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    # 1. Validate. Find employee.
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    # 2. Timestamp
    if body.punched_at is not None:
        t = ms_to_dt(body.punched_at)
    else:
        t = datetime.now(UTC).replace(microsecond=0)

    # 3. Find the latest record with punch_in <= t
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": t}},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(404, detail="No punch-in found")

    # 4. Already punched out?
    if rec.get("punch_out") is not None:
        raise HTTPException(409, detail="Already punched out")

    # 5. Time window checks
    if t <= rec["punch_in"]:
        raise unprocessable("punch_out must be after punch_in", ["body", "punched_at"])
    if (t - rec["punch_in"]).total_seconds() > 86400:
        raise unprocessable("punch_out must be within 24 hours of punch_in", ["body", "punched_at"])

    # 6. Compute derived fields
    wh = compute_work_hours(rec["punch_in"], t)
    ot = compute_overtime(t, rec["date"], emp["shift_start"], emp["shift_end"])
    hd = compute_half_day(wh)

    # 7. Atomic update (punch_out: None in filter guards against concurrent punch-outs)
    result = db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": t, "work_hours": wh, "overtime_minutes": ot, "half_day": hd}},
    )
    if result.matched_count == 0:
        raise HTTPException(409, detail="Concurrent punch-out detected")

    # 8. Return updated record
    updated = db.attendance_logs.find_one({"_id": rec["_id"]})
    return serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # Validate dates
    if date_from is not None:
        validate_date_str(date_from, "date_from")
    if date_to is not None:
        validate_date_str(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise unprocessable("date_from must be <= date_to", ["query", "date_from"])

    f, sort, skip, limit, hint = build_attendance_find(
        emp_code=emp_code,
        date_from=date_from,
        date_to=date_to,
        status=status,
        page=page,
        page_size=page_size,
    )
    total = db.attendance_logs.count_documents(f)
    items = list(
        db.attendance_logs.find(f, {"_id": 0})
        .sort(sort)
        .hint(hint)
        .skip(skip)
        .limit(limit)
    )
    return {
        "items": [serialize_record(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --- PATCH (Regularize) --------------------------------------------------- #
@app.patch("/attendance/{emp_code}/{date_str}")
def regularize_attendance(emp_code: str, date_str: str, body: RegularizeRequest):
    # Validate path date
    validate_date_str(date_str, "date")

    # 404 checks
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not rec:
        raise HTTPException(404, detail="Attendance record not found")

    # Current values (with defaults for legacy docs)
    old_status = rec["status"]
    old_in = rec.get("punch_in")
    old_out = rec.get("punch_out")
    old_wh = rec.get("work_hours")
    old_late = rec.get("late_minutes") or 0
    old_ot = rec.get("overtime_minutes") or 0
    old_hd = rec.get("half_day") or False

    # Build target state
    new_status = body.status if body.status is not None else old_status

    if body.punch_in is not None:
        new_in = ms_to_dt(body.punch_in)
    else:
        new_in = old_in

    if body.punch_out is not None:
        new_out = ms_to_dt(body.punch_out)
    else:
        new_out = old_out

    # ABSENT/LEAVE: clear punch times; supplying punch_in/punch_out with them is 422
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable(
                "Cannot supply punch times with ABSENT/LEAVE status",
                ["body", "status"],
            )
        new_in = None
        new_out = None
        new_wh = None
        new_late = 0
        new_ot = 0
        new_hd = False
    else:
        # Presence status: punch_in must exist
        if new_in is None:
            raise unprocessable(
                "Presence status requires a punch_in",
                ["body", "punch_in"],
            )
        # punch_in must stay on the record's attendance date
        computed_date = attendance_date(new_in, emp["shift_start"], emp["shift_end"])
        if computed_date != date_str:
            raise unprocessable(
                "punch_in must stay on the record's attendance date",
                ["body", "punch_in"],
            )
        # Validate punch_out constraints
        if new_out is not None:
            if new_out <= new_in:
                raise unprocessable("punch_out must be after punch_in", ["body", "punch_out"])
            if (new_out - new_in).total_seconds() > 86400:
                raise unprocessable("punch_out must be within 24 hours", ["body", "punch_out"])

        # Recompute derived fields
        new_late = compute_late(new_in, date_str, emp["shift_start"], emp["shift_end"])
        if new_out is not None:
            new_wh = compute_work_hours(new_in, new_out)
            new_ot = compute_overtime(new_out, date_str, emp["shift_start"], emp["shift_end"])
            new_hd = compute_half_day(new_wh)
        else:
            new_wh = None
            new_ot = 0
            new_hd = False

    # Build changes dict (only fields that actually changed)
    changes: dict[str, dict] = {}

    def _dt_eq(a, b):
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        return a == b

    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if not _dt_eq(new_in, old_in):
        changes["punch_in"] = {"from": old_in, "to": new_in}
    if not _dt_eq(new_out, old_out):
        changes["punch_out"] = {"from": old_out, "to": new_out}
    if new_wh != old_wh:
        changes["work_hours"] = {"from": old_wh, "to": new_wh}
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}
    if new_ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_ot}
    if new_hd != old_hd:
        changes["half_day"] = {"from": old_hd, "to": new_hd}

    if not changes:
        raise unprocessable("No changes detected", ["body"])

    # Build history entry
    entry = {
        "at": datetime.now(UTC).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency update
    update_filter: dict[str, Any] = {
        "_id": rec["_id"],
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
    }
    # History guard: if history exists and is a list, match by size; if missing, match by $exists false
    old_history = rec.get("history")
    if isinstance(old_history, list):
        update_filter["history"] = {"$size": len(old_history)}
    else:
        update_filter["history"] = {"$exists": False}

    new_fields = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": new_wh,
        "late_minutes": new_late,
        "overtime_minutes": new_ot,
        "half_day": new_hd,
    }

    result = db.attendance_logs.update_one(
        update_filter,
        {"$set": new_fields, "$push": {"history": entry}},
    )
    if result.matched_count == 0:
        raise HTTPException(409, detail="Concurrent modification detected")

    # Return updated record
    updated = db.attendance_logs.find_one({"_id": rec["_id"]})
    return serialize_record(updated)


# --- Analytics: Employee Monthly ------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    validate_month_str(month, "month")
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    first, last = month_bounds(month)
    pipeline = build_monthly_pipeline(emp_code, first, last)
    result = list(db.attendance_logs.aggregate(pipeline, hint="uniq_emp_date"))

    if result:
        r = result[0]
        present2 = r["present2"]
        leave_days = r["leave_days"]
        late_count = r["late_count"]
        total_late = r["total_late_minutes"]
        total_ot = r["total_overtime_minutes"]
    else:
        present2 = 0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    wd = working_days_in_month(month, emp["joined_on"])
    present_days = present2 / 2

    if wd == 0:
        attendance_pct = None
    else:
        attendance_pct = float(
            round_half_up(Decimal(present2) * 50 / Decimal(wd), 2)
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


# --- Analytics: Department Summary ----------------------------------------- #
@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    validate_month_str(month, "month")
    first, last = month_bounds(month)
    pipeline = build_summary_pipeline(first, last, department)

    hint = "dept_emp" if department else "joined_on"
    result = list(db.employees.aggregate(pipeline, hint=hint))

    items = []
    for r in result:
        present_days = r["present2"] / 2
        hours_n = r["hours_n"]
        hours_x100 = r["hours_x100"]

        if hours_n == 0:
            avg_wh = None
        else:
            avg_wh = float(
                round_half_up(Decimal(int(hours_x100)) / Decimal(hours_n) / 100, 2)
            )

        items.append(
            {
                "department": r["_id"],
                "headcount": r["headcount"],
                "present_days": present_days,
                "avg_work_hours": avg_wh,
                "late_count": r["late_count"],
                "total_late_minutes": r["total_late_minutes"],
                "leave_count": r["leave_count"],
                "on_duty_count": r["on_duty_count"],
            }
        )

    return {"month": month, "items": items}


# --- Analytics: Leaderboard ----------------------------------------------- #
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    validate_month_str(month, "month")
    first, last = month_bounds(month)
    pipeline = build_leaderboard_pipeline(first, last, limit, department)
    result = list(db.attendance_logs.aggregate(pipeline, hint="date_emp"))
    return {"month": month, "items": result}


# --- Analytics: Department Trend ------------------------------------------- #
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    # Validate dates
    validate_date_str(date_from, "from")
    validate_date_str(date_to, "to")

    frm = date.fromisoformat(date_from)
    to = date.fromisoformat(date_to)

    if to < frm:
        raise unprocessable("'to' must be >= 'from'", ["query", "to"])
    if (to - frm).days + 1 > 92:
        raise unprocessable("Range must not exceed 92 days", ["query", "to"])

    # Fetch department employees
    emp_docs = list(
        db.employees.find(
            {"department": department},
            {"emp_code": 1, "joined_on": 1, "_id": 0},
        ).hint("dept_emp")
    )
    if not emp_docs:
        raise HTTPException(404, detail="Department not found")

    codes = [e["emp_code"] for e in emp_docs]
    joined = sorted([e["joined_on"] for e in emp_docs])

    pipeline = build_trend_pipeline(department, frm, to, codes, joined)
    result = list(db.employees.aggregate(pipeline, hint="dept_emp"))

    return {"department": department, "items": result}


# --- Admin: Explain ------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, alias="date_from"),
    date_to: Optional[str] = Query(None, alias="date_to"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    trend_from: Optional[str] = Query(None, alias="from"),
    trend_to: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        # Validate dates if provided
        if date_from is not None:
            validate_date_str(date_from, "date_from")
        if date_to is not None:
            validate_date_str(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise unprocessable("date_from must be <= date_to", ["query", "date_from"])

        f, sort, sk, lim, hint = build_attendance_find(
            emp_code=emp_code,
            date_from=date_from,
            date_to=date_to,
            status=status,
            page=page,
            page_size=page_size,
        )
        explain_cmd = {
            "explain": {
                "find": "attendance_logs",
                "filter": f,
                "sort": dict(sort),
                "skip": sk,
                "limit": lim,
                "hint": hint,
            },
            "verbosity": "executionStats",
        }
        collection = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise unprocessable("emp_code is required", ["query", "emp_code"])
        if not month:
            raise unprocessable("month is required", ["query", "month"])
        validate_month_str(month, "month")
        first, last = month_bounds(month)
        pipeline = build_monthly_pipeline(emp_code, first, last)
        explain_cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
                "hint": "uniq_emp_date",
            },
            "verbosity": "executionStats",
        }
        collection = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise unprocessable("month is required", ["query", "month"])
        validate_month_str(month, "month")
        first, last = month_bounds(month)
        pipeline = build_summary_pipeline(first, last, department)
        hint = "dept_emp" if department else "joined_on"
        explain_cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
                "hint": hint,
            },
            "verbosity": "executionStats",
        }
        collection = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise unprocessable("month is required", ["query", "month"])
        validate_month_str(month, "month")
        first, last = month_bounds(month)
        pipeline = build_leaderboard_pipeline(first, last, limit, department)
        explain_cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
                "hint": "date_emp",
            },
            "verbosity": "executionStats",
        }
        collection = "attendance_logs"

    elif endpoint == "department_trend":
        if not department:
            raise unprocessable("department is required", ["query", "department"])
        if not trend_from:
            raise unprocessable("'from' is required", ["query", "from"])
        if not trend_to:
            raise unprocessable("'to' is required", ["query", "to"])
        validate_date_str(trend_from, "from")
        validate_date_str(trend_to, "to")

        frm = date.fromisoformat(trend_from)
        to_d = date.fromisoformat(trend_to)
        if to_d < frm:
            raise unprocessable("'to' must be >= 'from'", ["query", "to"])
        if (to_d - frm).days + 1 > 92:
            raise unprocessable("Range must not exceed 92 days", ["query", "to"])

        emp_docs = list(
            db.employees.find(
                {"department": department},
                {"emp_code": 1, "joined_on": 1, "_id": 0},
            ).hint("dept_emp")
        )
        codes = [e["emp_code"] for e in emp_docs] if emp_docs else []
        joined = sorted([e["joined_on"] for e in emp_docs]) if emp_docs else []

        pipeline = build_trend_pipeline(department, frm, to_d, codes, joined)
        explain_cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
                "hint": "dept_emp",
            },
            "verbosity": "executionStats",
        }
        collection = "employees"
    else:
        raise unprocessable("Unknown endpoint", ["path", "endpoint"])

    raw = db.command(explain_cmd)
    explain_json = json.loads(json_util.dumps(raw))

    return {"endpoint": endpoint, "collection": collection, "explain": explain_json}
