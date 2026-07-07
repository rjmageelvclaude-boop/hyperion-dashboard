/**
 * Shared budgets for the InterMountain Command Center.
 *
 * SETUP (one time, ~2 minutes):
 * 1. Open the Apps Script project that serves the Command Center data
 *    (the one deployed at the DATA_URL in site/command-center/index.html).
 * 2. Paste this entire file into a new script file (File > New > Script, name it "Budgets").
 *    NOTE: if your project already has a doPost function, tell Claude - it needs merging instead.
 * 3. Set the PIN: gear icon (Project Settings) > Script properties > Add script property:
 *       Property: BUDGET_PIN     Value: <the PIN you want, e.g. 4-6 digits>
 * 4. Redeploy so the change goes live: Deploy > Manage deployments > pencil icon >
 *    Version: "New version" > Deploy. (Editing the EXISTING deployment keeps the URL the same.)
 *
 * The dashboard then loads shared budgets automatically for every viewer, and the
 * first time someone saves a budget change it asks for the PIN.
 */

var BUDGETS_KEY = 'BUDGETS_V1';

function doPost(e) {
  var out;
  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(10000);
    out = handleBudgetRequest_(e);
  } catch (err) {
    out = { ok: false, error: String(err) };
  } finally {
    try { lock.releaseLock(); } catch (ignored) {}
  }
  return ContentService.createTextOutput(JSON.stringify(out))
    .setMimeType(ContentService.MimeType.JSON);
}

function handleBudgetRequest_(e) {
  var req = JSON.parse((e && e.postData && e.postData.contents) || '{}');
  var props = PropertiesService.getScriptProperties();
  var budgets = JSON.parse(props.getProperty(BUDGETS_KEY) || '{}');

  if (req.action === 'getBudgets') {
    return { ok: true, budgets: budgets };
  }

  if (req.action === 'setBudget') {
    var pin = props.getProperty('BUDGET_PIN');
    if (!pin) return { ok: false, error: 'BUDGET_PIN script property is not set' };
    if (String(req.pin) !== String(pin)) return { ok: false, error: 'bad-pin' };
    if (!req.co || !req.key) return { ok: false, error: 'missing co/key' };
    if (!budgets[req.co]) budgets[req.co] = {};
    budgets[req.co][req.key] = req.value;
    props.setProperty(BUDGETS_KEY, JSON.stringify(budgets));
    return { ok: true, budgets: budgets };
  }

  return { ok: false, error: 'unknown-action' };
}
