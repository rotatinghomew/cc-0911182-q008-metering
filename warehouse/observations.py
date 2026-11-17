from datetime import datetime
from .storage import records


def intervals(connection,meter):
    rows=records(connection,'SELECT * FROM readings WHERE meter_id=?',[meter])
    rows.sort(key=lambda row:(datetime.fromisoformat(row['observed_at']),row['reading_id']))
    result=[]
    for left,right in zip(rows,rows[1:]):
        seconds=(datetime.fromisoformat(right['observed_at'])-datetime.fromisoformat(left['observed_at'])).total_seconds()
        delta=right['value']-left['value']
        issue='重复时点' if seconds==0 else ('读数回退' if delta<0 else None)
        result.append({'meter_id':meter,'left':left['reading_id'],'right':right['reading_id'],'from':left['observed_at'],'to':right['observed_at'],'seconds':seconds,'raw_delta':delta,'issue':issue})
    return result


def placement_at(connection,meter,when):
    stamp=datetime.fromisoformat(when.replace('Z','+00:00'))
    rows=records(connection,'SELECT * FROM placements WHERE meter_id=?',[meter])
    matches=[row for row in rows if datetime.fromisoformat(row['valid_from'])<=stamp<datetime.fromisoformat(row['valid_to'])]
    if len(matches)>1:
        raise ValueError('同一时点表计归属重叠')
    return matches[0] if matches else None
