-- Replace only this namespace's inventory inside the caller's guarded snapshot transaction.
DELETE FROM polyad_graph_state WHERE scope = %s AND cluster = %s AND namespace = %s;
