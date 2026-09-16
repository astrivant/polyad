CREATE TABLE IF NOT EXISTS polyad_state_version (version integer PRIMARY KEY);
INSERT INTO polyad_state_version VALUES (1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS polyad_graph_state (
    scope text NOT NULL, cluster text NOT NULL, namespace text NOT NULL,
    kind text NOT NULL, name text NOT NULL, uid text NOT NULL,
    resource_version text NOT NULL, observed_at timestamptz NOT NULL,
    document jsonb NOT NULL,
    PRIMARY KEY (scope, cluster, namespace, kind, name)
);
CREATE TABLE IF NOT EXISTS polyad_namespace_state (
    scope text NOT NULL, cluster text NOT NULL, namespace text NOT NULL,
    observed_at timestamptz NOT NULL, snapshot jsonb NOT NULL,
    PRIMARY KEY (scope, cluster, namespace)
);
CREATE TABLE IF NOT EXISTS polyad_event_history (
    scope text NOT NULL, identity text NOT NULL, recorded_at timestamptz NOT NULL DEFAULT now(),
    payload jsonb NOT NULL, PRIMARY KEY (scope, identity)
);
CREATE INDEX IF NOT EXISTS polyad_event_retention ON polyad_event_history (scope, recorded_at);
REVOKE ALL ON polyad_graph_state, polyad_namespace_state, polyad_event_history, polyad_state_version FROM PUBLIC;
