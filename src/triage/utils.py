from datetime import date

from local_first_common.obsidian import get_week_dates as _get_week_dates
from local_first_common.obsidian import load_goal_context as _load_goal_context


def load_goal_context(vault_path, target_date: date | None = None):
    return _load_goal_context(vault_path, target_date)

def get_week_dates(week_str: str):
    # week_str is YYYY-WNN
    from datetime import datetime
    d = datetime.strptime(week_str + "-1", "%G-W%V-%u").date()  # noqa: DTZ007 - an ISO week string carries no timezone; this is a pure calendar date
    return _get_week_dates(d)
