"""Evidence-gated Coinbase research → strategy → risk → paper monitoring."""
import json
import math
import re
import time

STAGES = ('research', 'strategy', 'risk', 'paper')
OWNERS = dict(zip(STAGES, ('crypto-researcher', 'strategy-analyst', 'risk-manager', 'execution-operator')))
CONTRACTS = {
    'research': 'pairs (list of USD spot pairs), sources (list of URLs), dataQuality (nonempty text), liquidity (nonempty text). Fetch actual Coinbase quotes AND at least 100 candles for EACH proposed pair.',
    'strategy': 'pairs (list), allocationUsd (positive number), maxOrderUsd (positive number no larger than allocation), entryRules, exitRules, noTradeConditions (nonempty lists), backtest (object with trainStart, trainEnd, testStart, testEnd UNIX timestamps, trades positive integer, netPnlUsd finite number, maxDrawdownUsd nonnegative number, feesBps and slippageBps positive numbers). Run the backtest code with execute_script, using held-out data and no future leakage; save reproducible code/data and cite the actual output.',
    'risk': 'approved (boolean), rationale (nonempty text), lossLimitUsd (exactly 50), includesUnrealizedLosses (true), exposureLimits (nonempty text), emergencyStop (nonempty text). Read prior reports, challenge sizing and backtesting evidence. Approve only a defensible PAPER experiment; this cannot authorize live trading.',
    'paper': 'monitorReady (true), reconciliationPlan (nonempty text), stopConditions (nonempty text). Read coinbase_paper_account and identify its simulated balance, estimated fees/slippage, realized-only loss-limit limitation and absence of live trading. Monitor the ledger without submitting orders or bypassing player authorization.',
}


def stage_instructions(state, stage):
    workflow = state['tradingWorkflow']
    predecessors = {k: v for k, v in workflow.get('reports', {}).items() if k != stage}
    return ('Work only on Coinbase USD spot trading. Weather research is outside this workflow. '
            'Use real tools and timestamped sources; state limitations instead of inventing results. '
            'The user loss limit is $50 USD; allocation, sizing and pairs must be evidence-based proposals. '
            'No live orders are permitted. Read the predecessor reports with read_library_file.\n'
            f'Predecessor Library paths: {json.dumps(predecessors)}\n'
            f'Acceptance contract for {stage}: {CONTRACTS[stage]}\n'
            'End your saved report with a fenced ```json object containing EXACTLY the required fields. '
            'Do not mark an incomplete or unverified requirement as satisfied. Missing requirements block the next stage.')


def execution_observations(output):
    text = output if isinstance(output, str) else json.dumps(output)
    decoder = json.JSONDecoder()
    observations = []
    for match in re.finditer(r'\{', text):
        try:
            data, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        metrics = data.get('backtest', data) if isinstance(data, dict) else None
        if isinstance(metrics, dict) and 'testStart' in metrics and 'netPnlUsd' in metrics:
            observations.append({'tool': 'execute_script', 'backtest': metrics})
    return observations


def start(state):
    if state.get('tradingWorkflow', {}).get('version') == 1:
        return False
    state['tradingWorkflow'] = {'version': 1, 'stage': 'research', 'phase': 'ready',
                                'reports': {}, 'evidence': {}, 'taskIds': {}, 'lossLimitUsd': '50', 'mode': 'paper'}
    advance(state)
    return True


def validate_report(stage, text, observations, prior=None):
    errors = []
    prior = prior or {}
    blocks = re.findall(r'```json\s*(.*?)\s*```', text or '', flags=re.S | re.I)
    try:
        data = json.loads(blocks[-1]) if blocks else None
    except (ValueError, TypeError):
        data = None
    if not isinstance(data, dict):
        return {'accepted': False, 'errors': ['Missing a valid structured acceptance manifest.'], 'manifest': {}}
    def required_text(key):
        if not isinstance(data.get(key), str) or not data[key].strip():
            errors.append(f'Missing {key}.')
        elif re.search(r'not covered|not assessed|not performed|no .* found', data[key], re.I):
            errors.append(f'{key} is explicitly incomplete.')
    def positive(key, source=data, zero=False):
        value = source.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (not zero and value == 0):
            errors.append(f'Invalid {key}.')
            return None
        return value
    if stage in ('research', 'strategy'):
        pairs = data.get('pairs')
        if not isinstance(pairs, list) or not pairs or any(not isinstance(p, str) or not re.fullmatch(r'[A-Z0-9]{2,15}-USD', p) for p in pairs):
            errors.append('Missing valid USD spot pairs.')
            pairs = []
    if stage == 'research':
        for key in ('dataQuality', 'liquidity'):
            required_text(key)
        if not isinstance(data.get('sources'), list) or not data['sources'] or any(not isinstance(s, str) or not s.startswith('https://') for s in data['sources']):
            errors.append('Missing source URLs.')
        for pair in pairs:
            if not any(o.get('tool') == 'coinbase_spot_market' and o.get('productId') == pair for o in observations):
                errors.append(f'No observed Coinbase quote for {pair}.')
            if not any(o.get('tool') == 'coinbase_spot_candles' and o.get('productId') == pair and o.get('count', 0) >= 100 for o in observations):
                errors.append(f'Fewer than 100 observed candles for {pair}.')
    elif stage == 'strategy':
        if not set(pairs).issubset(set(prior.get('research', {}).get('pairs', []))):
            errors.append('Strategy pairs lack accepted research.')
        allocation, size = positive('allocationUsd'), positive('maxOrderUsd')
        if allocation is not None and size is not None and size > allocation:
            errors.append('Order size exceeds allocation.')
        for key in ('entryRules', 'exitRules', 'noTradeConditions'):
            if not isinstance(data.get(key), list) or not data[key] or any(not isinstance(v, str) or not v.strip() for v in data[key]):
                errors.append(f'Missing {key}.')
        backtest = data.get('backtest')
        if not isinstance(backtest, dict):
            errors.append('Missing backtest metrics.')
        else:
            values = [positive(k, backtest) for k in ('trainStart', 'trainEnd', 'testStart', 'testEnd')]
            if all(v is not None for v in values) and not values[0] < values[1] < values[2] < values[3]:
                errors.append('Training and held-out test windows overlap or are invalid.')
            for key in ('trades', 'feesBps', 'slippageBps'):
                positive(key, backtest)
            positive('maxDrawdownUsd', backtest, zero=True)
            pnl = backtest.get('netPnlUsd')
            if isinstance(pnl, bool) or not isinstance(pnl, (int, float)) or not math.isfinite(pnl):
                errors.append('Missing finite net P&L.')
            if not any(o.get('tool') == 'execute_script' and o.get('backtest') == backtest for o in observations):
                errors.append('Backtest metrics do not match an observed execution result.')
    elif stage == 'risk':
        for key in ('rationale', 'exposureLimits', 'emergencyStop'):
            required_text(key)
        if data.get('approved') is not True:
            errors.append('Risk Manager did not approve the paper experiment.')
        if data.get('lossLimitUsd') != 50 or data.get('includesUnrealizedLosses') is not True:
            errors.append('The $50 total-equity loss constraint was not addressed.')
        required_reports = set(prior.get('_reports', {}).values())
        read_reports = {o.get('path') for o in observations if o.get('tool') == 'read_library_file'}
        if not read_reports or not required_reports.issubset(read_reports):
            errors.append('Risk review did not read saved evidence.')
    elif stage == 'paper':
        for key in ('reconciliationPlan', 'stopConditions'):
            required_text(key)
        if data.get('monitorReady') is not True or not any(o.get('tool') == 'coinbase_paper_account' for o in observations):
            errors.append('Paper ledger was not inspected or monitoring is not ready.')
    else:
        errors.append('Unknown workflow stage.')
    return {'accepted': not errors, 'errors': errors, 'manifest': data}


def advance(state):
    import sim
    workflow = state.get('tradingWorkflow') or {}
    if workflow.get('stage') == 'paper' and workflow.get('evidence', {}).get('risk', {}).get('approved') is True:
        monitor_paper(state)
    if workflow.get('version') != 1 or workflow.get('phase') in ('complete', 'cancelled'):
        return
    stage = workflow['stage']
    tasks = [t for t in (state.get('tasks') or {}).values() if t.get('tradingStage') == stage]
    task = tasks[-1] if tasks else None
    if task:
        workflow.setdefault('taskIds', {})[stage] = task['id']
        if task.get('status') in ('walking', 'working'):
            workflow['phase'] = 'working'
            return
        if task.get('status') == 'done':
            acceptance = task.get('tradingAcceptance') or {}
            if acceptance.get('accepted') and task.get('libraryPath'):
                workflow['reports'][stage] = task['libraryPath']
                workflow['evidence'][stage] = acceptance['manifest']
                index = STAGES.index(stage) + 1
                if index == len(STAGES):
                    workflow['phase'] = 'complete'
                    return
                stage = workflow['stage'] = STAGES[index]
                workflow['phase'] = 'ready'
            else:
                task.update(status='blocked', manualAttention=True,
                            blockedReason='acceptance_failed', failNote='The saved report did not pass its measurable acceptance contract.')
                workflow['phase'] = 'blocked'
                return
        elif task.get('status') in ('failed', 'blocked', 'cancelled'):
            errors = (task.get('tradingAcceptance') or {}).get('errors') or []
            retries = workflow.setdefault('repairAttempts', {}).get(stage, 0)
            if errors and task.get('status') != 'cancelled' and retries < 2:
                if not any(q.get('tradingStage') == stage for q in state.get('workQueue') or []):
                    sim.queue_work(state, [{'title': f'Coinbase spot workflow: {stage} repair {retries + 1}',
                        'room': 'observatory', 'taskType': 'spike', 'tradingStage': stage,
                        'assignedTo': OWNERS[stage], 'directRoute': True, 'priority': 'high', 'budgetUsd': 0.50,
                        'projectLabel': 'Coinbase spot trading', 'acceptanceCriteria': CONTRACTS[stage],
                        'instructions': stage_instructions(state, stage) + '\nRepair the actual failed checks: '
                            + '; '.join(errors) + '\nPrevious report: ' + str(task.get('libraryPath') or 'none')}])
                    workflow['repairAttempts'][stage] = retries + 1
                    task.update(status='cancelled', manualAttention=False, blockedReason=None,
                                supersededByAutomaticRepair=True)
                    sim._queue_player_email(state, 'work_recovery', 'Automatic report repair queued',
                        f"{task['id']} was blocked because: {'; '.join(errors)}. "
                        f"{OWNERS[stage]} will repair it (attempt {retries + 1}/2). Acceptance checks remain required.")
                workflow['phase'] = 'queued'
                return
            if task.get('supersededByAutomaticRepair') and any(q.get('tradingStage') == stage for q in state.get('workQueue') or []):
                workflow['phase'] = 'queued'
                return
            workflow['phase'] = 'blocked'
            task['manualAttention'] = True
            task['blockedReason'] = 'acceptance_failed'
            return
    if workflow.get('phase') == 'blocked':
        return
    if not any(q.get('tradingStage') == stage for q in state.get('workQueue') or []):
        sim.queue_work(state, [{'title': f'Coinbase spot workflow: {stage}', 'room': 'observatory',
            'taskType': 'spike', 'tradingStage': stage, 'assignedTo': OWNERS[stage], 'directRoute': True,
            'projectLabel': 'Coinbase spot trading', 'instructions': stage_instructions(state, stage),
            'acceptanceCriteria': CONTRACTS[stage], 'priority': 'high', 'budgetUsd': 0.50}])
    workflow['phase'] = 'queued'


def monitor_paper(state):
    """Observe the durable simulated ledger without making orders or model calls."""
    if time.time() - state.get('paperMonitoring', {}).get('observedAt', 0) < 15:
        return
    import serve
    with serve._db() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='coinbase_paper_orders'").fetchone():
            orders = []
        else:
            orders = [json.loads(row[0]) for row in conn.execute('SELECT blob FROM coinbase_paper_orders ORDER BY rowid')]
    state['paperMonitoring'] = {'owner': OWNERS['paper'], 'observedAt': time.time(),
        'orderCount': len(orders), 'lastOrderId': orders[-1].get('clientOrderId') if orders else None,
        'status': 'monitoring_simulated_fills' if orders else 'waiting_for_player_authorized_paper_orders',
        'liveTrading': False}
