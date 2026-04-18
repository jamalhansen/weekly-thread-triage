from datetime import date
from local_first_common.obsidian import load_goal_context as _load_goal_context, get_week_dates as _get_week_dates

def load_goal_context(vault_path, target_date: date = None):
    return _load_goal_context(vault_path, target_date)

def get_week_dates(week_str: str):
    # week_str is YYYY-WNN
    from datetime import datetime
    d = datetime.strptime(week_str + "-1", "%G-W%V-%u").date()
    return _get_week_dates(d)
