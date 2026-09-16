INSERT INTO polyad_namespace_state VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (scope, cluster, namespace) DO UPDATE
SET observed_at = EXCLUDED.observed_at, snapshot = EXCLUDED.snapshot
WHERE polyad_namespace_state.observed_at <= EXCLUDED.observed_at
RETURNING observed_at;
