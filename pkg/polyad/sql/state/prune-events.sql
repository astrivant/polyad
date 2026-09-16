DELETE FROM polyad_event_history WHERE scope = %s AND identity IN (
    SELECT identity FROM polyad_event_history
    WHERE scope = %s AND recorded_at < now() - %s * interval '1 day'
    ORDER BY recorded_at LIMIT 1000
);
