-- tables: mc_work_queue
SELECT id, title, status, assigned_to AS "assignedTo", not_before AS "notBefore"
FROM mc_work_queue
ORDER BY not_before
LIMIT 200
