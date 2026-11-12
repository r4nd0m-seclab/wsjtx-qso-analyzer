# WSJT-X QSO Analyzer (Web GUI & UDP Companion)

A containerized, real-time ham radio station companion that listens to **WSJT-X decodes** over UDP port `2237`, resolves **Country and US/Canadian State codes** via Big CTY DXCC data and Maidenhead grid mapping, computes link margin and two-way QSO probability using **MMANA-GAL 3D far-field radiation patterns**, verifies empirical propagation in real time via **PSK Reporter live MQTT streaming + 60-minute HTTP seeding**, factors in **real-time space weather** from [solar.w5mmw.net](https://solar.w5mmw.net/), and displays an interactive dark-mode dashboard on `http://localhost:8080`.

---

## Architecture Overview

```
+---------------------------------------------------------------------------------+
| Host Machine                                                                    |
|                                                                                 |
|  +--------------------+        UDP (2237)        +---------------------------+  |
|  |  WSJT-X (v3.0+)    | -----------------------> | WSJT-X QSO Analyzer       |  |
|  |  Transceiver GUI   | <----------------------- | (Host Network Mode)       |  |
|  +--------------------+        Type 4 Reply      |                           |  |
|                                                  | - UDP Protocol Parser     |  |
|  +--------------------+   TLS/WSS MQTT + HTTPS   | - Country / State Resolver|  |
|  |  PSK Reporter      | -----------------------> | - PSK-RPT Live Streamer   |  |
|  |  (v2 MQTT + XML)   |                          | - MMANA-GAL Spatial Gain  |  |
|  +--------------------+                          | - Space Weather Poller    |  |
|                                                  | - Port 8080 Dashboard     |  |
|  +--------------------+                          +---------------------------+  |
|  | Web Browser/Tablet | <====== HTTP / JSON ===>               ^                |
|  | (Local or LAN)     | ---------------------------------------+                |
|  +--------------------+                                                         |
+---------------------------------------------------------------------------------+
```

---

## Features

1. **Zero-Modification Companion:** Operates with standard, unmodified WSJT-X binaries via the official binary UDP protocol (`0xadbccbda`).
2. **Country & State Code Resolution:** Resolves 2-letter US State codes (e.g. `AZ`, `CA`, `TX`, `WI`), Canadian provinces (`BC`, `ON`), and international DX country codes/entities (`JP`, `DE`, `GB`) via embedded Maidenhead grid boundary mapping and Big CTY DXCC prefix tables.
3. **Deterministic MMANA-GAL 3D Pattern Queries:** Computes Great Circle bearing ($\Phi$) and takeoff elevation angle ($\theta_{el}$) to retrieve precise directional gain in dBi from the CSV matrix.
4. **Space Weather Ingestion:** Ingests live Solar Flux Index (SFI), Sunspot Number, Kp-index, solar wind speed, and noise floor from `solar.w5mmw.net`.
5. **Diurnal F2 Layer Adaptation:** Adjusts virtual reflection height ($h_v = 280\text{ km}$ day, $350\text{ km}$ night) based on solar elevation angle, accurately reflecting nighttime antenna gain boosts.
6. **Interactive 1-Click Call:** Clicking **"CALL"** sends a Type 4 `Reply` UDP packet to WSJT-X, populating the DX Call and scheduling transmission on the next slot.
7. **Antenna Directivity Radar:** HTML5 Canvas polar radar plot displaying the antenna's radiation envelope alongside plotted decoded station bearings.
8. **TX Passband Optimization:** Monitors rolling 60-second passband activity to detect sub-frequency collisions and direct WSJT-X to the clearest open transmission slot.
9. **Directed CQ Compliance Filtering:** Automatically parses targeted CQ modifiers (`CQ DX`, US states `CQ TX`, continents `CQ NA`/`CQ EU`, countries/prefixes `CQ JA`/`CQ VK`). Non-matching calls are restricted and suppressed from the candidate table so operators only see QSOs they are eligible to answer.
10. **Real-Time PSK Reporter Ground-Truth Streaming:** Mirrors `pskreporter.info`'s hybrid browser architecture — seeds the initial 60-minute reception window once via HTTPS XML query, then streams incoming TX reception reports in real time over PSK Reporter's public MQTT v2 feed using a zero-dependency Python stdlib TLS/WSS client. Active WSJT-X candidates rescore within 1 second of spot arrival.

---

## Quick Start

### Option 1: Run with Podman (Recommended)
```bash
cd web
./run.sh podman
```
Or manually:
```bash
podman build -t wsjtx-qso-analyzer .
podman run -d --name wsjtx-qso-analyzer --network host -v ./patterns:/app/patterns:ro wsjtx-qso-analyzer
```

### Option 2: Run with Docker
```bash
cd web
./run.sh docker
```

### Option 3: Run Directly with Python (No Dependencies)
The backend uses only Python's standard library (`socket`, `ssl`, `select`, `struct`, `http.server`, `urllib`, `threading`):
```bash
cd web
python3 app.py
```

Open your browser at: **`http://localhost:8080`**

---

## WSJT-X Configuration

To ensure WSJT-X broadcasts decodes to the scorer:
1. Open WSJT-X.
2. Go to **File -> Settings -> Reporting**.
3. Under **UDP Server**:
   - **UDP Server:** `127.0.0.1` (or `2237`)
   - **UDP Server port number:** `2237`
   - Check **"Accept UDP requests"** (allows the web dashboard's "CALL" button to queue transmissions).
   - Check **"Notify on decode"**.

---

## Worked Before (B4) Log Tracking

The service automatically ingests and live-monitors your official WSJT-X contact log (`~/.local/share/WSJT-X/wsjtx.log` mounted into `/wsjtx-data/wsjtx.log`):

1. **Automatic Log Monitoring:** Detects when new QSOs are logged and reloads immediately.
2. **Band-Specific Intelligence:**
   - **`B4 (Band)`:** If you have already worked a station on the **current band** (e.g. 40m), they are flagged as `WORKED B4` and automatically hidden when **`[✓] Hide Worked (B4)`** is enabled.
   - **`NEW BAND (WKD Band)`:** If you worked a station on a *different* band (e.g., worked on 20m, but now calling on 40m), they are flagged as a fresh **NEW BAND** opportunity so you can work them for band slot credits!
3. **Toggle Controls:** The top toolbar features **`[✓] Hide Worked (B4)`** so you can effortlessly toggle between seeing only fresh unworked stations or reviewing all callers with their contact history badges.

---

## Multi-Band Antenna Pattern Auto-Switching

The service automatically discovers and indexes all band pattern CSV files inside `web/patterns/` (named by nominal frequency, e.g., `<frequency_in_mhz>.csv`):

| Frequency File | Band | Dial Frequency Range |
|---|---|---|
| `1.84.csv` | **160m** | 1.800 – 2.000 MHz |
| `3.573.csv` | **80m** | 3.500 – 4.000 MHz |
| `5.357.csv` | **60m** | 5.300 – 5.500 MHz |
| `7.074.csv` | **40m** | 7.000 – 7.300 MHz |
| `10.136.csv` | **30m** | 10.100 – 10.150 MHz |
| `14.074.csv` | **20m** | 14.000 – 14.350 MHz |
| `18.1.csv` | **17m** | 18.068 – 18.168 MHz |
| `21.074.csv` | **15m** | 21.000 – 21.450 MHz |
| `24.915.csv` | **12m** | 24.890 – 24.990 MHz |
| `28.074.csv` | **10m** | 28.000 – 29.700 MHz |

**Dynamic Band Tracking:**
Whenever you switch bands in WSJT-X, WSJT-X broadcasts its new dial frequency in a Status packet (Type 1). The scorer automatically detects the frequency change, loads the matching MMANA-GAL radiation pattern from `patterns/`, re-draws the azimuth radar, and scores incoming decodes against that band's exact far-field pattern without requiring any manual intervention or restarts.

---

## PSK Reporter Ground-Truth Propagation (Live MQTT + HTTP Seed)

Instead of relying solely on 5-minute HTTP polling, `PskReporterManager` and `psk_mqtt_streamer` combine historical HTTP seeding with real-time MQTT streaming — matching how `pskreporter.info/pskmap.html` operates in the browser while requiring **zero third-party Python dependencies**:

1. **60-Minute Historical Seed (HTTPS):**
   On startup, the service fetches the trailing 1-hour reception window (`flowStartSeconds=-3600`) for your station callsign from `retrieve.pskreporter.info/query`.
2. **Real-Time Spot Streaming (MQTT v2 over TLS / WSS):**
   A persistent stdlib MQTT 3.1.1 client connects to `mqtt.pskreporter.info:1884` over TLS (with automatic failover to `wss://mqtt2.pskreporter.info:1886/mqtt` over RFC 6455 WebSockets and `:1883` TCP) and subscribes to:
   ```text
   pskr/filter/v2/+/+/<DE_CALL>/+/+/+/+/+
   ```
   As remote monitors upload FT8/FT4 spots at the end of your 15-second TX slot, spots merge into the rolling 3600-second window and active candidate rows rescore within 1 second (`● LIVE` indicator in the top toolbar).
3. **Reduced HTTP Polling & Non-Destructive Merge:**
   While the MQTT stream is live, full HTTP refetches drop from every **5 minutes** to every **15 minutes** (automatically reverting to 5-minute HTTP polling if MQTT is unreachable or if operating with a `/` portable callsign that cannot appear in an MQTT topic level). HTTP syncs preserve any live MQTT spots that are newer than PSK Reporter's XML query database snapshot.
4. **Seamless Multi-Band QSY & Warm Restart Cache:**
   - Because the MQTT subscription wildcards both `{band}` and `{mode}` (`+`) and indexes spots by each report's RF frequency, band changes in WSJT-X require zero reconnects or extra HTTP queries — all bands worked within the past hour stay warm in memory simultaneously.
   - Rolling spots are persisted to `config/psk_spots_cache.json` (`0600` permissions, git-ignored) so container restarts (such as swapping antenna pattern sets) retain the 60-minute propagation window and avoid HTTP `429` rate limits.

### Empirical Scoring Tiers

When **`[✓] PSK-RPT`** is enabled, each decoded station is matched against your rolling 60-minute reception reports:

| Match Tier | Badge | Condition | Scoring Impact |
|---|---|---|---|
| `direct_band` | **`2-WAY +XdB`** | Station heard your signal on the **active band** | Computes true two-way link bottleneck $\min(\text{RX margin}, \text{TX margin})$ + **+12 pt** verified confidence bonus |
| `direct_other` | **`HEARD (<band>)`** | Station heard your signal recently on **another band** | Base link margin + **+5 pt** active-monitor confidence bonus |
| `grid4` | **`⚡` (`PATH <grid4>`)** | One or more stations in the same 4-char Maidenhead grid heard you on this band | Blends modeled margin (70%) with observed regional TX margin (30%) + **+2 to +6 pt** path-openness bonus |
| `grid2` | **`⚡` (`PATH <field>`)** | Stations in the same 2-char Maidenhead field heard you on this band | Base link margin + **+3 pt** regional propagation bonus |

### Optional Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MY_CALL` / `CALLSIGN` | Auto-learned from WSJT-X UDP (`DE Call`) | Station callsign used before the first WSJT-X Status packet arrives |
| `PSK_REPORTER` | `true` | Enable PSK Reporter ground-truth scoring by default at startup |
| `PSK_MQTT_URL` | *(auto failover list)* | Optional explicit broker override (`mqtts://host:1884`, `wss://host:1886/mqtt`, or `mqtt://host:1883`) |
| `CONFIG_DIR` | `config` (`/app/config` in container) | Directory for persistent radar settings and `psk_spots_cache.json` |
