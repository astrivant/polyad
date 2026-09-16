SELECT count(*) FROM pg_stat_activity
WHERE datname = current_database() AND application_name = %s
AND backend_type = 'client backend';
