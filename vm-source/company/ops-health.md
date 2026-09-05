# Infrastructure Health Report
**Generated:** 2026-09-05 17:21 UTC  
**Host:** linux-6a7c7ec398b44106c8cd6038-vm  
**OS:** Red Hat Enterprise Linux 10.2 (Coughlan)  
**Uptime:** 24 days, 3h 06m

---

## 🟡 Overall Status: YELLOW → GREEN (after auto-fix)

One critical service was down and has been automatically remediated.

---

## 🖥️ CPU

| Metric | Value |
|--------|-------|
| CPU Model | Intel Xeon Platinum 8573C |
| Logical Cores | 16 |
| Load Average (1m / 5m / 15m) | 0.63 / 0.92 / 0.94 |
| Load % of capacity | ~6% (healthy) |

**Top CPU Consumers at check time:**
- `confluent-control-center` (java): 91% — JVM startup spike, normal
- `redis-mcp-server` (python): 42% — MCP startup spike
- `bob` (node): 32% — current session

✅ CPU load nominal for 16-core system.

---

## 💾 Memory

| Metric | Value |
|--------|-------|
| Total RAM | 62 GiB |
| Used | 9.6 GiB (15%) |
| Free | 43 GiB |
| Buff/Cache | 11 GiB |
| Available | 52 GiB |
| Swap | None configured |

> ⚠️ **No swap configured.** Under memory pressure, OOM killer would be triggered directly. Consider adding a swapfile as safety net given the JVM-heavy workload.

✅ Memory utilization is healthy at 15%.

---

## 💿 Disk

| Filesystem | Size | Used | Avail | Use% | Mount |
|-----------|------|------|-------|------|-------|
| rootvg-rootlv | 2.0G | 108M | 1.9G | **6%** | / |
| rootvg-usrlv | 10G | 7.2G | 2.9G | **72%** | /usr |
| rootvg-homelv | 960M | 856M | 105M | **⚠️ 90%** | /home |
| rootvg-varlv | 10G | 1.9G | 8.2G | **19%** | /var |
| rootvg-tmplv | 2.0G | 79M | 1.9G | **4%** | /tmp |
| /dev/sdb | 984G | 770M | 933G | **1%** | /data |
| /dev/sda2 | 960M | 319M | 642M | **34%** | /boot |

### ⚠️ WARNING: /home at 90% (856M / 960M)

Breakdown of /home/itzuser usage:
- `~/.local` — **615M** (share: 330M containers, lib: 232M, bin: 54M)
- `~/.cache` — **284M** (uv Python caches, npm caches)
- Total: ~900M

**Candidates for cleanup:**
- `/home/itzuser/.local/share/containers/` — **330M** (Podman container storage)
- `/home/itzuser/.cache/` — **284M** (package caches, safe to prune)

---

## 🔧 Services Status

| Service | Status | Notes |
|---------|--------|-------|
| confluent-server (Kafka) | ✅ active (running) | Up since Aug 13, 24d |
| confluent-zookeeper | ✅ active (running) | Up since Aug 13, 24d |
| confluent-schema-registry | ✅ active (running) | Up since Aug 13 |
| confluent-kafka-connect | ✅ active (running) | Up since Aug 13 |
| confluent-kafka-rest | ✅ active (running) | Up since Aug 13 |
| confluent-control-center | ✅ active (running) | Started this session |
| **confluent-ksqldb** | **🔴→✅ FIXED** | Was failed since Aug 13 — **auto-remediated** |
| grafana-server | ✅ active (running) | Up since Aug 19 |
| mongod (MongoDB) | ✅ active (running) | Up since Aug 13 |
| postgresql | ✅ active (running) | Up since Aug 13 |
| valkey (Redis-compatible) | ✅ active (running) | :6379, 8MB memory |

---

## 🔴 Incident: confluent-ksqldb — RESOLVED

**Duration of outage:** 2026-08-13 14:13 UTC → 2026-09-05 17:20 UTC (~23 days)

**Root Cause:** Java 21 removed `SecurityManager` API (`java.lang.UnsupportedOperationException: The Security Manager is deprecated and will be removed in a future release`). ksqlDB 7.9.9 tries to call `System.setSecurityManager()` in `UserFunctionLoader` at startup, which throws an uncaught exception on JDK 21.

**Fix Applied:**
```
/etc/systemd/system/confluent-ksqldb.service.d/java21-fix.conf
→ Environment="KSQL_OPTS=-Djava.security.manager=allow"
```
Created missing dirs with correct ownership:
- `/var/log/confluent/ksql` (cp-ksql:confluent 750)
- `/var/lib/kafka-streams` (cp-ksql:confluent 750)

**Verification:** Service now `active (running)`, listening on `http://0.0.0.0:8088`.

---

## 🌐 Network Ports Confirmed Listening

| Port | Service |
|------|---------|
| 6379 | Valkey (Redis) — localhost only |
| 5432 | PostgreSQL — localhost only |
| 27017 | MongoDB — localhost only |
| 8081 | Schema Registry |
| 8082 | Kafka REST |
| 8083 | Kafka Connect |
| 8088 | ksqlDB (**newly restored**) |
| 9090 | Confluent metrics / Control Center |

---

## ⚠️ Recommendations

1. **[HIGH] /home disk at 90%** — Clean `/home/itzuser/.cache/` (284M safe to prune) and review `/home/itzuser/.local/share/containers/` (330M Podman storage). Total recovery potential: ~500–600M.
   ```bash
   uv cache clean && rm -rf ~/.cache/pip ~/.cache/uv
   podman system prune -f  # if containers not needed
   ```

2. **[MEDIUM] Enable confluent-ksqldb on boot** — Service is `disabled`. After fix, enable it:
   ```bash
   sudo systemctl enable confluent-ksqldb.service
   ```

3. **[MEDIUM] No swap space** — 62GB RAM is large, but JVM processes can spike. Add a swapfile:
   ```bash
   sudo fallocate -l 8G /swapfile && sudo chmod 600 /swapfile
   sudo mkswap /swapfile && sudo swapon /swapfile
   ```

4. **[LOW] /usr at 72%** — Monitor, not critical yet. Current Confluent Platform install accounts for most of it.

5. **[LOW] Confluent version-check telemetry errors** — ksqlDB logs `Could not submit metrics to version-check.confluent.io`. Disable noisy telemetry in `/etc/ksqldb/ksql-server.properties`:
   ```
   confluent.support.metrics.enable=false
   ```

---

*Report generated by ops-agent | Bob*
