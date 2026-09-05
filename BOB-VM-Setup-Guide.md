# BOB Terminal VM — Full Setup Guide

> **Replicate this entire setup from scratch on any RHEL/CentOS VM.**  
> Written: August 2026 | VM: Azure RHEL 10.2 | Bob: v2.0.0

---

## Table of Contents

1. [VM Specifications](#1-vm-specifications)
2. [Prerequisites](#2-prerequisites)
3. [Mount the Secondary Disk](#3-mount-the-secondary-disk)
4. [Install Node.js 22](#4-install-nodejs-22)
5. [Install IBM Bob](#5-install-ibm-bob)
6. [Configure Bob API Key](#6-configure-bob-api-key)
7. [Install Supporting Tools](#7-install-supporting-tools)
8. [Install Python + LangGraph](#8-install-python--langgraph)
9. [Configure Firecrawl MCP](#9-configure-firecrawl-mcp)
10. [Keep Bob Running with tmux + systemd](#10-keep-bob-running-with-tmux--systemd)
11. [Bob Terminal — Bloomberg Dashboard + Ops Chat](#11-bob-terminal--bloomberg-dashboard--ops-chat)
12. [Bob Ops Mode (custom_modes.yaml)](#12-bob-ops-mode-custom_modesyaml)
13. [Fix /home Disk Full Issue](#13-fix-home-disk-full-issue)
14. [Port Reference](#14-port-reference)
15. [Service Management Cheat Sheet](#15-service-management-cheat-sheet)

---

## 1. VM Specifications

| Property | Value |
|----------|-------|
| Cloud | Microsoft Azure |
| OS | Red Hat Enterprise Linux 10.2 (RHEL) |
| CPU | 16 vCPU (Intel Xeon Platinum 8573C) |
| RAM | 64 GB |
| Primary Disk | 100 GB (`/dev/sda`) |
| Secondary Disk | 1 TB (`/dev/sdb`) — mounted at `/data` |
| Public IP | `20.89.63.64` |
| Open Ports | 22 (SSH), 44283–44290 (ITZ NSG rule) |

---

## 2. Prerequisites

- SSH private key (`ssh_private_key.pem`) downloaded from ITZ portal
- Bob API Key from [bob.ibm.com](https://bob.ibm.com)
- Firecrawl API Key from [firecrawl.dev](https://firecrawl.dev)

### Connect to the VM

```bash
chmod 600 ssh_private_key.pem
ssh -i ssh_private_key.pem itzuser@20.89.63.64
```

---

## 3. Mount the Secondary Disk

The 1 TB `/dev/sdb` disk is unformatted by default. Format, mount, and persist it.

```bash
# Format
sudo mkfs.ext4 -F /dev/sdb

# Mount
sudo mkdir -p /data
sudo mount /dev/sdb /data
sudo chown itzuser:itzuser /data

# Persist across reboots
echo '/dev/sdb /data ext4 defaults 0 2' | sudo tee -a /etc/fstab

# Verify
df -h /data
```

> **Always use `/data/` for new projects** — `/home` is only 960 MB.

---

## 4. Install Node.js 22

RHEL doesn't include Node.js 22 by default. Use the NodeSource RPM repo.

```bash
# Add NodeSource repo for Node 22
curl -fsSL https://rpm.nodesource.com/setup_22.x | sudo bash -

# Install
sudo dnf install -y nodejs

# Verify
node --version   # v22.x.x
npm --version
```

---

## 5. Install IBM Bob

Bob requires Node.js 22.15+. Install globally with sudo.

```bash
curl -fsSL https://bob.ibm.com/download/bobshell.sh | sudo bash

# Verify
bob --version   # 2.0.0
```

---

## 6. Configure Bob API Key

Set the API key so Bob never needs browser-based OAuth login.

```bash
# Add to ~/.bashrc permanently
echo 'export BOB_API_KEY=<your-bob-api-key>' >> ~/.bashrc
source ~/.bashrc

# Test
bob run "say hello" --accept-license
```

Replace `<your-bob-api-key>` with your key from [bob.ibm.com](https://bob.ibm.com).

---

## 7. Install Supporting Tools

```bash
# Git
sudo dnf install -y git tmux
git --version    # 2.52.0
tmux -V          # next-3.4
```

---

## 8. Install Python + LangGraph

```bash
# pip
sudo dnf install -y python3-pip python3-devel gcc

# LangGraph + AI stack
pip3 install --user langgraph langchain langchain-community httpx fastapi uvicorn

# Verify
python3 -c "import langgraph; print('LangGraph OK')"
```

---

## 9. Configure Firecrawl MCP

```bash
mkdir -p ~/.bob/settings

cat > ~/.bob/settings/mcp.json << 'EOF'
{
  "mcpServers": {
    "firecrawl-mcp": {
      "command": "npx",
      "args": ["-y", "firecrawl-mcp"],
      "env": { "FIRECRAWL_API_KEY": "<your-firecrawl-api-key>" },
      "alwaysAllow": ["firecrawl_scrape"],
      "metadata": {
        "description": "Firecrawl MCP server for web scraping capabilities",
        "version": "latest"
      },
      "disabled": false
    }
  }
}
EOF

# Verify
bob mcp list
# firecrawl-mcp: npx -y firecrawl-mcp | enabled | stdio | global
```

---

## 10. Keep Bob Running with tmux + systemd

### Startup Script

```bash
cat > ~/start-bob.sh << 'EOF'
#!/bin/bash

SESSION="bob"
API_KEY="<your-bob-api-key>"

if tmux has-session -t $SESSION 2>/dev/null; then
  echo "Bob already running. Attach: tmux attach -t $SESSION"
  exit 0
fi

tmux new-session -d -s $SESSION -x 220 -y 50 \
  "export BOB_API_KEY=$API_KEY; bob chat --accept-license 2>&1 | tee /tmp/bob.log"

sleep 2
tmux has-session -t $SESSION && echo "Bob started" || echo "Failed"

echo "Attach: tmux attach -t $SESSION"
echo "Detach: Ctrl+B then D"
EOF

chmod +x ~/start-bob.sh
```

### Useful Aliases

```bash
cat >> ~/.bashrc << 'EOF'
alias bob-start="~/start-bob.sh"
alias bob-attach="tmux attach -t bob"
alias bob-kill="tmux kill-session -t bob"
alias bob-status="tmux ls 2>/dev/null | grep bob || echo 'Bob not running'"
EOF
source ~/.bashrc
```

### systemd Service (auto-start on boot)

```bash
sudo tee /etc/systemd/system/bob.service << 'EOF'
[Unit]
Description=IBM Bob AI Assistant (tmux)
After=network-online.target
Wants=network-online.target

[Service]
Type=forking
User=itzuser
Environment="BOB_API_KEY=<your-bob-api-key>"
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

sudo systemctl daemon-reload
sudo systemctl enable bob.service
sudo systemctl start bob.service
sudo systemctl status bob.service
```

---

## 11. Bob Terminal — Bloomberg Dashboard + Ops Chat

A Node.js Express server that provides:
- `http://<IP>:44290/` — Bloomberg-style market dashboard
- `http://<IP>:44290/chat` — Mobile-friendly Bob Ops panel
- `http://<IP>:44290/tasks` — Async task history
- REST API for stocks, news, bob chat, task management

### Project Setup

```bash
mkdir -p /data/bob-terminal/{public,data,tasks,logs}
cd /data/bob-terminal
npm init -y
npm install express cors node-cron axios yahoo-finance2
```

### File Structure

```
/data/bob-terminal/
├── server.js              # Express backend
├── package.json
├── public/
│   ├── index.html         # Bloomberg dashboard
│   ├── chat.html          # Bob Ops mobile panel
│   └── manifest.json      # PWA manifest
├── data/
│   ├── cache.json         # Stocks + news cache
│   └── tasks/             # Async task results (JSON + MD)
└── logs/
    └── server.log
```

### Key API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/status` | Server health + counts |
| `GET` | `/api/stocks` | Cached stock quotes (auto-refresh every 5min) |
| `GET` | `/api/news` | Cached news headlines |
| `POST` | `/api/refresh` | Trigger full data refresh via Bob+Firecrawl |
| `POST` | `/api/chat` | Submit task to Bob (async, returns task ID immediately) |
| `GET` | `/api/task/:id` | Check task status |
| `GET` | `/api/task/:id/result` | Download raw markdown result |
| `GET` | `/api/tasks` | List all tasks (JSON) |
| `GET` | `/tasks` | Task history page |
| `GET` | `/tasks/:id` | Individual task result page (auto-polls until done) |

### Async Task System

The `/api/chat` endpoint is **fully async** — it returns a task ID immediately and Bob runs in the background with **no timeout**.

```bash
# Submit a task
curl -X POST http://20.89.63.64:44290/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"Create a Confluent solution architecture","mode":"terminal-ops"}'

# Response (instant):
# { "taskId": "abc123", "status": "queued", "resultMdUrl": "/tasks/abc123" }

# Check status
curl http://20.89.63.64:44290/api/task/abc123

# View result in browser
open http://20.89.63.64:44290/tasks/abc123
```

### Data Sources

| Data | Source | Notes |
|------|--------|-------|
| Equities | [marketdata.app](https://marketdata.app) | Free, no key, individual quotes |
| Crypto (BTC/ETH) | [CoinGecko API](https://coingecko.com) | Free, no key, 24/7 |
| News headlines | Bob + Firecrawl → Yahoo Finance | Scraped on demand |
| Cron refresh | `0 6 * * *` | 6AM UTC daily |

### systemd Service

```bash
sudo tee /etc/systemd/system/bob-terminal.service << 'EOF'
[Unit]
Description=Bob Terminal - Bloomberg Dashboard
After=network-online.target

[Service]
Type=simple
User=itzuser
WorkingDirectory=/data/bob-terminal
Environment="BOB_API_KEY=<your-bob-api-key>"
Environment="HOME=/home/itzuser"
Environment="PATH=/usr/local/bin:/usr/bin:/bin"
ExecStart=/usr/bin/node /data/bob-terminal/server.js
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable bob-terminal.service
sudo systemctl start bob-terminal.service
```

### Mobile PWA Setup

The `/chat` page is a PWA — installable on phone home screen.

**iPhone:** Safari → Share → Add to Home Screen  
**Android:** Chrome → Menu → Add to Home Screen

---

## 12. Bob Ops Mode (custom_modes.yaml)

A custom Bob mode called `terminal-ops` that knows everything about this VM setup.

```bash
cat > ~/.bob/settings/custom_modes.yaml << 'EOF'
customModes:
  - slug: terminal-ops
    name: Terminal Ops
    roleDefinition: >-
      You are the AI operator of the BOB Terminal running on this Azure VM (IP: 20.89.63.64).
      You have full knowledge of every service running here and can build, deploy, fix, and extend them.

      INFRASTRUCTURE:
      - OS: RHEL 10.2, 16 CPU, 60GB RAM, 1TB disk at /data
      - Ports 44283-44290 open (Azure NSG)
      - Bob v2.0.0 at /usr/bin/bob, API key in BOB_API_KEY env
      - Firecrawl MCP at ~/.bob/settings/mcp.json

      SERVICES:
      1. bob.service — Bob chat in tmux session 'bob'
      2. bob-terminal.service — Dashboard at http://20.89.63.64:44290

      PATHS:
      - Server: /data/bob-terminal/server.js
      - Dashboard: /data/bob-terminal/public/index.html
      - Chat UI: /data/bob-terminal/public/chat.html
      - Cache: /data/bob-terminal/data/cache.json
      - Tasks: /data/bob-terminal/data/tasks/
      - Bob DB: /data/bob-home/db/ (symlinked from ~/.bob/db)

      RULES:
      - Always restart bob-terminal after changes: sudo systemctl restart bob-terminal
      - Verify after restart: curl -s http://localhost:44290/api/status
      - Use /data/ for all new projects
      - Keep BOB_API_KEY set in all spawned processes
    whenToUse: Use when managing the BOB Terminal VM setup.
    groups:
      - read
      - edit
      - execute
      - mcp
      - skill
      - todo
      - subagent
EOF
```

**Usage:**
```bash
bob chat --mode terminal-ops
bob run "restart bob-terminal service" --mode terminal-ops
bob run "add a new widget to the dashboard" --mode terminal-ops
```

---

## 13. Fix /home Disk Full Issue

`/home` is only 960 MB. The npm cache fills it fast.

```bash
# Check what's eating space
du -sh ~/.bob/* ~/.npm 2>/dev/null | sort -rh

# Fix 1: Clear npm cache (~580MB)
npm cache clean --force

# Fix 2: Move Bob DB and logs to /data
sudo systemctl stop bob.service bob-terminal.service

mkdir -p /data/bob-home
cp -a ~/.bob/db   /data/bob-home/db
cp -a ~/.bob/logs /data/bob-home/logs
rm -rf ~/.bob/db ~/.bob/logs
ln -s /data/bob-home/db   ~/.bob/db
ln -s /data/bob-home/logs ~/.bob/logs

# Fix 3: Redirect npm cache to /data permanently
mkdir -p /data/npm-cache
npm config set cache /data/npm-cache

# Restart services
sudo systemctl start bob-terminal.service bob.service

# Verify
df -h /home    # should be < 80%
```

---

## 14. Port Reference

| Port | Service | Access |
|------|---------|--------|
| 22 | SSH | Public |
| 44283–44290 | ITZ reserved range | Public (NSG opened by ITZ support) |
| **44290** | Bob Terminal (dashboard + chat) | **Public** |
| 9090 | Cockpit web UI | Internal only |

---

## 15. Service Management Cheat Sheet

```bash
# ── BOB CHAT SERVICE ─────────────────────────────────
sudo systemctl status bob           # status
sudo systemctl restart bob          # restart
sudo systemctl stop bob             # stop
tmux attach -t bob                  # attach to live session
# Detach from tmux: Ctrl+B then D

# ── BOB TERMINAL SERVICE ─────────────────────────────
sudo systemctl status bob-terminal  # status
sudo systemctl restart bob-terminal # restart
sudo journalctl -u bob-terminal -f  # live logs
curl -s http://localhost:44290/api/status  # health check

# ── BOTH SERVICES ────────────────────────────────────
sudo systemctl status bob bob-terminal --no-pager

# ── MCP ──────────────────────────────────────────────
bob mcp list                        # list all MCPs
bob mcp add <name> <command>        # add an MCP
# or edit ~/.bob/settings/mcp.json directly

# ── DISK ─────────────────────────────────────────────
df -h                               # all disk usage
du -sh /data/*                      # data disk usage
du -sh ~/.bob/* ~/.npm 2>/dev/null  # home usage breakdown

# ── TASKS ────────────────────────────────────────────
# View all tasks in browser:
# http://20.89.63.64:44290/tasks

# Submit a background task via CLI:
curl -X POST http://localhost:44290/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"your task here","mode":"terminal-ops"}'
```

---

## Quick Reproduce Checklist

```
[ ] SSH into VM with PEM key
[ ] Mount /dev/sdb to /data
[ ] Install Node.js 22 via NodeSource
[ ] Install Bob via curl installer (sudo)
[ ] Set BOB_API_KEY in ~/.bashrc
[ ] Install git + tmux via dnf
[ ] Install Python pip + langgraph
[ ] Write ~/.bob/settings/mcp.json with Firecrawl config
[ ] Create ~/start-bob.sh + enable bob.service
[ ] Set up /data/bob-terminal project + npm install
[ ] Write server.js, index.html, chat.html
[ ] Enable bob-terminal.service
[ ] Write ~/.bob/settings/custom_modes.yaml (terminal-ops mode)
[ ] Fix /home disk: npm cache clean + symlink ~/.bob/db to /data
[ ] Open firewall: sudo firewall-cmd --permanent --add-port=44283-44290/tcp && reload
[ ] Verify: curl http://localhost:44290/api/status
```

---

*Setup completed August 2026 on IBM Technology Zone Azure VM.*  
*Maintained by: BOB Terminal Ops — `bob chat --mode terminal-ops`*
