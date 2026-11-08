-- tables: mc_tasks
SELECT id AS "id", title AS "title", status AS "status",
       assigned_to AS "assignedTo", task_type AS "taskType",
       priority AS "priority", feature AS "feature",
       created_ms AS "createdAt", updated_ms AS "updatedAt",
       blob::jsonb->>'budgetUsd' AS "budgetUsd",
       blob::jsonb->>'ownSpendUsd' AS "ownSpendUsd",
       blob::jsonb->>'note' AS "note",
       blob::jsonb->>'statusNote' AS "statusNote"
FROM mc_tasks
WHERE id = ?
