const express = require('express');
const cors    = require('cors');
const path    = require('path');
const { spawn } = require('child_process');
const fs      = require('fs');
const crypto  = require('crypto');
const cron    = require('node-cron');
const axios   = require('axios');

const app  = express();
const PORT = 44290;
const DATA_FILE  = '/data/bob-terminal/data/cache.json';
const TASKS_DIR  = '/data/bob-terminal/data/tasks';
const BOB_API_KEY = 'bob_prod_bob-apikey_5Ap754RkPFcTQeVFxsmoYTp4C8ZjtyW3tru8yTUE4KGe6zsnVe5ZhTpowAxrsnLjz6psvKogXn5ZzwkejVgeshDt_Fi2Fxt58GvA9yj86fsSJ6VzoZc8HmEpaBbttUQPx6uSB';

// ── SALES INTELLIGENCE ────────────────────────────────────────────────────
const salesIntel = require('/data/sales-intel/sales-intel.js');
const tickets    = require('./ticket-engine.js');
// Set your Slack Incoming Webhook: export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
// Or paste it directly here:
const SLACK_WEBHOOK_URL = process.env.SLACK_WEBHOOK_URL || '';

app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

fs.mkdirSync(TASKS_DIR, { recursive: true });
fs.mkdirSync('/data/bob-terminal/data', { recursive: true });
fs.mkdirSync('/data/bob-terminal/logs', { recursive: true });

function log(msg) {
  const line = `[${new Date().toISOString()}] ${msg}`;
  console.log(line);
  try { fs.appendFileSync('/data/bob-terminal/logs/server.log', line + '\n'); } catch {}
}

function loadCache() {
  try { return JSON.parse(fs.readFileSync(DATA_FILE, 'utf8')); }
  catch { return { news: [], stocks: {}, lastUpdated: null }; }
}
function saveCache(data) {
  fs.writeFileSync(DATA_FILE, JSON.stringify(data, null, 2));
}

// ── TASK STORE ────────────────────────────────────────────────────────────
// Each task stored as JSON + MD result in /data/bob-terminal/data/tasks/
function taskPath(id)  { return path.join(TASKS_DIR, `${id}.json`); }
function resultPath(id){ return path.join(TASKS_DIR, `${id}.md`);   }

function saveTask(task) {
  fs.writeFileSync(taskPath(task.id), JSON.stringify(task, null, 2));
}

function loadTask(id) {
  try { return JSON.parse(fs.readFileSync(taskPath(id), 'utf8')); }
  catch { return null; }
}

function listTasks() {
  try {
    return fs.readdirSync(TASKS_DIR)
      .filter(f => f.endsWith('.json'))
      .map(f => {
        try { return JSON.parse(fs.readFileSync(path.join(TASKS_DIR, f), 'utf8')); }
        catch { return null; }
      })
      .filter(Boolean)
      .sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt));
  } catch { return []; }
}

// ── BOB ASYNC RUNNER ──────────────────────────────────────────────────────
function runBobAsync(task) {
  const args = ['run', task.prompt, '--format', 'pretty', '--accept-license'];
  if (task.mode && task.mode !== 'agent') args.push('--mode', task.mode);

  log(`Task ${task.id} started: ${task.prompt.slice(0, 80)}`);
  task.status = 'running';
  task.startedAt = new Date().toISOString();
  saveTask(task);

  const proc = spawn('bob', args, {
    env: Object.assign({}, process.env, { BOB_API_KEY }),
    detached: false
  });

  let out = '', err = '';
  proc.stdout.on('data', d => { out += d.toString(); });
  proc.stderr.on('data', d => { err += d.toString(); });

  proc.on('close', (code) => {
    task.status      = code === 0 ? 'done' : 'error';
    task.finishedAt  = new Date().toISOString();
    task.durationMs  = Date.now() - new Date(task.startedAt).getTime();
    task.resultUrl   = `/api/task/${task.id}/result`;
    task.resultMdUrl = `/tasks/${task.id}`;

    // Save full markdown result
    const md = `# Task: ${task.prompt}\n\n` +
      `**Mode:** ${task.mode || 'agent'}  \n` +
      `**Started:** ${task.startedAt}  \n` +
      `**Finished:** ${task.finishedAt}  \n` +
      `**Duration:** ${(task.durationMs/1000).toFixed(1)}s  \n\n` +
      `---\n\n${out.trim()}` +
      (err.trim() ? `\n\n---\n**Stderr:**\n\`\`\`\n${err.trim()}\n\`\`\`` : '');

    fs.writeFileSync(resultPath(task.id), md);
    // Store short preview in task meta
    task.preview = out.trim().slice(0, 300);
    saveTask(task);
    log(`Task ${task.id} ${task.status} in ${(task.durationMs/1000).toFixed(1)}s`);
  });

  proc.on('error', (e) => {
    task.status = 'error';
    task.error  = e.message;
    task.finishedAt = new Date().toISOString();
    saveTask(task);
    log(`Task ${task.id} error: ${e.message}`);
  });
}

// ── STOCKS ────────────────────────────────────────────────────────────────
const SYMBOL_NAMES = {
  'AAPL':'Apple Inc','MSFT':'Microsoft','GOOGL':'Alphabet','AMZN':'Amazon',
  'NVDA':'NVIDIA','META':'Meta','TSLA':'Tesla','IBM':'IBM Corp',
  'BTC-USD':'Bitcoin','ETH-USD':'Ethereum'
};

async function fetchStocks() {
  const equities = ['AAPL','MSFT','GOOGL','AMZN','NVDA','META','TSLA','IBM'];
  const results  = {};
  await Promise.all(equities.map(sym =>
    axios.get(`https://api.marketdata.app/v1/stocks/quotes/${sym}/`, { timeout: 8000 })
      .then(r => {
        const d = r.data;
        if (d.s === 'ok') results[sym] = { name: SYMBOL_NAMES[sym]||sym, price: d.last[0], change: d.change[0], changePct: d.changepct[0]*100, volume: d.volume?d.volume[0]:null, currency:'USD' };
      }).catch(() => {})
  ));
  await axios.get('https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true', { timeout: 8000 })
    .then(r => {
      const d = r.data;
      if (d.bitcoin)  results['BTC-USD'] = { name:'Bitcoin',  price: d.bitcoin.usd,  changePct: d.bitcoin.usd_24h_change,  currency:'USD' };
      if (d.ethereum) results['ETH-USD'] = { name:'Ethereum', price: d.ethereum.usd, changePct: d.ethereum.usd_24h_change, currency:'USD' };
    }).catch(() => {});
  log(`Stocks: ${Object.keys(results).length} symbols`);
  return results;
}

function fetchNewsViaBob() {
  return new Promise(resolve => {
    const proc = spawn('bob', ['run',
      'Use firecrawl to scrape top 10 headlines from https://finance.yahoo.com. Return ONLY a JSON array with fields: title, url, source, summary.',
      '--format', 'json', '--accept-license'],
      { env: Object.assign({}, process.env, { BOB_API_KEY }) });
    let out = '';
    proc.stdout.on('data', d => { out += d.toString(); });
    proc.stderr.on('data', d => log(`news bob: ${d.toString().trim().slice(0,80)}`));
    proc.on('close', () => {
      try { const m = out.match(/\[[\s\S]*?\]/); resolve(m ? JSON.parse(m[0]) : []); }
      catch { resolve([]); }
    });
    setTimeout(() => { proc.kill(); resolve([]); }, 90000);
  });
}

async function morningRefresh() {
  log('Morning refresh starting...');
  const cache = loadCache();
  const [stocks, news] = await Promise.all([fetchStocks(), fetchNewsViaBob()]);
  cache.stocks = stocks; cache.news = news; cache.lastUpdated = new Date().toISOString();
  saveCache(cache);
  log('Morning refresh done');
}

// ── API ROUTES ────────────────────────────────────────────────────────────

// POST /api/chat — fire and forget, returns task ID immediately
app.post('/api/chat', (req, res) => {
  const { message, mode } = req.body;
  if (!message) return res.status(400).json({ error: 'message required' });

  const id = crypto.randomBytes(6).toString('hex');
  const task = {
    id,
    prompt:    message,
    mode:      mode || 'agent',
    status:    'queued',
    createdAt: new Date().toISOString(),
    startedAt: null,
    finishedAt: null,
    durationMs: null,
    preview:   null,
    resultUrl:  `/api/task/${id}/result`,
    resultMdUrl: `/tasks/${id}`
  };
  saveTask(task);
  log(`Task queued ${id}: ${message.slice(0,80)}`);

  // Fire async — do NOT await
  setImmediate(() => runBobAsync(task));

  res.json({
    taskId:      id,
    status:      'queued',
    message:     'Task started. Bob is working on it in the background.',
    pollUrl:     `/api/task/${id}`,
    resultUrl:   `/api/task/${id}/result`,
    resultMdUrl: `/tasks/${id}`
  });
});

// GET /api/task/:id — check status
app.get('/api/task/:id', (req, res) => {
  const task = loadTask(req.params.id);
  if (!task) return res.status(404).json({ error: 'Task not found' });
  res.json(task);
});

// GET /api/task/:id/result — raw markdown result
app.get('/api/task/:id/result', (req, res) => {
  const rp = resultPath(req.params.id);
  const task = loadTask(req.params.id);
  if (!task) return res.status(404).json({ error: 'Task not found' });
  if (task.status !== 'done' && task.status !== 'error') return res.json({ status: task.status, message: 'Task still running' });
  try {
    const md = fs.readFileSync(rp, 'utf8');
    res.type('text/plain').send(md);
  } catch { res.json({ status: task.status, preview: task.preview }); }
});

// GET /api/tasks — list all tasks
app.get('/api/tasks', (req, res) => {
  res.json(listTasks());
});

// GET /tasks/:id — human readable result page
app.get('/tasks/:id', (req, res) => {
  const task = loadTask(req.params.id);
  const rp   = resultPath(req.params.id);
  let result = '';
  try { result = fs.readFileSync(rp, 'utf8'); } catch {}

  const statusColor = { queued:'#e8b04b', running:'#4a9eff', done:'#00d084', error:'#ff4444' };
  const col = statusColor[task?.status] || '#57606a';

  res.send(`<!DOCTYPE html><html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Task ${req.params.id}</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:#0a0e1a;color:#c8d0e0;font-family:-apple-system,'Segoe UI',system-ui,sans-serif;padding:16px;max-width:800px;margin:0 auto}
  .topbar{display:flex;justify-content:space-between;align-items:center;padding:12px 0;border-bottom:1px solid #1e3a5f;margin-bottom:20px}
  .logo{color:#e8b04b;font-weight:700;font-size:16px;letter-spacing:1px}
  .logo span{color:#4a9eff}
  a{color:#4a9eff;text-decoration:none}
  .meta{background:#0d1117;border:1px solid #1e3a5f;border-radius:10px;padding:14px;margin-bottom:16px;font-size:13px;line-height:1.8}
  .meta b{color:#e8b04b}
  .badge{display:inline-block;padding:2px 10px;border-radius:10px;font-size:12px;font-weight:700;border:1px solid ${col};color:${col};background:${col}22}
  .result{background:#0d1117;border:1px solid #1e3a5f;border-radius:10px;padding:16px;white-space:pre-wrap;font-size:13px;line-height:1.7;overflow-x:auto}
  .refresh{display:inline-block;margin-top:12px;padding:8px 16px;background:#1e3a5f;color:#4a9eff;border-radius:8px;font-size:13px;cursor:pointer;border:none;font-family:inherit}
  .spinner{display:inline-block;width:14px;height:14px;border:2px solid #1e3a5f;border-top-color:#4a9eff;border-radius:50%;animation:spin .8s linear infinite;vertical-align:middle;margin-right:6px}
  @keyframes spin{to{transform:rotate(360deg)}}
  h2{color:#4a9eff;font-size:15px;margin-bottom:12px}
</style>
${task && (task.status === 'queued' || task.status === 'running') ? '<script>setTimeout(()=>location.reload(),5000)</script>' : ''}
</head><body>
<div class="topbar">
  <div class="logo">BOB <span>TASK</span></div>
  <a href="/chat">← Back to Ops</a>
</div>
${task ? `
<div class="meta">
  <b>Task ID:</b> ${task.id}<br>
  <b>Status:</b> <span class="badge">${task.status.toUpperCase()}</span><br>
  <b>Mode:</b> ${task.mode || 'agent'}<br>
  <b>Created:</b> ${new Date(task.createdAt).toLocaleString()}<br>
  ${task.finishedAt ? `<b>Finished:</b> ${new Date(task.finishedAt).toLocaleString()}<br>` : ''}
  ${task.durationMs ? `<b>Duration:</b> ${(task.durationMs/1000).toFixed(1)}s<br>` : ''}
  <b>Prompt:</b> ${task.prompt}
</div>
${task.status === 'queued' || task.status === 'running' ? `
<div style="text-align:center;padding:40px 0;color:#4a9eff">
  <div class="spinner"></div> Bob is working on this task...<br>
  <small style="color:#57606a">Page auto-refreshes every 5 seconds</small>
</div>` : `
<h2>Result</h2>
<div class="result">${result.replace(/</g,'&lt;').replace(/>/g,'&gt;') || task.preview || 'No result yet.'}</div>
<a href="/api/task/${task.id}/result" style="display:inline-block;margin-top:12px;padding:8px 16px;background:#003d1a;color:#00d084;border-radius:8px;font-size:13px">⬇ Download .md</a>
`}` : '<div style="color:#ff4444;padding:20px">Task not found</div>'}
</body></html>`);
});

// GET /tasks — list all tasks page
app.get('/tasks', (req, res) => {
  const tasks = listTasks();
  const statusColor = { queued:'#e8b04b', running:'#4a9eff', done:'#00d084', error:'#ff4444' };
  res.send(`<!DOCTYPE html><html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BOB Tasks</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:#0a0e1a;color:#c8d0e0;font-family:-apple-system,'Segoe UI',system-ui,sans-serif;padding:16px;max-width:800px;margin:0 auto}
  .topbar{display:flex;justify-content:space-between;align-items:center;padding:12px 0;border-bottom:1px solid #1e3a5f;margin-bottom:20px}
  .logo{color:#e8b04b;font-weight:700;font-size:16px;letter-spacing:1px}
  .logo span{color:#4a9eff}
  a{color:#4a9eff;text-decoration:none}
  .task-card{background:#0d1117;border:1px solid #1e3a5f;border-radius:10px;padding:14px;margin-bottom:10px;cursor:pointer}
  .task-card:active{background:#151c2e}
  .task-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
  .task-prompt{font-size:13px;color:#c8d0e0;line-height:1.4;margin-bottom:6px}
  .task-meta{font-size:11px;color:#57606a}
  .badge{display:inline-block;padding:1px 8px;border-radius:8px;font-size:11px;font-weight:700}
  .empty{text-align:center;padding:40px;color:#57606a}
</style>
</head><body>
<div class="topbar">
  <div class="logo">BOB <span>TASKS</span></div>
  <a href="/chat">← Back to Ops</a>
</div>
<h3 style="color:#57606a;font-size:12px;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px">${tasks.length} Tasks</h3>
${tasks.length === 0 ? '<div class="empty">No tasks yet. Send a message from the Ops panel.</div>' :
  tasks.map(t => {
    const col = statusColor[t.status] || '#57606a';
    return `<a href="/tasks/${t.id}" style="text-decoration:none">
    <div class="task-card">
      <div class="task-head">
        <span style="font-size:11px;color:#57606a">${t.id}</span>
        <span class="badge" style="color:${col};border:1px solid ${col};background:${col}22">${t.status.toUpperCase()}</span>
      </div>
      <div class="task-prompt">${t.prompt.slice(0,120)}${t.prompt.length>120?'…':''}</div>
      <div class="task-meta">${t.mode || 'agent'} · ${new Date(t.createdAt).toLocaleString()}${t.durationMs?` · ${(t.durationMs/1000).toFixed(1)}s`:''}</div>
    </div></a>`;
  }).join('')}
</body></html>`);
});

app.get('/api/stocks', async (req, res) => {
  const cache = loadCache();
  const age = cache.lastUpdated ? (Date.now() - new Date(cache.lastUpdated)) / 1000 : 999999;
  if (age > 300 || !cache.stocks || Object.keys(cache.stocks).length === 0) {
    cache.stocks = await fetchStocks();
    cache.lastUpdated = new Date().toISOString();
    saveCache(cache);
  }
  res.json({ stocks: cache.stocks, lastUpdated: cache.lastUpdated });
});

app.get('/api/news', (req, res) => {
  const c = loadCache(); res.json({ news: c.news || [], lastUpdated: c.lastUpdated });
});

app.post('/api/refresh', (req, res) => {
  res.json({ status: 'ok', message: 'Refresh started' });
  morningRefresh();
});

app.get('/api/status', (req, res) => {
  const c = loadCache();
  res.json({ status:'ok', lastUpdated: c.lastUpdated, newsCount:(c.news||[]).length, stockCount:Object.keys(c.stocks||{}).length, uptime: process.uptime(), taskCount: listTasks().length });
});

app.get('/chat', (req, res) => res.sendFile(path.join(__dirname, 'public', 'chat.html')));
app.get('/',     (req, res) => res.sendFile(path.join(__dirname, 'public', 'index.html')));

// ── SALES INTEL ROUTES ───────────────────────────────────────────────────

// GET /api/intel — list all saved account briefs
app.get('/api/intel', (req, res) => {
  res.json(salesIntel.listBriefs().map(b => ({
    accountId:   b.accountId,
    accountName: b.accountName,
    domain:      b.domain,
    tier:        b.tier,
    owner:       b.owner,
    urgency:     b.brief && b.brief.urgency,
    summary:     b.brief && b.brief.summary,
    signalCount: b.brief && b.brief.signals ? b.brief.signals.length : 0,
    generatedAt: b.generatedAt
  })));
});

// GET /api/intel/:id — full brief for one account
app.get('/api/intel/:id', (req, res) => {
  const brief = salesIntel.loadBrief(req.params.id);
  if (!brief) return res.status(404).json({ error: 'Brief not found' });
  res.json(brief);
});

// POST /api/intel/refresh — trigger a fresh run (body: { accounts: ['id1','id2'] } or empty for all)
app.post('/api/intel/refresh', async (req, res) => {
  const ids = (req.body && req.body.accounts) ? req.body.accounts : [];
  res.json({ status: 'started', message: `Sales intel refresh queued for ${ids.length || 'all'} accounts` });
  // Run in background, do not await here
  salesIntel.runAll(ids.length ? ids : null).catch(e => log(`sales-intel runAll error: ${e.message}`));
});

// POST /api/intel/accounts — replace the accounts list
app.post('/api/intel/accounts', (req, res) => {
  const accounts = req.body && req.body.accounts;
  if (!Array.isArray(accounts)) return res.status(400).json({ error: 'accounts array required' });
  require('fs').writeFileSync('/data/sales-intel/accounts.json', JSON.stringify({ accounts }, null, 2));
  res.json({ status: 'ok', count: accounts.length });
});

// ── TICKET ROUTES ────────────────────────────────────────────────────────

// POST /api/tickets — create a new ticket
app.post('/api/tickets', async (req, res) => {
  try {
    const { title, body, type, source, priority, mode, meta } = req.body;
    if (!body) return res.status(400).json({ error: 'body required' });
    const ticket = await tickets.createTicket({ title, body, type, source: source||'web', priority, mode, meta });
    res.json({ requestId: ticket.requestId, status: ticket.status, createdAt: ticket.createdAt });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

// GET /api/tickets — list all tickets
app.get('/api/tickets', async (req, res) => {
  try {
    const { status, type, limit, skip } = req.query;
    const list   = await tickets.listTickets({ status, type, limit: parseInt(limit)||50, skip: parseInt(skip)||0 });
    const counts = await tickets.countTickets();
    res.json({ counts, tickets: list });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

// GET /api/tickets/counts — just counts
app.get('/api/tickets/counts', async (req, res) => {
  try { res.json(await tickets.countTickets()); }
  catch(e) { res.status(500).json({ error: e.message }); }
});

// GET /api/tickets/:id — single ticket by requestId
app.get('/api/tickets/:id', async (req, res) => {
  try {
    const ticket = await tickets.getTicket(req.params.id);
    if (!ticket) return res.status(404).json({ error: 'Ticket not found' });
    res.json(ticket);
  } catch(e) { res.status(500).json({ error: e.message }); }
});

// PATCH /api/tickets/:id — update status manually
app.patch('/api/tickets/:id', async (req, res) => {
  try {
    const { status, result } = req.body;
    await tickets.updateTicketStatus(req.params.id, status, result);
    res.json({ ok: true });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

cron.schedule('0 6 * * *', () => { log('Cron: morning refresh'); morningRefresh(); });
cron.schedule('0 7 * * *', () => { log('Cron: sales intel daily run'); salesIntel.runAll(null).catch(e => log('sales-intel cron error: ' + e.message)); });

app.listen(PORT, '0.0.0.0', async () => {
  log(`Bob Terminal → http://20.89.63.64:${PORT}`);
  log(`  /        → Dashboard`);
  log(`  /chat    → Bob Ops`);
  log(`  /tasks   → Task History`);
  log(`  /api/intel → Sales Intelligence API`);
  const stocks = await fetchStocks();
  const cache = loadCache(); cache.stocks = stocks; cache.lastUpdated = new Date().toISOString();
  saveCache(cache);
  log(`Ready — ${Object.keys(stocks).length} stocks`);
});

// ── SYSTEM STATUS API ─────────────────────────────────────────────────────
const { execSync } = require('child_process');

function svcStatus(name) {
  try { execSync(`systemctl is-active ${name}`, { stdio: 'pipe' }); return 'active'; }
  catch { return 'inactive'; }
}

function dbPing(cmd) {
  try { execSync(cmd, { stdio: 'pipe', timeout: 3000, env: Object.assign({}, process.env, { PATH: process.env.PATH + ':/usr/bin:/usr/local/bin' }) }); return true; }
  catch { return false; }
}

function gitInfo() {
  try {
    const branch  = execSync('git -C /data/vault rev-parse --abbrev-ref HEAD', { stdio: 'pipe' }).toString().trim();
    const hash    = execSync('git -C /data/vault rev-parse --short HEAD',      { stdio: 'pipe' }).toString().trim();
    const subject = execSync('git -C /data/vault log -1 --pretty=%s',          { stdio: 'pipe' }).toString().trim();
    const count   = execSync('git -C /data/vault rev-list --count HEAD',       { stdio: 'pipe' }).toString().trim();
    const status  = execSync('git -C /data/vault status --short',               { stdio: 'pipe' }).toString().trim();
    return { branch, hash, subject, commitCount: parseInt(count), dirty: status.length > 0, uncommitted: status.split('\n').filter(Boolean).length };
  } catch { return null; }
}

function vaultStats() {
  try {
    const files = execSync('find /data/vault -name "*.md" -not -path "*/.git/*" | wc -l', { stdio: 'pipe' }).toString().trim();
    const size  = execSync('du -sh /data/vault --exclude=.git 2>/dev/null | cut -f1',      { stdio: 'pipe' }).toString().trim();
    return { noteCount: parseInt(files), size };
  } catch { return { noteCount: 0, size: '?' }; }
}

function diskInfo() {
  try {
    const home = execSync("df /home --output=used,avail,pcent | tail -1", { stdio: 'pipe' }).toString().trim().split(/\s+/);
    const data = execSync("df /data --output=used,avail,pcent | tail -1", { stdio: 'pipe' }).toString().trim().split(/\s+/);
    return {
      home: { used: home[0], avail: home[1], pct: home[2] },
      data: { used: data[0], avail: data[1], pct: data[2] }
    };
  } catch { return null; }
}

app.get('/api/system', (req, res) => {
  const cache  = loadCache();
  const tasks  = listTasks();
  const done   = tasks.filter(t => t.status === 'done').length;
  const running= tasks.filter(t => t.status === 'running').length;
  const errors = tasks.filter(t => t.status === 'error').length;

  res.json({
    timestamp: new Date().toISOString(),
    vm: {
      ip: '20.89.63.64',
      os: 'RHEL 10.2',
      uptime: Math.floor(process.uptime())
    },
    services: {
      'bob-terminal': { status: 'active', port: 44290, note: 'This server' },
      postgresql:     { status: svcStatus('postgresql'), port: 5432, db: 'devdb', user: 'dbmanager', ping: dbPing("psql postgresql://dbmanager:dbmanager_pass@localhost:5432/devdb -c 'SELECT 1' -q") },
      mongod:         { status: svcStatus('mongod'),     port: 27017, ping: dbPing("mongosh mongodb://localhost:27017 --eval 'db.runCommand({ping:1})' --quiet") },
      valkey:         { status: svcStatus('valkey'),     port: 6379,  ping: dbPing('valkey-cli ping') }
    },
    mcp: [
      { key: 'obsidian-vault', pkg: '@modelcontextprotocol/server-filesystem', target: '/data/vault',         note: 'Read/write vault notes' },
      { key: 'postgres',       pkg: '@crystaldba/postgres-mcp',                target: 'localhost:5432',      note: 'SQL queries + schema + perf' },
      { key: 'mongodb',        pkg: 'mongodb-mcp-server',                      target: 'localhost:27017',     note: 'Document CRUD + aggregation' },
      { key: 'redis',          pkg: 'redis-mcp-server (uvx)',                  target: 'localhost:6379',      note: 'Key-value + cache + TTL' },
      { key: 'firecrawl-mcp',  pkg: 'firecrawl-mcp',                          target: 'web',                 note: 'Scraping + search' }
    ],
    modes: [
      { slug: 'terminal-ops',  name: 'Terminal Ops',      desc: 'Full VM operator — services, dashboard, deployments' },
      { slug: 'db-manager',    name: 'Database Manager',  desc: 'PostgreSQL, MongoDB, Valkey via MCP tools' },
      { slug: 'vault-manager', name: 'Vault Manager',     desc: 'Obsidian vault notes + git commit/push' }
    ],
    skills: [
      { name: 'db-manager',      desc: 'Step-by-step DB ops — query, insert, backup, safety checks' },
      { name: 'obsidian-vault',  desc: 'Write notes, sync bob-config, commit and push to GitHub' }
    ],
    vault: { ...vaultStats(), git: gitInfo(), path: '/data/vault', github: 'https://github.com/FadlyHidayat2651/HeadlessAI' },
    tasks: { total: tasks.length, done, running, errors, queued: tasks.filter(t => t.status === 'queued').length },
    market: { stockCount: Object.keys(cache.stocks || {}).length, newsCount: (cache.news || []).length, lastUpdated: cache.lastUpdated },
    disk: diskInfo()
  });
});

app.get('/system',  (req, res) => res.sendFile(path.join(__dirname, 'public', 'system.html')));
app.get('/tickets', (req, res) => res.sendFile(path.join(__dirname, 'public', 'tickets.html')));
