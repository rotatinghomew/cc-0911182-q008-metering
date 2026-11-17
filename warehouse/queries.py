from .observations import intervals
from .storage import records


def overview(connection):
    return {'intervals':{meter:intervals(connection,meter) for meter, in connection.execute('SELECT meter_id FROM meters ORDER BY meter_id').fetchall()},'handovers':records(connection,'SELECT * FROM handovers ORDER BY handover_id')}
