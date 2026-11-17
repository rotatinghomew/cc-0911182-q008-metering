from datetime import datetime
from zoneinfo import ZoneInfo
from .checks import text,number,moment


def normalize(table,original):
    row=dict(original)
    for field,value in row.items():
        if field in {'value','modulus','old_value','new_value'}:
            row[field]=number(value,True)
        elif field.endswith('_at') or field in {'valid_from','valid_to'}:
            row[field]=moment(value)
        else:
            text(value)
    if table=='campuses':
        ZoneInfo(row['timezone'])
    if table=='placements' and datetime.fromisoformat(row['valid_from'])>=datetime.fromisoformat(row['valid_to']):
        raise ValueError('表计归属区间为空')
    return row
