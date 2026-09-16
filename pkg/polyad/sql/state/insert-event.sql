INSERT INTO polyad_event_history (scope, identity, payload)
VALUES (%s, %s, %s) ON CONFLICT DO NOTHING;
