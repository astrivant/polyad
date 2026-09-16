INSERT INTO polyad_auth_lanes(scope, key_group, key_name)
VALUES (%s, %s, %s) ON CONFLICT DO NOTHING;
