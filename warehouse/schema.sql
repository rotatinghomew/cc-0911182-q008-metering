CREATE TABLE IF NOT EXISTS receipts(batch_id VARCHAR PRIMARY KEY, signature VARCHAR NOT NULL, inserted INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS source_rows(table_name VARCHAR, row_key VARCHAR, payload VARCHAR NOT NULL, batch_id VARCHAR NOT NULL, PRIMARY KEY(table_name,row_key));
        CREATE TABLE IF NOT EXISTS campuses(campus_id VARCHAR PRIMARY KEY, timezone VARCHAR, label VARCHAR);
        CREATE TABLE IF NOT EXISTS meters(meter_id VARCHAR PRIMARY KEY, modulus DECIMAL(20,6), unit VARCHAR);
        CREATE TABLE IF NOT EXISTS readings(reading_id VARCHAR PRIMARY KEY, meter_id VARCHAR, observed_at VARCHAR, value DECIMAL(20,6), received_at VARCHAR);
        CREATE TABLE IF NOT EXISTS placements(placement_id VARCHAR PRIMARY KEY, meter_id VARCHAR, campus_id VARCHAR, building VARCHAR, valid_from VARCHAR, valid_to VARCHAR);
        CREATE TABLE IF NOT EXISTS handovers(handover_id VARCHAR PRIMARY KEY, old_meter VARCHAR, new_meter VARCHAR, occurred_at VARCHAR, old_value DECIMAL(20,6), new_value DECIMAL(20,6));

CREATE TABLE IF NOT EXISTS meter_incidents(incident_id VARCHAR PRIMARY KEY, meter_id VARCHAR, reading_from VARCHAR, reading_to VARCHAR, reason VARCHAR);