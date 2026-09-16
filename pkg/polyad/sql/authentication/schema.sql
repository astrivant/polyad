CREATE TABLE IF NOT EXISTS polyad_auth_lanes (
    scope text NOT NULL, key_group text NOT NULL, key_name text NOT NULL,
    disabled boolean NOT NULL DEFAULT false,
    PRIMARY KEY (scope, key_group, key_name)
);
CREATE TABLE IF NOT EXISTS polyad_auth_keys (
    scope text NOT NULL, key_group text NOT NULL, key_name text NOT NULL,
    verifier text NOT NULL, policy_digest text NOT NULL, policy jsonb NOT NULL,
    first_seen timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, key_group, key_name, verifier, policy_digest),
    FOREIGN KEY (scope, key_group, key_name)
      REFERENCES polyad_auth_lanes(scope, key_group, key_name)
);
REVOKE ALL ON polyad_auth_lanes, polyad_auth_keys FROM PUBLIC;
