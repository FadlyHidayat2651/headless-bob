import sys, os, json, subprocess, re, time, threading, queue
import urllib.request as ur
sys.path.insert(0, '/data/pylibs')
from flask import Flask, jsonify, request, Response, stream_with_context
from datetime import datetime
import markdown as md_lib

app = Flask(__name__)
COMPANY    = '/data/company'
HARNESS    = 'http://localhost:44285'
MODES_YAML = '/home/itzuser/.bob/settings/custom_modes.yaml'
SLACK_WEBHOOK = 'https://hooks.slack.com/triggers/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'

# ── SSE broadcast bus ─────────────────────────────────────────────────────────
# All connected /api/events subscribers receive every pushed event
_sse_subscribers: list[queue.Queue] = []
_sse_lock = threading.Lock()

def _sse_broadcast(event_type: str, data: dict):
    """Push an event to all /api/events subscribers."""
    payload = json.dumps({'type': event_type, 'content': data,
                          'ts': datetime.utcnow().strftime('%H:%M UTC')})
    with _sse_lock:
        dead = []
        for q in _sse_subscribers:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_subscribers.remove(q)

def slack_push(text: str, icon: str = '🤖'):
    """Fire-and-forget POST to Slack webhook — runs in background thread."""
    def _post():
        try:
            msg = f"{icon} *Bob Agent HQ* — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n{text[:3000]}"
            data = json.dumps({'message_queue': msg}).encode()
            req = ur.Request(SLACK_WEBHOOK, data=data,
                             headers={'Content-Type': 'application/json'}, method='POST')
            with ur.urlopen(req, timeout=10) as r:
                pass
        except Exception:
            pass
    threading.Thread(target=_post, daemon=True).start()

# ── Helpers ───────────────────────────────────────────────────────────────────

def read_file(path):
    try: return open(path).read()
    except: return None

def file_mtime(path):
    try: return datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%d %H:%M UTC')
    except: return 'never'

def svc_status(name):
    try:
        r = subprocess.run(['systemctl','is-active',name], capture_output=True, text=True)
        return r.stdout.strip()
    except: return 'unknown'

def load_agent_descriptions():
    """Read whenToUse field for each agent from custom_modes.yaml."""
    defaults = {
        'intel-agent': 'Live web research & competitor intelligence',
        'ops-agent':   'Infrastructure health & self-healing',
        'dev-agent':   'Software engineering & deployment',
        'ceo-agent':   'Executive orchestrator · routes to sub-agents · synthesizes results',
    }
    try:
        import yaml
        with open(MODES_YAML) as f:
            data = yaml.safe_load(f)
        for m in data.get('customModes', []):
            slug = m.get('slug', '')
            if slug in defaults:
                # Prefer whenToUse (short, user-facing), fall back to description
                text = m.get('whenToUse') or m.get('description') or ''
                text = text.strip()
                if text:
                    defaults[slug] = text
    except Exception:
        pass
    return defaults

AGENT_DESCS = load_agent_descriptions()

def extract_reply(raw):
    """
    Bob output has lines padded to 120 chars.
    Section separators use U+2500 BOX DRAWING LIGHT HORIZONTAL (─), NOT ASCII hyphen (-).
    We ONLY break on ─────, never on markdown --- which appears inside content.
    """
    # Strip 120-char right-padding Bob adds to every line
    lines = [l.rstrip() for l in raw.split('\n')]

    last = -1
    for i, l in enumerate(lines):
        if re.search(r'Assistant\s*\(\d+\)', l):
            last = i

    if last >= 0:
        out = []
        for j in range(last + 1, len(lines)):
            if 'Task Summary' in lines[j]:
                break
            # Only stop on Bob's box-drawing separator (U+2500 ─), NOT on markdown ---
            stripped = lines[j].strip()
            if stripped and all(c == '─' for c in stripped) and len(stripped) >= 5:
                break
            out.append(lines[j])
        reply = '\n'.join(out).strip()
        if reply:
            return reply

    # Fallback: strip only Bob chrome (─── separators and Task Summary), preserve content
    cleaned = re.sub(r'Task Summary[\s\S]*$', '', raw)
    cleaned = re.sub(r'^─{5,}\s*$', '', cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r'Assistant\s*\(\d+\)[^\n]*\n', '', cleaned)
    return cleaned.strip()

def start_job(prompt, mode):
    data = json.dumps({'prompt': prompt, 'mode': mode}).encode()
    req = ur.Request(HARNESS + '/jobs', data=data,
                     headers={'Content-Type': 'application/json'}, method='POST')
    with ur.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def poll_job(job_id, timeout=180):
    """Poll /jobs/{id} until done. Returns (status, output)."""
    for _ in range(timeout // 3):
        time.sleep(3)
        with ur.urlopen(f'{HARNESS}/jobs/{job_id}', timeout=5) as r:
            d = json.loads(r.read())
        if d.get('status') in ('completed', 'failed', 'timeout'):
            return d.get('status'), extract_reply(d.get('output', ''))
    return 'timeout', ''

def run_agent_with_retry(prompt, mode, max_retries=2):
    """
    Loop-engineering: run agent, if output is empty or status is failed,
    push back to the SAME agent with the error context so it can self-resolve.
    Returns (final_output, attempts, recovered).
    """
    attempts = 0
    last_error = None

    for attempt in range(max_retries + 1):
        attempts += 1

        if attempt > 0:
            # Build self-healing retry prompt
            retry_prompt = (
                f"SELF-HEALING RETRY (attempt {attempt + 1}):\n\n"
                f"Your previous run encountered this issue:\n{last_error}\n\n"
                f"Original task:\n{prompt}\n\n"
                f"Please analyse the error above, resolve it autonomously, "
                f"and complete the original task successfully."
            )
            job = start_job(retry_prompt, mode)
        else:
            job = start_job(prompt, mode)

        job_id = job.get('id')
        if not job_id:
            last_error = 'No job ID returned from Harness'
            continue

        status, output = poll_job(job_id)

        if status == 'completed' and output.strip():
            return output, attempts, attempt > 0

        # Capture error for next retry
        if not output.strip():
            last_error = f"Agent returned empty output (status={status})"
        else:
            last_error = f"Status={status}. Agent output:\n{output[:500]}"

    # All retries exhausted — return whatever we have
    return output or f'[Agent did not produce output after {attempts} attempts]', attempts, False

def invoke_bob_via_job(prompt, mode, timeout=300):
    """Synchronous wrapper: start job, poll, return extracted reply."""
    job = start_job(prompt, mode)
    job_id = job.get('id')
    if not job_id:
        raise RuntimeError('Harness did not return job ID')
    _, output = poll_job(job_id, timeout=timeout)
    return output

# ── API routes ────────────────────────────────────────────────────────────────

@app.route('/api/state')
def state():
    try:
        with ur.urlopen(f'{HARNESS}/jobs', timeout=3) as r:
            raw = json.loads(r.read())
            jobs = raw.get('runs', raw) if isinstance(raw, dict) else raw
            total_jobs = len(jobs) if isinstance(jobs, list) else 0
            agent_jobs = {}
            for j in (jobs if isinstance(jobs, list) else []):
                m = j.get('mode', 'unknown')
                agent_jobs[m] = agent_jobs.get(m, 0) + 1
    except:
        total_jobs = 0; agent_jobs = {}

    agents = [
        {'id':'intel','name':'Intel Agent','icon':'🔍','color':'#42be65','mode':'intel-agent',
         'tools':['firecrawl_scrape','firecrawl_search','write_file'],
         'report': f'{COMPANY}/intel-report.md'},
        {'id':'ops',  'name':'Ops Agent',  'icon':'⚙️', 'color':'#f1c21b','mode':'ops-agent',
         'tools':['execute_command','write_file'],
         'report': f'{COMPANY}/ops-health.md'},
        {'id':'dev',  'name':'Dev Agent',  'icon':'💻', 'color':'#be95ff','mode':'dev-agent',
         'tools':['write_file','execute_command','read_file'],
         'report': f'{COMPANY}/dev-status.md'},
    ]
    result = []
    for a in agents:
        content = read_file(a['report']) or ''
        result.append({
            **a,
            'specialty': AGENT_DESCS.get(a['mode'], a['mode']),
            'last_run': file_mtime(a['report']),
            'status': 'active' if len(content) > 100 else 'idle',
            'preview': content[:180].strip(),
            'jobs_run': agent_jobs.get(a['mode'], 0),
        })
    board = read_file(f'{COMPANY}/board-report.md') or ''
    alerts_raw = read_file(f'{COMPANY}/alerts.md') or ''
    return jsonify({
        'agents': result,
        'ceo': {
            'last_run': file_mtime(f'{COMPANY}/board-report.md'),
            'report': board,
            'report_html': md_lib.markdown(board, extensions=['tables']) if board else '',
            'jobs_run': agent_jobs.get('ceo-agent', 0),
            'description': AGENT_DESCS.get('ceo-agent', ''),
        },
        'meta': {
            'timestamp': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC'),
            'total_jobs': total_jobs,
            'alerts': alerts_raw.count('⚠️'),
            'services': {
                'bob':          svc_status('bob'),
                'bob-harness':  svc_status('bob-harness'),
                'bob-terminal': svc_status('bob-terminal'),
            }
        }
    })

@app.route('/api/report/<agent_id>')
def report(agent_id):
    files = {
        'intel': f'{COMPANY}/intel-report.md',
        'ops':   f'{COMPANY}/ops-health.md',
        'dev':   f'{COMPANY}/dev-status.md',
        'ceo':   f'{COMPANY}/board-report.md',
    }
    f = files.get(agent_id)
    if not f: return jsonify({'error': 'unknown'}), 404
    content = read_file(f) or '*(no report yet)*'
    return jsonify({
        'raw': content,
        'html': md_lib.markdown(content, extensions=['tables']),
        'last_updated': file_mtime(f),
    })

@app.route('/api/chat', methods=['POST'])
def chat():
    body    = request.get_json()
    message = body.get('message') or body.get('prompt', '')

    def generate():
        def send(t, d): return f"data: {json.dumps({'type': t, 'content': d})}\n\n"

        msg_lower = message.lower()
        needs_intel = any(w in msg_lower for w in [
            'market','kompetitor','competitor','pasar','industri','industry',
            'pricing','trend','berita','news','internet','web','search','cari','riset',
            'latest','terbaru','scrape','harga','saham','stock'])
        needs_ops = any(w in msg_lower for w in [
            'server','disk','cpu','ram','memory','service','health','infra',
            'sistem','system','uptime','error','alert','storage','log','kondisi'])
        needs_dev = any(w in msg_lower for w in [
            'code','deploy','build','ship','feature','bug','engineering',
            'dev','app','api','website','script','program'])

        try:
            intel_out = ops_out = dev_out = None

            # ── Intel Agent ──────────────────────────────────────────────────
            if needs_intel:
                yield send('thinking', {'agent':'ceo','text':'Analyzing request — routing to Intel Agent for live web research...'})
                yield send('delegate', {'from':'ceo','to':'intel','reason':'Live market data required'})
                time.sleep(0.4)
                yield send('agent_start', {'agent':'intel','task': f'Research: {message[:80]}'})
                job = start_job(
                    f"Use firecrawl_scrape or firecrawl_search to research: {message}. Get LIVE data from the internet.",
                    'intel-agent')
                job_id = job.get('id')
                yield send('job_started', {'agent':'intel','job_id':job_id})

                last_error = None
                for tick in range(60):  # up to 3 min
                    time.sleep(3)
                    with ur.urlopen(f'{HARNESS}/jobs/{job_id}', timeout=5) as r:
                        d = json.loads(r.read())
                    status = d.get('status')
                    if status in ('completed', 'failed', 'timeout'):
                        intel_out = extract_reply(d.get('output', ''))
                        # ── Loop engineering: retry if empty ─────────────────
                        if not intel_out.strip() and status != 'timeout':
                            last_error = f"Status={status}, output empty"
                            yield send('loop_retry', {'agent':'intel','attempt':1,'reason':last_error})
                            yield send('agent_start', {'agent':'intel','task':'Self-healing retry...'})
                            retry_prompt = (
                                f"SELF-HEALING RETRY:\nYour previous run produced no output (status={status}).\n"
                                f"Original task: research: {message}\n"
                                f"Please retry and ensure you produce a complete response."
                            )
                            rjob = start_job(retry_prompt, 'intel-agent')
                            rjob_id = rjob.get('id')
                            for rtick in range(40):
                                time.sleep(3)
                                with ur.urlopen(f'{HARNESS}/jobs/{rjob_id}', timeout=5) as r2:
                                    rd = json.loads(r2.read())
                                if rd.get('status') in ('completed','failed','timeout'):
                                    intel_out = extract_reply(rd.get('output',''))
                                    break
                                yield send('agent_progress', {'agent':'intel','elapsed':(rtick+1)*3,'retrying':True})
                        break
                    yield send('agent_progress', {'agent':'intel','elapsed':(tick+1)*3})

                yield send('agent_done', {'agent':'intel','preview':(intel_out or '')[:120]})

            # ── Ops Agent ────────────────────────────────────────────────────
            if needs_ops:
                yield send('thinking', {'agent':'ceo','text':'Routing to Ops Agent for infrastructure check...'})
                yield send('delegate', {'from':'ceo','to':'ops','reason':'System state required'})
                time.sleep(0.4)
                yield send('agent_start', {'agent':'ops','task':'Infrastructure health check'})
                job = start_job(
                    f"Check system health for this request: {message}. Run actual shell commands (df, free, systemctl, ss).",
                    'ops-agent')
                job_id = job.get('id')
                yield send('job_started', {'agent':'ops','job_id':job_id})

                for tick in range(40):  # up to 2 min
                    time.sleep(3)
                    with ur.urlopen(f'{HARNESS}/jobs/{job_id}', timeout=5) as r:
                        d = json.loads(r.read())
                    status = d.get('status')
                    if status in ('completed', 'failed', 'timeout'):
                        ops_out = extract_reply(d.get('output', ''))
                        # ── Loop engineering: retry on failure ────────────────
                        if (not ops_out.strip() or status == 'failed') and status != 'timeout':
                            raw_output = d.get('output','')
                            yield send('loop_retry', {'agent':'ops','attempt':1,'reason':f'status={status}'})
                            yield send('agent_start', {'agent':'ops','task':'Self-healing: resolving error...'})
                            retry_prompt = (
                                f"SELF-HEALING RETRY:\nYour previous run failed (status={status}).\n"
                                f"Error context:\n{raw_output[:400]}\n\n"
                                f"Original task: {message}\n\n"
                                f"Analyse the error, resolve it, and complete the task."
                            )
                            rjob = start_job(retry_prompt, 'ops-agent')
                            rjob_id = rjob.get('id')
                            for rtick in range(30):
                                time.sleep(3)
                                with ur.urlopen(f'{HARNESS}/jobs/{rjob_id}', timeout=5) as r2:
                                    rd = json.loads(r2.read())
                                if rd.get('status') in ('completed','failed','timeout'):
                                    ops_out = extract_reply(rd.get('output',''))
                                    break
                                yield send('agent_progress', {'agent':'ops','elapsed':(rtick+1)*3,'retrying':True})
                        break
                    yield send('agent_progress', {'agent':'ops','elapsed':(tick+1)*3})

                yield send('agent_done', {'agent':'ops','preview':(ops_out or '')[:120]})

            # ── Dev Agent ────────────────────────────────────────────────────
            if needs_dev:
                yield send('thinking', {'agent':'ceo','text':'Routing to Dev Agent for engineering task...'})
                yield send('delegate', {'from':'ceo','to':'dev','reason':'Software development required'})
                time.sleep(0.4)
                yield send('agent_start', {'agent':'dev','task':message[:80]})
                job = start_job(message, 'dev-agent')
                job_id = job.get('id')
                yield send('job_started', {'agent':'dev','job_id':job_id})

                for tick in range(60):
                    time.sleep(3)
                    with ur.urlopen(f'{HARNESS}/jobs/{job_id}', timeout=5) as r:
                        d = json.loads(r.read())
                    status = d.get('status')
                    if status in ('completed', 'failed', 'timeout'):
                        dev_out = extract_reply(d.get('output', ''))
                        # ── Loop engineering: retry on failure ────────────────
                        if (not dev_out.strip() or status == 'failed') and status != 'timeout':
                            raw_output = d.get('output','')
                            yield send('loop_retry', {'agent':'dev','attempt':1,'reason':f'status={status}'})
                            yield send('agent_start', {'agent':'dev','task':'Self-healing: resolving error...'})
                            retry_prompt = (
                                f"SELF-HEALING RETRY:\nYour previous run failed (status={status}).\n"
                                f"Error context:\n{raw_output[:400]}\n\n"
                                f"Original task: {message}\n\n"
                                f"Analyse the error, resolve it, and complete the task."
                            )
                            rjob = start_job(retry_prompt, 'dev-agent')
                            rjob_id = rjob.get('id')
                            for rtick in range(40):
                                time.sleep(3)
                                with ur.urlopen(f'{HARNESS}/jobs/{rjob_id}', timeout=5) as r2:
                                    rd = json.loads(r2.read())
                                if rd.get('status') in ('completed','failed','timeout'):
                                    dev_out = extract_reply(rd.get('output',''))
                                    break
                                yield send('agent_progress', {'agent':'dev','elapsed':(rtick+1)*3,'retrying':True})
                        break
                    yield send('agent_progress', {'agent':'dev','elapsed':(tick+1)*3})

                yield send('agent_done', {'agent':'dev','preview':(dev_out or '')[:120]})

            # ── CEO Synthesis ─────────────────────────────────────────────────
            yield send('thinking', {'agent':'ceo','text':'All sub-agents reported back. Synthesizing executive response...'})
            yield send('delegate', {'from':None,'to':'ceo','reason':'Synthesis'})

            parts = [f"Question: {message}"]
            if intel_out: parts.append(f"Intel Agent findings:\n{intel_out[:2000]}")
            if ops_out:   parts.append(f"Ops Agent report:\n{ops_out[:1500]}")
            if dev_out:   parts.append(f"Dev Agent output:\n{dev_out[:1500]}")
            if not any([intel_out, ops_out, dev_out]):
                # No agents delegated — read cached reports directly
                for fname, label in [
                    (f'{COMPANY}/board-report.md', 'Board Report'),
                    (f'{COMPANY}/intel-report.md', 'Intel Report'),
                    (f'{COMPANY}/ops-health.md',   'Ops Health'),
                ]:
                    content = read_file(fname)
                    if content: parts.append(f"{label}:\n{content[:800]}")

            ceo_prompt = (
                "You are the CEO Agent. Synthesize the following sub-agent results into a sharp, "
                "data-driven executive answer. Use markdown. Be concise but specific. "
                "Do NOT truncate — provide the complete answer.\n\n"
                + "\n\n".join(parts)
            )
            ceo_out = invoke_bob_via_job(ceo_prompt, 'ceo-agent', timeout=300)

            # Fallback: if CEO synthesis returned empty, build a plain summary ourselves
            if not ceo_out.strip():
                fallback_parts = []
                if dev_out:
                    fallback_parts.append(f"**Dev Agent** completed the task:\n\n{dev_out}")
                elif ops_out:
                    fallback_parts.append(f"**Ops Agent** completed the task:\n\n{ops_out}")
                elif intel_out:
                    fallback_parts.append(f"**Intel Agent** findings:\n\n{intel_out}")
                ceo_out = "\n\n".join(fallback_parts) if fallback_parts else f"Task completed. Sub-agents processed: {message}"

            yield send('reply', ceo_out)

            # ── Push to Slack + SSE broadcast bus ─────────────────────────────
            agents_used = [a for a, o in [('Intel',intel_out),('Ops',ops_out),('Dev',dev_out)] if o]
            slack_summary = f"*Q:* {message}\n\n{ceo_out}"
            if agents_used:
                slack_summary += f"\n\n_Agents used: {', '.join(agents_used)}_"
            slack_push(slack_summary, icon='👔')
            _sse_broadcast('reply', {'message': message, 'reply': ceo_out,
                                     'agents': agents_used,
                                     'ts': datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')})

        except Exception as e:
            import traceback
            yield send('error', str(e))

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@app.route('/api/events')
def sse_events():
    """SSE stream — broadcasts every CEO reply to all connected browsers.
    Lets any open tab (including dashboards) receive live Bob replies without
    being the one who sent the chat message.
    """
    q = queue.Queue(maxsize=50)
    with _sse_lock:
        _sse_subscribers.append(q)

    def stream():
        try:
            # Send a heartbeat immediately so the browser knows it's connected
            yield f"data: {json.dumps({'type':'connected','content':'Bob HQ live'})}\n\n"
            while True:
                try:
                    payload = q.get(timeout=25)
                    yield f"data: {payload}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"  # SSE comment keeps connection alive
        except GeneratorExit:
            pass
        finally:
            with _sse_lock:
                try:
                    _sse_subscribers.remove(q)
                except ValueError:
                    pass

    return Response(stream_with_context(stream()), mimetype='text/event-stream',
                    headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@app.route('/api/slack/push', methods=['POST'])
def slack_push_manual():
    """Manually push a message to Slack. Body: {text, icon?}"""
    body = request.get_json() or {}
    text = body.get('text', '')
    icon = body.get('icon', '🤖')
    if not text:
        return jsonify({'error': 'text required'}), 400
    slack_push(text, icon)
    return jsonify({'ok': True})

@app.route('/dashboard/<path:filename>')
def dashboard(filename):
    """Serve any HTML file from /data/my-dashboard/"""
    import os
    from flask import send_from_directory
    base_dir = '/data/my-dashboard'
    safe = os.path.realpath(os.path.join(base_dir, filename))
    if not safe.startswith(os.path.realpath(base_dir)):
        return 'forbidden', 403
    return send_from_directory(base_dir, filename)

@app.route('/api/dashboards')
def list_dashboards():
    """List available dashboards in /data/my-dashboard/"""
    import glob
    files = glob.glob('/data/my-dashboard/*.html')
    return jsonify([{
        'name': os.path.basename(f),
        'url': f'/dashboard/{os.path.basename(f)}',
        'size': os.path.getsize(f),
        'mtime': file_mtime(f),
    } for f in sorted(files)])

@app.route('/')
def index():
    base = request.args.get('base', '')
    return HTML.replace("const API_BASE = '';", f"const API_BASE = '{base}';")

# ── HTML ──────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>IBM Bob — Agent HQ</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap">
<style>
:root{
  --bg:#0d1117;--s1:#161b22;--s2:#21262d;--s3:#30363d;
  --text:#e6edf3;--muted:#7d8590;--dim:#484f58;
  --blue:#4589ff;--ibm:#0f62fe;
  --green:#3fb950;--yellow:#d29922;--red:#f85149;--purple:#bc8cff;--cyan:#76e3ea;
  --mono:'IBM Plex Mono',monospace;--sans:'IBM Plex Sans',sans-serif;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px;min-height:100vh;overflow-x:hidden}

/* TOP BAR */
.topbar{background:#000;border-bottom:1px solid var(--s3);height:48px;display:flex;align-items:center;padding:0 24px;gap:0;position:sticky;top:0;z-index:200}
.t-logo{display:flex;align-items:center;gap:10px;font-size:14px}
.t-logo strong{font-weight:600}
.t-div{width:1px;height:16px;background:var(--s3);margin:0 12px}
.t-nav{display:flex;align-items:center;margin-left:auto;gap:0}
.t-link{height:48px;padding:0 14px;display:flex;align-items:center;font-size:13px;color:var(--muted);text-decoration:none;border:none;background:none;cursor:pointer;font-family:var(--sans);transition:color .1s,background .1s}
.t-link:hover{color:var(--text);background:rgba(255,255,255,.05)}
.t-status{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--muted);padding:0 16px;border-left:1px solid var(--s3)}
.t-dot{width:7px;height:7px;border-radius:50%;background:var(--green)}
.t-time{font-family:var(--mono);font-size:11px;color:var(--dim)}

/* PAGE */
.page{max-width:1280px;margin:0 auto;padding:24px;display:grid;grid-template-columns:1fr 400px;gap:20px;min-height:calc(100vh - 48px)}

/* LEFT COLUMN */
.left-col{display:flex;flex-direction:column;gap:20px;min-width:0}

/* CEO CARD */
.ceo-card{background:linear-gradient(135deg,#0a1628 0%,var(--s1) 70%);border:1px solid #1e3a5f;border-radius:8px;overflow:hidden;position:relative}
.ceo-top-bar{height:2px;background:linear-gradient(90deg,var(--ibm) 0%,#a6c8ff 50%,var(--ibm) 100%);background-size:200%;animation:shimmer 3s linear infinite}
@keyframes shimmer{0%{background-position:0%}100%{background-position:200%}}
.ceo-body{padding:20px 24px}
.ceo-header{display:flex;align-items:center;gap:16px;margin-bottom:20px}
.ceo-avatar{width:48px;height:48px;border-radius:50%;background:var(--ibm);display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0;border:2px solid #2d5aad}
.ceo-meta{flex:1}
.ceo-name{font-size:16px;font-weight:600;margin-bottom:3px}
.ceo-desc{font-size:12px;color:var(--muted)}
.ceo-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px}
.stat{background:rgba(0,0,0,.3);border:1px solid var(--s3);border-radius:6px;padding:10px 12px;text-align:center}
.stat-n{font-size:22px;font-weight:700;font-family:var(--mono);color:var(--blue);line-height:1}
.stat-l{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-top:4px}
.ceo-report{background:rgba(0,0,0,.3);border:1px solid var(--s3);border-radius:6px;padding:14px;font-size:12px;line-height:1.7;max-height:160px;overflow-y:auto;cursor:pointer;transition:border-color .2s}
.ceo-report:hover{border-color:var(--blue)}
.ceo-report h1,.ceo-report h2,.ceo-report h3{font-size:12px;font-weight:600;color:#a6c8ff;margin:6px 0 3px}
.ceo-report p{color:#c6c6c6;margin:3px 0}
.ceo-report table{width:100%;border-collapse:collapse;font-size:11px}
.ceo-report th{padding:3px 6px;border-bottom:1px solid var(--s3);color:var(--muted);text-align:left}
.ceo-report td{padding:3px 6px;border-bottom:1px solid var(--s2)}

/* ORCHESTRATION VISUALIZER */
.orch-card{background:var(--s1);border:1px solid var(--s3);border-radius:8px;overflow:hidden}
.orch-header{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--s3)}
.orch-title{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:var(--muted)}
.orch-live{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--green)}
.orch-body{padding:16px;min-height:120px;display:flex;align-items:center;justify-content:center}
.flow-idle{color:var(--dim);font-size:12px;text-align:center}

/* FLOW ANIMATION */
.flow-active{width:100%;display:flex;align-items:center;gap:0;justify-content:center;flex-wrap:nowrap}
.flow-node{display:flex;flex-direction:column;align-items:center;gap:6px;padding:10px 12px;border-radius:8px;border:1px solid var(--s3);background:var(--s2);transition:all .3s;min-width:80px;position:relative}
.flow-node.active{border-color:var(--color, var(--blue));background:rgba(69,137,255,.08);box-shadow:0 0 16px rgba(69,137,255,.2)}
.flow-node.active.intel{border-color:var(--green);background:rgba(63,185,80,.08);box-shadow:0 0 16px rgba(63,185,80,.2)}
.flow-node.active.ops{border-color:var(--yellow);background:rgba(210,153,34,.08);box-shadow:0 0 16px rgba(210,153,34,.2)}
.flow-node.active.dev{border-color:var(--purple);background:rgba(188,140,255,.08);box-shadow:0 0 16px rgba(188,140,255,.2)}
.flow-node.active.ceo{border-color:var(--blue);background:rgba(69,137,255,.08)}
.flow-node.done{border-color:var(--green);opacity:.7}
.flow-node.retrying{border-color:var(--yellow)!important;background:rgba(210,153,34,.1)!important;animation:retryPulse .6s infinite}
@keyframes retryPulse{0%,100%{box-shadow:0 0 0 transparent}50%{box-shadow:0 0 12px rgba(210,153,34,.5)}}
.flow-icon{font-size:22px}
.flow-name{font-size:10px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.flow-task{font-size:9px;color:var(--dim);text-align:center;max-width:72px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.flow-arrow{font-size:18px;color:var(--s3);padding:0 4px;transition:color .3s;flex-shrink:0}
.flow-arrow.active{color:var(--blue);animation:arrowPulse .6s infinite}
@keyframes arrowPulse{0%,100%{opacity:1}50%{opacity:.3}}
.flow-status-bar{padding:8px 16px;border-top:1px solid var(--s3);font-size:11px;color:var(--muted);font-family:var(--mono);min-height:32px;display:flex;align-items:center;gap:8px}
.blink{animation:blink 1.2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}

/* AGENT GRID */
.agent-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.agent-card{background:var(--s1);border:1px solid var(--s3);border-radius:8px;overflow:hidden;cursor:pointer;transition:border-color .2s,transform .15s,box-shadow .2s}
.agent-card:hover{transform:translateY(-2px)}
.agent-card.active-job{animation:cardGlow .8s ease infinite alternate}
@keyframes cardGlow{from{box-shadow:0 0 0 transparent}to{box-shadow:0 0 20px var(--glow-color,rgba(69,137,255,.4))}}
.agent-card.selected{border-color:var(--blue)}
.ac-top{padding:14px;border-bottom:1px solid var(--s3);display:flex;align-items:center;gap:10px}
.ac-icon{width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0;border:2px solid transparent}
.ac-name{font-size:13px;font-weight:600;margin-bottom:1px}
.ac-role{font-size:11px;color:var(--muted)}
.ac-body{padding:12px 14px}
.ac-row{display:flex;justify-content:space-between;align-items:center;font-size:11px;margin-bottom:6px}
.ac-label{color:var(--muted)}
.pill{font-size:10px;padding:1px 8px;border-radius:100px;font-weight:600}
.pill-green{background:#0a2318;color:var(--green);border:1px solid #1a3a28}
.pill-idle{background:var(--s2);color:var(--muted);border:1px solid var(--s3)}
.pill-run{background:#0a1628;color:var(--blue);border:1px solid #1e3a5f;animation:blink .8s infinite}
.ac-tools{display:flex;gap:4px;flex-wrap:wrap;margin-top:8px}
.tool-tag{font-size:9px;padding:1px 6px;border-radius:3px;background:var(--s2);color:var(--dim);border:1px solid var(--s3);font-family:var(--mono)}
.ac-preview{font-size:10px;color:var(--dim);margin-top:8px;font-family:var(--mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:4px 6px;background:rgba(0,0,0,.3);border-radius:3px}

/* SERVICES */
.svc-row{display:flex;gap:10px;flex-wrap:wrap}
.svc{background:var(--s1);border:1px solid var(--s3);border-radius:6px;padding:8px 14px;display:flex;align-items:center;gap:8px;font-size:12px}
.svc-dot{width:7px;height:7px;border-radius:50%}
.svc-dot.active{background:var(--green)}
.svc-dot.inactive{background:var(--red)}
.svc-name{font-weight:500}
.svc-s{color:var(--muted);font-size:11px}

/* DETAIL PANEL */
.detail{background:var(--s1);border:1px solid var(--s3);border-radius:8px;padding:18px;display:none}
.detail.open{display:block}
.detail-h{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
.detail-t{font-size:14px;font-weight:600}
.detail-close{background:transparent;border:1px solid var(--s3);color:var(--muted);padding:3px 10px;border-radius:4px;cursor:pointer;font-size:11px;font-family:var(--sans)}
.detail-close:hover{color:var(--text);border-color:var(--text)}
.detail-body{font-size:12px;line-height:1.7;color:#c6c6c6}
.detail-body h1,.detail-body h2,.detail-body h3{font-size:13px;font-weight:600;color:#a6c8ff;margin:10px 0 5px;border-bottom:1px solid var(--s3);padding-bottom:4px}
.detail-body table{width:100%;border-collapse:collapse;font-size:11px;margin:8px 0}
.detail-body th{padding:5px 8px;border-bottom:1px solid var(--s3);color:var(--muted);text-align:left}
.detail-body td{padding:5px 8px;border-bottom:1px solid var(--s2)}
.detail-body p{margin:5px 0}
.detail-body ul,.detail-body ol{padding-left:18px;margin:5px 0}
.detail-body code{background:var(--s2);padding:1px 5px;border-radius:3px;font-family:var(--mono);font-size:11px;color:#a8c7fa}
.detail-body strong{color:var(--text)}

/* RIGHT COLUMN — CHAT */
.chat-col{display:flex;flex-direction:column;gap:16px;position:sticky;top:68px;height:calc(100vh - 88px)}
.chat-card{background:var(--s1);border:1px solid #1e3a5f;border-radius:8px;display:flex;flex-direction:column;flex:1;overflow:hidden;position:relative}
.chat-top-bar{height:2px;background:linear-gradient(90deg,var(--ibm) 0%,#a6c8ff 50%,var(--ibm) 100%);background-size:200%;animation:shimmer 3s linear infinite}
.chat-header{padding:14px 16px;border-bottom:1px solid var(--s3);display:flex;align-items:center;gap:10px;background:rgba(15,98,254,.04);flex-shrink:0}
.chat-av{width:32px;height:32px;border-radius:50%;background:var(--ibm);display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0}
.chat-n{font-size:13px;font-weight:600}
.chat-s{font-size:11px;color:var(--muted)}
.chat-thinking{margin-left:auto;display:none;align-items:center;gap:4px;font-size:11px;color:var(--blue)}
.chat-thinking.show{display:flex}
.td{width:5px;height:5px;border-radius:50%;background:var(--blue);animation:td .8s infinite}
.td:nth-child(2){animation-delay:.15s}.td:nth-child(3){animation-delay:.3s}
@keyframes td{0%,80%,100%{opacity:.2}40%{opacity:1}}
.chat-sugs{padding:8px 12px 4px;display:flex;flex-wrap:wrap;gap:6px;flex-shrink:0}
.sug{font-size:11px;padding:4px 10px;border-radius:100px;background:rgba(69,137,255,.08);border:1px solid rgba(69,137,255,.25);color:#78a9ff;cursor:pointer;transition:all .15s;font-family:var(--sans)}
.sug:hover{background:rgba(69,137,255,.18);border-color:var(--blue)}
.chat-msgs{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:10px}
.msg{display:flex;flex-direction:column;max-width:90%}
.msg.user{align-self:flex-end;align-items:flex-end}
.msg.ceo{align-self:flex-start;align-items:flex-start}
.msg.system{align-self:center;align-items:center;max-width:100%}
.bubble{padding:9px 13px;border-radius:10px;font-size:13px;line-height:1.55}
.msg.user .bubble{background:var(--ibm);color:#fff;border-radius:10px 10px 2px 10px}
.msg.ceo .bubble{background:var(--s2);border:1px solid var(--s3);border-radius:10px 10px 10px 2px;max-width:100%}
.msg.ceo .bubble h1,.msg.ceo .bubble h2,.msg.ceo .bubble h3{font-size:12px;font-weight:600;color:#a6c8ff;margin:6px 0 3px}
.msg.ceo .bubble p{margin:3px 0;color:#c6c6c6}
.msg.ceo .bubble ul,.msg.ceo .bubble ol{padding-left:14px;margin:4px 0}
.msg.ceo .bubble li{margin:2px 0;color:#c6c6c6}
.msg.ceo .bubble strong{color:var(--text)}
.msg.ceo .bubble code{background:rgba(0,0,0,.4);padding:1px 5px;border-radius:3px;font-family:var(--mono);font-size:11px;color:#a8c7fa}
.msg.ceo .bubble table{width:100%;border-collapse:collapse;font-size:11px;margin:5px 0}
.msg.ceo .bubble th{padding:3px 7px;border-bottom:1px solid var(--s3);color:var(--muted);text-align:left}
.msg.ceo .bubble td{padding:3px 7px;border-bottom:1px solid var(--s2);color:#c6c6c6}
.msg-t{font-size:10px;color:var(--dim);margin-top:3px;padding:0 4px}
/* delegation bubble */
.del-bubble{background:rgba(10,19,40,.6)!important;border:1px solid #1e3a5f!important;padding:10px 12px!important;min-width:200px}
.del-flow{display:flex;align-items:center;gap:6px;margin-bottom:6px;flex-wrap:wrap}
.del-node{font-size:11px;padding:2px 8px;border-radius:100px;border:1px solid;font-weight:600;transition:all .3s}
.del-arr{color:var(--blue);font-size:13px;font-weight:700}
.del-status{font-size:10px;color:var(--dim);font-family:var(--mono)}
.del-status.live{color:var(--blue);animation:blink 1.2s infinite}
.del-retry{font-size:10px;color:var(--yellow);font-family:var(--mono);margin-top:4px}
/* input */
.chat-inp-wrap{padding:10px;border-top:1px solid var(--s3);display:flex;gap:8px;flex-shrink:0}
.chat-inp{flex:1;background:rgba(0,0,0,.4);border:1px solid var(--s3);color:var(--text);font-family:var(--sans);font-size:13px;padding:8px 12px;border-radius:6px;outline:none;resize:none;height:38px;min-height:38px;overflow:hidden}
.chat-inp:focus{border-color:var(--blue)}
.chat-inp::placeholder{color:var(--dim)}
.chat-send{background:var(--ibm);border:none;color:#fff;width:38px;height:38px;border-radius:6px;cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:background .15s}
.chat-send:hover:not(:disabled){background:#0353e9}
.chat-send:disabled{opacity:.4;cursor:not-allowed}

::-webkit-scrollbar{width:4px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--s3);border-radius:2px}
</style>
</head>
<body>

<div class="topbar">
  <div class="t-logo">
    <svg width="20" height="20" viewBox="0 0 32 32"><rect width="32" height="32" fill="#0F62FE"/><text x="4" y="22" font-family="IBM Plex Sans,sans-serif" font-weight="700" font-size="13" fill="white">IBM</text></svg>
    <div class="t-div"></div>
    <strong>Bob</strong>&nbsp;Agent HQ
  </div>
  <nav class="t-nav">
    <a class="t-link" href="/docs" target="_blank">API Docs</a>
    <a class="t-link" href="http://20.89.63.64:44290" target="_blank">Terminal</a>
    <a class="t-link" href="/ui" target="_blank">Harness UI</a>
    <div class="t-status"><div class="t-dot" id="hDot"></div><span id="hTxt">—</span></div>
    <span class="t-time" id="clock"></span>
  </nav>
</div>

<div class="page">
  <div class="left-col">

    <!-- CEO -->
    <div class="ceo-card">
      <div class="ceo-top-bar"></div>
      <div class="ceo-body">
        <div class="ceo-header">
          <div class="ceo-avatar">👔</div>
          <div class="ceo-meta">
            <div class="ceo-name">CEO Agent</div>
            <div class="ceo-desc" id="ceoDesc">Loading...</div>
          </div>
          <div style="text-align:right;flex-shrink:0">
            <div style="font-size:10px;color:var(--muted);margin-bottom:3px">Last report</div>
            <div style="font-size:11px;font-family:var(--mono);color:#a6c8ff" id="ceoLastRun">—</div>
          </div>
        </div>
        <div class="ceo-stats">
          <div class="stat"><div class="stat-n" id="sJobs">—</div><div class="stat-l">Jobs Run</div></div>
          <div class="stat"><div class="stat-n" id="sAgents">3</div><div class="stat-l">Sub-agents</div></div>
          <div class="stat"><div class="stat-n" id="sAlerts">—</div><div class="stat-l">Alerts</div></div>
          <div class="stat"><div class="stat-n" id="sSvcs">—</div><div class="stat-l">Svcs Up</div></div>
        </div>
        <div class="ceo-report" id="ceoReport" onclick="openDetail('ceo')">Loading board report…</div>
        <div style="font-size:10px;color:var(--dim);text-align:right;margin-top:6px">Click to expand full report →</div>
      </div>
    </div>

    <!-- ORCHESTRATION VISUALIZER -->
    <div class="orch-card">
      <div class="orch-header">
        <span class="orch-title">Live Orchestration</span>
        <span class="orch-live" id="orchLive">● Idle</span>
      </div>
      <div class="orch-body" id="orchBody">
        <div class="flow-idle">No active task — ask the CEO agent something to see delegation in action</div>
      </div>
      <div class="flow-status-bar" id="flowStatus"></div>
    </div>

    <!-- AGENT GRID -->
    <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1.4px;color:var(--muted);margin-bottom:2px">Sub-agents</div>
    <div class="agent-grid" id="agentGrid">
      <div class="agent-card" style="height:160px;background:var(--s1)"></div>
      <div class="agent-card" style="height:160px;background:var(--s1)"></div>
      <div class="agent-card" style="height:160px;background:var(--s1)"></div>
    </div>

    <!-- DETAIL -->
    <div class="detail" id="detail">
      <div class="detail-h">
        <span class="detail-t" id="detailT">Report</span>
        <button class="detail-close" onclick="closeDetail()">✕ Close</button>
      </div>
      <div class="detail-body" id="detailB"></div>
    </div>

    <!-- SERVICES -->
    <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1.4px;color:var(--muted);margin-bottom:6px">Infrastructure</div>
    <div class="svc-row" id="svcRow"></div>

  </div>

  <!-- RIGHT — CHAT -->
  <div class="chat-col">
    <div class="chat-card">
      <div class="chat-top-bar"></div>
      <div class="chat-header">
        <div class="chat-av">👔</div>
        <div><div class="chat-n">CEO Agent</div><div class="chat-s" id="chatSubtitle">Ask anything — I'll delegate to the right agent</div></div>
        <div class="chat-thinking" id="cThink"><div class="td"></div><div class="td"></div><div class="td"></div><span style="margin-left:4px;font-size:11px">Working…</span></div>
      </div>
      <div class="chat-sugs" id="chatSugs"></div>
      <div class="chat-msgs" id="chatMsgs"></div>
      <div class="chat-inp-wrap">
        <textarea id="chatInp" class="chat-inp" placeholder="Ask the CEO…" rows="1"></textarea>
        <button id="chatBtn" class="chat-send" onclick="doChat()">↑</button>
      </div>
    </div>
  </div>
</div>

<script>
const API_BASE = '';
const AGENTS = {
  intel:{icon:'🔍',name:'Intel Agent',color:'#3fb950',cls:'intel'},
  ops:  {icon:'⚙️',name:'Ops Agent',  color:'#d29922',cls:'ops'},
  dev:  {icon:'💻',name:'Dev Agent',  color:'#bc8cff',cls:'dev'},
  ceo:  {icon:'👔',name:'CEO Agent',  color:'#4589ff',cls:'ceo'},
};
const SUGS = [
  "What's our competitive position vs GitHub Copilot?",
  "Check server health right now",
  "What did the dev team ship?",
  "Search latest AI coding tools news",
  "Give me the full board report",
  "Are there any infrastructure risks?",
];

// ── Clock ──
setInterval(()=>{document.getElementById('clock').textContent=new Date().toUTCString().replace(' GMT','');},1000);

// ── Health ──
async function checkHealth(){
  const dot=document.getElementById('hDot'),txt=document.getElementById('hTxt');
  try{
    const d=await fetch(API_BASE+'/api/state').then(r=>r.json());
    dot.style.background=d.meta.services.bob==='active'?'var(--green)':'var(--red)';
    txt.textContent='Bob active · '+d.meta.total_jobs+' jobs';
  }catch{dot.style.background='var(--red)';txt.textContent='offline';}
}
checkHealth();setInterval(checkHealth,20000);

// ── State ──
let appState=null;
async function fetchState(){
  try{
    const d=await fetch(API_BASE+'/api/state').then(r=>r.json());
    appState=d;
    renderState(d);
  }catch(e){console.error(e);}
}
fetchState();setInterval(fetchState,12000);

function renderState(d){
  // CEO
  document.getElementById('ceoLastRun').textContent=d.ceo.last_run;
  document.getElementById('ceoReport').innerHTML=d.ceo.report_html||'<em style="color:var(--muted)">No board report yet</em>';
  if(d.ceo.description){
    document.getElementById('ceoDesc').textContent=d.ceo.description;
  }
  document.getElementById('sJobs').textContent=d.meta.total_jobs;
  document.getElementById('sAlerts').textContent=d.meta.alerts;
  const svcsUp=Object.values(d.meta.services).filter(s=>s==='active').length;
  document.getElementById('sSvcs').textContent=svcsUp+'/'+Object.keys(d.meta.services).length;
  // Agents
  renderAgents(d.agents);
  // Services
  document.getElementById('svcRow').innerHTML=Object.entries(d.meta.services).map(([n,s])=>
    '<div class="svc"><div class="svc-dot '+(s==='active'?'active':'inactive')+'"></div><span class="svc-name">'+n+'</span><span class="svc-s">'+s+'</span></div>'
  ).join('');
}

function renderAgents(agents){
  document.getElementById('agentGrid').innerHTML=agents.map(a=>
    '<div class="agent-card" id="ac-'+a.id+'" onclick="selAgent(\''+a.id+'\')">'+
      '<div class="ac-top">'+
        '<div class="ac-icon" style="background:'+a.color+'18;border-color:'+a.color+'44">'+a.icon+'</div>'+
        '<div><div class="ac-name">'+a.name+'</div><div class="ac-role">'+esc(a.specialty)+'</div></div>'+
      '</div>'+
      '<div class="ac-body">'+
        '<div class="ac-row"><span class="ac-label">Status</span><span class="pill '+(a.status==='active'?'pill-green':'pill-idle')+'">'+(a.status==='active'?'● Active':'○ Idle')+'</span></div>'+
        '<div class="ac-row"><span class="ac-label">Last run</span><span style="font-size:10px;font-family:var(--mono);color:var(--dim)">'+a.last_run+'</span></div>'+
        '<div class="ac-tools">'+a.tools.map(t=>'<span class="tool-tag">'+t+'</span>').join('')+'</div>'+
        '<div class="ac-preview">'+esc(a.preview.split('\n')[0])+'</div>'+
      '</div>'+
    '</div>'
  ).join('');
}

// ── Detail ──
async function selAgent(id){
  try{
    const d=await fetch(API_BASE+'/api/report/'+id).then(r=>r.json());
    document.getElementById('detail').className='detail open';
    const a=appState&&appState.agents.find(x=>x.id===id);
    document.getElementById('detailT').textContent=(a?a.icon+' '+a.name:'Report')+' — '+d.last_updated;
    document.getElementById('detailB').innerHTML=d.html||'<em>No content</em>';
    document.getElementById('detail').scrollIntoView({behavior:'smooth',block:'nearest'});
  }catch(e){console.error(e);}
}
async function openDetail(id){
  try{
    const d=await fetch(API_BASE+'/api/report/'+id).then(r=>r.json());
    document.getElementById('detail').className='detail open';
    document.getElementById('detailT').textContent=(AGENTS[id]?AGENTS[id].icon+' '+AGENTS[id].name:id)+' — '+d.last_updated;
    document.getElementById('detailB').innerHTML=d.html||'<em>No content</em>';
  }catch(e){}
}
function closeDetail(){document.getElementById('detail').className='detail';}

// ── Orchestration Visualizer ──
let orchNodes={};
function orchStart(activeAgents){
  const body=document.getElementById('orchBody');
  const orch=document.getElementById('orchLive');
  orch.textContent='● Active';
  orch.style.color='var(--blue)';
  orch.style.animation='blink 1s infinite';

  const all=['ceo',...activeAgents];
  orchNodes={};
  let html='<div class="flow-active">';
  all.forEach(function(id,i){
    const a=AGENTS[id]||{icon:'🤖',name:id,color:'#888',cls:id};
    html+='<div class="flow-node" id="fn-'+id+'" style="--color:'+a.color+'">';
    html+='<span class="flow-icon">'+a.icon+'</span>';
    html+='<span class="flow-name">'+a.name.split(' ')[0]+'</span>';
    html+='<span class="flow-task" id="ft-'+id+'">standby</span>';
    html+='</div>';
    if(i<all.length-1) html+='<div class="flow-arrow" id="fa-'+id+'">→</div>';
  });
  html+='</div>';
  body.innerHTML=html;
  orchNodes=Object.fromEntries(all.map(function(id){return [id,true];}));
}
function orchActivate(agentId, taskText){
  const node=document.getElementById('fn-'+agentId);
  if(!node)return;
  Object.keys(orchNodes).forEach(function(id){
    const n=document.getElementById('fn-'+id);
    if(n) n.className='flow-node';
  });
  node.className='flow-node active '+agentId;
  const t=document.getElementById('ft-'+agentId);
  if(t) t.textContent=(taskText||'working...').slice(0,18);
  const keys=Object.keys(orchNodes);
  const prevId=keys[keys.indexOf(agentId)-1];
  if(prevId){
    const arr=document.getElementById('fa-'+prevId);
    if(arr) arr.className='flow-arrow active';
  }
}
function orchRetrying(agentId){
  const node=document.getElementById('fn-'+agentId);
  if(node) node.className='flow-node retrying '+agentId;
  const t=document.getElementById('ft-'+agentId);
  if(t) t.textContent='retrying…';
}
function orchDone(agentId){
  const node=document.getElementById('fn-'+agentId);
  if(node) node.className='flow-node done';
  const arr=document.getElementById('fa-'+agentId);
  if(arr){arr.className='flow-arrow';arr.style.color='var(--green)';}
}
function orchReset(){
  document.getElementById('orchBody').innerHTML='<div class="flow-idle">No active task — ask the CEO agent something to see delegation in action</div>';
  const orch=document.getElementById('orchLive');
  orch.textContent='● Idle';orch.style.color='var(--muted)';orch.style.animation='';
  document.getElementById('flowStatus').textContent='';
}
function orchStatus(text){
  const el=document.getElementById('flowStatus');
  el.innerHTML='<span class="blink">▶</span> '+esc(text);
}
function glowAgent(id, on){
  const card=document.getElementById('ac-'+id);
  if(!card)return;
  const a=AGENTS[id]||{color:'#888'};
  if(on){card.style.setProperty('--glow-color',a.color+'66');card.classList.add('active-job');}
  else{card.classList.remove('active-job');}
}

// ── Suggestions ──
function initSugs(){
  document.getElementById('chatSugs').innerHTML=SUGS.map(function(s){
    return '<button class="sug" onclick="fillChat(\''+s.replace(/'/g,"\\'")+'\')">' + s + '</button>';
  }).join('');
  appendMsg('ceo',"Good morning. I'm your CEO Agent — I coordinate Intel, Ops, and Dev agents to answer anything. Try a suggestion or ask your own question.",false);
  document.getElementById('chatInp').addEventListener('keydown',function(e){
    if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();doChat();}
  });
}
function fillChat(t){document.getElementById('chatInp').value=t;doChat();}

// ── Chat ──
let delegationBubble=null;

async function doChat(){
  const inp=document.getElementById('chatInp');
  const msg=inp.value.trim();if(!msg)return;
  inp.value='';
  appendMsg('user',msg,false);

  const btn=document.getElementById('chatBtn');
  const think=document.getElementById('cThink');
  btn.disabled=true;think.classList.add('show');
  delegationBubble=null;

  try{
    const resp=await fetch(API_BASE+'/api/chat',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:msg})
    });

    const reader=resp.body.getReader();
    const dec=new TextDecoder();
    let buf='';
    const seenAgents=[];

    while(true){
      const{done,value}=await reader.read();if(done)break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split('\n');buf=lines.pop();
      for(const ln of lines){
        if(!ln.startsWith('data: '))continue;
        try{
          const ev=JSON.parse(ln.slice(6));
          handleEvent(ev,seenAgents);
        }catch(e){}
      }
    }
  }catch(e){
    removeDelegationBubble();
    appendMsg('ceo','⚠️ '+e.message,false);
    orchReset();
  }

  btn.disabled=false;think.classList.remove('show');
}

function handleEvent(ev, seenAgents){
  if(ev.type==='thinking'){
    const ag=ev.content.agent;
    updateDelegationBubble('thinking',ev.content.text);
    orchStatus(ev.content.text);
    if(ag) orchActivate(ag,'thinking...');

  }else if(ev.type==='delegate'){
    const to=ev.content.to;
    const from=ev.content.from;
    const reason=ev.content.reason;
    if(!seenAgents.includes(to)) seenAgents.push(to);
    if(seenAgents.length===1||!delegationBubble){
      const active=seenAgents.filter(function(a){return a!=='ceo';});
      orchStart(active.length>0?active:['ceo']);
      delegationBubble=createDelegationBubble(seenAgents);
    }
    orchActivate(to,reason||'working...');
    if(to!=='ceo') glowAgent(to,true);
    updateDelegationBubble('delegate',{from:from,to:to,reason:reason});
    orchStatus('Delegating to '+(AGENTS[to]?AGENTS[to].name:to)+'...');

  }else if(ev.type==='agent_start'){
    const agent=ev.content.agent;
    const task=ev.content.task;
    orchActivate(agent,task);
    orchStatus((AGENTS[agent]?AGENTS[agent].name:agent)+': '+task);
    updateDelegationBubble('status',(AGENTS[agent]?AGENTS[agent].icon:'')+' '+task);

  }else if(ev.type==='agent_progress'){
    const agent=ev.content.agent;
    const elapsed=ev.content.elapsed;
    const retrying=ev.content.retrying;
    orchStatus((AGENTS[agent]?AGENTS[agent].name:agent)+' working… '+elapsed+'s'+(retrying?' [retrying]':''));
    updateDelegationBubble('status',(AGENTS[agent]?AGENTS[agent].name:agent)+' working… '+elapsed+'s');

  }else if(ev.type==='loop_retry'){
    const agent=ev.content.agent;
    const attempt=ev.content.attempt;
    const reason=ev.content.reason;
    orchRetrying(agent);
    orchStatus('⟳ Loop-engineering: pushing error back to '+(AGENTS[agent]?AGENTS[agent].name:agent)+' for self-resolution…');
    updateDelegationBubble('retry','⟳ Retry '+attempt+': '+reason);

  }else if(ev.type==='agent_done'){
    const agent=ev.content.agent;
    orchDone(agent);
    glowAgent(agent,false);
    updateDelegationBubble('done',agent);
    orchStatus((AGENTS[agent]?AGENTS[agent].name:agent)+' ✓ done');

  }else if(ev.type==='reply'){
    removeDelegationBubble();
    orchDone('ceo');
    orchStatus('✓ Complete');
    setTimeout(orchReset,4000);
    appendMsg('ceo',ev.content,true);
    seenAgents.forEach(function(a){glowAgent(a,false);});
    fetchState();

  }else if(ev.type==='error'){
    removeDelegationBubble();
    orchReset();
    appendMsg('ceo','⚠️ '+ev.content,false);
  }
}

function createDelegationBubble(agents){
  const wrap=document.getElementById('chatMsgs');
  const div=document.createElement('div');
  div.className='msg ceo';
  div.id='delBubble';
  div.innerHTML='<div class="bubble del-bubble"><div class="del-flow" id="delFlow"></div><div class="del-status live" id="delStatus">Routing request…</div><div class="del-retry" id="delRetry"></div></div>';
  wrap.appendChild(div);wrap.scrollTop=9e9;
  return div;
}
function updateDelegationBubble(type, data){
  const flowEl=document.getElementById('delFlow');
  const statusEl=document.getElementById('delStatus');
  const retryEl=document.getElementById('delRetry');
  if(!flowEl)return;
  if(type==='delegate'){
    const to=data.to;
    const aTo=AGENTS[to]||{icon:'🤖',name:to,color:'#888'};
    const exists=flowEl.querySelector('[data-id="'+to+'"]');
    if(!exists){
      if(flowEl.children.length>0){
        const arr=document.createElement('span');
        arr.className='del-arr';arr.textContent='→';
        flowEl.appendChild(arr);
      }
      const node=document.createElement('span');
      node.className='del-node';node.dataset.id=to;
      node.style.borderColor=aTo.color;node.style.color=aTo.color;
      node.textContent=aTo.icon+' '+aTo.name.split(' ')[0];
      flowEl.appendChild(node);
    }
  }else if(type==='status'||type==='thinking'){
    if(statusEl){statusEl.textContent=typeof data==='string'?data:'';}
  }else if(type==='retry'){
    if(retryEl){retryEl.textContent=typeof data==='string'?data:'';}
  }else if(type==='done'){
    const node=flowEl.querySelector('[data-id="'+data+'"]');
    if(node){node.style.borderColor='var(--green)';node.style.color='var(--green)';}
  }
  const wrap=document.getElementById('chatMsgs');if(wrap)wrap.scrollTop=9e9;
}
function removeDelegationBubble(){
  const el=document.getElementById('delBubble');
  if(el&&el.parentNode)el.parentNode.removeChild(el);
  delegationBubble=null;
}

// ── Messages ──
function appendMsg(role,text,isMd){
  const wrap=document.getElementById('chatMsgs');
  if(!wrap)return;
  const t=new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
  const div=document.createElement('div');
  div.className='msg '+role;
  div.innerHTML='<div class="bubble">'+(isMd?md(text):'<p style="white-space:pre-wrap">'+esc(text)+'</p>')+'</div><div class="msg-t">'+t+'</div>';
  wrap.appendChild(div);wrap.scrollTop=9e9;
}
function md(t){
  return esc(t)
    .replace(/```[\s\S]*?```/g,function(m){return '<pre style="background:rgba(0,0,0,.4);padding:8px;border-radius:4px;overflow-x:auto;margin:4px 0;font-family:var(--mono);font-size:11px;color:#a8c7fa">'+m.slice(3,-3).replace(/^[a-z]+\n/,'')+'</pre>';})
    .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
    .replace(/^### (.+)$/gm,'<h3 style="font-size:12px;font-weight:600;color:#a6c8ff;margin:8px 0 4px">$1</h3>')
    .replace(/^## (.+)$/gm,'<h2 style="font-size:13px;font-weight:600;color:#a6c8ff;margin:10px 0 5px">$1</h2>')
    .replace(/^# (.+)$/gm,'<h1 style="font-size:14px;font-weight:700;color:#a6c8ff;margin:10px 0 5px">$1</h1>')
    .replace(/^- (.+)$/gm,'<li>$1</li>')
    .replace(/(<li>.*<\/li>\n?)+/g,function(m){return '<ul style="padding-left:14px;margin:4px 0">'+m+'</ul>';})
    .replace(/\n{2,}/g,'<br><br>')
    .replace(/\n/g,'<br>');
}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

// ── Live event bus (/api/events SSE) ──
// Receives replies from ALL sources (other browser tabs, scheduled jobs, etc.)
// and shows them as a system bubble with a 📡 badge so you know it's background.
function initEventBus(){
  var es=new EventSource(API_BASE+'/api/events');
  es.onmessage=function(e){
    try{
      var ev=JSON.parse(e.data);
      if(ev.type==='reply'){
        // Only show if this tab didn't originate it (no active thinking indicator)
        var think=document.getElementById('cThink');
        if(think&&!think.classList.contains('show')){
          var c=ev.content;
          var label='📡 <em style="font-size:10px;color:var(--cyan)">Live update '+esc(c.ts||'')+'</em>';
          if(c.agents&&c.agents.length){
            label+=' <em style="font-size:10px;color:var(--muted)">via '+esc(c.agents.join(', '))+'</em>';
          }
          var wrap=document.getElementById('chatMsgs');
          if(wrap){
            var div=document.createElement('div');
            div.className='msg ceo';
            div.innerHTML='<div class="bubble" style="border-color:var(--cyan)44;background:rgba(118,227,234,.04)">'+
              label+'<br><br>'+md(c.reply||'')+'</div>';
            wrap.appendChild(div);wrap.scrollTop=9e9;
          }
          fetchState();
        }
      }
    }catch(e){}
  };
  es.onerror=function(){
    // Reconnect after 5s on error
    es.close();
    setTimeout(initEventBus,5000);
  };
}

// ── Init ──
if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',function(){initSugs();initEventBus();});}
else{initSugs();initEventBus();}
</script>
</body>
</html>"""

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=44283, debug=False)
