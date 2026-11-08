-- tables: mc_issues
SELECT id, title, status, created_ms AS "createdAt", updated_ms AS "updatedAt"
FROM mc_issues
WHERE status IS NULL OR status NOT IN ('closed', 'cancelled')
ORDER BY created_ms DESC
LIMIT 300
