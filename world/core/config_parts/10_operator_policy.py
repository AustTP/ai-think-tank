# ---------------------------------------------------------------------------
# Operator policy (world/operator_policy.json). The director/admin knobs that
# used to be hardcoded constants in sim.py/serve.py -- team-size caps, grade
# floors, hire/fire cooldowns, governance cadences, peer-review cadence,
# escalation caps, morale weights, the sim-loop watchdog thresholds. DATA, not
# code: the operator edits the JSON; sim/serve rebind their module constants
# through ON_RELOAD_HOOKS when reload_policy_config() runs (import time and
# POST /api/policy/reload). Spend ceilings stay in .env / POST /api/budget --
# one source of truth per knob.
# ---------------------------------------------------------------------------
OPERATOR_POLICY_PATH = os.path.join(ROOT, 'operator_policy.json')

# Fail-closed defaults, kept in code so a fresh clone (or a deleted/corrupt
# JSON file) boots with the shipped behavior. The shipped operator_policy.json
# carries the SAME values; the operator edits the JSON, not these.
OPERATOR_POLICY_DEFAULT: dict = {
    # Teams & workforce
    'maxTeamMembers': 6,
    'scrumMasterMinTeamSize': 4,
    'smallTeamStuckTrigger': 3,
    'maxActiveAgents': 25,
    'maxTotalAgents': 500,
    # Grading floors (a deliverable graded below is coaching-worthy; at/above
    # the success floor it counts as a success).
    'deliverableGradeFloor': 5.0,
    'successGradeFloor': 8.0,
    # Hiring / firing cooldowns (ms)
    'hireCooldownMs': 45000,
    'firingCooldownMs': 60000,
    # Governance cadences (ms)
    'skillReviewCadenceMs': 30 * 60 * 1000,
    'distillCadenceMs': 30 * 60 * 1000,
    'refinementCadenceMs': 7 * 24 * 3600 * 1000,
    'roadmapCadenceMs': 7 * 24 * 3600 * 1000,
    # Work items / sprints / backlog
    'workItemMaxAttempts': 3,
    'staleWorkTimeoutMs': 3 * 3600 * 1000,
    'staleWorkBudgetGraceS': 900,
    'maxBacklogItems': 200,
    'backlogPullsPerClose': 1,
    'escalationMaxOpen': 3,
    # Peer review + director escalations (serve.py)
    'peerReviewIntervalS': 90,
    'peerReviewFileProbability': 0.4,
    'directorEscalationMaxAsks': 6,
    'directorEscalationReaskCooldownS': 15 * 60,
    # Sim-loop watchdog (serve.py; .env SIM_WATCHDOG_*_S still overrides)
    'simWatchdogIntervalS': 30.0,
    'simWatchdogStaleS': 180.0,
    # Background health-check loop cadences (serve.py). The health loop
    # reads these per-iteration/per-call, so a policy reload applies
    # without a restart (the check interval takes effect on the next
    # loop sleep).
    'healthCheckIntervalS': 300,
    'healthAlertDedupWindowS': 12 * 3600,
    'healthDigestIntervalS': 6 * 3600,
    # Morale arithmetic
    'moraleApprovedWeight': 0.5,
    'moraleApprovedCap': 15,
    'moraleDroppedWeight': 6.0,
    'moraleNeglectWeight': 3.0,
    'moraleNeglectCap': 30,
    # Delivery shape: review-gate cycles, coaching rounds, help escalation,
    # sprint velocity cap, and the "no one is picking this up" stall ceiling.
    'maxReviewCycles': 8,
    'coachingMaxRounds': 3,
    'workerHelpAfterFails': 2,
    'teamVelocityMax': 20,
    'workItemStallCeiling': 600,
    # Sim-loop timing (global: one loop serves the whole tank).
    'taskCycleS': 6.0,
    'taskWorkDurationS': 8.0,
    'taskContentTimeoutS': 120.0,
    'taskContentTimeoutMaxS': 600.0,
    # Tier throttle: today's spend below ratio * trailing 7-day median.
    'tierThrottleRatio': 0.7,
    # People-process timers (ms)
    'hireDurationMs': 8000,
    'firingReviewDurationMs': 10000,
    'reportStrikeWindowMs': 14 * 24 * 3600 * 1000,
    'onboardMeetDurationMs': 10000,
    'onboardReadinessTimeoutMs': 15 * 60 * 1000,
    # Villager socials
    'socialCadenceMs': 7 * 24 * 3600 * 1000,
    'socialMeetMs': 30 * 60 * 1000,
    'socialAdoptConfidence': 0.6,
    # Failure cooldown (hire throttling after a bad run)
    'failureCooldownHalfLifeS': 1800,
    'failureCooldownThreshold': 0.5,
    # Free spikes (spare-time moonshots)
    'freeSpikeAllowancePerWeek': 1,
    'freeSpikeGlobalCapPerWeek': 3,
    'freeSpikeBudgetMs': 2 * 60 * 1000,
    'freeSpikeDeferMs': 60 * 1000,
    # Press Office code/review lane (content.py)
    'codeContinuationAttempts': 2,
    'maxCodeProbeRounds': 3,
    'maxCodeColabRounds': 3,
    'maxReviewProbeRounds': 2,
    # Request/response ceilings (serve.py). Were hardcoded constants;
    # now operator data like every other ceiling.
    'chatMaxTokensCap': 4000,
    'smsTtlDefaultS': 300,
    'smsTtlMinS': 60,
    'smsTtlMaxS': 900,
    'decideBurstAllow': 6,
    'decideBurstWindowS': 5.0,
    'decideMinIntervalS': 0.4,
    # Sim-loop ceilings (sim.py), same promotion.
    'placementMinDist': 60,
    'ruleMineCadenceMs': 7 * 24 * 3600 * 1000,
    'runbookMaxEntriesPerProduct': 20,
    'staleWorkMaxReplans': 2,
    'refinementMaxRequests': 20,
    'breakdownMaxRequests': 3,
    'taskContentGraceMaxS': 300.0,
    'feedbackMax': 10,
    'minResearchCadenceMs': 5 * 60 * 1000,
    'minPipelineCadenceMs': 60 * 60 * 1000,
    'deviceCheckinHistoryMax': 50,
    'deviceCheckinRetentionDays': 30,
    # Experiment grant caps (sublow.py; shared with jev_grants.py).
    'grantTtlDefaultS': 1800,
    'grantTtlMaxS': 86400,
    'grantTokensDefault': 600,
    'grantTokensMax': 4000,
    # Ops hygiene (serve.py backup + prune loops; the loops sleep on the
    # live module value, so a reload takes effect on the next iteration).
    'dbBackupKeep': 1,
    'dbBackupIntervalS': 3600,
    'logRetentionDaysDefault': 7,
    'logPruneIntervalS': 21600,
    'logPruneMaxRows': 50000,
    # Model-tier watch loops (serve.py: full re-pick, price watch,
    # failed-tier recheck cooldown, tier-gate confidence floor).
    'modelTierRefreshIntervalS': 7200,
    'tierPriceWatchIntervalS': 1800,
    'tierRecheckCooldownS': 900,
    'tierGateMinConfidence': 0.55,
    # Rule/success mining bounds (serve.py proposal lanes).
    'failureMaxRecords': 200,
    'successMaxRecords': 200,
    'ruleProposalsMax': 50,
    'successProposalsMax': 50,
    'ruleMinRecurrence': 2,
    'successMinRecurrence': 2,
    # Memory maintenance (serve.py age decay/keep scan).
    'memoryMaxAgeDays': 90,
    'memoryDecayFactor': 0.2,
    'memoryKeepCount': 20,
    # Browse download cap and YouTube transcript caps (serve.py).
    'maxDownloadBytes': 25 * 1024 * 1024,
    'youtubeTranscriptMaxChars': 50000,
    'youtubeTranscriptMaxSegments': 800,
    # Agent-directory sync throttle (serve.py; env
    # AGENT_DIR_SYNC_INTERVAL_S keeps precedence through the rebind).
    'agentDirSyncIntervalS': 15.0,
    # Peer-review gate depth (serve.py lookback and signal staleness).
    'peerReviewMinLookbackS': 3600,
    'peerReviewSignalStaleS': 21600,
    # Supervisor and chief-of-staff operating rhythm. Cadences and the
    # per-monitoring-card price are rhythm/economics, not safety gates.
    'supervisorBriefIntervalMs': 15 * 60 * 1000,
    'supervisorCardIntervalMs': 30 * 60 * 1000,
    'chiefMonitorIntervalMs': 60 * 60 * 1000,
    'supervisorCardBudgetUsd': 0.05,
    # Delegation and budget-top-up bounds. Operational spend shape, not the
    # tank-wide spend ceiling (that stays in .env / POST /api/budget).
    'delegateMaxOpen': 3,
    'delegateBudgetCapUsd': 0.50,
    'supervisorBudgetCapUsd': 5.0,
    # Calibration loop knobs (targets, dead-bands, sample floors, step size,
    # timer). The threshold min/max clamps stay in code as safety bounds.
    'jevCalibrationTargetReliability': 0.9,
    'jevCalibrationHysteresis': 0.05,
    'jevCalibrationMinOutcome': 10,
    'jevCalibrationStep': 0.05,
    'reviewGradeCalibrationTarget': 0.9,
    'reviewGradeCalibrationHysteresis': 0.05,
    'reviewGradeCalibrationMinSamples': 10,
    'reviewGradeCalibrationStep': 0.05,
    'calibrationAdjustIntervalS': 30 * 60,
    # Director avatar generation. The server only coordinates: it queues a
    # supervisor turn for a missing worker avatar set, and a director calls
    # PixelLab through generate_agent_avatar. The minimum-credit floor keeps a
    # generation from draining the PixelLab account.
    'avatarTaskIntervalS': 30,
    'avatarTaskBatchMax': 3,
    'avatarTaskRetryCooldownS': 6 * 3600,
    'avatarGenerationMinCreditsUsd': 0.50,
}

# Per-key validation: (kind, min, max). A wrong-typed or out-of-range value
# keeps the default for THAT key rather than crashing boot (fail-closed per
# field, same discipline as the other policy loaders).
_OPERATOR_SCHEMA: dict = {
    'maxTeamMembers': ('int', 1, 12),
    'scrumMasterMinTeamSize': ('int', 1, 12),
    'smallTeamStuckTrigger': ('int', 1, 20),
    'maxActiveAgents': ('int', 1, 100),
    'maxTotalAgents': ('int', 1, 1000),
    'deliverableGradeFloor': ('float', 0.0, 10.0),
    'successGradeFloor': ('float', 0.0, 10.0),
    'hireCooldownMs': ('int', 0, 3600000),
    'firingCooldownMs': ('int', 0, 3600000),
    'skillReviewCadenceMs': ('int', 60000, 7 * 24 * 3600 * 1000),
    'distillCadenceMs': ('int', 60000, 7 * 24 * 3600 * 1000),
    'refinementCadenceMs': ('int', 60000, 30 * 24 * 3600 * 1000),
    'roadmapCadenceMs': ('int', 60000, 30 * 24 * 3600 * 1000),
    'workItemMaxAttempts': ('int', 1, 10),
    'staleWorkTimeoutMs': ('int', 60000, 24 * 3600 * 1000),
    'staleWorkBudgetGraceS': ('int', 0, 86400),
    'maxBacklogItems': ('int', 1, 1000),
    'backlogPullsPerClose': ('int', 1, 10),
    'escalationMaxOpen': ('int', 1, 20),
    'peerReviewIntervalS': ('int', 10, 3600),
    'peerReviewFileProbability': ('float', 0.0, 1.0),
    'directorEscalationMaxAsks': ('int', 1, 20),
    'directorEscalationReaskCooldownS': ('int', 60, 86400),
    'simWatchdogIntervalS': ('float', 5.0, 600.0),
    'simWatchdogStaleS': ('float', 30.0, 3600.0),
    'healthCheckIntervalS': ('int', 60, 86400),
    'healthAlertDedupWindowS': ('int', 60, 7 * 86400),
    'healthDigestIntervalS': ('int', 3600, 7 * 86400),
    'moraleApprovedWeight': ('float', 0.0, 10.0),
    'moraleApprovedCap': ('int', 0, 100),
    'moraleDroppedWeight': ('float', 0.0, 100.0),
    'moraleNeglectWeight': ('float', 0.0, 100.0),
    'moraleNeglectCap': ('int', 0, 100),
    'maxReviewCycles': ('int', 1, 50),
    'coachingMaxRounds': ('int', 1, 20),
    'workerHelpAfterFails': ('int', 1, 10),
    'teamVelocityMax': ('int', 1, 500),
    'workItemStallCeiling': ('int', 10, 86400),
    'taskCycleS': ('float', 1.0, 60.0),
    'taskWorkDurationS': ('float', 1.0, 600.0),
    'taskContentTimeoutS': ('float', 5.0, 3600.0),
    'taskContentTimeoutMaxS': ('float', 10.0, 7200.0),
    'tierThrottleRatio': ('float', 0.0, 1.0),
    'hireDurationMs': ('int', 0, 3600000),
    'firingReviewDurationMs': ('int', 0, 3600000),
    'reportStrikeWindowMs': ('int', 3600000, 90 * 24 * 3600 * 1000),
    'onboardMeetDurationMs': ('int', 0, 3600000),
    'onboardReadinessTimeoutMs': ('int', 60000, 86400000),
    'socialCadenceMs': ('int', 3600000, 30 * 24 * 3600 * 1000),
    'socialMeetMs': ('int', 60000, 86400000),
    'socialAdoptConfidence': ('float', 0.0, 1.0),
    'failureCooldownHalfLifeS': ('int', 60, 86400),
    'failureCooldownThreshold': ('float', 0.0, 1.0),
    'freeSpikeAllowancePerWeek': ('int', 0, 100),
    'freeSpikeGlobalCapPerWeek': ('int', 0, 100),
    'freeSpikeBudgetMs': ('int', 1000, 3600000),
    'freeSpikeDeferMs': ('int', 0, 3600000),
    'codeContinuationAttempts': ('int', 1, 10),
    'maxCodeProbeRounds': ('int', 1, 20),
    'maxCodeColabRounds': ('int', 1, 20),
    'maxReviewProbeRounds': ('int', 1, 20),
    'chatMaxTokensCap': ('int', 1, 32000),
    'smsTtlDefaultS': ('int', 1, 86400),
    'smsTtlMinS': ('int', 1, 3600),
    'smsTtlMaxS': ('int', 1, 86400),
    'decideBurstAllow': ('int', 1, 100),
    'decideBurstWindowS': ('float', 0.1, 3600.0),
    'decideMinIntervalS': ('float', 0.0, 60.0),
    'placementMinDist': ('int', 0, 500),
    'ruleMineCadenceMs': ('int', 60000, 30 * 24 * 3600 * 1000),
    'runbookMaxEntriesPerProduct': ('int', 1, 200),
    'staleWorkMaxReplans': ('int', 0, 100),
    'refinementMaxRequests': ('int', 1, 500),
    'breakdownMaxRequests': ('int', 1, 100),
    'taskContentGraceMaxS': ('float', 0.0, 3600.0),
    'feedbackMax': ('int', 1, 100),
    'minResearchCadenceMs': ('int', 0, 24 * 3600 * 1000),
    'minPipelineCadenceMs': ('int', 0, 7 * 24 * 3600 * 1000),
    'deviceCheckinHistoryMax': ('int', 1, 1000),
    'deviceCheckinRetentionDays': ('int', 1, 365),
    'grantTtlDefaultS': ('int', 1, 86400),
    'grantTtlMaxS': ('int', 1, 7 * 86400),
    'grantTokensDefault': ('int', 1, 100000),
    'grantTokensMax': ('int', 1, 1000000),
    'dbBackupKeep': ('int', 1, 20),
    'dbBackupIntervalS': ('int', 300, 86400),
    'logRetentionDaysDefault': ('int', 1, 365),
    'logPruneIntervalS': ('int', 600, 86400),
    'logPruneMaxRows': ('int', 1000, 500000),
    'modelTierRefreshIntervalS': ('int', 1800, 7 * 86400),
    'tierPriceWatchIntervalS': ('int', 300, 86400),
    'tierRecheckCooldownS': ('int', 60, 86400),
    'tierGateMinConfidence': ('float', 0.0, 1.0),
    'failureMaxRecords': ('int', 10, 5000),
    'successMaxRecords': ('int', 10, 5000),
    'ruleProposalsMax': ('int', 1, 500),
    'successProposalsMax': ('int', 1, 500),
    'ruleMinRecurrence': ('int', 2, 100),
    'successMinRecurrence': ('int', 2, 100),
    'memoryMaxAgeDays': ('int', 1, 3650),
    'memoryDecayFactor': ('float', 0.0, 1.0),
    'memoryKeepCount': ('int', 1, 1000),
    'maxDownloadBytes': ('int', 1048576, 1073741824),
    'youtubeTranscriptMaxChars': ('int', 1000, 1000000),
    'youtubeTranscriptMaxSegments': ('int', 10, 100000),
    'agentDirSyncIntervalS': ('float', 1.0, 3600.0),
    'peerReviewMinLookbackS': ('int', 300, 86400),
    'peerReviewSignalStaleS': ('int', 300, 7 * 86400),
    'supervisorBriefIntervalMs': ('int', 60000, 7 * 24 * 3600 * 1000),
    'supervisorCardIntervalMs': ('int', 60000, 7 * 24 * 3600 * 1000),
    'chiefMonitorIntervalMs': ('int', 60000, 30 * 24 * 3600 * 1000),
    'supervisorCardBudgetUsd': ('float', 0.0, 5.0),
    'delegateMaxOpen': ('int', 1, 20),
    'delegateBudgetCapUsd': ('float', 0.0, 100.0),
    'supervisorBudgetCapUsd': ('float', 0.0, 1000.0),
    'jevCalibrationTargetReliability': ('float', 0.0, 1.0),
    'jevCalibrationHysteresis': ('float', 0.0, 1.0),
    'jevCalibrationMinOutcome': ('int', 1, 1000),
    'jevCalibrationStep': ('float', 0.001, 1.0),
    'reviewGradeCalibrationTarget': ('float', 0.0, 1.0),
    'reviewGradeCalibrationHysteresis': ('float', 0.0, 1.0),
    'reviewGradeCalibrationMinSamples': ('int', 1, 1000),
    'reviewGradeCalibrationStep': ('float', 0.001, 1.0),
    'calibrationAdjustIntervalS': ('int', 60, 86400),
    'avatarTaskIntervalS': ('int', 10, 3600),
    'avatarTaskBatchMax': ('int', 1, 20),
    'avatarTaskRetryCooldownS': ('int', 300, 7 * 86400),
    'avatarGenerationMinCreditsUsd': ('float', 0.0, 100.0),
}

