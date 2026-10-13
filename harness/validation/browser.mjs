import fs from 'node:fs';

// Inspect a disposable Chromium session displaying the self-contained report.
const palette = JSON.parse(fs.readFileSync(
  new URL('../reporting/agent-colors.json', import.meta.url), 'utf8'
));
const profile = process.argv[2];
if (!profile) {
  throw new Error('Provide the disposable Chromium profile directory.');
}

const lines = fs.readFileSync(profile + '/DevToolsActivePort', 'utf8')
  .trim().split('\n');
const targets = await (await fetch('http://127.0.0.1:' + lines[0] + '/json/list')).json();
const page = targets.find(target => target.type === 'page');
const socket = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, { once: true });
  socket.addEventListener('error', reject, { once: true });
});

let nextId = 0;
const pending = new Map();
const errors = [];
socket.addEventListener('message', event => {
  const message = JSON.parse(event.data);
  if (message.id) {
    const request = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) {
      request.reject(new Error(JSON.stringify(message.error)));
    } else {
      request.resolve(message.result);
    }
  } else if (message.method === 'Runtime.exceptionThrown') {
    errors.push(message.params.exceptionDetails);
  }
});

function send(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++nextId;
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const result = await send('Runtime.evaluate', {
    expression,
    returnByValue: true,
    awaitPromise: true
  });
  if (result.exceptionDetails) {
    throw new Error(JSON.stringify(result.exceptionDetails));
  }
  return result.result.value;
}

async function capture(name) {
  const screenshot = await send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(
    profile + '/public-report-' + name + '.png',
    Buffer.from(screenshot.data, 'base64')
  );
}

async function inspectTheme(scheme) {
  await send('Emulation.setEmulatedMedia', {
    features: [{ name: 'prefers-color-scheme', value: scheme }]
  });
  const actual = await evaluate(`({
    background: getComputedStyle(document.body).backgroundColor,
    text: getComputedStyle(document.body).color,
    scoreText: getComputedStyle(document.querySelector('.score')).color,
    chartLabels: Array.from(document.querySelectorAll('#scoreChart svg text')).map(
      element => getComputedStyle(element).fill
    ),
    colors: C,
    width: innerWidth,
    scroll: document.documentElement.scrollWidth
  })`);
  const expectedBackground = scheme === 'dark' ? 'rgb(0, 0, 0)' : 'rgb(255, 255, 255)';
  if (actual.background !== expectedBackground || actual.scoreText !== actual.text) {
    throw new Error('Theme colors failed: ' + JSON.stringify(actual));
  }
  if (actual.chartLabels.some(color => color === actual.background)) {
    throw new Error('Invisible chart label in ' + scheme);
  }
  for (const agent of ['leon', 'hermes', 'openclaw']) {
    if (actual.colors[agent] !== palette[agent]) {
      throw new Error('Agent palette mismatch: ' + agent);
    }
  }
  if (actual.scroll > actual.width + 2) {
    throw new Error('Theme overflow: ' + JSON.stringify(actual));
  }
  return { scheme, ...actual };
}

await send('Runtime.enable');
try {
  const initial = await evaluate(`({
    ready: window.reportReady,
    records: D.records.length,
    tasks: D.tasks.length,
    scoreCards: document.querySelectorAll('#rankCards .card').length,
    charts: document.querySelectorAll('svg').length
  })`);
  if (!initial.ready || initial.records !== 324 || initial.tasks !== 36 || initial.scoreCards !== 3) {
    throw new Error(JSON.stringify(initial));
  }
  const panes = await evaluate(`Array.from(document.querySelectorAll('[data-tab]')).map(button => {
    button.click();
    return { name: button.dataset.tab, visible: !document.getElementById(button.dataset.tab).hidden };
  })`);
  if (panes.some(pane => !pane.visible)) {
    throw new Error('Tab failed');
  }
  await evaluate(`
    document.getElementById('cohort').value = 'strict_shared_success';
    document.getElementById('cohort').dispatchEvent(new Event('change'));
    selectPane('efficiency');
  `);
  await send('Emulation.setDeviceMetricsOverride', {
    width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false
  });
  await evaluate('window.scrollTo(0, 380)');
  await capture('efficiency');
  await evaluate(`
    selectPane('tasks');
    document.getElementById('taskSearch').value = 'escalation';
    document.getElementById('taskSearch').dispatchEvent(new Event('input'));
  `);
  const filter = await evaluate(`({
    tasks: document.querySelectorAll('#taskTable tbody tr').length,
    attempts: document.querySelectorAll('#attemptTable tbody tr').length
  })`);
  if (filter.tasks !== 1 || filter.attempts !== 9) {
    throw new Error('Filter failed: ' + JSON.stringify(filter));
  }
  await evaluate("document.querySelector('[data-task]').click()");
  if (!(await evaluate("document.getElementById('detail').open"))) {
    throw new Error('Task dialog failed');
  }
  await evaluate("document.querySelector('#detailBody [data-attempt]').click()");
  if (!(await evaluate("document.getElementById('detailBody').textContent.includes('All attempt metrics')"))) {
    throw new Error('Attempt drilldown failed');
  }
  if (!(await evaluate("Boolean(document.querySelector('#detailBody .trace-link'))"))) {
    throw new Error('Available action-history link missing');
  }
  await evaluate(`
    document.getElementById('closeDetail').click();
    selectPane('overview');
    window.scrollTo(0, 0);
  `);
  await send('Emulation.setDeviceMetricsOverride', {
    width: 390, height: 844, deviceScaleFactor: 1, mobile: true
  });
  const mobile = await evaluate('({ width: innerWidth, scroll: document.documentElement.scrollWidth })');
  if (mobile.scroll > mobile.width + 2) {
    throw new Error('Mobile page overflow: ' + JSON.stringify(mobile));
  }
  await capture('mobile');
  await evaluate(`
    selectPane('failures');
    document.getElementById('failureAgent').value = 'leon';
    document.getElementById('failureType').value = 'zero';
    document.getElementById('failureAgent').dispatchEvent(new Event('change'));
  `);
  const zeroRows = await evaluate("document.querySelectorAll('#failureTable tbody tr').length");
  if (zeroRows !== 12) {
    throw new Error('Failure filter mismatch: ' + zeroRows);
  }
  await send('Emulation.setDeviceMetricsOverride', {
    width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false
  });
  await evaluate('window.scrollTo(0, 400)');
  await capture('failures');
  if (errors.length) {
    throw new Error('Browser errors: ' + JSON.stringify(errors));
  }
  const themes = [];
  await evaluate("selectPane('overview');window.scrollTo(0, 0)");
  for (const scheme of ['light', 'dark']) {
    await send('Emulation.setDeviceMetricsOverride', {
      width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false
    });
    themes.push(await inspectTheme(scheme));
    await capture(scheme + '-desktop');
    await send('Emulation.setDeviceMetricsOverride', {
      width: 390, height: 844, deviceScaleFactor: 1, mobile: true
    });
    themes.push(await inspectTheme(scheme));
    await capture(scheme + '-mobile');
  }
  console.log(JSON.stringify({
    passed: true, initial, panes, filter, zeroRows, mobile, themes,
    console_errors: errors.length
  }));
} finally {
  await send('Browser.close').catch(() => {});
  socket.close();
}
