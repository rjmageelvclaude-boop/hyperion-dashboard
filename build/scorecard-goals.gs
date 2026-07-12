/**
 * Scorecard goal + pay-target store for the InterMountain dashboards.
 *
 * SETUP (one time, ~2 minutes) - same Apps Script project as the Command
 * Center budgets:
 * 1. Open the Apps Script project deployed at the store URL (the one that
 *    already holds command-center-budgets.gs).
 * 2. Paste this file in as a new script file (File > New > Script, name it
 *    "Scorecard").
 * 3. IMPORTANT: this project already has a doPost (budgets). Rename THIS
 *    file's doPost to nothing - instead add the two routes below into the
 *    existing handleBudgetRequest_ dispatcher, OR simpler: replace the
 *    existing doPost body's final `return { ok:false, error:'unknown-action' }`
 *    with `return handleScorecardRequest_(req, props);`.
 * 4. Set the PIN: Project Settings > Script properties > add
 *       Property: SCORECARD_PIN   Value: <passphrase managers will use>
 *    (You can reuse the value of SCORECARD_KEY from GitHub if you want ONE
 *    passphrase for everything - recommended.)
 * 5. Redeploy: Deploy > Manage deployments > pencil > Version "New version"
 *    > Deploy (editing the existing deployment keeps the URL).
 *
 * Actions:
 *   getScorecardGoals                       open read - KPI goals only
 *   setScorecardGoals {pin, role, goals}    PIN write - replaces one role's table
 *   getScorecardPayTargets {pin}            PIN read  - annual pay targets
 *   setScorecardPayTarget {pin, co, employeeId, value}   PIN write
 *
 * Pay targets are PIN-gated even for reads: they are compensation data.
 * The refresh engine reads them with SCORECARD_STORE_PIN and ships them to
 * the site only inside the encrypted pay block.
 */

var SC_GOALS_KEY = 'SCORECARD_GOALS_V1';
var SC_PAY_KEY = 'SCORECARD_PAY_TARGETS_V1';

function handleScorecardRequest_(req, props) {
  props = props || PropertiesService.getScriptProperties();

  if (req.action === 'getScorecardGoals') {
    return { ok: true, goals: JSON.parse(props.getProperty(SC_GOALS_KEY) || '{}') };
  }

  var pin = props.getProperty('SCORECARD_PIN');
  var pinOk = pin && String(req.pin) === String(pin);

  if (req.action === 'setScorecardGoals') {
    if (!pin) return { ok: false, error: 'SCORECARD_PIN script property is not set' };
    if (!pinOk) return { ok: false, error: 'bad-pin' };
    if (!req.role || typeof req.goals !== 'object')
      return { ok: false, error: 'missing role/goals' };
    var goals = JSON.parse(props.getProperty(SC_GOALS_KEY) || '{}');
    goals[req.role] = req.goals;
    props.setProperty(SC_GOALS_KEY, JSON.stringify(goals));
    return { ok: true, goals: goals };
  }

  if (req.action === 'getScorecardPayTargets') {
    if (!pin) return { ok: false, error: 'SCORECARD_PIN script property is not set' };
    if (!pinOk) return { ok: false, error: 'bad-pin' };
    return { ok: true, targets: JSON.parse(props.getProperty(SC_PAY_KEY) || '{}') };
  }

  if (req.action === 'setScorecardPayTarget') {
    if (!pin) return { ok: false, error: 'SCORECARD_PIN script property is not set' };
    if (!pinOk) return { ok: false, error: 'bad-pin' };
    if (!req.co || !req.employeeId) return { ok: false, error: 'missing co/employeeId' };
    var targets = JSON.parse(props.getProperty(SC_PAY_KEY) || '{}');
    if (!targets[req.co]) targets[req.co] = {};
    if (req.value == null || req.value === '') delete targets[req.co][String(req.employeeId)];
    else targets[req.co][String(req.employeeId)] = Number(req.value);
    props.setProperty(SC_PAY_KEY, JSON.stringify(targets));
    return { ok: true, targets: targets };
  }

  return { ok: false, error: 'unknown-action' };
}
