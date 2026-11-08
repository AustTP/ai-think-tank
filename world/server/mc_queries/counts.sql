-- tables: mc_tasks, mc_work_queue, mc_issues
SELECT
  (SELECT COUNT(*) FROM mc_tasks WHERE status IN ('working','walking','repairing','needs_review','investigating')) AS "liveTasks",
  (SELECT COUNT(*) FROM mc_tasks WHERE status IN ('blocked','failed')) AS "stuckTasks",
  (SELECT COUNT(*) FROM mc_tasks WHERE status = 'done') AS "doneTasks",
  (SELECT COUNT(*) FROM mc_work_queue) AS "queuedItems",
  (SELECT COUNT(*) FROM mc_issues WHERE status IS NULL OR status NOT IN ('closed','cancelled')) AS "openIssues"
