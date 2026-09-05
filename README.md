# 🤖 Headless Bob — Multi-Agent Company Simulator

> IBM Bob running headless on Azure RHEL — 4 specialized AI agents (CEO, Intel, Ops, Dev) orchestrating each other via a job queue, with a gamified HQ dashboard showing live delegation animations.

## 🌐 Live URLs

| URL | What |
|---|---|
| `http://20.89.63.64:44285/hq` | **Agent HQ Dashboard** — gamified, live orchestration view |
| `http://20.89.63.64:44285/ui` | **Bob Harness UI** — job runner & schedule manager |
| `http://20.89.63.64:44290` | **Bob Terminal** — Bloomberg-style market dashboard |

## 🧠 The 4 Agents

All agents are **IBM Bob custom modes** (`bob run --mode <mode>`), not separate apps.

| Agent | Mode | Role | Schedule |
|---|---|---|---|
| 👔 CEO | `ceo-agent` | Orchestrator — delegates to sub-agents, synthesizes board report | Daily 08:00 UTC |
| 🔍 Intel | `intel-agent` | Web research via Firecrawl MCP, competitor intelligence | Daily 07:00 UTC |
| ⚙️ Ops | `ops-agent` | Infrastructure health check, systemd/disk/port monitoring | Every 6 hours |
| 💻 Dev | `dev-agent` | Code review, codebase health | Daily 06:00 UTC |

## 🏗️ Architecture

```
Browser → port 44285/hq → Harness proxy → Flask HQ app (port 44283)
                                              ↓ SSE stream: thinking/delegate/reply events
                        → port 44285/ui → Carbon Design Harness UI
Browser → port 44290    → Bob Terminal (Express + Bloomberg dashboard)

Harness /jobs API → bob run --mode <agent> → agent writes to /data/company/*.md
```

## 📁 Repo Structure

```
vm-source/
├─ bob-harness/
│   ├─ server.py          # FastAPI Harness: job queue, schedules, /hq SSE proxy
│   └─ harness-ui.html    # Carbon Design UI
├─ company-hq/
│   └─ app.py             # Flask HQ dashboard (gamified, SSE chat API)
├─ bob-terminal/
│   ├─ server.js          # Express Bloomberg dashboard backend
│   └─ terminal-dashboard.html
├─ company/
│   ├─ intel-report.md    # Latest Intel Agent output
│   ├─ ops-health.md      # Latest Ops Agent output
│   └─ board-report.md    # Latest CEO Board Report
└─ custom_modes.yaml      # All 4 agent mode definitions
BOB-VM-Setup-Guide.md     # Full VM setup guide from scratch
```

## ⚡ Key Technical Details

- **Bob v2.0.0** — `bob run --mode <mode>` with `BOB_API_KEY` env var
- **Harness** — `BOBSHELL_API_KEY → BOB_API_KEY` mapping patch in `server.py`
- **CEO delegation** — real `curl` calls to `POST /jobs`, polls `GET /jobs/{id}` until done
- **HQ dashboard** — SSE stream emits `thinking` → `delegate` → `reply` events for live animation
- **All 4 services** run as systemd units, auto-start on boot

## 🚀 SSH

```bash
ssh -i ssh_private_key.pem -o StrictHostKeyChecking=no itzuser@20.89.63.64
```

## 🔗 Related

- [IBM Bob documentation](https://pages.github.ibm.com/IBM-Bob/bob-docs/)
- [Bob Builders Day Session Log](../Bob-Builders-Day/)
