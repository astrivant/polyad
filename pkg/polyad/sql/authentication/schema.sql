CREATE TABLE IF NOT EXISTS polyad_auth_lanes (
    scope text NOT NULL, key_group text NOT NULL, key_name text NOT NULL,
    disabled boolean NOT NULL DEFAULT false,
    PRIMARY KEY (scope, key_group, key_name)
);

-- Token and policy versions share the lane's durable revocation state through this foreign key.
CREATE TABLE IF NOT EXISTS polyad_auth_keys (
    scope text NOT NULL, key_group text NOT NULL, key_name text NOT NULL,
    verifier text NOT NULL, policy_digest text NOT NULL, policy jsonb NOT NULL,
    first_seen timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, key_group, key_name, verifier, policy_digest),
    FOREIGN KEY (scope, key_group, key_name)
      REFERENCES polyad_auth_lanes(scope, key_group, key_name)
);

-- Credential metadata is private even when the database has broad default public privileges.
REVOKE ALL ON polyad_auth_lanes, polyad_auth_keys FROM PUBLIC;
