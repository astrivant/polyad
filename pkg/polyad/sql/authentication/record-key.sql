-- Preserve first-seen history; repeated authentication of the same token and policy is idempotent.
INSERT INTO polyad_auth_keys(scope, key_group, key_name, verifier, policy_digest, policy)
VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING;
