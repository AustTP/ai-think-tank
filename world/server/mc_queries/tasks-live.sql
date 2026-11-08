-- tables: mc_tasks
SELECT id, title, status, assigned_to AS "assignedTo",
       task_type AS "taskType", deliverable, priority, feature,
       created_ms AS "createdAt", updated_ms AS "updatedAt"
FROM mc_tasks
ORDER BY created_ms DESC
LIMIT 400
