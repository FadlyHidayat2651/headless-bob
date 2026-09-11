# Headless Bob — Full VM Redeploy Guide

> **For Bob agents:** Read this file top-to-bottom and execute every code block in sequence.
> This guide rebuilds the entire Headless Bob stack from a fresh RHEL/Ubuntu VM.
> All source code is pulled from GitHub. Secrets must be supplied before starting.

---

## 0. Prerequisites

| Requirement | Minimum | Notes |
|---|---|---|
| OS | RHEL 9+ / Ubuntu 22.04+ | Guide uses RHEL 10.2 syntax |
| vCPU | 4 | 16 recommended |
| RAM | 8 GB | 64 GB on production VM |
| Disk | 50 GB `/data` mount | 1 TB on production VM |
| Public IP | Required | For external access to dashboards |
| User | `itzuser` (sudo) | Replace with your username throughout |

### Secrets needed before starting

Collect these values — they are referenced as `<PLACEHOLDER>` below:

```
BOB_API_KEY=<your IBM Bob API key>
FIRECRAWL_API_KEY=<your Firecrawl API key>
SLACK_WEBHOOK=<your Slack workflow trigger URL>
VM_PUBLIC_IP=<new VM public IP address>
```

---

## 1. System Bootstrap

```bash
# ── 1.1 Create /data mount (if secondary disk available) ──────────────────────
# Skip if /data already exists
lsblk                              # identify the secondary disk (e.g. /dev/sdb)
sudo mkfs.xfs /dev/sdb
sudo mkdir -p /data
echo '/dev/sdb /data xfs defaults 0 0' | sudo tee -a /etc/fstab
sudo mount -a
df -h /data                        # confirm mount

# ── 1.2 Install system packages ───────────────────────────────────────────────
# RHEL / Rocky / Alma
sudo dnf install -y git curl wget tmux python3 python3-pip nodejs npm nginx \
  policycoreutils-python-utils        # for semanage

# Ubuntu alternative:
# sudo apt-get update && sudo apt-get install -y git curl wget tmux python3 \
#   python3-pip nodejs npm nginx

# ── 1.3 Install Node.js 22 (if dnf version is older) ─────────────────────────
node --version | grep -q 'v22' || (
  curl -fsSL https://rpm.nodesource.com/setup_22.x | sudo bash -
  sudo dnf install -y nodejs
)

# ── 1.4 Create working directories ───────────────────────────────────────────
sudo mkdir -p /data/{company,company-hq,bob-harness/api,bob-terminal,my-dashboard,vault,bob-home/db,bob-home/logs,sales-intel/briefs}
sudo chown -R itzuser:itzuser /data
```

---

## 2. Install IBM Bob

```bash
# ── 2.1 Install Bob globally ─────────────────────────────────────────────────
sudo npm install -g @ibm/bob

# Verify
bob --version   # expect: 2.0.x

# ── 2.2 Set API key permanently ──────────────────────────────────────────────
echo "export BOB_API_KEY=<BOB_API_KEY>" >> ~/.bashrc
source ~/.bashrc

# ── 2.3 Accept license ───────────────────────────────────────────────────────
bob --accept-license --version

# ── 2.4 Create Bob settings directory ────────────────────────────────────────
mkdir -p ~/.bob/settings
```

---

## 3. Clone Source Code

```bash
# ── 3.1 Clone the headless-bob repo ─────────────────────────────────────────
cd /data
git clone https://github.com/FadlyHidayat2651/headless-bob.git repo
ls /data/repo/vm-source/
```

---

## 4. Deploy Bob Harness (FastAPI — port 44285)

```bash
# ── 4.1 Copy server files ────────────────────────────────────────────────────
cp -r /data/repo/vm-source/bob-harness/. /data/bob-harness/api/

# ── 4.2 Install Python dependencies ─────────────────────────────────────────
pip3 install --target=/data/pylibs \
  fastapi uvicorn pydantic PyYAML python-dotenv flask markdown requests

# ── 4.3 Write .env file ──────────────────────────────────────────────────────
cat > /data/bob-harness/.env << 'EOF'
BOBSHELL_API_KEY=<BOB_API_KEY>
BOB_MODE=unrestricted-dev
BOB_WORKDIR=/data
BOB_BIN=/usr/bin/bob
BOB_MAX_JOBS=200
BOB_SCHEDULES_FILE=/data/bob-harness/schedules.json
BOB_CRON_LOG=/data/bob-harness/cron.log
EOF

# ── 4.4 Write systemd unit ───────────────────────────────────────────────────
sudo tee /etc/systemd/system/bob-harness.service << 'EOF'
[Unit]
Description=Bob Harness - FastAPI REST wrapper for IBM Bob
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=itzuser
WorkingDirectory=/data/bob-harness/api
EnvironmentFile=/data/bob-harness/.env
Environment="HOME=/home/itzuser"
Environment="PATH=/home/itzuser/.local/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/usr/bin/python3 -m uvicorn server:app --host 0.0.0.0 --port 44285
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# ── 4.5 Enable and start ─────────────────────────────────────────────────────
sudo systemctl daemon-reload
sudo systemctl enable --now bob-harness
sudo systemctl is-active bob-harness    # expect: active

# ── 4.6 Smoke test ───────────────────────────────────────────────────────────
sleep 3
curl -s http://localhost:44285/health | python3 -m json.tool
```

---

## 5. Deploy Company HQ Dashboard (Flask — port 44283)

```bash
# ── 5.1 Copy app ─────────────────────────────────────────────────────────────
cp /data/repo/vm-source/company-hq/app.py /data/company-hq/app.py

# ── 5.2 Update Slack webhook URL in app.py ───────────────────────────────────
sed -i "s|https://hooks.slack.com/.*|<SLACK_WEBHOOK>|" /data/company-hq/app.py

# ── 5.3 Write systemd unit ───────────────────────────────────────────────────
sudo tee /etc/systemd/system/bob-hq.service << 'EOF'
[Unit]
Description=Bob Company HQ Dashboard
After=network.target bob-harness.service

[Service]
Type=simple
User=itzuser
WorkingDirectory=/data/company-hq
ExecStart=/usr/bin/python3 app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# ── 5.4 Enable and start ─────────────────────────────────────────────────────
sudo systemctl daemon-reload
sudo systemctl enable --now bob-hq
sudo systemctl is-active bob-hq        # expect: active

# ── 5.5 Smoke test ───────────────────────────────────────────────────────────
sleep 3
curl -s -o /dev/null -w '%{http_code}' http://localhost:44283/
```

---

## 6. Deploy Bob Terminal (Node.js Express — port 44290)

```bash
# ── 6.1 Copy source ──────────────────────────────────────────────────────────
cp -r /data/repo/vm-source/bob-terminal/. /data/bob-terminal/ 2>/dev/null || \
  cp /data/repo/vm-source/bob-terminal/server.js /data/bob-terminal/server.js

# ── 6.2 Install Node dependencies ───────────────────────────────────────────
cd /data/bob-terminal
npm install

# ── 6.3 Write systemd unit ───────────────────────────────────────────────────
sudo tee /etc/systemd/system/bob-terminal.service << 'EOF'
[Unit]
Description=Bob Terminal - Bloomberg-style Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=itzuser
WorkingDirectory=/data/bob-terminal
Environment="BOB_API_KEY=<BOB_API_KEY>"
Environment="HOME=/home/itzuser"
Environment="PATH=/usr/local/bin:/usr/bin:/bin"
ExecStart=/usr/bin/node /data/bob-terminal/server.js
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# ── 6.4 Enable and start ─────────────────────────────────────────────────────
sudo systemctl daemon-reload
sudo systemctl enable --now bob-terminal
sudo systemctl is-active bob-terminal  # expect: active

# ── 6.5 Smoke test ───────────────────────────────────────────────────────────
sleep 3
curl -s -o /dev/null -w '%{http_code}' http://localhost:44290/
```

---

## 7. Deploy bob.service (Bob Daemon in tmux)

```bash
# ── 7.1 Write start script ───────────────────────────────────────────────────
cat > ~/start-bob.sh << 'SCRIPT'
#!/bin/bash
SESSION="bob"
API_KEY="<BOB_API_KEY>"

if tmux has-session -t $SESSION 2>/dev/null; then
  echo "Bob already running. Attach: tmux attach -t $SESSION"
  exit 0
fi

tmux new-session -d -s $SESSION -x 220 -y 50 \
  "export BOB_API_KEY=$API_KEY; bob chat --accept-license 2>&1 | tee /tmp/bob.log"

sleep 2
tmux has-session -t $SESSION && echo "Bob started OK" || echo "FAILED — check /tmp/bob.log"
SCRIPT

chmod +x ~/start-bob.sh

# ── 7.2 Write systemd unit ───────────────────────────────────────────────────
sudo tee /etc/systemd/system/bob.service << 'EOF'
[Unit]
Description=IBM Bob AI Assistant (tmux)
After=network-online.target
Wants=network-online.target

[Service]
Type=forking
User=itzuser
Environment="BOB_API_KEY=<BOB_API_KEY>"
Environment="HOME=/home/itzuser"
Environment="PATH=/usr/local/bin:/usr/bin:/bin"
ExecStart=/bin/bash /home/itzuser/start-bob.sh
ExecStop=/usr/bin/tmux kill-session -t bob
RemainAfterExit=yes
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# ── 7.3 Enable and start ─────────────────────────────────────────────────────
sudo systemctl daemon-reload
sudo systemctl enable --now bob
sudo systemctl is-active bob            # expect: active
```

---

## 8. Configure Bob Custom Modes

```bash
# ── 8.1 Copy custom_modes.yaml ───────────────────────────────────────────────
# Option A: pull from vault (if vault was cloned)
cp /data/vault/bob-config/custom_modes.yaml ~/.bob/settings/custom_modes.yaml

# Option B: pull from the headless-bob repo
# cp /data/repo/vm-source/custom_modes.yaml ~/.bob/settings/custom_modes.yaml

# ── 8.2 Verify all 11 modes loaded ──────────────────────────────────────────
python3 -c "
import yaml
d = yaml.safe_load(open('/home/itzuser/.bob/settings/custom_modes.yaml').read())
slugs = [m['slug'] for m in d['customModes']]
print('Modes:', len(slugs), slugs)
"
# Expected: 11 modes including intel-agent, ops-agent, dev-agent, ceo-agent, data-analyst
```

### Custom mode slugs (11 total)

| Slug | Purpose |
|---|---|
| `ceo-agent` | Orchestrator — delegates to sub-agents, board reports |
| `intel-agent` | Live web research via Firecrawl MCP |
| `ops-agent` | Infrastructure health, self-healing |
| `dev-agent` | Code, build, deploy |
| `data-analyst` | SQL, Python pandas, statistical analysis, vault reports |
| `unrestricted-dev` | Headless autonomous execution |
| `terminal-ops` | Bloomberg terminal VM management |
| `db-manager` | PostgreSQL, MongoDB, Redis management |
| `vault-manager` | Obsidian vault notes + git |
| `webapp-tester` | Playwright browser automation |
| `ticket-reader` | MongoDB ticket queue executor |

---

## 9. Configure MCP Servers

```bash
# ── 9.1 Write mcp.json ───────────────────────────────────────────────────────
mkdir -p ~/.bob/settings
cat > ~/.bob/settings/mcp.json << 'EOF'
{
  "mcpServers": {
    "obsidian-vault": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data/vault"],
      "metadata": { "description": "Obsidian vault at /data/vault" }
    },
    "firecrawl-mcp": {
      "command": "npx",
      "args": ["-y", "firecrawl-mcp"],
      "env": { "FIRECRAWL_API_KEY": "<FIRECRAWL_API_KEY>" },
      "alwaysAllow": ["firecrawl_scrape", "firecrawl_search", "firecrawl_map"]
    },
    "postgres": {
      "command": "npx",
      "args": ["-y", "@crystaldba/postgres-mcp", "--access-mode=unrestricted"],
      "env": { "DATABASE_URL": "postgresql://dbmanager:dbmanager_pass@localhost:5432/devdb" }
    },
    "mongodb": {
      "command": "npx",
      "args": ["-y", "mongodb-mcp-server@latest"],
      "env": { "MDB_MCP_CONNECTION_STRING": "mongodb://localhost:27017" }
    },
    "redis": {
      "command": "/home/itzuser/.local/bin/uvx",
      "args": ["--from", "redis-mcp-server@latest", "redis-mcp-server", "--url", "redis://localhost:6379/0"]
    }
  }
}
EOF

# Install uvx for redis MCP
pip3 install --user uv
```

---

## 10. Register Harness Schedules

```bash
# ── 10.1 Register all 4 cron schedules ───────────────────────────────────────
H="http://localhost:44285"

# Dev Daily Status — 06:00 UTC
curl -s -X POST $H/schedules -H 'Content-Type: application/json' -d '{
  "name": "Dev Daily Status",
  "cron": "0 6 * * *",
  "mode": "dev-agent",
  "prompt": "Review system services and codebase health. Check /data/bob-harness/api/server.py, /data/company-hq/app.py, and /data/bob-terminal/server.js for any issues. Write a dev status report to /data/company/dev-status.md"
}'

# Intel Daily Briefing — 07:00 UTC
curl -s -X POST $H/schedules -H 'Content-Type: application/json' -d '{
  "name": "Intel Daily Briefing",
  "cron": "0 7 * * *",
  "mode": "intel-agent",
  "prompt": "Research the latest AI coding assistant news, competitor moves (Cursor, GitHub Copilot, Tabnine, Codeium), and market trends. Write a comprehensive intel report to /data/company/intel-report.md"
}'

# CEO Daily Board Report — 08:00 UTC
curl -s -X POST $H/schedules -H 'Content-Type: application/json' -d '{
  "name": "CEO Daily Board Report",
  "cron": "0 8 * * *",
  "mode": "ceo-agent",
  "prompt": "Generate today daily board report. Delegate to intel-agent for market intelligence and ops-agent for system health. Synthesize findings into a board-ready executive summary and write it to /data/company/board-report.md"
}'

# Ops Health Check — every 6 hours
curl -s -X POST $H/schedules -H 'Content-Type: application/json' -d '{
  "name": "Ops Health Check",
  "cron": "0 */6 * * *",
  "mode": "ops-agent",
  "prompt": "Run a full system health check: disk usage, memory, CPU, all systemd services (bob.service bob-terminal.service bob-harness.service bob-hq.service), and port availability (44283, 44285, 44290). Write the report to /data/company/ops-health.md"
}'

# ── 10.2 Verify all 4 schedules registered ───────────────────────────────────
curl -s $H/schedules | python3 -c "
import sys, json
for s in json.load(sys.stdin):
    print(s['name'], '|', s['cron'], '|', s['mode'])
"
```

---

## 11. Copy Static Dashboards

```bash
# ── 11.1 Copy HTML dashboards ────────────────────────────────────────────────
cp /data/repo/vm-source/harness-docs.html /data/my-dashboard/harness-docs.html
cp /data/repo/vm-source/*.html /data/my-dashboard/ 2>/dev/null || true

# ── 11.2 Verify served correctly ─────────────────────────────────────────────
curl -s -o /dev/null -w '%{http_code}' http://localhost:44283/dashboard/harness-docs.html
# expect: 200
```

---

## 12. Configure nginx (HTTPS — optional)

> Skip this section if you don't need HTTPS. All services work on HTTP ports.
> To activate HTTPS, you also need to open ports 443/80/44286/44291 in the cloud firewall.

```bash
# ── 12.1 Install nginx ───────────────────────────────────────────────────────
sudo dnf install -y nginx   # RHEL
# sudo apt-get install -y nginx   # Ubuntu

# ── 12.2 Generate self-signed certificate ────────────────────────────────────
sudo mkdir -p /etc/nginx/ssl
sudo openssl req -x509 -nodes -days 825 -newkey rsa:2048 \
  -keyout /etc/nginx/ssl/bob.key \
  -out /etc/nginx/ssl/bob.crt \
  -subj "/C=US/ST=TX/L=Dallas/O=IBM/OU=Bob/CN=<VM_PUBLIC_IP>" \
  -addext "subjectAltName=IP:<VM_PUBLIC_IP>"

# ── 12.3 Write nginx config ───────────────────────────────────────────────────
sudo tee /etc/nginx/conf.d/bob.conf << 'NGINXEOF'
server {
    listen 443 ssl;
    server_name <VM_PUBLIC_IP>;
    ssl_certificate     /etc/nginx/ssl/bob.crt;
    ssl_certificate_key /etc/nginx/ssl/bob.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    location / {
        proxy_pass http://127.0.0.1:44283;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_read_timeout 300s;
    }
    location /api/events {
        proxy_pass http://127.0.0.1:44283/api/events;
        proxy_buffering off; proxy_cache off;
        proxy_read_timeout 3600s;
        chunked_transfer_encoding on;
    }
    location /api/ { proxy_pass http://127.0.0.1:44283; }
}
server {
    listen 44286 ssl;
    server_name <VM_PUBLIC_IP>;
    ssl_certificate /etc/nginx/ssl/bob.crt;
    ssl_certificate_key /etc/nginx/ssl/bob.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    location / { proxy_pass http://127.0.0.1:44285; proxy_read_timeout 300s; }
    location ~ ^/jobs/.*/stream {
        proxy_pass http://127.0.0.1:44285;
        proxy_buffering off; proxy_cache off; proxy_read_timeout 3600s;
    }
}
server {
    listen 44291 ssl;
    server_name <VM_PUBLIC_IP>;
    ssl_certificate /etc/nginx/ssl/bob.crt;
    ssl_certificate_key /etc/nginx/ssl/bob.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    location / { proxy_pass http://127.0.0.1:44290; proxy_read_timeout 300s; }
}
server { listen 80; server_name <VM_PUBLIC_IP>; return 301 https://$host$request_uri; }
NGINXEOF

# ── 12.4 Fix SELinux (RHEL only) ─────────────────────────────────────────────
sudo setsebool -P httpd_can_network_connect 1
sudo semanage port -a -t http_port_t -p tcp 44283 2>/dev/null || true
sudo semanage port -a -t http_port_t -p tcp 44285 2>/dev/null || true
sudo semanage port -a -t http_port_t -p tcp 44290 2>/dev/null || true

# ── 12.5 Start nginx ─────────────────────────────────────────────────────────
sudo nginx -t && sudo systemctl enable --now nginx
```

---

## 13. Open Firewall Ports

```bash
# ── RHEL / firewalld ─────────────────────────────────────────────────────────
# If firewalld is healthy:
sudo firewall-cmd --permanent --add-port=44283/tcp
sudo firewall-cmd --permanent --add-port=44285/tcp
sudo firewall-cmd --permanent --add-port=44290/tcp
sudo firewall-cmd --reload

# If firewalld is broken (common on this setup), use iptables directly:
sudo iptables -I INPUT -p tcp --dport 44283 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 44285 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 44290 -j ACCEPT

# ── Cloud firewall (NSG / Security Group) ────────────────────────────────────
# Open these inbound TCP ports in your cloud provider's firewall UI:
# 44283  — HQ Dashboard
# 44285  — Harness API
# 44290  — Bob Terminal
# 443    — HTTPS HQ (optional)
# 44286  — HTTPS Harness (optional)
# 44291  — HTTPS Terminal (optional)
```

---

## 14. Verify Everything

```bash
# ── 14.1 All services running ────────────────────────────────────────────────
systemctl is-active bob.service bob-harness.service bob-hq.service bob-terminal.service
# expect: active active active active

# ── 14.2 All ports listening ─────────────────────────────────────────────────
ss -tlnp | grep -E '44283|44285|44290'

# ── 14.3 HTTP smoke tests ────────────────────────────────────────────────────
curl -s -o /dev/null -w 'HQ        :44283 → %{http_code}\n' http://localhost:44283/
curl -s -o /dev/null -w 'Harness   :44285 → %{http_code}\n' http://localhost:44285/health
curl -s -o /dev/null -w 'Terminal  :44290 → %{http_code}\n' http://localhost:44290/
curl -s -o /dev/null -w 'Docs page :44283 → %{http_code}\n' http://localhost:44283/dashboard/harness-docs.html

# ── 14.4 Harness API test ────────────────────────────────────────────────────
JOB=$(curl -s -X POST http://localhost:44285/jobs \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Reply with one word: READY","mode":"ops-agent"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Job submitted: $JOB"

sleep 10
curl -s http://localhost:44285/jobs/$JOB \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['status'], '|', d.get('output','')[-100:])"
# expect: completed | ... READY ...

# ── 14.5 Schedule count ───────────────────────────────────────────────────────
curl -s http://localhost:44285/schedules | python3 -c "import sys,json; print('Schedules:', len(json.load(sys.stdin)))"
# expect: Schedules: 4
```

---

## 15. Quick Reference — URLs after deploy

Replace `<VM_PUBLIC_IP>` with your new VM's IP address.

| Service | URL | Description |
|---|---|---|
| HQ Dashboard | `http://<VM_PUBLIC_IP>:44283` | Main dashboard + CEO chat |
| Harness API | `http://<VM_PUBLIC_IP>:44285` | REST API for agent jobs |
| Harness UI | `http://<VM_PUBLIC_IP>:44285/ui` | Carbon Design Harness UI |
| Bob Terminal | `http://<VM_PUBLIC_IP>:44290` | Bloomberg-style dashboard |
| Harness Docs | `http://<VM_PUBLIC_IP>:44283/dashboard/harness-docs.html` | This doc set |
| Sales Dashboard | `http://<VM_PUBLIC_IP>:44283/dashboard/sales.html` | Sales KPI dashboard |

---

## 16. Troubleshooting

### Service won't start
```bash
sudo journalctl -u bob-harness -n 50 --no-pager
sudo journalctl -u bob-hq -n 50 --no-pager
sudo journalctl -u bob-terminal -n 50 --no-pager
```

### Port not reachable externally
```bash
# Check if process is listening
ss -tlnp | grep 44285
# Check iptables
sudo iptables -L INPUT -n | head -20
# Check cloud NSG — must open ports in Azure/AWS/GCP portal
```

### Bob API key error
```bash
# Test API key directly
BOB_API_KEY=<your_key> bob run "say hello" --format pretty
# If error: check key in /data/bob-harness/.env and systemd unit
```

### SELinux blocking nginx (RHEL)
```bash
sudo ausearch -c nginx | tail -5
sudo setsebool -P httpd_can_network_connect 1
sudo semanage port -m -t http_port_t -p tcp 44283
sudo nginx -s reload
```

### /home partition full (common on RHEL)
```bash
df -h /home
# Bob npm cache often fills /home — move it to /data
npm config set cache /data/npm-cache
# Or move ~/.bob to /data
mv ~/.bob /data/bob-home/.bob && ln -s /data/bob-home/.bob ~/.bob
```

---

## Appendix — Estimated Deploy Time

| Step | Time |
|---|---|
| System bootstrap + disk mount | ~5 min |
| Bob install | ~3 min |
| Clone repo + copy files | ~1 min |
| Python deps (pip install) | ~3 min |
| Node deps (npm install) | ~2 min |
| Systemd units + start | ~2 min |
| MCP config + modes | ~2 min |
| Schedules registration | ~1 min |
| nginx + SSL (optional) | ~3 min |
| Verification | ~2 min |
| **Total** | **~24 min** |

---

*Generated from live VM `20.89.63.64` — last updated 2026-09-07*
*Source: https://github.com/FadlyHidayat2651/headless-bob*
