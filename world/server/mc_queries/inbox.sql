-- tables: mc_inbox
SELECT id AS "id", agent_id AS "agentId", question AS "question",
       status AS "status", created_ms AS "createdAt"
FROM mc_inbox
WHERE status IS NULL OR status = 'awaiting_input'
ORDER BY created_ms DESC
LIMIT 50
