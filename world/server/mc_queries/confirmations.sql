-- tables: confirmation_proposals
SELECT id AS "id", action AS "action", actor AS "actor",
       status AS "status", channel AS "channel",
       created_at AS "createdAt", expires_at AS "expiresAt"
FROM confirmation_proposals
WHERE status = 'pending'
ORDER BY created_at DESC
LIMIT 30
