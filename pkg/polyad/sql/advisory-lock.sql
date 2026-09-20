-- Transaction-scoped ownership releases automatically on commit or rollback, including errors.
SELECT pg_advisory_xact_lock(%s, %s);
