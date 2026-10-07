#!/usr/bin/env python3
"""
WSJT-X QSO Analyzer (Web GUI & UDP Companion)
Listens on WSJT-X UDP port 2237, scores incoming decodes against 3D antenna patterns
and real-time space weather, resolves country and state codes, and provides a web dashboard on port 8080.
"""

import base64
import cmath
import csv
import io
import json
import math
import os
import re
import socket
import struct
import sys
import threading
import time
import urllib.request
import zlib
from urllib.error import HTTPError
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from typing import Dict, Tuple, List, Optional, Any

# Prevent BrokenPipeError / IOError when running detached or terminal is closed
class SafeStream:
    def __init__(self, stream):
        self._stream = stream
    def write(self, data):
        try:
            return self._stream.write(data)
        except (BrokenPipeError, IOError, OSError):
            return len(data)
    def flush(self):
        try:
            return self._stream.flush()
        except (BrokenPipeError, IOError, OSError):
            pass
    def isatty(self):
        try:
            return self._stream.isatty()
        except Exception:
            return False
    def __getattr__(self, name):
        return getattr(self._stream, name)

sys.stdout = SafeStream(sys.stdout)
sys.stderr = SafeStream(sys.stderr)

# -----------------------------------------------------------------------------
# Configuration Defaults
# -----------------------------------------------------------------------------
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
UDP_PORT = int(os.environ.get("UDP_PORT", "2237"))
DEFAULT_HOME_GRID = os.environ.get("HOME_GRID", "EL09")
DEFAULT_MY_CALL = os.environ.get("MY_CALL", os.environ.get("CALLSIGN", "K5JEE"))
DEFAULT_TX_POWER_W = float(os.environ.get("TX_POWER_W", "100.0"))
NORTH_OFFSET_DEG = float(os.environ.get("NORTH_OFFSET_DEG", "0.0"))
PATTERNS_DIR = os.environ.get("PATTERNS_DIR", "patterns")
DEFAULT_CQ_ONLY = os.environ.get("CQ_ONLY", "true").lower() in ("true", "1", "yes")
DEFAULT_HIDE_WORKED = os.environ.get("HIDE_WORKED", "true").lower() in ("true", "1", "yes")
DEFAULT_TX_FREQ_OPTIMIZE = os.environ.get("TX_FREQ_OPTIMIZE", "true").lower() in ("true", "1", "yes")
DEFAULT_PSK_REPORTER = os.environ.get("PSK_REPORTER", "true").lower() in ("true", "1", "yes")
WSJTX_DATA_DIR = os.environ.get("WSJTX_DATA_DIR", "/wsjtx-data")

# -----------------------------------------------------------------------------
# MMANA-GAL Antenna Gain Pattern & Multi-Band Manager
# -----------------------------------------------------------------------------
def get_band_name(freq_mhz: float) -> str:
    if 1.8 <= freq_mhz <= 2.0: return "160m"
    if 3.5 <= freq_mhz <= 4.0: return "80m"
    if 5.3 <= freq_mhz <= 5.5: return "60m"
    if 7.0 <= freq_mhz <= 7.3: return "40m"
    if 10.1 <= freq_mhz <= 10.15: return "30m"
    if 14.0 <= freq_mhz <= 14.35: return "20m"
    if 18.068 <= freq_mhz <= 18.168: return "17m"
    if 21.0 <= freq_mhz <= 21.45: return "15m"
    if 24.89 <= freq_mhz <= 24.99: return "12m"
    if 28.0 <= freq_mhz <= 29.7: return "10m"
    if 50.0 <= freq_mhz <= 54.0: return "6m"
    return f"{freq_mhz:.3f}MHz"

def get_solar_band_group(freq_mhz: float) -> str:
    """Maps dial frequency to solar.w5mmw.net band propagation ranges."""
    if freq_mhz < 10.0:
        return "80m-40m"
    elif freq_mhz < 18.0:
        return "30m-20m"
    elif freq_mhz < 24.0:
        return "17m-15m"
    else:
        return "12m-10m"

class MmanaGainPattern:
    def __init__(self, csv_path: str, north_offset_deg: float = 0.0):
        self.csv_path = csv_path
        self.north_offset = north_offset_deg
        self.gain_grid: Dict[Tuple[int, int], Tuple[float, float, float]] = {}
        self.max_gain = -999.0
        self.peak_coord = (0, 0)
        self.loaded = False
        if os.path.exists(csv_path):
            self._load(csv_path)

    def _load(self, path: str):
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader)
                for row in reader:
                    zenith = float(row[0])
                    az = float(row[1])
                    vert = float(row[2])
                    hori = float(row[3])
                    tot = float(row[4])
                    
                    elev = 90.0 - zenith
                    if elev >= 0.0:
                        az_idx = int(round(az)) % 360
                        el_idx = int(round(elev))
                        self.gain_grid[(az_idx, el_idx)] = (vert, hori, tot)
                        if tot > self.max_gain:
                            self.max_gain = tot
                            self.peak_coord = (az_idx, el_idx)
            self.loaded = True
            print(f"[Antenna] Loaded {len(self.gain_grid)} points from {os.path.basename(path)}. Peak: {self.max_gain:.2f} dBi at Az {self.peak_coord[0]}°, El {self.peak_coord[1]}°")
        except Exception as e:
            print(f"[Antenna] Failed to load pattern from {path}: {e}")

    def get_gain(self, azimuth_deg: float, elevation_deg: float) -> Tuple[float, float, float]:
        if not self.loaded:
            return (0.0, 0.0, 0.0)
        az_eff = int(round(azimuth_deg - self.north_offset)) % 360
        el_eff = int(round(max(0.0, min(90.0, elevation_deg))))
        return self.gain_grid.get((az_eff, el_eff), (-30.0, -30.0, -30.0))

    def get_azimuth_slice(self, elevation_deg: float) -> List[float]:
        """Returns 360 total gain values for polar plot at a given elevation."""
        el_eff = int(round(max(0.0, min(90.0, elevation_deg))))
        slice_vals = []
        for az in range(360):
            az_eff = int(round(az - self.north_offset)) % 360
            tot = self.gain_grid.get((az_eff, el_eff), (-30.0, -30.0, -30.0))[2]
            slice_vals.append(round(tot, 2))
        return slice_vals

class AntennaPatternManager:
    """Manages multi-band MMANA-GAL pattern files dynamically matched to WSJT-X dial frequency."""
    def __init__(self, patterns_dir: str, north_offset_deg: float = 0.0):
        self.patterns_dir = patterns_dir
        self.north_offset = north_offset_deg
        self.patterns_cache: Dict[str, MmanaGainPattern] = {}
        self.file_map: Dict[float, str] = {} # nominal_freq_mhz -> full_path
        self.active_pattern: Optional[MmanaGainPattern] = None
        self.active_filename: str = ""
        self.active_freq_mhz: float = 0.0
        self.active_band_name: str = ""
        self.scan_patterns()

    def scan_patterns(self):
        self.file_map.clear()
        if not os.path.exists(self.patterns_dir):
            print(f"[PatternManager] Directory not found: {self.patterns_dir}")
            return

        for entry in os.listdir(self.patterns_dir):
            if entry.lower().endswith(".csv"):
                m = re.match(r'^([0-9]+(?:\.[0-9]+)?)\.csv$', entry, re.IGNORECASE)
                if m:
                    f_mhz = float(m.group(1))
                    self.file_map[f_mhz] = os.path.join(self.patterns_dir, entry)

        print(f"[PatternManager] Discovered {len(self.file_map)} band pattern files in {self.patterns_dir}: {sorted(self.file_map.keys())} MHz")

    def set_frequency(self, freq_mhz: float) -> bool:
        """Selects and activates the best matching pattern file for the given dial frequency."""
        if not self.file_map:
            self.scan_patterns()
        if not self.file_map:
            return False

        best_nominal = min(self.file_map.keys(), key=lambda f: abs(f - freq_mhz))
        target_path = self.file_map[best_nominal]
        filename = os.path.basename(target_path)

        if self.active_filename == filename and self.active_pattern is not None:
            return False # Already active

        if filename not in self.patterns_cache:
            self.patterns_cache[filename] = MmanaGainPattern(target_path, self.north_offset)

        self.active_pattern = self.patterns_cache[filename]
        self.active_filename = filename
        self.active_freq_mhz = best_nominal
        self.active_band_name = get_band_name(best_nominal)
        print(f"[PatternManager] Switched active antenna pattern -> {filename} ({self.active_band_name}) for {freq_mhz:.3f} MHz (Peak: {self.active_pattern.max_gain:.2f} dBi)")
        return True

# -----------------------------------------------------------------------------
# WSJT-X Log Watcher (Worked Before / B4 Tracking)
# -----------------------------------------------------------------------------
class WsjtxLogWatcher:
    """Monitors and parses WSJT-X log file (wsjtx.log) to track worked-before stations."""
    def __init__(self, log_path: Optional[str] = None):
        self.log_path = log_path or self._detect_log_path()
        self.last_mtime: float = 0.0
        self.worked_any: Dict[str, List[Dict[str, Any]]] = {} # call -> list of qsos
        self.worked_band: Dict[Tuple[str, str], Dict[str, Any]] = {} # (call, band) -> qso
        self.reload_if_changed()

    def _detect_log_path(self) -> str:
        candidates = [
            os.environ.get("WSJTX_LOG_PATH", ""),
            os.path.join(WSJTX_DATA_DIR, "wsjtx.log"),
            "/wsjtx-data/wsjtx.log",
            os.path.expanduser("~/.local/share/WSJT-X/wsjtx.log"),
        ]
        for c in candidates:
            if c and os.path.exists(c):
                return c
        return os.path.join(WSJTX_DATA_DIR, "wsjtx.log")

    def reload_if_changed(self) -> bool:
        if not self.log_path or not os.path.exists(self.log_path):
            detected = self._detect_log_path()
            if detected and os.path.exists(detected):
                self.log_path = detected
            else:
                return False

        try:
            mtime = os.path.getmtime(self.log_path)
            if mtime == self.last_mtime:
                return False

            self.last_mtime = mtime
            w_any = {}
            w_band = {}

            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                reader = csv.reader(f)
                for row in reader:
                    if len(row) >= 8:
                        date_off = row[2].strip()
                        time_off = row[3].strip()
                        call = row[4].strip().upper()
                        grid = row[5].strip().upper()
                        try:
                            freq_mhz = float(row[6].strip())
                        except ValueError:
                            freq_mhz = 0.0
                        mode = row[7].strip()
                        band = get_band_name(freq_mhz)

                        if call:
                            qso = {
                                "date": date_off,
                                "time": time_off,
                                "grid": grid,
                                "freq": freq_mhz,
                                "band": band,
                                "mode": mode
                            }
                            if call not in w_any:
                                w_any[call] = []
                            w_any[call].append(qso)
                            w_band[(call, band)] = qso

            self.worked_any = w_any
            self.worked_band = w_band
            print(f"[LogWatcher] Loaded {len(w_any)} unique stations ({len(w_band)} band-slots) from {self.log_path}")
            return True
        except Exception as e:
            print(f"[LogWatcher] Error reading {self.log_path}: {e}")
            return False

    def check_worked(self, call: str, current_band: str) -> Tuple[bool, bool, Optional[Dict[str, Any]], List[str]]:
        """Returns (worked_current_band, worked_any_band, last_qso, other_bands)."""
        self.reload_if_changed()
        call_clean = call.strip().upper()
        base_call = call_clean.split("/")[0] if "/" in call_clean else call_clean

        qso_band = self.worked_band.get((call_clean, current_band))
        if not qso_band and base_call != call_clean:
            qso_band = self.worked_band.get((base_call, current_band))

        qsos_any = self.worked_any.get(call_clean)
        if not qsos_any and base_call != call_clean:
            qsos_any = self.worked_any.get(base_call)

        last_qso = qso_band or (qsos_any[-1] if qsos_any else None)
        other_bands = sorted(list({q["band"] for q in (qsos_any or []) if q.get("band") and q["band"] != current_band}))
        return bool(qso_band), bool(qsos_any), last_qso, other_bands

# -----------------------------------------------------------------------------
# Country & State Code Resolver (DXCC & Maidenhead Grid Mapping)
# -----------------------------------------------------------------------------
class LocationResolver:
    """Resolves Country, DXCC entity, and US/Canadian State/Province codes from callsign and Maidenhead grid."""
    def __init__(self, cty_path: Optional[str] = None, grids_path: Optional[str] = None):
        self.grids_map: Dict[str, Dict[str, str]] = {}
        self.entities: Dict[str, Dict[str, Any]] = {}
        self.prefix_map: Dict[str, Dict[str, Any]] = {}
        self.exact_map: Dict[str, Dict[str, Any]] = {}
        
        # Determine grids path
        grids_file = grids_path or os.environ.get("GRIDS_PATH", "")
        if not grids_file or not os.path.exists(grids_file):
            for candidate in [
                "grids_na.json",
                "/app/grids_na.json",
                os.path.join(os.path.dirname(__file__), "grids_na.json")
            ]:
                if os.path.exists(candidate):
                    grids_file = candidate
                    break

        if grids_file and os.path.exists(grids_file):
            try:
                with open(grids_file, "r", encoding="utf-8") as f:
                    self.grids_map = json.load(f)
                print(f"[Location] Loaded {len(self.grids_map)} North American grid squares from {grids_file}")
            except Exception as e:
                print(f"[Location] Error loading grids map {grids_file}: {e}")

        # Determine cty.dat path
        cty_file = cty_path or os.environ.get("CTY_DAT_PATH", "")
        if not cty_file or not os.path.exists(cty_file):
            for candidate in [
                "cty.dat",
                "/app/cty.dat",
                os.path.join(os.path.dirname(__file__), "cty.dat"),
                "/usr/share/wsjtx/cty.dat",
                os.path.expanduser("~/local/wsjtx/cty.dat"),
                os.path.expanduser("~/.local/share/WSJT-X/cty.dat")
            ]:
                if os.path.exists(candidate):
                    cty_file = candidate
                    break

        if cty_file and os.path.exists(cty_file):
            try:
                self._load_cty(cty_file)
                print(f"[Location] Loaded {len(self.entities)} DXCC entities, {len(self.prefix_map)} prefixes from {cty_file}")
            except Exception as e:
                print(f"[Location] Error parsing {cty_file}: {e}")

    def _map_to_iso(self, pfx: str, name: str) -> str:
        table = {
            'K': 'US', 'VE': 'CA', 'XE': 'MX', 'JA': 'JP', 'DL': 'DE', 'G': 'GB',
            'GM': 'GB', 'GW': 'GB', 'GI': 'GB', 'GD': 'GB', 'GJ': 'GB', 'GU': 'GB',
            'VK': 'AU', 'ZL': 'NZ', 'PY': 'BR', 'LU': 'AR', 'CE': 'CL', 'CX': 'UY',
            'OA': 'PE', 'HC': 'EC', 'HK': 'CO', 'YV': 'VE', 'ZP': 'PY', 'CP': 'BO',
            'EA': 'ES', 'EA8': 'ES', 'EA9': 'ES', 'I': 'IT', 'IS0': 'IT', 'F': 'FR',
            'OH': 'FI', 'OH0': 'AX', 'SM': 'SE', 'LA': 'NO', 'PA': 'NL', 'ON': 'BE',
            'SP': 'PL', 'OK': 'CZ', 'OM': 'SK', 'HA': 'HU', 'YO': 'RO', 'LZ': 'BG',
            'SV': 'GR', 'SV5': 'GR', 'SV9': 'GR', 'UR': 'UA', 'UA': 'RU', 'UA9': 'RU',
            'BY': 'CN', 'HL': 'KR', 'BV': 'TW', 'VR': 'HK', 'HS': 'TH', '9V': 'SG',
            'YB': 'ID', '9M2': 'MY', '9M6': 'MY', 'DU': 'PH', 'VU': 'IN', '4X': 'IL',
            'ZS': 'ZA', 'KL': 'US', 'KH6': 'US', 'KP4': 'PR', 'KP2': 'VI', 'KH2': 'GU',
            'KH0': 'MP', 'TI': 'CR', 'HP': 'PA', 'YS': 'SV', 'TG': 'GT', 'HR': 'HN',
            'YN': 'NI', 'ZF': 'KY', 'HI': 'DO', 'CO': 'CU', 'C6': 'BS', 'FP': 'PM',
            'TF': 'IS', 'OY': 'FO', 'OX': 'GL', 'ER': 'MD', 'EW': 'BY', 'ES': 'EE',
            'YL': 'LV', 'LY': 'LT', 'OE': 'AT', 'HB': 'CH', 'HB0': 'LI', 'CT': 'PT',
            'CU': 'PT', 'CT3': 'PT', 'TA': 'TR', 'EK': 'AM', '4L': 'GE', '4J': 'AZ',
            'UN': 'KZ', 'EX': 'KG', 'EY': 'TJ', 'UK': 'UZ', 'EZ': 'TM', 'JT': 'MN',
            'A6': 'AE', 'A7': 'QA', 'A9': 'BH', '9K': 'KW', 'HZ': 'SA', '7X': 'DZ',
            'CN': 'MA', '3V': 'TN', '5A': 'LY', 'SU': 'EG', '5Z': 'KE', '5N': 'NG'
        }
        if pfx in table:
            return table[pfx]
        m = re.match(r'^[A-Z]{1,2}', pfx)
        return m.group(0) if m else pfx

    def _load_cty(self, path: str):
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        records = content.split(';')
        for rec in records:
            rec = rec.strip()
            if not rec:
                continue
            lines = [line.strip() for line in rec.splitlines() if line.strip()]
            if not lines:
                continue
            parts = [p.strip() for p in lines[0].split(':')]
            if len(parts) < 8:
                continue
            name = parts[0]
            cq = int(parts[1]) if parts[1].isdigit() else 0
            itu = int(parts[2]) if parts[2].isdigit() else 0
            continent = parts[3]
            primary_pfx = parts[7]

            pfx_str = ' '.join(lines[1:])
            pfx_list = [p.strip() for p in pfx_str.split(',') if p.strip()]
            iso = self._map_to_iso(primary_pfx, name)

            ent = {
                'name': name,
                'primary_pfx': primary_pfx,
                'iso': iso,
                'continent': continent,
                'cq': cq,
                'itu': itu
            }
            self.entities[primary_pfx] = ent

            for p in pfx_list:
                clean_p = re.sub(r'[\(\[\<\{].*?[\)\]\>\}]', '', p).strip()
                if not clean_p:
                    continue
                if clean_p.startswith('='):
                    self.exact_map[clean_p[1:]] = ent
                else:
                    self.prefix_map[clean_p] = ent

    def resolve(self, call: str, grid: str) -> Dict[str, Any]:
        call_clean = call.strip().upper()
        # Handle portable strokes e.g. W4/G3XYZ or G3XYZ/W4 or K5JEE/P
        parts = call_clean.split('/') if '/' in call_clean else [call_clean]
        base_call = parts[0]
        portable_pfx = None
        if len(parts) > 1:
            if len(parts[0]) <= 3 and any(c.isdigit() for c in parts[0]):
                portable_pfx = parts[0]
                base_call = parts[1]
            elif len(parts[1]) <= 3 and any(c.isdigit() for c in parts[1]):
                portable_pfx = parts[1]
                base_call = parts[0]

        grid_4 = grid.strip().upper()[:4] if grid else ''

        # 1. Look up DXCC entity from exact or prefix
        ent = None
        if portable_pfx:
            ent = self.exact_map.get(portable_pfx) or self.prefix_map.get(portable_pfx)

        if not ent:
            ent = self.exact_map.get(call_clean) or self.exact_map.get(base_call)

        if not ent:
            lookup_call = portable_pfx if portable_pfx else base_call
            for length in range(len(lookup_call), 0, -1):
                p = lookup_call[:length]
                # Special ARRL / FCC rule for KG4:
                # Under FCC rules, KG4 with a 1-letter or 3-letter suffix is a mainland US station (4th district).
                # Only KG4 with exactly a 2-letter suffix (KG4xx) is Guantanamo Bay.
                if p == "KG4":
                    suf = lookup_call[3:]
                    if len(suf) in (1, 3) and suf.isalpha():
                        continue
                if p in self.prefix_map:
                    ent = self.prefix_map[p]
                    break

        country_name = ent['name'] if ent else 'Unknown'
        country_code = ent['iso'] if ent else ''
        primary_pfx = ent['primary_pfx'] if ent else ''

        # 2. Check North American State/Province mapping from grid
        grid_info = self.grids_map.get(grid_4)
        state_code = ''
        state_name = ''

        if grid_info:
            g_country = grid_info.get('country', '')
            g_state = grid_info.get('state', '')
            g_name = grid_info.get('name', '')
            # Grid squares in grids_na.json represent physical operating QTHs in US/Canada.
            # Override country/state if:
            # - Station was mapped to US or Canada
            # - Station has a US callsign or territory callsign (starts with K, W, N, AA-AL, including KG4, KP4, KH6, etc.)
            # - Station has a Canadian callsign (starts with VE, VA, VO, VY)
            # - Or country was undetermined
            is_us_call = re.match(r'^(K|W|N|A[A-L])[0-9A-Z]', base_call) is not None
            is_ca_call = re.match(r'^(VE|VA|VO|VY)[0-9A-Z]', base_call) is not None
            if country_code in ('US', 'CA', 'PR', 'VI', 'GU', 'MP', 'KG') or not country_code or is_us_call or is_ca_call:
                country_code = g_country
                country_name = 'United States' if g_country == 'US' else 'Canada'
                state_code = g_state
                state_name = g_name

        # Fallback for US states if grid wasn't in grids_map but call is US
        if country_code == 'US' and not state_code:
            if base_call.startswith(('KL', 'AL', 'NL', 'WL')):
                state_code, state_name = 'AK', 'Alaska'
            elif base_call.startswith(('KH6', 'AH6', 'NH6', 'WH6')):
                state_code, state_name = 'HI', 'Hawaii'
            elif base_call.startswith(('KP4', 'NP4', 'WP4')):
                state_code, state_name = 'PR', 'Puerto Rico'
            elif base_call.startswith(('KP2', 'NP2', 'WP2')):
                state_code, state_name = 'VI', 'Virgin Islands'

        # Location presentation fields
        if state_code:
            loc_code = state_code
            loc_sub = 'USA' if country_code == 'US' else ('CAN' if country_code == 'CA' else country_code)
            loc_full = f"{state_name}, {country_name}"
            is_dx = False
        else:
            loc_code = country_code or primary_pfx or 'DX'
            loc_sub = country_name
            loc_full = country_name
            is_dx = (country_code != 'US')

        continent = ent.get('continent', '') if ent else ''
        if not continent and country_code in ('US', 'CA', 'MX'):
            continent = 'NA'

        return {
            'country': country_name,
            'country_code': country_code,
            'state': state_name,
            'state_code': state_code,
            'continent': continent,
            'loc_code': loc_code,
            'loc_sub': loc_sub,
            'loc_full': loc_full,
            'is_dx': is_dx
        }

# -----------------------------------------------------------------------------
# Space Weather Service
# -----------------------------------------------------------------------------
class SpaceWeather:
    @staticmethod
    def fetch() -> Dict[str, Any]:
        url = "https://solar.w5mmw.net/"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) HamRadioTool/1.0"}
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                html = resp.read().decode("utf-8")
        except Exception as e:
            return {
                "source": "fallback (offline)",
                "solar_flux": 100.0,
                "sunspot_number": 75,
                "kp_index": 2.0,
                "a_index": 7,
                "geomagnetic_storm": "Quiet",
                "solar_wind_km_s": 400.0,
                "noise_floor": "S0-S1",
                "x_ray": "B1.0",
                "bands": {"30m-20m": {"day": "Good", "night": "Good"}},
                "updated_utc": datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            }

        res: Dict[str, Any] = {"source": "https://solar.w5mmw.net/"}
        rows = re.findall(
            r'<div class="cond_row"><dt>(.*?)</dt><dd class="cond_value">(.*?)</dd></div>',
            html
        )
        raw_map = {}
        for dt, val in rows:
            clean_dt = re.sub(r'<[^>]+>', '', dt).strip()
            clean_val = re.sub(r'<[^>]+>', '', val).strip()
            raw_map[clean_dt] = clean_val

        def parse_float(v, default=0.0):
            m = re.search(r'[-+]?\d*\.?\d+', str(v))
            return float(m.group(0)) if m else default

        def parse_int(v, default=0):
            m = re.search(r'[-+]?\d+', str(v))
            return int(m.group(0)) if m else default

        res["solar_flux"] = parse_float(raw_map.get("Solar Flux", 100))
        res["sunspot_number"] = parse_int(raw_map.get("Sunspot Number", 70))
        res["kp_index"] = parse_float(raw_map.get("Kp-Index", 2.0))
        res["a_index"] = parse_int(raw_map.get("A-Index", 5))
        res["geomagnetic_storm"] = raw_map.get("Geomagnetic Storm", "Quiet")
        res["solar_wind_km_s"] = parse_float(raw_map.get("Solar Wind", 400.0))
        res["noise_floor"] = raw_map.get("Noise Floor", "S0-S1")
        res["x_ray"] = raw_map.get("X-Ray", "B1.0")

        bands = {}
        band_rows = re.findall(
            r'<tr>\s*<th scope="row">([^<]+)</th>\s*<td><span[^>]*>([^<]+)</span></td>\s*<td><span[^>]*>([^<]+)</span></td>',
            html
        )
        for b, day, night in band_rows:
            bands[b.strip()] = {"day": day.strip(), "night": night.strip()}
        res["bands"] = bands
        res["updated_utc"] = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        return res

# -----------------------------------------------------------------------------
# RF & Propagation Math
# -----------------------------------------------------------------------------
def maidenhead_to_latlon(grid: str) -> Tuple[float, float]:
    grid = grid.strip().upper()
    lon = (ord(grid[0]) - ord('A')) * 20.0 - 180.0 + 10.0
    lat = (ord(grid[1]) - ord('A')) * 10.0 - 90.0 + 5.0
    if len(grid) >= 4:
        lon += (ord(grid[2]) - ord('0')) * 2.0 - 10.0 + 1.0
        lat += (ord(grid[3]) - ord('0')) * 1.0 - 5.0 + 0.5
    if len(grid) >= 6:
        lon += (ord(grid[4]) - ord('A')) * (2.0 / 24.0) - 1.0 + (1.0 / 24.0)
        lat += (ord(grid[5]) - ord('A')) * (1.0 / 24.0) - 0.5 + (0.5 / 24.0)
    return lat, lon

def great_circle(lat1: float, lon1: float, lat2: float, lon2: float) -> Tuple[float, float]:
    r_earth = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0)**2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0)**2
    dist_km = r_earth * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    bearing_deg = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    return dist_km, bearing_deg

def solar_elevation(lat_deg: float, lon_deg: float, dt_utc: datetime) -> float:
    day_of_year = dt_utc.timetuple().tm_yday
    utc_hours = dt_utc.hour + dt_utc.minute / 60.0 + dt_utc.second / 3600.0
    N = day_of_year + utc_hours / 24.0
    delta_deg = 23.44 * math.sin(math.radians((360.0 / 365.25) * (N - 80.0)))
    delta = math.radians(delta_deg)
    b_rad = math.radians((360.0 / 365.25) * (N - 81.0))
    e_min = 9.87 * math.sin(2.0 * b_rad) - 7.53 * math.cos(b_rad) - 1.5 * math.sin(b_rad)
    solar_time_hours = utc_hours + (lon_deg / 15.0) + (e_min / 60.0)
    hour_angle_deg = (solar_time_hours - 12.0) * 15.0
    lat_rad = math.radians(lat_deg)
    sin_elev = math.sin(lat_rad) * math.sin(delta) + math.cos(lat_rad) * math.cos(delta) * math.cos(math.radians(hour_angle_deg))
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_elev))))

def estimate_elevation(dist_km: float, virtual_height_km: float = 300.0) -> Tuple[float, int]:
    r_earth = 6371.0
    hops = max(1, math.ceil(dist_km / 3000.0))
    hop_dist = dist_km / hops
    psi = hop_dist / (2.0 * r_earth)
    tan_elev = (math.cos(psi) - (r_earth / (r_earth + virtual_height_km))) / math.sin(psi)
    elev_deg = math.degrees(math.atan(tan_elev))
    return max(1.0, min(90.0, elev_deg)), hops

def ft8_decode_probability(snr_db: float, snr_threshold: float = -21.0, slope: float = 0.8) -> float:
    margin = snr_db - snr_threshold
    return 1.0 / (1.0 + math.exp(-slope * margin))

# -----------------------------------------------------------------------------
# Global Land/Sea Mask & Ground Reflection Physics
# -----------------------------------------------------------------------------
LAND_MASK_B85 = "c-rlmy=xsu5WwejFK-Q&bxK@V5LxFgDO|h|h$Sq14-#;hKp?FJY0^cVokq98!AfPNOB2w)5J;0MT4RIKDP>aW2|-8^qO)c23C=g!y^p=wk9*c@QUotZobG-%H#;*sJ39h+J*JEa2Is*<iHT(8uXBtN@iEG=i?3!gRwku_F_M4rs{r?^`-xv!42ueuO7nC@f6zr&^M^0+tYRxI((Y00h+TwhS;DcE4C@2^Y{`b#1wTEDMa2@K6ZKvhSuo$b|I;C>EP|aeCi0G`Y&kJ>nFlE50CEG~ls|ILp(PpN!Hcz<(td^-%ax7raGL=mS{$bPH;UW!v(B&)tW2tD#Jp0Y34{}5cMH7ez+cw(iT}V_bY8tw#91yCQ1Io(q16{rkEjWWBizCG<k5W$WH8lt1u7Xk1jd6TsFlKyJvm^~v#l{QWrXAO7o;NseR;Bd7D=nt%1MjGrz!&JIQ+WM*Fm8i9yIZ~8!G_@TxR<64irmVerlr3fyZfv_89YAPvG&(5Tq7DPL1Q!w6xg-LJdC}NOP5}3GTv?D=o|QHGC)nC~B39q;%msAi9qTY2mz?U+lnZa_UlY(ZX_OqJa}zjZ~-rE8ee79fL^c^}<UwEcmO<D&j&YYV`#+tCR;n#FpU#SgoR9q1Ic?wFx*8j^pPh46vo1i~wpqq7Du=qtY^Qz21i{oQQTsiSWm;v_gn0LA(i5IZoRsT4mQZO)cUseKamVZekf$7joOmZ-7n+nF%i?=)$21%XqxfWGoLn*TTQG_iC^3BL}|JrGnO7?_Gy>omgYuOytThY^#>+M%;kFFY8<E6Z$R<BMLY7zK2eYyHuBC-i6yVm*Y$PyfmHS!1Ef;<yxoicGYOJ+-9+Via>*utvGP|u{`CEv0HFTy{I2fU<b1!BSIaxb$=d@e*T!u?=Xh2AC5+g`$Xg03kF>3J*|cT9uhcQfWU#<zj6k{2L!&cEGS$U@OYBK-~-~4<&46)2^VSSDCLr+lERs>wN5Hq5%`<8jl#qTfK0td;xrb<zRC4|)F{P<25j!#DxeveZj^pQ95^=j<1-6Ru`fX7c!u$CXc+PY5B#A4@LXy<G+eYNchVmCAF0Fwoh`<wv%jszw-UZi?T85`6S{4a@tuS(baj|=QMuPzk6G{{saIM!U2f=ioO&@deUGS0iu&qJ8!ke)YQrdvC=H%2+OUk_4I>0)NtjBQAr76Av0%$QFeE>%DjQa&6bqWAv{LV08a}EjWLa0ZlC#~Ys_Z)WSUO`O)lTLdn4`66;J&o4Twik4jNDo~GS?Vx%hfIml>$95M+@GOH)Nq8FmPe$(nbyM##7+JlDsEFf>*Hc)bW;NWnTr$;>JCVh&JpRYor@hIm+{3>>5u5C!dn2L)@z1@urSlIxe52B=~DRuE6*j3|z@@&?GQUmfl-ZJK!Gp*Cy#wc%*8KF$0fVn&nb<>r|RJgwS&~6K!rR>Y}Gv)JDN?B)ob-)@h~=meEl3!2!tzHM23C1pcF#0oM}{^Lf|<ha?-+FY=3Jxac_%nsBrBI4e(kVAjEHI*epWakb7naTH!C)_CZL13keucit-2;F(vA2H_-ku8Vg+Dw$7m^Knjc=PHaLi@k7TFanPx{3N+_K3MJBPmud`)+ZnhtFxdi_y?{BJur+v8^FHL-R*8)(w*djJapK}L-fIF3_Ky)y09?eVBm}A*2z+Ge64#D58Q0mIoX6oG(QbiSKXBUj+TAx5CTW5ofHq8n{c<kV{%IxkUNI%;4mgOy``Mv7&_80P&n~rnHDak!DI5p^xSY!g-qbWtbLyHh&n5PITsGKFbxY@0lS&8k_-mSJTa<^a|tsVx{(KtG#qOsg|DbmFe9+BD9#JNQgTL=<i0|%>XqqBX2P*2f6W93TF=nCC=4~aNs&-cjl9T}tb<FWM%nVa>%k0fYJkQVgTK&|b6H<!Q%zy+oG>WZ%pJva_<|+5x7+$fwFMaP^k%D>JBsP>l2z&U_k-onMH7yv!Pd^1_e&n@7SlP@Xd0h1i%#uR-<t&<^&UT7J<iuI*G*b`%=N=tv$XBEU<RevXOUdY0%x<Bj=%nI%YQV6GqB@X1TXeI@SUy}Oz+oeWaVDfnRW)Va(j0uOI`t2#}?gsbpmok(^qz?kgm68)0_HZ>#7SZ$E3fDQ#LF1PfcV-=iaxxJv~$kZwz5_oUZ06J@DG2(IeYCy+StV9yqkxyv*O6MN)+qW^)O#9~QKk->*asZyVjuyjO6g<1(kVDFcC1jiD#R3Xwn`9NCj+x&_fEnLa+k{?XG-?-P9(_d3BD>R$yZ{-4Lc9{FTT"
_LAND_MASK_BYTES: Optional[bytes] = None

def _get_land_mask_bytes() -> bytes:
    global _LAND_MASK_BYTES
    if _LAND_MASK_BYTES is None:
        _LAND_MASK_BYTES = zlib.decompress(base64.b85decode(LAND_MASK_B85))
    return _LAND_MASK_BYTES

def is_land_coord(lat: float, lon: float) -> bool:
    """Returns True if (lat, lon) is on land according to the 1-degree resolution mask."""
    mask = _get_land_mask_bytes()
    lat_i = int(round(89.5 - max(-89.5, min(89.5, lat))))
    lon_i = int(round((lon + 180.0) % 360.0 - 0.5)) % 360
    idx = lat_i * 360 + lon_i
    byte_idx = idx // 8
    bit_idx = idx % 8
    return bool(mask[byte_idx] & (1 << bit_idx))

def classify_ground(lat: float, lon: float) -> Tuple[str, str]:
    """Classifies Earth surface type at (lat, lon) into (type, label)."""
    if lat <= -60.0 or lat >= 75.0 or (lat >= 60.0 and -55.0 <= lon <= -20.0):
        return "ICE", "Polar Ice Sheet"
    if not is_land_coord(lat, lon):
        return "SEA", "Sea Water"
    if 15.0 <= lat <= 32.0 and -15.0 <= lon <= 55.0:
        return "ARID", "Desert / Dry Sand"
    if -32.0 <= lat <= -18.0 and 115.0 <= lon <= 142.0:
        return "ARID", "Arid Scrub / Rock"
    if 25.0 <= lat <= 42.0 and -118.0 <= lon <= -102.0:
        return "ARID", "Arid / Desert"
    return "LAND", "Pastoral / Soil"

def ground_reflection_loss(surface_type: str, elev_deg: float, freq_mhz: float = 14.074) -> float:
    """Calculates Fresnel reflection loss in dB for mixed polarization at grazing angle elev_deg."""
    if surface_type == "SEA":
        sigma = 5.0
        eps_r = 80.0
    elif surface_type == "ARID":
        sigma = 0.001
        eps_r = 4.0
    elif surface_type == "ICE":
        sigma = 0.0001
        eps_r = 3.0
    else:  # LAND
        sigma = 0.005
        eps_r = 13.0

    psi = math.radians(max(1.0, min(89.0, elev_deg)))
    f = max(1.0, freq_mhz)
    eta = eps_r - 1j * (1.8e4 * sigma / f)

    sin_psi = math.sin(psi)
    cos_psi = math.cos(psi)
    sq = cmath.sqrt(eta - cos_psi**2)

    r_h = (sin_psi - sq) / (sin_psi + sq)
    r_v = (eta * sin_psi - sq) / (eta * sin_psi + sq)

    p_eff = (abs(r_h)**2 + abs(r_v)**2) / 2.0
    p_eff = max(0.001, min(0.999, p_eff))
    loss_db = -10.0 * math.log10(p_eff)
    return round(loss_db, 1)

def intermediate_point(lat1: float, lon1: float, lat2: float, lon2: float, f: float) -> Tuple[float, float]:
    """Calculates intermediate point at fraction f along great circle between two lat/lon coordinates."""
    if f <= 0.0:
        return lat1, lon1
    if f >= 1.0:
        return lat2, lon2
    p1, l1 = math.radians(lat1), math.radians(lon1)
    p2, l2 = math.radians(lat2), math.radians(lon2)
    d = 2.0 * math.asin(math.sqrt(
        math.sin((p2 - p1) / 2.0)**2 +
        math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2.0)**2
    ))
    if d == 0.0:
        return lat1, lon1
    a = math.sin((1.0 - f) * d) / math.sin(d)
    b = math.sin(f * d) / math.sin(d)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    lat_f = math.atan2(z, math.sqrt(x * x + y * y))
    lon_f = math.atan2(y, x)
    return math.degrees(lat_f), (math.degrees(lon_f) + 180.0) % 360.0 - 180.0

def latlon_to_maidenhead(lat: float, lon: float, precision: int = 4) -> str:
    """Converts lat/lon to Maidenhead grid square string."""
    adj_lon = (lon + 180.0) % 360.0
    adj_lat = max(-90.0, min(90.0, lat)) + 90.0
    field_lon = chr(ord('A') + int(adj_lon / 20.0))
    field_lat = chr(ord('A') + min(17, int(adj_lat / 10.0)))
    sq_lon = str(int((adj_lon % 20.0) / 2.0))
    sq_lat = str(int((adj_lat % 10.0) / 1.0))
    res = f"{field_lon}{field_lat}{sq_lon}{sq_lat}"
    if precision >= 6:
        rem_lon = (adj_lon % 20.0) % 2.0
        rem_lat = (adj_lat % 10.0) % 1.0
        sub_lon = chr(ord('A') + int(rem_lon / (2.0 / 24.0)))
        sub_lat = chr(ord('A') + int(rem_lat / (1.0 / 24.0)))
        res += f"{sub_lon}{sub_lat}"
    return res

def analyze_ray_path(
    lat1: float, lon1: float, lat2: float, lon2: float,
    dist_km: float, freq_mhz: float, dt_utc: datetime
) -> Dict[str, Any]:
    """Calculates multi-hop ray path geometry, ionospheric heights, and intermediate ground bounces."""
    hops = max(1, math.ceil(dist_km / 3000.0))
    hop_km = round(dist_km / hops)

    iono_hops = []
    total_hv = 0.0
    for i in range(1, hops + 1):
        f_iono = (2 * i - 1) / (2.0 * hops)
        h_lat, h_lon = intermediate_point(lat1, lon1, lat2, lon2, f_iono)
        sun_el = solar_elevation(h_lat, h_lon, dt_utc)
        state = "DAY" if sun_el > 0 else ("TWILIGHT" if sun_el > -12 else "NIGHT")
        hv = 280.0 if state == "DAY" else (310.0 if state == "TWILIGHT" else 350.0)
        total_hv += hv
        iono_hops.append({
            "hop": i,
            "lat": round(h_lat, 2),
            "lon": round(h_lon, 2),
            "sun_el": round(sun_el, 1),
            "state": state,
            "hv_km": round(hv)
        })

    avg_hv_km = total_hv / hops
    el_deg, _ = estimate_elevation(dist_km, virtual_height_km=avg_hv_km)

    bounces = []
    total_ground_loss = 0.0
    for k in range(1, hops):
        f_gnd = k / float(hops)
        b_lat, b_lon = intermediate_point(lat1, lon1, lat2, lon2, f_gnd)
        g_type, g_label = classify_ground(b_lat, b_lon)
        loss = ground_reflection_loss(g_type, el_deg, freq_mhz)
        total_ground_loss += loss
        bounces.append({
            "idx": k,
            "lat": round(b_lat, 2),
            "lon": round(b_lon, 2),
            "grid": latlon_to_maidenhead(b_lat, b_lon),
            "type": g_type,
            "label": g_label,
            "loss_db": round(loss, 1)
        })

    fspl = 0.0
    if dist_km > 0 and freq_mhz > 0:
        fspl = 32.44 + 20.0 * math.log10(dist_km) + 20.0 * math.log10(freq_mhz)

    return {
        "hops": hops,
        "hop_km": hop_km,
        "el": round(el_deg, 1),
        "avg_hv_km": round(avg_hv_km),
        "iono_hops": iono_hops,
        "bounces": bounces,
        "total_ground_loss_db": round(total_ground_loss, 1),
        "fspl_db": round(fspl, 1)
    }


# -----------------------------------------------------------------------------
# Directed CQ Target Constants & Matching
# -----------------------------------------------------------------------------
CONTINENT_CODES = {"NA", "SA", "EU", "AS", "AF", "OC", "AN"}

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC"
}

CA_PROVINCES = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE",
    "QC", "SK", "YT"
}

OPEN_CQ_MODIFIERS = {
    "TEST", "FD", "FIELD", "POTA", "SOTA", "IOTA", "BOTA", "WW",
    "QRP", "LP", "SP", "UP", "DOWN", "QSX", "VHF", "UHF", "EME",
    "SAT", "ROVER", "PORT", "MOBILE", "MM", "AM", "CONTEST",
    "RTTY", "FT8", "FT4", "SKCC", "NAQP", "CWT", "WPX", "ARRL",
    "CQWW", "WWDX", "DXP", "EXP", "SPEC", "R", "RR73", "73"
}

US_CALL_DISTRICTS = {
    "1": {"CT", "ME", "MA", "NH", "RI", "VT"},
    "2": {"NJ", "NY"},
    "3": {"DE", "MD", "PA", "DC"},
    "4": {"AL", "FL", "GA", "KY", "NC", "SC", "TN", "VA"},
    "5": {"AR", "LA", "MS", "NM", "OK", "TX"},
    "6": {"CA"},
    "7": {"AZ", "ID", "MT", "NV", "OR", "UT", "WA", "WY", "AK"},
    "8": {"MI", "OH", "WV"},
    "9": {"IL", "IN", "WI"},
    "0": {"CO", "IA", "KS", "MN", "MO", "NE", "ND", "SD"}
}

def is_directed_cq_eligible(
    target: Optional[str],
    caller_call: str,
    caller_grid: str,
    my_call: str,
    my_grid: str,
    loc_resolver: Optional[LocationResolver]
) -> Tuple[bool, str]:
    """
    Checks if our station matches a CQ modifier / directive.
    Returns (is_eligible, reason_category).
    If False, our station should not answer or see this CQ candidate.
    """
    if not target:
        return True, "OPEN"

    t = target.strip().upper()
    if not t or t in OPEN_CQ_MODIFIERS:
        return True, "OPEN"

    # Split audio frequency or slot offset e.g. "CQ 2400", "CQ 999"
    if t.isdigit() and len(t) >= 2:
        return True, "SPLIT"

    if not loc_resolver:
        return True, "OPEN"

    my_loc = loc_resolver.resolve(my_call, my_grid) if my_call else {}
    my_country_code = (my_loc.get("country_code") or "").upper()
    my_state_code = (my_loc.get("state_code") or "").upper()
    my_continent = (my_loc.get("continent") or "").upper()
    if not my_continent and my_country_code in ("US", "CA", "MX"):
        my_continent = "NA"

    caller_loc = loc_resolver.resolve(caller_call, caller_grid) if caller_call else {}
    caller_country_code = (caller_loc.get("country_code") or "").upper()
    caller_state_code = (caller_loc.get("state_code") or "").upper()
    caller_continent = (caller_loc.get("continent") or "").upper()
    if not caller_continent and caller_country_code in ("US", "CA", "MX"):
        caller_continent = "NA"

    # 1. CQ DX
    if t == "DX":
        # Cannot be same country
        if caller_country_code and my_country_code and caller_country_code == my_country_code:
            return False, "SAME_COUNTRY"
        # In North America, US and Canada stations calling CQ DX exclude W/VE
        if caller_country_code in ("US", "CA") and my_country_code in ("US", "CA"):
            return False, "DOMESTIC_NA"
        # Cannot be same continent
        if caller_continent and my_continent and caller_continent == my_continent:
            return False, "SAME_CONTINENT"
        return True, "DX"

    # 2. Continent directed: CQ NA, CQ EU, CQ AS, CQ OC, CQ AF, CQ SA, CQ AN
    if t in CONTINENT_CODES:
        if my_continent == t:
            return True, f"CONTINENT_{t}"
        return False, f"CONTINENT_{t}_MISMATCH"

    # 3. US State / Canadian Province directed
    if t == "CA":
        if my_state_code == "CA" or my_country_code == "CA":
            return True, "STATE_CA"
        return False, "STATE_CA_MISMATCH"

    if t in US_STATES:
        if my_state_code == t:
            return True, f"STATE_{t}"
        return False, f"STATE_{t}_MISMATCH"

    if t in CA_PROVINCES:
        if my_state_code == t:
            return True, f"PROV_{t}"
        return False, f"PROV_{t}_MISMATCH"

    # 4. US Call District directed: CQ 0 through CQ 9
    if t in US_CALL_DISTRICTS:
        m = re.search(r'\d', my_call) if my_call else None
        call_digit = m.group(0) if m else ""
        state_match = my_state_code in US_CALL_DISTRICTS[t]
        if call_digit == t or state_match:
            return True, f"DISTRICT_{t}"
        return False, f"DISTRICT_{t}_MISMATCH"

    # 5. Maidenhead Grid square (e.g. "CQ EL09") or field ("CQ EL")
    if len(t) == 4 and re.match(r'^[A-R]{2}[0-9]{2}$', t):
        if my_grid and my_grid.upper().startswith(t):
            return True, f"GRID_{t}"
        return False, f"GRID_{t}_MISMATCH"
    if len(t) == 2 and re.match(r'^[A-R]{2}$', t) and my_grid and my_grid.upper().startswith(t):
        return True, f"GRID_{t}"

    # 6. Country / DXCC Entity directed
    country_aliases = {
        "USA": "US", "US": "US", "K": "US", "W": "US",
        "CAN": "CA", "VE": "CA",
        "MEX": "MX", "XE": "MX",
        "UK": "GB", "GB": "GB", "G": "GB",
        "JA": "JP", "JP": "JP",
        "VK": "AU", "AU": "AU",
        "ZL": "NZ", "NZ": "NZ",
        "DL": "DE", "DE": "DE",
        "F": "FR", "FR": "FR",
        "I": "IT", "IT": "IT",
        "EA": "ES", "ES": "ES"
    }
    target_iso = country_aliases.get(t)
    if not target_iso and loc_resolver:
        ent = loc_resolver.prefix_map.get(t) or loc_resolver.exact_map.get(t)
        if ent:
            target_iso = ent.get("iso", "")

    if target_iso:
        if my_country_code == target_iso:
            return True, f"COUNTRY_{target_iso}"
        return False, f"COUNTRY_{target_iso}_MISMATCH"

    # Unrecognized modifier: treat as open contest/special event (e.g. CQ SKCC)
    return True, "UNKNOWN_OPEN"

def extract_call_and_grid(message: str) -> Tuple[Optional[str], Optional[str], bool, Optional[str]]:
    """Extracts callsign, 4-char grid square, is_cq flag, and directed cq_target from FT8 messages."""
    msg = message.strip()
    tokens = msg.split()
    # CQ forms:
    # "CQ K7DUR DM42" -> len=3, tokens[0]="CQ", call=tokens[1], grid=tokens[2], target=None
    # "CQ DX KD9XX EN52" -> len>=4, tokens[0]="CQ", target=tokens[1], call=tokens[2], grid=tokens[3]
    # "CQ TX K7DUR DM42" -> len>=4, tokens[0]="CQ", target=tokens[1], call=tokens[2], grid=tokens[3]
    # "QRZ K7DUR DM42" -> len=3, tokens[0]="QRZ", call=tokens[1], grid=tokens[2], target=None
    # Non-CQ: "K6VVK W7AJP DM09" -> len=3, target=None, is_cq=False
    if len(tokens) >= 3 and tokens[0] in ("CQ", "QRZ"):
        if len(tokens) >= 4 and re.match(r'^[A-R]{2}[0-9]{2}$', tokens[3].upper()) and tokens[3].upper() != "RR73":
            target = tokens[1].upper() if tokens[0] == "CQ" else None
            call = tokens[2]
            grid = tokens[3].upper()
            return call, grid, True, target
        elif re.match(r'^[A-R]{2}[0-9]{2}$', tokens[2].upper()) and tokens[2].upper() != "RR73":
            call = tokens[1]
            grid = tokens[2].upper()
            return call, grid, True, None
    elif len(tokens) >= 3:
        # Station-to-station exchange: e.g. K6VVK W7AJP DM09
        call = tokens[1]
        grid = tokens[2].upper()
        if re.match(r'^[A-R]{2}[0-9]{2}$', grid) and grid != "RR73":
            return call, grid, False, None
    return None, None, False, None

# -----------------------------------------------------------------------------
# PSK Reporter Ground-Truth Propagation Service
# -----------------------------------------------------------------------------
class PskReporterManager:
    """
    Fetches and caches ground-truth FT8 reception reports from PSK Reporter.
    Provides two-way empirical link margin verification and regional path validation.
    """
    def __init__(self, callsign: str = "K5JEE", min_interval_s: float = 300.0):
        self.callsign = callsign.upper()
        self.min_interval_s = min_interval_s
        self.last_poll_time = 0.0
        self.last_poll_status = "idle"
        self.last_poll_error = ""
        self.lock = threading.RLock()
        self.next_retry_time = 0.0  # Exponential backoff for 429
        self.backoff_factor = 60.0  # Start at 60s, double each time
        
        self.total_spots = 0
        self.last_update_utc = ""
        
        # Indexed lookups:
        # (receiver_call, band) -> spot dict
        self.by_call_band: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # receiver_call -> most recent spot dict across all bands
        self.by_call_latest: Dict[str, Dict[str, Any]] = {}
        # (band, grid[:4]) -> {"count": int, "avg_snr": float, "max_snr": int, "min_snr": int}
        self.by_band_grid4: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # (band, grid[:2]) -> {"count": int, "avg_snr": float, "max_snr": int, "min_snr": int}
        self.by_band_grid2: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # band -> spot count
        self.band_counts: Dict[str, int] = {}
        
    def should_poll(self) -> bool:
        return (time.time() - self.last_poll_time) >= self.min_interval_s and time.time() >= self.next_retry_time

    def fetch_reports(self, callsign: Optional[str] = None) -> bool:
        target_call = (callsign or self.callsign).strip().upper()
        if not target_call:
            return False
            
        url = (
            f"https://retrieve.pskreporter.info/query"
            f"?senderCallsign={target_call}"
            f"&flowStartSeconds=-3600"
            f"&statistics=1"
        )
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "WSJTX-QSO-Companion/1.0 (Amateur Radio QSO Success Estimator)"}
        )
        
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            # Reset backoff on success
            self.backoff_factor = 1.0
            self.next_retry_time = 0.0
        except HTTPError as he:
            with self.lock:
                self.last_poll_status = "error"
                self.last_poll_error = f"HTTP {he.code}: {he.reason}"
            if he.code == 429:
                retry_after = he.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = max(int(retry_after), self.backoff_factor)
                    except ValueError:
                        wait = self.backoff_factor
                else:
                    wait = self.backoff_factor
                self.backoff_factor = min(self.backoff_factor * 2, 3600)
                self.next_retry_time = time.time() + wait
                print(f"[PSKReporter] Rate limited (429) for {target_call}, backing off {wait}s")
            else:
                print(f"[PSKReporter] Query failed for {target_call}: HTTP {he.code}: {he.reason}")
            return False
        except Exception as e:
            with self.lock:
                self.last_poll_status = "error"
                self.last_poll_error = str(e)
            print(f"[PSKReporter] Query failed for {target_call}: {e}")
            return False

        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            with self.lock:
                self.last_poll_status = "parse_error"
                self.last_poll_error = str(e)
            print(f"[PSKReporter] XML parse error: {e}")
            return False

        # Extract reception reports from XML
        reports_raw = list(root.findall("receptionReport"))
        now = time.time()
        
        new_by_call_band: Dict[Tuple[str, str], Dict[str, Any]] = {}
        new_by_call_latest: Dict[str, Dict[str, Any]] = {}
        grid4_acc: Dict[Tuple[str, str], List[int]] = {}
        grid2_acc: Dict[Tuple[str, str], List[int]] = {}
        new_band_counts: Dict[str, int] = {}

        for r in reports_raw:
            rcall = str(r.get("receiverCallsign", "")).strip().upper()
            if not rcall:
                continue
            rgrid = str(r.get("receiverLocator", "")).strip().upper()
            try:
                freq_hz = float(r.get("frequency", 0) or 0)
            except (ValueError, TypeError):
                freq_hz = 0.0
            try:
                snr = int(r.get("sNR", -99) or -99)
            except (ValueError, TypeError):
                snr = -99
            try:
                flow_sec = int(r.get("flowStartSeconds", 0) or 0)
            except (ValueError, TypeError):
                flow_sec = int(now)

            band = get_band_name(freq_hz / 1e6) if freq_hz > 0 else "unknown"
            new_band_counts[band] = new_band_counts.get(band, 0) + 1

            spot = {
                "call": rcall,
                "grid": rgrid,
                "snr": snr,
                "freq_hz": freq_hz,
                "band": band,
                "flow_sec": flow_sec,
                "age_min": max(0, round((now - flow_sec) / 60.0))
            }

            # Keyed by (call, band) - keep most recent
            call_band_key = (rcall, band)
            if call_band_key not in new_by_call_band or flow_sec > new_by_call_band[call_band_key]["flow_sec"]:
                new_by_call_band[call_band_key] = spot

            # Keyed by call - keep most recent across all bands
            if rcall not in new_by_call_latest or flow_sec > new_by_call_latest[rcall]["flow_sec"]:
                new_by_call_latest[rcall] = spot

            # Regional accumulation
            if len(rgrid) >= 4:
                g4 = (band, rgrid[:4])
                grid4_acc.setdefault(g4, []).append(snr)
            if len(rgrid) >= 2:
                g2 = (band, rgrid[:2])
                grid2_acc.setdefault(g2, []).append(snr)

        # Summarize regional grids
        new_grid4: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for (b, g4), snrs in grid4_acc.items():
            new_grid4[(b, g4)] = {
                "count": len(snrs),
                "avg_snr": round(sum(snrs) / len(snrs), 1),
                "max_snr": max(snrs),
                "min_snr": min(snrs)
            }

        new_grid2: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for (b, g2), snrs in grid2_acc.items():
            new_grid2[(b, g2)] = {
                "count": len(snrs),
                "avg_snr": round(sum(snrs) / len(snrs), 1),
                "max_snr": max(snrs),
                "min_snr": min(snrs)
            }

        with self.lock:
            self.callsign = target_call
            self.total_spots = len(reports_raw)
            self.last_poll_time = now
            self.last_poll_status = "ok"
            self.last_poll_error = ""
            self.last_update_utc = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            self.by_call_band = new_by_call_band
            self.by_call_latest = new_by_call_latest
            self.by_band_grid4 = new_grid4
            self.by_band_grid2 = new_grid2
            self.band_counts = new_band_counts

        print(f"[PSKReporter] Updated for {target_call}: {len(reports_raw)} spots across {len(new_band_counts)} bands ({dict(new_band_counts)})")
        return True

    def lookup(self, call: str, grid: str, current_band: str) -> Dict[str, Any]:
        c = call.strip().upper()
        g = grid.strip().upper()
        b = current_band.strip().lower()

        with self.lock:
            # 1. Exact direct match on active band
            direct_band = self.by_call_band.get((c, b))
            if direct_band:
                return {
                    "match": "direct_band",
                    "snr": direct_band["snr"],
                    "band": direct_band["band"],
                    "age_min": direct_band["age_min"],
                    "grid": direct_band["grid"]
                }

            # 2. Exact direct match on another band
            direct_any = self.by_call_latest.get(c)
            if direct_any:
                return {
                    "match": "direct_other",
                    "snr": direct_any["snr"],
                    "band": direct_any["band"],
                    "age_min": direct_any["age_min"],
                    "grid": direct_any["grid"]
                }

            # 3. 4-character Maidenhead grid match on current band
            if len(g) >= 4:
                g4 = g[:4]
                reg4 = self.by_band_grid4.get((b, g4))
                if reg4:
                    return {
                        "match": "grid4",
                        "grid": g4,
                        "spots": reg4["count"],
                        "avg_snr": reg4["avg_snr"],
                        "max_snr": reg4["max_snr"]
                    }

            # 4. 2-character Maidenhead field match on current band
            if len(g) >= 2:
                g2 = g[:2]
                reg2 = self.by_band_grid2.get((b, g2))
                if reg2:
                    return {
                        "match": "grid2",
                        "grid": g2,
                        "spots": reg2["count"],
                        "avg_snr": reg2["avg_snr"],
                        "max_snr": reg2["max_snr"]
                    }

            return {"match": "none"}

    def get_stats(self) -> Dict[str, Any]:
        with self.lock:
            now = time.time()
            age_s = max(0, int(now - self.last_poll_time)) if self.last_poll_time > 0 else None
            age_str = f"{round(age_s / 60)}m ago" if age_s is not None else "never"
            return {
                "callsign": self.callsign,
                "total_spots": self.total_spots,
                "last_update_utc": self.last_update_utc,
                "last_poll_time": self.last_poll_time,
                "age_seconds": age_s,
                "age_str": age_str,
                "status": self.last_poll_status,
                "band_counts": dict(self.band_counts)
            }

# -----------------------------------------------------------------------------
# Application State
# -----------------------------------------------------------------------------
class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.wsjt_connected = False
        self.last_heartbeat_time = 0.0
        self.dial_freq_hz = 14074000
        self.mode = "FT8"
        self.de_call = DEFAULT_MY_CALL
        self.de_grid = DEFAULT_HOME_GRID
        self.dx_call = ""
        self.dx_grid = ""
        self.transmitting = False
        self.decoding = False
        self.tx_enabled = False
        self.rx_df = 1500
        self.tx_df = 1500
        self.tx_freq_optimize = DEFAULT_TX_FREQ_OPTIMIZE
        self.last_optimized_rx_df = 0
        self.last_tx_optimize_send_time = 0.0
        self.passband_decodes: List[Dict[str, Any]] = []
        self.cq_only = DEFAULT_CQ_ONLY
        self.hide_worked = DEFAULT_HIDE_WORKED
        self.psk_reporter = DEFAULT_PSK_REPORTER
        self.wsjt_client_id = "WSJT-X"
        self.wsjt_remote_addr: Optional[Tuple[str, int]] = None
        
        self.solar: Dict[str, Any] = {}
        self.decodes: Dict[str, Dict[str, Any]] = {} # keyed by callsign
        self.pattern_manager: Optional[AntennaPatternManager] = None
        self.log_watcher: Optional[WsjtxLogWatcher] = None
        self.loc_resolver: LocationResolver = LocationResolver()
        self.psk_manager: Optional[PskReporterManager] = None
        self.qso_tracker: Optional[QsoTracker] = None

state = State()

class QsoTracker:
    """Tracks QSO attempts and successful completions for scoring analysis."""
    def __init__(self, log_path: Optional[str] = None, history_path: Optional[str] = None):
        self.log_path = log_path or self._detect_log_path()
        self.history_path = history_path or os.path.join(
            os.path.dirname(self.log_path) if self.log_path else ".",
            "qso_history.json"
        )
        self.attempts: Dict[str, Dict[str, Any]] = {}  # call -> attempt record
        self.successes: List[Dict[str, Any]] = []       # completed QSOs with scores
        self.last_check_mtime = 0.0
        self._load_history()

    def _detect_log_path(self) -> str:
        candidates = [
            os.environ.get("WSJTX_LOG_PATH", ""),
            "/wsjtx-data/wsjtx.log",
            os.path.expanduser("~/.local/share/WSJT-X/wsjtx.log"),
        ]
        for c in candidates:
            if c and os.path.exists(c):
                return c
        return "/wsjtx-data/wsjtx.log"

    def _load_history(self):
        try:
            if os.path.exists(self.history_path):
                with open(self.history_path, "r") as f:
                    data = json.load(f)
                    self.successes = data.get("successes", [])
                    print(f"[QsoTracker] Loaded {len(self.successes)} historical QSOs from {self.history_path}")
        except Exception as e:
            print(f"[QsoTracker] Error loading history: {e}")

    def save_history(self):
        try:
            with open(self.history_path, "w") as f:
                json.dump({"successes": self.successes}, f, indent=2)
        except Exception as e:
            print(f"[QsoTracker] Error saving history: {e}")

    def record_attempt(self, call: str, score_data: Dict[str, Any], snr: int):
        """Record a QSO attempt when the user clicks CALL."""
        self.attempts[call] = {
            "time": time.time(),
            "snr": snr,
            "score": score_data.get("score", 0),
            "gain": score_data.get("gain", 0),
            "delta_from_peak_dbi": score_data.get("delta_from_peak_dbi", 0),
            "rx_margin": score_data.get("rx_margin", 0),
            "rec": score_data.get("rec", ""),
            "grid": score_data.get("grid", ""),
            "dist_km": score_data.get("dist_km", 0),
            "az": score_data.get("az", 0),
            "el": score_data.get("el", 0),
        }

    def check_completions(self, log_watcher: Optional[WsjtxLogWatcher]):
        """Check wsjtx.log for newly completed QSOs matching our attempts."""
        if not log_watcher:
            return
        log_watcher.reload_if_changed()
        for call, attempt in list(self.attempts.items()):
            # Check if this call appeared in the log since our attempt
            qsos = log_watcher.worked_any.get(call, [])
            for qso in qsos:
                qso_date = qso.get("date", "")
                qso_time_str = qso.get("time", "")
                if qso_date and qso_time_str:
                    try:
                        qso_ts = datetime.strptime(
                            f"{qso_date} {qso_time_str}", "%Y/%m/%d %H:%M"
                        ).replace(tzinfo=timezone.utc).timestamp()
                        # If the QSO happened after our attempt, mark success
                        if qso_ts >= attempt["time"] - 300:  # within 5 min window
                            success = {
                                **attempt,
                                "completed_at": qso_ts,
                                "completed_date": qso_date,
                                "completed_time": qso_time_str,
                                "band": qso.get("band", ""),
                                "freq": qso.get("freq", 0),
                                "mode": qso.get("mode", ""),
                                "grid": qso.get("grid", attempt.get("grid", "")),
                            }
                            # Avoid duplicates
                            if not any(s.get("completed_at") == qso_ts and s.get("call") == call
                                      for s in self.successes):
                                self.successes.append(success)
                                print(f"[QsoTracker] QSO confirmed: {call} ({qso_date} {qso_time_str})")
                            # Remove from attempts
                            del self.attempts[call]
                            self.save_history()
                            break
                    except (ValueError, TypeError):
                        pass

    def get_stats(self) -> Dict[str, Any]:
        """Return summary stats for tuning weights."""
        if not self.successes:
            return {"total": 0}
        gains = [s["gain"] for s in self.successes if "gain" in s]
        snrs = [s["snr"] for s in self.successes if "snr" in s]
        deltas = [s["delta_from_peak_dbi"] for s in self.successes if "delta_from_peak_dbi" in s]
        return {
            "total": len(self.successes),
            "avg_gain": round(sum(gains) / len(gains), 2) if gains else 0,
            "avg_snr": round(sum(snrs) / len(snrs), 1) if snrs else 0,
            "avg_delta_from_peak": round(sum(deltas) / len(deltas), 2) if deltas else 0,
        }


# -----------------------------------------------------------------------------
# WSJT-X UDP Serialization & Listener
# -----------------------------------------------------------------------------
def decode_utf8(data: bytes, offset: int) -> Tuple[str, int]:
    if offset + 4 > len(data):
        return "", offset
    length = struct.unpack_from(">I", data, offset)[0]
    offset += 4
    if length == 0xffffffff or offset + length > len(data):
        return "", offset
    s = data[offset:offset+length].decode("utf-8", errors="replace")
    offset += length
    return s, offset

def encode_utf8(s: Optional[str]) -> bytes:
    if s is None:
        return struct.pack(">I", 0xffffffff)
    b = s.encode("utf-8")
    return struct.pack(">I", len(b)) + b

def build_wsjt_reply_packet(target_id: str, qtime_ms: int, snr: int, delta_time: float,
                            delta_freq: int, mode: str, message: str, low_conf: bool = False,
                            modifiers: int = 0) -> bytes:
    magic = 0xadbccbda
    schema = 3
    msg_type = 4 # Reply
    pkt = struct.pack(">III", magic, schema, msg_type)
    pkt += encode_utf8(target_id)
    pkt += struct.pack(">I", qtime_ms)
    pkt += struct.pack(">i", snr)
    pkt += struct.pack(">d", float(delta_time))
    pkt += struct.pack(">I", delta_freq)
    pkt += encode_utf8(mode)
    pkt += encode_utf8(message)
    pkt += struct.pack("?B", low_conf, modifiers)
    return pkt

def build_wsjt_configure_packet(target_id: str, rx_df: int) -> bytes:
    magic = 0xadbccbda
    schema = 3
    msg_type = 15 # Configure
    pkt = struct.pack(">III", magic, schema, msg_type)
    pkt += encode_utf8(target_id)
    pkt += encode_utf8("") # mode: no change
    pkt += struct.pack(">I", 0xffffffff) # freq tol: no change
    pkt += encode_utf8("") # submode: no change
    pkt += struct.pack("?", False) # fast mode
    pkt += struct.pack(">I", 0xffffffff) # T/R period: no change
    pkt += struct.pack(">I", int(rx_df)) # rx_df
    pkt += encode_utf8("") # dx call: no change
    pkt += encode_utf8("") # dx grid: no change
    pkt += struct.pack("?", False) # generate-messages
    return pkt

def send_wsjt_configure_rx_df(rx_df: int) -> bool:
    try:
        with state.lock:
            target_addr = state.wsjt_remote_addr or ("127.0.0.1", 2237)
            client_id = state.wsjt_client_id or "WSJT-X"
        pkt = build_wsjt_configure_packet(client_id, rx_df)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(pkt, target_addr)
        if target_addr != ("127.0.0.1", 2237):
            try:
                sock.sendto(pkt, ("127.0.0.1", 2237))
            except Exception:
                pass
        sock.close()
        print(f"[Configure] Sent Configure Rx DF={rx_df}Hz (ID: {client_id}) to {target_addr}")
        return True
    except Exception as e:
        print(f"[Configure] Error sending Configure: {e}")
        return False

def analyze_passband(current_tx_df: int, min_hz: int = 300, max_hz: int = 2950, step_hz: int = 10) -> Dict[str, Any]:
    now = time.time()
    with state.lock:
        state.passband_decodes = [d for d in state.passband_decodes if (now - d["timestamp"]) <= 60.0]
        recent = list(state.passband_decodes)
        tx_optimize_enabled = state.tx_freq_optimize
        last_opt = state.last_optimized_rx_df
        last_send = getattr(state, "last_tx_optimize_send_time", 0.0)
        target_addr = state.wsjt_remote_addr

    effective_tx = current_tx_df if current_tx_df > 0 else 1500

    # Check if current_tx has any decode within 35 Hz in the last 60s
    current_tx_clear = True
    for d in recent:
        if abs(d["df"] - effective_tx) < 35:
            current_tx_clear = False
            break

    # Build candidate pool: descending from 2950 Hz down to 300 Hz
    cand_set = set(range(max_hz, min_hz - 1, -step_hz))
    
    # Also evaluate the exact midpoints of gaps between adjacent active signals
    if recent:
        recent_dfs = sorted(set(d["df"] for d in recent))
        for s1, s2 in zip(recent_dfs[:-1], recent_dfs[1:]):
            if s2 > min_hz and s1 < max_hz:
                mid = int(round((s1 + s2) / 2))
                if min_hz <= mid <= max_hz:
                    cand_set.add(mid)

    candidates = sorted(cand_set, reverse=True)
    scored = []

    for f in candidates:
        if recent:
            min_dist = min(abs(d["df"] - f) for d in recent)
        else:
            min_dist = 500.0

        # Tier 2: completely clean free slot (min_dist >= 35 Hz -> clearance >= 70 Hz)
        # Tier 1: marginal gap (min_dist >= 25 Hz)
        # Tier 0: collision (< 25 Hz)
        if min_dist >= 35.0:
            tier = 2
        elif min_dist >= 25.0:
            tier = 1
        else:
            tier = 0

        scored.append({
            "df": f,
            "min_dist": min_dist,
            "tier": tier,
            "is_clean": (tier == 2)
        })

    # Heavily favor clean slots close to 2950 and then down:
    # Tier 2 (clean): primary sort is frequency descending (highest f <= 2950 first!)
    # Lower tiers: sort by min_dist descending, then frequency descending
    scored.sort(
        key=lambda s: (
            s["tier"],
            s["df"] if s["tier"] == 2 else s["min_dist"],
            s["df"]
        ),
        reverse=True
    )

    best = scored[0]
    optimal_df = best["df"]
    clearance_hz = min(500, int(round(best["min_dist"] * 2)))

    # Auto-dispatch Configure packet if enabled and collision occurs or slot changes significantly
    if tx_optimize_enabled and target_addr:
        should_send = (not current_tx_clear or abs(optimal_df - last_opt) >= 30)
        if should_send and (now - last_send > 6.0):
            try:
                send_wsjt_configure_rx_df(optimal_df)
                with state.lock:
                    state.last_optimized_rx_df = optimal_df
                    state.last_tx_optimize_send_time = now
            except Exception:
                pass

    return {
        "enabled": tx_optimize_enabled,
        "optimal_df": optimal_df,
        "clearance_hz": clearance_hz,
        "current_tx_df": effective_tx,
        "current_tx_clear": current_tx_clear,
        "active_signals_60s": len(recent)
    }

def calculate_decode_score(
    call: str,
    grid: str,
    snr: int,
    is_cq: bool,
    cq_target: Optional[str],
    home_grid: str,
    my_call: str,
    pattern: Optional[MmanaGainPattern],
    current_band: str,
    kp: float,
    log_watcher: Optional[WsjtxLogWatcher],
    loc_resolver: Optional[LocationResolver],
    psk_enabled: bool = False,
    psk_manager: Optional[PskReporterManager] = None,
    freq_mhz: float = 14.074
) -> Optional[Dict[str, Any]]:
    home_lat, home_lon = maidenhead_to_latlon(home_grid)
    rem_lat, rem_lon = maidenhead_to_latlon(grid)
    dist_km, az_deg = great_circle(home_lat, home_lon, rem_lat, rem_lon)
    
    mid_lat = (home_lat + rem_lat) / 2.0
    mid_lon = (home_lon + rem_lon) / 2.0
    now_utc = datetime.now(timezone.utc)
    mid_sun_el = solar_elevation(mid_lat, mid_lon, now_utc)
    path_state = "DAY" if mid_sun_el > 0 else ("TWILIGHT" if mid_sun_el > -12 else "NIGHT")
    
    path_analysis = analyze_ray_path(home_lat, home_lon, rem_lat, rem_lon, dist_km, freq_mhz, now_utc)
    hops = path_analysis["hops"]
    hop_km = path_analysis["hop_km"]
    el_deg = path_analysis["el"]
    hv_km = path_analysis["avg_hv_km"]
    bounces = path_analysis["bounces"]
    total_ground_loss_db = path_analysis["total_ground_loss_db"]
    fspl_db = path_analysis["fspl_db"]
    
    v_dbi, h_dbi, tot_dbi = pattern.get_gain(az_deg, el_deg) if pattern else (0.0, 0.0, 0.0)
    
    # Directional penalty: how far below peak gain is this direction?
    # Targets significantly off the main lobe are penalized even with decent SNR,
    # as antenna gain is a stronger QSO success predictor than momentary SNR.
    delta_from_peak_dbi = 0.0
    directional_penalty = 1.0
    if pattern:
        peak_gain = pattern.max_gain
        delta_from_peak_dbi = round(peak_gain - tot_dbi, 2)
        # Penalty kicks in at 4 dB below peak, scales linearly to 50% at 18 dB
        if delta_from_peak_dbi > 4.0:
            directional_penalty = max(0.5, 1.0 - (delta_from_peak_dbi - 4.0) * 0.035)
    
    # Resolve Country & State
    loc = loc_resolver.resolve(call, grid) if loc_resolver else {
        "country": "Unknown", "country_code": "", "state": "", "state_code": "",
        "loc_code": "--", "loc_sub": "", "loc_full": "", "is_dx": False
    }

    # Check if worked before
    worked_band, worked_any, last_qso, other_bands = (
        log_watcher.check_worked(call, current_band) if log_watcher else (False, False, None, [])
    )
    
    # Geomagnetic loss for high latitude if Kp > 2
    geomag_loss_db = 0.0
    if kp > 2.0 and rem_lat > 45.0:
        geomag_loss_db = (kp - 2.0) * (rem_lat - 45.0) * 0.15
        
    rx_margin = snr - (-21.0)
    base_margin = rx_margin + (tot_dbi * 2) - geomag_loss_db - (total_ground_loss_db * 0.4)
    
    psk_info: Dict[str, Any] = {"match": "disabled"}
    psk_status = ""
    rec_tag = ""

    if psk_enabled and psk_manager:
        psk_info = psk_manager.lookup(call, grid, current_band)
        m_type = psk_info.get("match", "none")
        if m_type == "direct_band":
            tx_snr = psk_info["snr"]
            tx_margin = tx_snr - (-21.0)
            # Bidirectional link bottleneck: QSO requires both sides to decode successfully
            two_way_margin = min(rx_margin, tx_margin)
            effective_margin = two_way_margin + (tot_dbi * 0.3) - (geomag_loss_db * 0.5)
            # Direct two-way verified confidence bonus (+12 pts)
            score = max(5.0, min(100.0, 50.0 + (effective_margin * 2.5) + 12.0))
            psk_status = f"2-WAY {tx_snr:+d}dB"
            rec_tag = " (2-WAY)"
        elif m_type == "direct_other":
            tx_snr = psk_info["snr"]
            effective_margin = base_margin
            # Heard on another band recently: active station confidence bonus (+5 pts)
            score = max(5.0, min(100.0, 50.0 + (effective_margin * 2.5) + 5.0))
            psk_status = f"HEARD ({psk_info.get('band', '')})"
        elif m_type == "grid4":
            est_tx_margin = psk_info["avg_snr"] - (-21.0)
            openness_bonus = 6.0 if est_tx_margin >= 0 else 2.0
            effective_margin = (base_margin * 0.7) + (est_tx_margin * 0.3)
            score = max(5.0, min(100.0, 50.0 + (effective_margin * 2.5) + openness_bonus))
            psk_status = f"PATH {psk_info.get('grid', '')}"
        elif m_type == "grid2":
            effective_margin = base_margin
            score = max(5.0, min(100.0, 50.0 + (effective_margin * 2.5) + 3.0))
            psk_status = f"PATH {psk_info.get('grid', '')}"
        else:
            effective_margin = base_margin
            score = max(5.0, min(100.0, 50.0 + (effective_margin * 2.5)))
    else:
        effective_margin = base_margin
        score = max(5.0, min(100.0, 50.0 + (effective_margin * 2.5)))

    if worked_band:
        # Heavily penalize priority for stations already worked on this band
        score = score * 0.35
        rec = "WORKED B4"
    else:
        rec = ("EXCELLENT" if score >= 80 else ("GOOD" if score >= 60 else "MARGINAL")) + rec_tag

    # Apply directional penalty AFTER score computed — demotes targets in antenna nulls
    score = score * directional_penalty

    return {
        "dist_km": round(dist_km),
        "dist_mi": round(dist_km * 0.621371),
        "az": round(az_deg, 1),
        "el": round(el_deg, 1),
        "hops": hops,
        "hop_km": hop_km,
        "fspl_db": fspl_db,
        "ground_loss_db": total_ground_loss_db,
        "bounces": bounces,
        "gain": round(tot_dbi, 2),
        "delta_from_peak_dbi": delta_from_peak_dbi,
        "rx_margin": round(rx_margin, 1),
        "path_state": path_state,
        "hv_km": round(hv_km),
        "score": round(score, 1),
        "rec": rec,
        "country": loc["country"],
        "country_code": loc["country_code"],
        "state": loc["state"],
        "state_code": loc["state_code"],
        "loc_code": loc["loc_code"],
        "loc_sub": loc["loc_sub"],
        "loc_full": loc["loc_full"],
        "is_dx": loc["is_dx"],
        "worked_band": worked_band,
        "worked_any": worked_any,
        "other_bands": other_bands,
        "other_bands_count": len(other_bands),
        "last_qso": last_qso,
        "psk_info": psk_info,
        "psk_status": psk_status
    }

def rescore_all_decodes():
    """Recalculates scores and recommendations for all active decodes in state."""
    with state.lock:
        home_grid = state.de_grid or DEFAULT_HOME_GRID
        my_call = state.de_call or DEFAULT_MY_CALL
        pattern = state.pattern_manager.active_pattern if state.pattern_manager else None
        freq_mhz = round(state.dial_freq_hz / 1e6, 3)
        band_name = state.pattern_manager.active_band_name if state.pattern_manager else ""
        current_band = band_name or get_band_name(freq_mhz)
        kp = state.solar.get("kp_index", 2.0)
        log_watcher = state.log_watcher
        loc_resolver = state.loc_resolver
        psk_enabled = state.psk_reporter
        psk_manager = state.psk_manager

        for call, d in list(state.decodes.items()):
            calc = calculate_decode_score(
                d["call"], d["grid"], d["snr"], d.get("is_cq", False), d.get("cq_target"),
                home_grid, my_call, pattern, current_band, kp, log_watcher, loc_resolver,
                psk_enabled, psk_manager, freq_mhz=freq_mhz
            )
            if calc:
                d.update(calc)

def process_decode(qtime_ms: int, snr: int, dt: float, df: int, mode: str, message: str):
    with state.lock:
        state.passband_decodes.append({
            "timestamp": time.time(),
            "df": df,
            "snr": snr,
            "qtime_ms": qtime_ms
        })
        if len(state.passband_decodes) > 300:
            state.passband_decodes = state.passband_decodes[-300:]

    call, grid, is_cq, cq_target = extract_call_and_grid(message)
    if not call or not grid:
        return

    with state.lock:
        home_grid = state.de_grid or DEFAULT_HOME_GRID
        my_call = state.de_call or DEFAULT_MY_CALL
        pattern = state.pattern_manager.active_pattern if state.pattern_manager else None
        freq_mhz = round(state.dial_freq_hz / 1e6, 3)
        band_name = state.pattern_manager.active_band_name if state.pattern_manager else ""
        current_band = band_name or get_band_name(freq_mhz)
        kp = state.solar.get("kp_index", 2.0)
        log_watcher = state.log_watcher
        loc_resolver = state.loc_resolver
        psk_enabled = state.psk_reporter
        psk_manager = state.psk_manager

    # Check directed CQ restriction (e.g. CQ DX, CQ State, CQ Country, CQ Continent)
    # If our station does not match the directive, returning the call is restricted and it must not appear in the list
    if is_cq and cq_target:
        eligible, match_reason = is_directed_cq_eligible(
            cq_target, call, grid, my_call, home_grid, loc_resolver
        )
        if not eligible:
            with state.lock:
                state.decodes.pop(call, None)
            return

    calc = calculate_decode_score(
        call, grid, snr, is_cq, cq_target,
        home_grid, my_call, pattern, current_band, kp, log_watcher, loc_resolver,
        psk_enabled, psk_manager, freq_mhz=freq_mhz
    )
    if not calc:
        return

    entry = {
        "call": call,
        "grid": grid,
        "message": message,
        "snr": snr,
        "df": df,
        "dt": round(dt, 2),
        "is_cq": is_cq,
        "cq_target": cq_target,
        "qtime_ms": qtime_ms,
        "mode": mode,
        "timestamp": time.time(),
        **calc
    }

    with state.lock:
        state.decodes[call] = entry

def udp_listener_thread():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", UDP_PORT))
        print(f"[UDP] Listening for WSJT-X on 0.0.0.0:{UDP_PORT}")
    except Exception as e:
        print(f"[UDP] Bind error on port {UDP_PORT}: {e}")
        return

    while True:
        try:
            data, addr = sock.recvfrom(8192)
            if len(data) < 12:
                continue
            magic, schema, msg_type = struct.unpack_from(">III", data, 0)
            if magic != 0xadbccbda:
                continue

            with state.lock:
                state.wsjt_connected = True
                state.last_heartbeat_time = time.time()
                state.wsjt_remote_addr = addr

            off = 12
            client_id, off = decode_utf8(data, off)
            with state.lock:
                if client_id:
                    state.wsjt_client_id = client_id

            if msg_type == 1: # Status
                if off + 8 <= len(data):
                    dial_freq = struct.unpack_from(">Q", data, off)[0]; off += 8
                    mode, off = decode_utf8(data, off)
                    dx_call, off = decode_utf8(data, off)
                    rpt, off = decode_utf8(data, off)
                    tx_mode, off = decode_utf8(data, off)
                    if off + 3 <= len(data):
                        tx_en, xmit, dec = struct.unpack_from("???", data, off); off += 3
                        rx_df, tx_df = struct.unpack_from(">II", data, off); off += 8
                        de_call, off = decode_utf8(data, off)
                        de_grid, off = decode_utf8(data, off)
                        dx_grid, off = decode_utf8(data, off)
                        with state.lock:
                            old_band = get_band_name(state.dial_freq_hz / 1e6) if state.dial_freq_hz > 0 else ""
                            new_band = get_band_name(dial_freq / 1e6)
                            freq_changed = (state.dial_freq_hz != dial_freq)
                            band_changed = (old_band != new_band and bool(old_band))
                            state.dial_freq_hz = dial_freq
                            state.rx_df = rx_df
                            state.tx_df = tx_df
                            state.mode = mode
                            if de_call:
                                state.de_call = de_call
                                if state.psk_manager:
                                    state.psk_manager.callsign = de_call
                            if de_grid: state.de_grid = de_grid
                            state.dx_call = dx_call
                            state.dx_grid = dx_grid
                            state.tx_enabled = tx_en
                            state.transmitting = xmit
                            state.decoding = dec
                            if band_changed:
                                state.decodes.clear()
                                state.passband_decodes.clear()
                            if state.pattern_manager and (freq_changed or state.pattern_manager.active_pattern is None):
                                state.pattern_manager.set_frequency(dial_freq / 1e6)

            elif msg_type == 2: # Decode
                if off < len(data):
                    is_new = struct.unpack_from("?", data, off)[0]; off += 1
                    qtime_ms = struct.unpack_from(">I", data, off)[0]; off += 4
                    snr = struct.unpack_from(">i", data, off)[0]; off += 4
                    dt = struct.unpack_from(">d", data, off)[0]; off += 8
                    df = struct.unpack_from(">I", data, off)[0]; off += 4
                    mode, off = decode_utf8(data, off)
                    message, off = decode_utf8(data, off)
                    process_decode(qtime_ms, snr, dt, df, mode, message)

            elif msg_type == 3: # Clear
                with state.lock:
                    state.decodes.clear()

        except Exception as e:
            time.sleep(0.01)

# -----------------------------------------------------------------------------
# Background Space Weather Poller
# -----------------------------------------------------------------------------
def space_weather_poller():
    while True:
        try:
            sw = SpaceWeather.fetch()
            with state.lock:
                state.solar = sw
            print(f"[SpaceWeather] Updated: SFI={sw.get('solar_flux')}, Kp={sw.get('kp_index')}, Wind={sw.get('solar_wind_km_s')} km/s")
        except Exception as e:
            print(f"[SpaceWeather] Error: {e}")
        time.sleep(1800) # update every 30 minutes

def psk_reporter_poller():
    """Background poller for PSK Reporter ground-truth reception reports."""
    time.sleep(2)
    while True:
        try:
            with state.lock:
                enabled = state.psk_reporter
                callsign = state.de_call or DEFAULT_MY_CALL
                mgr = state.psk_manager

            if enabled and mgr and mgr.should_poll():
                if mgr.fetch_reports(callsign):
                    rescore_all_decodes()
            
            # Check for completed QSOs
            if state.qso_tracker:
                state.qso_tracker.check_completions(state.log_watcher)
        except Exception as e:
            print(f"[PSKReporter] Poller loop error: {e}")
        time.sleep(10)

# -----------------------------------------------------------------------------
# Web Request Handler & REST API
# -----------------------------------------------------------------------------
class DashboardHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress noisy HTTP 200/304 GET logging to console
        if args and len(args) > 1 and str(args[1]) in ("200", "304"):
            return
        try:
            super().log_message(format, *args)
        except (BrokenPipeError, IOError, OSError):
            pass

    def _send_json(self, status: int, data: Any):
        body = json.dumps(data).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, IOError, OSError):
            pass

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            try:
                with open("index.html", "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(content)
            except Exception as e:
                self.send_error(500, str(e))
            return

        elif self.path == "/favicon.ico":
            svg = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><path d="M16 8v20M10 28l6-12 6 12M12 24h8" stroke="#38bdf8" stroke-width="2" stroke-linecap="round" fill="none"/><circle cx="16" cy="8" r="2.5" fill="#38bdf8"/><path d="M11 5a7 7 0 0 0 0 6M21 5a7 7 0 0 1 0 6" stroke="#34d399" stroke-width="2" stroke-linecap="round" fill="none"/><path d="M7 2a13 13 0 0 0 0 12M25 2a13 13 0 0 1 0 12" stroke="#38bdf8" stroke-width="2" stroke-linecap="round" fill="none"/></svg>"""
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(svg)))
            self.end_headers()
            self.wfile.write(svg)
            return
            
        elif self.path == "/api/state":
            try:
                with state.lock:
                    # Disconnect watchdog if no packets for 15s
                    is_connected = (time.time() - state.last_heartbeat_time < 20.0) if state.last_heartbeat_time > 0 else False
                    
                    # Prune decodes older than 6 minutes (24 FT8 cycles)
                    now = time.time()
                    pruned = {k: v for k, v in state.decodes.items() if now - v["timestamp"] < 360}
                    state.decodes = pruned
                    
                    # Filter candidate list against directed CQ restrictions with current station profile
                    ranked_list = []
                    for d in state.decodes.values():
                        if d.get("is_cq") and d.get("cq_target"):
                            eligible, _ = is_directed_cq_eligible(
                                d["cq_target"], d["call"], d["grid"],
                                state.de_call, state.de_grid, state.loc_resolver
                            )
                            if not eligible:
                                continue
                        ranked_list.append(d)
                    ranked_list.sort(key=lambda x: x["score"], reverse=True)
                    
                    active_pat = state.pattern_manager.active_pattern if state.pattern_manager else None
                    pattern_slice = active_pat.get_azimuth_slice(15.0) if active_pat else []
                    peak_gain = active_pat.max_gain if active_pat else 0.0
                    pattern_file = state.pattern_manager.active_filename if state.pattern_manager else "None"
                    band_name = state.pattern_manager.active_band_name if state.pattern_manager else ""
                    nominal_mhz = state.pattern_manager.active_freq_mhz if state.pattern_manager else 0.0

                    freq_mhz = round(state.dial_freq_hz / 1e6, 3)
                    current_band = band_name or get_band_name(freq_mhz)
                    solar_group = get_solar_band_group(freq_mhz)
                    solar_bands = state.solar.get("bands", {}) if state.solar else {}
                    band_cond = solar_bands.get(solar_group, {"day": "--", "night": "--"})

                    solar_payload = dict(state.solar) if state.solar else {}
                    solar_payload["active_band"] = current_band
                    solar_payload["active_group"] = solar_group
                    solar_payload["active_condition"] = band_cond

                    passband_info = analyze_passband(state.tx_df)

                    payload = {
                        "connected": is_connected,
                        "dial_freq_hz": state.dial_freq_hz,
                        "dial_freq_mhz": freq_mhz,
                        "mode": state.mode,
                        "de_call": state.de_call,
                        "de_grid": state.de_grid,
                        "dx_call": state.dx_call,
                        "dx_grid": state.dx_grid,
                        "tx_enabled": state.tx_enabled,
                        "transmitting": state.transmitting,
                        "decoding": state.decoding,
                        "rx_df": state.rx_df,
                        "tx_df": state.tx_df,
                        "tx_freq_optimize": state.tx_freq_optimize,
                        "passband": passband_info,
                        "solar": solar_payload,
                        "pattern": {
                            "filename": pattern_file,
                            "band": current_band,
                            "nominal_mhz": nominal_mhz,
                            "peak_gain": round(peak_gain, 2),
                            "offset_deg": NORTH_OFFSET_DEG,
                            "slice_15deg": pattern_slice
                        },
                        "cq_only": state.cq_only,
                        "hide_worked": state.hide_worked,
                        "psk_reporter": state.psk_reporter,
                        "psk_stats": state.psk_manager.get_stats() if state.psk_manager else {},
                        "worked_total": len(state.log_watcher.worked_any) if state.log_watcher else 0,
                        "decodes": ranked_list,
                        "server_time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    }
                self._send_json(200, payload)
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send_json(500, {"error": str(e)})
            return

        super().do_GET()

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
        
        if self.path == "/api/reply":
            try:
                params = json.loads(body)
                call = params.get("call")
                msg = params.get("message")
                snr = int(params.get("snr", -10))
                qtime_ms = int(params.get("qtime_ms", 0))
                df = int(params.get("df", 1500))
                dt = float(params.get("dt", 0.2))
                mode = params.get("mode", "~")

                # Verify if this message is a restricted directed CQ
                if msg:
                    c, g, is_cq, cq_target = extract_call_and_grid(msg)
                    if is_cq and cq_target:
                        ok, reason = is_directed_cq_eligible(
                            cq_target, call or c or "", g or "",
                            state.de_call, state.de_grid, state.loc_resolver
                        )
                        if not ok:
                            self._send_json(400, {
                                "error": f"Returning call restricted: directed to '{cq_target}' ({reason})"
                            })
                            return
                
                with state.lock:
                    target_addr = state.wsjt_remote_addr or ("127.0.0.1", 2237)
                    client_id = state.wsjt_client_id or "WSJT-X"
                
                    # Record attempt in QSO tracker for later analysis
                    if state.qso_tracker and call in state.decodes:
                        decode = state.decodes[call]
                        if decode.get("scored"):
                            state.qso_tracker.record_attempt(
                                call=call,
                                score_data=decode.get("scored", {}),
                                snr=int(snr) if snr else 0
                            )
                
                pkt = build_wsjt_reply_packet(
                    target_id=client_id,
                    qtime_ms=qtime_ms,
                    snr=snr,
                    delta_time=dt,
                    delta_freq=df,
                    mode=mode,
                    message=msg
                )
                
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.sendto(pkt, target_addr)
                if target_addr != ("127.0.0.1", 2237):
                    try:
                        sock.sendto(pkt, ("127.0.0.1", 2237))
                    except Exception:
                        pass
                sock.close()
                print(f"[Reply] Sent Type 4 Reply for '{msg}' (ID: {client_id}) to {target_addr} and 127.0.0.1:2237")
                
                self._send_json(200, {"success": True, "call": call})
                return
            except Exception as e:
                self._send_json(500, {"error": str(e)})
                return

        elif self.path == "/api/simulate":
            # Injects benchmark dataset for instant testing (domestic US/Canada & international DX)
            simulated = [
                (2*3600*1000 + 13*60*1000, -12, 0.3, 1250, "~", "CQ K7DUR DM42"),
                (2*3600*1000 + 13*60*1000, -18, 0.2, 1819, "~", "CQ KD9XX EN52"),
                (2*3600*1000 + 13*60*1000, -15, 0.5, 2102, "~", "CQ VE7LWW DO00"),
                (2*3600*1000 + 13*60*1000,  -4, 0.2, 2315, "~", "K6VVK W7AJP DM09"),
                (2*3600*1000 + 13*60*1000, -17, 0.2, 2488, "~", "CQ WA6ZTY CM97"),
                (2*3600*1000 + 13*60*1000 + 15000, -13, 0.2, 1735, "~", "CQ KF7FNC CN85"),
                (2*3600*1000 + 13*60*1000 + 15000,  -7, 0.4, 2005, "~", "CQ KE6RAD CM97"),
                (2*3600*1000 + 13*60*1000 + 15000,  -6, 0.2, 2218, "~", "CQ N7SBL CN85"),
                (2*3600*1000 + 13*60*1000 + 15000, -10, 0.3, 1550, "~", "CQ AE6CH CM87"),
                (2*3600*1000 + 13*60*1000 + 15000, -14, 0.3, 1420, "~", "CQ JA1ABC PM95"),
                (2*3600*1000 + 13*60*1000 + 15000, -16, 0.4, 1680, "~", "CQ DL1ABC JO31"),
                (2*3600*1000 + 13*60*1000 + 15000, -11, 0.4, 1920, "~", "CQ VK4XA QG62")
            ]
            for t, snr, dt, df, m, msg in simulated:
                process_decode(t, snr, dt, df, m, msg)
            self._send_json(200, {"success": True, "count": len(simulated)})
            return

        elif self.path == "/api/qso-history":
            if self.command == "GET":
                with state.lock:
                    tracker = state.qso_tracker
                if tracker:
                    self._send_json(200, {
                        "stats": tracker.get_stats(),
                        "successes": tracker.successes,
                        "attempts": list(tracker.attempts.values()),
                    })
                else:
                    self._send_json(200, {"stats": {}, "successes": [], "attempts": []})
                return
            self._send_json(405, {"error": "Method not allowed"})
            return
        
        elif self.path == "/api/clear":
            with state.lock:
                state.decodes.clear()
                state.passband_decodes.clear()
            self._send_json(200, {"success": True})
            return

        elif self.path == "/api/tx-optimize/apply":
            try:
                params = json.loads(body) if body else {}
                target_df = int(params.get("df", 0))
                if target_df <= 0:
                    opt = analyze_passband(state.tx_df)
                    target_df = opt["optimal_df"]
                success = send_wsjt_configure_rx_df(target_df)
                self._send_json(200, {"success": success, "applied_df": target_df})
                return
            except Exception as e:
                self._send_json(500, {"error": str(e)})
                return

        elif self.path == "/api/settings":
            try:
                params = json.loads(body)
                with state.lock:
                    if "cq_only" in params:
                        state.cq_only = bool(params["cq_only"])
                    if "hide_worked" in params:
                        state.hide_worked = bool(params["hide_worked"])
                    if "tx_freq_optimize" in params:
                        state.tx_freq_optimize = bool(params["tx_freq_optimize"])
                        if state.tx_freq_optimize:
                            opt = analyze_passband(state.tx_df)
                            send_wsjt_configure_rx_df(opt["optimal_df"])
                            state.last_optimized_rx_df = opt["optimal_df"]
                            state.last_tx_optimize_send_time = time.time()
                    if "psk_reporter" in params:
                        new_val = bool(params["psk_reporter"])
                        state.psk_reporter = new_val
                        rescore_all_decodes()
                        if new_val and state.psk_manager and (time.time() - state.psk_manager.last_poll_time > 300.0):
                            target_c = state.de_call or DEFAULT_MY_CALL
                            mgr = state.psk_manager
                            def _fetch_bg():
                                if mgr.fetch_reports(target_c):
                                    rescore_all_decodes()
                            threading.Thread(target=_fetch_bg, daemon=True).start()
                self._send_json(200, {
                    "success": True,
                    "cq_only": state.cq_only,
                    "hide_worked": state.hide_worked,
                    "tx_freq_optimize": state.tx_freq_optimize,
                    "psk_reporter": state.psk_reporter
                })
                return
            except Exception as e:
                self._send_json(500, {"error": str(e)})
                return

        self.send_response(404)
        self.end_headers()

# -----------------------------------------------------------------------------
# Main Application Entrypoint
# -----------------------------------------------------------------------------
def main():
    print("=" * 80)
    print("WSJT-X QSO ANALYZER SERVICE")
    print("=" * 80)
    
    # Initialize Multi-Band Antenna Pattern Manager
    state.pattern_manager = AntennaPatternManager(PATTERNS_DIR, north_offset_deg=NORTH_OFFSET_DEG)
    # Set initial active pattern for default frequency (e.g. 14.074 MHz / 20m)
    state.pattern_manager.set_frequency(state.dial_freq_hz / 1e6)
    
    # Initialize WSJT-X Log Watcher (tracks B4 / worked-before stations)
    state.log_watcher = WsjtxLogWatcher()
    
    # Initialize Country & State Location Resolver
    state.loc_resolver = LocationResolver()
    
    # Initialize PSK Reporter Ground-Truth Manager
    state.psk_manager = PskReporterManager(callsign=state.de_call or DEFAULT_MY_CALL)
    
    # Initialize QSO Tracker (records attempts & confirmed completions)
    state.qso_tracker = QsoTracker()
    
    # Fetch initial solar data
    state.solar = SpaceWeather.fetch()
    
    # Start threads
    t_udp = threading.Thread(target=udp_listener_thread, daemon=True)
    t_udp.start()
    
    t_sw = threading.Thread(target=space_weather_poller, daemon=True)
    t_sw.start()
    
    t_psk = threading.Thread(target=psk_reporter_poller, daemon=True)
    t_psk.start()
    
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), DashboardHandler)
    print(f"[HTTP] Dashboard ready at http://localhost:{HTTP_PORT}/")
    print("=" * 80)
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.server_close()

if __name__ == "__main__":
    main()
