-- tables: action_log
SELECT id AS "id", agent_id AS "agentId", action AS "action",
       details AS "details", ts AS "ts"
FROM action_log
ORDER BY ts DESC
LIMIT 120
