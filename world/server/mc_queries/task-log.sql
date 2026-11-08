-- tables: action_log
SELECT id AS "id", ts AS "ts", agent_id AS "agentId", action AS "action",
       details AS "details"
FROM action_log
WHERE details LIKE '%' || ? || '%'
ORDER BY ts DESC
LIMIT 80
