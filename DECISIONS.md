# DECISIONS.md

1. I created the specific indexes that match the hot queries in the contract: `uniq_emp_code`, `dept_emp`, and `joined_on` on `employees`, and `uniq_emp_date`, `date_emp`, `status_date_emp`, and `emp_punch_in` on `attendance_logs`. They guard duplicates, support sorting and filtering, and let the app use `hint()` for the slowest read patterns. I did not keep a broader `emp_code`-only index because the combination with `date` and the daily uniqueness rule is the one that matters for correctness.

2. The punch-in race is solved with a unique index on `(emp_code, date)` and an atomic `insert_one()`. Both requests create the same doc shape, but only one insert wins. The loser hits `DuplicateKeyError`, which is turned into a 409. This prevents a silent double-in without relying on any check-then-insert race.

3. The leaderboard ranks by `total_late_minutes` descending and includes all rows whose computed rank is `<= limit`. If two employees tie at the cutoff, both are returned because the code ranks them together and then keeps the rows whose rank is within the limit; sorting by `total_late_minutes` and `emp_code` makes the output stable.

4. The department summary aggregates employees first and then left-joins the matching attendance rows. That means employees with `joined_on <= month_end` are still included even when their `attendance_logs` array is empty, so headcount is preserved while zeros are filled in for missing logs.

5. If this had to serve 100x the data, I would move the monthly and department summaries into materialized aggregate collections or a warehouse-style precompute job, so the API spends less time recalculating large groupings and can serve reads from a smaller, indexed summary layer.
