-- Revocation follows the stable key identity across bearer-token rotations.
SELECT disabled FROM polyad_auth_lanes
WHERE scope = %s AND key_group = %s AND key_name = %s;
