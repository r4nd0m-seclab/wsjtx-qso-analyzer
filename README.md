# WSJT-X QSO Analyzer (Web GUI & UDP Companion)

A containerized, real-time ham radio station companion that listens to **WSJT-X decodes** over UDP port `2237`, resolves **Country and US/Canadian State codes** via Big CTY DXCC data and Maidenhead grid mapping, computes link margin and two-way QSO probability using **MMANA-GAL 3D far-field radiation patterns**, factors in **real-time space weather** from [solar.w5mmw.net](https://solar.w5mmw.net/), and displays an interactive dark-mode dashboard on `http://localhost:8080`.

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
|                                                  | - Country / State Resolver|  |
|                                                  | - MMANA-GAL Spatial Gain  |  |
|  +--------------------+                          | - Space Weather Poller    |  |
|  | Web Browser/Tablet | <====== HTTP / JSON ===> | - Port 8080 Dashboard     |  |
|  | (Local or LAN)     |                          +---------------------------+  |
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
8. **Test Simulation Mode:** Built-in **"Inject Test Decodes"** button populates benchmark decodes instantly without requiring an active radio or on-air signals.

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
The backend uses Python's standard library (`socket`, `struct`, `http.server`, `urllib`, `threading`):
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
