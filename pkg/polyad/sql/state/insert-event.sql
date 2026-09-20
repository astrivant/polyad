-- HA publishers can observe the same event; its scoped identity admits only one history record.
INSERT INTO polyad_event_history (scope, identity, payload)
VALUES (%s, %s, %s) ON CONFLICT DO NOTHING;
