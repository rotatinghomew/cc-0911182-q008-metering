from .observations import intervals,placement_at
from .storage import digest,transaction


def inspect(connection):
    incidents=[]
    for meter, in connection.execute('SELECT meter_id FROM meters ORDER BY meter_id').fetchall():
        for row in intervals(connection,meter):
            reason=row['issue']
            if reason is None and placement_at(connection,meter,row['from']) is None:
                reason='区间开始归属缺失'
            if reason:
                incidents.append({'meter_id':meter,'reading_from':row['left'],'reading_to':row['right'],'reason':reason})
    with transaction(connection):
        connection.execute('DELETE FROM meter_incidents')
        for incident in incidents:
            connection.execute('INSERT INTO meter_incidents VALUES (?,?,?,?,?)',[digest(incident),incident['meter_id'],incident['reading_from'],incident['reading_to'],incident['reason']])
    return incidents
