#!/usr/bin/env python3
"""
StatGolf Stats Scraper
======================
Scrapes season-level stat values from Sports Reference sites and writes them
into sg_stat_values in statgolf.db.

Usage:
    python build_statgolf_stats.py --sport all          # All sports
    python build_statgolf_stats.py --sport nfl          # NFL only
    python build_statgolf_stats.py --sport nba nhl      # NBA + NHL
    python build_statgolf_stats.py --sport nfl --start 2000   # NFL from 2000

NFL requires undetected-chromedriver (PFR blocks plain HTTP):
    pip install undetected-chromedriver

NBA / MLB / NHL use plain requests with SSL verification disabled (Windows cert store
has intermediate CAs that fail verification; the sites themselves are trusted).

All pages are cached in sg_cache/ — re-runs skip already-fetched pages.
Full scrape times (first run, 4s/page):
    NFL: ~25 min   NBA: ~15 min   MLB: ~20 min   NHL: ~15 min
"""

import argparse
import os
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import date

try:
    from bs4 import BeautifulSoup, Comment
except ImportError:
    print("pip install beautifulsoup4 lxml"); sys.exit(1)

from dotenv import load_dotenv
load_dotenv()

DB_PATH    = os.environ.get("STATGOLF_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "statgolf.db"))
CACHE_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sg_cache")
REQUEST_DELAY = 4.0
CURRENT_YEAR  = date.today().year

# Minimum games / plate appearances to store a season line
NBA_MIN_GAMES = 20
MLB_MIN_PA    = 100    # plate appearances (filters pitchers / cup-of-coffee hitters)
NHL_MIN_GAMES = 10

# ── HTTP SESSION ──────────────────────────────────────────────────────────────

import urllib3
import requests

urllib3.disable_warnings()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

def _make_session():
    s = requests.Session()
    s.verify = False
    s.headers.update(HEADERS)
    print("Using requests (verify=False) for BBR / SRef / HRef")
    return s

SESSION = _make_session()

try:
    import undetected_chromedriver as uc
    HAS_UC = True
    print("undetected_chromedriver available — NFL scraping enabled")
except ImportError:
    HAS_UC = False
    print("undetected_chromedriver NOT installed — NFL scraping disabled")
    print("  To enable: pip install undetected-chromedriver")

DRIVER = None

def _get_driver():
    global DRIVER
    # Reuse the existing session only if it's still alive; a forcibly-closed
    # connection (PFR/Cloudflare) leaves a dead handle that must be respawned.
    if DRIVER is not None:
        try:
            _ = DRIVER.current_url
            return DRIVER
        except Exception:
            _close_driver()
    opts = uc.ChromeOptions()
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    DRIVER = uc.Chrome(options=opts, version_main=148)
    DRIVER.get("https://www.pro-football-reference.com/")
    time.sleep(5)
    return DRIVER

# ── CACHING ───────────────────────────────────────────────────────────────────

def _cache_path(url, sport):
    key = re.sub(r'[^\w]', '_', url)[:200]
    return os.path.join(CACHE_DIR, sport, key + ".html")

def _cached_get_http(url, sport):
    path = _cache_path(url, sport)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path, encoding='utf-8', errors='replace') as f:
            html = f.read()
        if len(html) > 3000:
            return html

    for attempt in range(4):
        time.sleep(REQUEST_DELAY + attempt * 2)
        try:
            SESSION.headers["Referer"] = f"https://{url.split('/')[2]}/"
            resp = SESSION.get(url, timeout=30, verify=False)
            if resp.status_code == 429:
                wait = 30 * (attempt + 1)
                print(f"    Rate limited — waiting {wait}s...")
                time.sleep(wait); continue
            if resp.status_code == 403:
                if attempt < 3:
                    print(f"    403 blocked, retrying ({attempt+1}/4)...")
                    continue
                print(f"    403 after 4 attempts: {url}")
                return None
            resp.raise_for_status()
            html = resp.content.decode('utf-8', errors='replace')
            if len(html) < 3000:
                print(f"    Page too small ({len(html)} bytes)")
                return None
            with open(path, 'w', encoding='utf-8') as f:
                f.write(html)
            return html
        except Exception as e:
            if attempt < 3: continue
            print(f"    Failed: {e}")
            return None
    return None

def _cached_get_chrome(url, sport="nfl"):
    """Use undetected Chrome for sites that block plain HTTP (PFR, Baseball Ref)."""
    path = _cache_path(url, sport)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path, encoding='utf-8', errors='replace') as f:
            html = f.read()
        if len(html) > 3000 and 'Just a moment' not in html[:500]:
            return html

    for attempt in range(3):
        time.sleep(REQUEST_DELAY + attempt * 5)  # back off harder after a failure
        try:
            driver = _get_driver()
            driver.get(url)
            time.sleep(3)
            html = driver.page_source
            if 'Just a moment' in html[:500]:
                print("    Cloudflare challenge, waiting 10s...", end=" ", flush=True)
                time.sleep(10)
                html = driver.page_source
                if 'Just a moment' in html[:500]:
                    print("still blocked"); continue
                print("passed!")
            if len(html) > 3000:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(html)
                return html
            print(f"    Page too small ({len(html)} bytes)")
        except Exception as e:
            # A forcibly-closed connection kills the browser — tear it down so the
            # next attempt spawns a fresh session instead of reusing a dead handle.
            print(f"    Chrome error (attempt {attempt+1}/3): {str(e)[:90]} — restarting browser")
            _close_driver()
    return None

# ── TABLE PARSING ─────────────────────────────────────────────────────────────

def parse_table(html, table_id):
    """Parse a Sports Reference table — handles tables hidden in HTML comments."""
    soup = BeautifulSoup(html, 'lxml')
    table = soup.find('table', id=table_id)
    if not table:
        for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
            if table_id in str(comment):
                cs = BeautifulSoup(str(comment), 'lxml')
                table = cs.find('table', id=table_id)
                if table: break
    if not table:
        return []
    thead = table.find('thead')
    if not thead: return []
    headers = [th.get('data-stat', th.get_text(strip=True))
               for th in thead.find_all('tr')[-1].find_all(['th','td'])]
    tbody = table.find('tbody')
    if not tbody: return []
    rows = []
    for tr in tbody.find_all('tr'):
        cls = tr.get('class', [])
        if 'thead' in cls or 'over_header' in cls: continue
        cells = tr.find_all(['th','td'])
        if len(cells) < 2: continue
        row = {}
        for i, cell in enumerate(cells):
            key = cell.get('data-stat', headers[i] if i < len(headers) else f'col_{i}')
            row[key] = cell.get_text(strip=True)
            link = cell.find('a')
            if link and link.get('href'):
                row[key + '_link'] = link['href']
        rows.append(row)
    return rows

def _player_id(row):
    """Extract Sports Reference player ID from any row with a player link."""
    link = (row.get('player_link') or row.get('name_display_link') or
            row.get('player_name_link') or '')
    if not link: return ''
    seg = link.rstrip('/').split('/')[-1]
    return re.sub(r'\.s?html?$', '', seg)

def _name(row):
    raw = (row.get('player') or row.get('name_display') or
           row.get('player_name') or '')
    if not raw: return ''
    raw = raw.replace('’', "'").replace('‘', "'")
    raw = unicodedata.normalize('NFC', raw)
    raw = re.sub(r'\s*\(.*?\)\s*$', '', raw)
    return raw.rstrip('*#+ ').strip()

MULTI_TEAM = {'TOT', '2TM', '3TM', '4TM', '5TM'}

def _dedup_multi_team(rows, team_key='team_id'):
    """
    For players traded mid-season, SR shows per-team rows plus a TOT/2TM total row.
    Keep only the total row for those players; keep the single row for others.
    """
    pid_has_tot = set()
    for row in rows:
        team = (row.get(team_key) or '').strip().upper()
        if team in MULTI_TEAM:
            pid_has_tot.add(_player_id(row))

    out = []
    for row in rows:
        pid  = _player_id(row)
        team = (row.get(team_key) or '').strip().upper()
        if pid in pid_has_tot:
            if team not in MULTI_TEAM:
                continue  # skip individual-team split rows
        out.append(row)
    return out

def _flt(val, *fallbacks):
    for v in (val,) + fallbacks:
        try:
            f = float(str(v).replace(',', ''))
            if f > 0: return f
        except (TypeError, ValueError): pass
    return 0.0

def _int(val, *fallbacks):
    return int(_flt(val, *fallbacks))

# ── DATABASE ──────────────────────────────────────────────────────────────────

def _db():
    return sqlite3.connect(DB_PATH)

def _ensure_schema():
    con = _db()
    con.execute("""
        CREATE TABLE IF NOT EXISTS sg_stat_values (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            player_name   TEXT    NOT NULL,
            player_id     TEXT    NOT NULL,
            sport         TEXT    NOT NULL,
            stat_category TEXT    NOT NULL,
            season_year   INTEGER NOT NULL,
            value         REAL    NOT NULL,
            UNIQUE(player_id, stat_category, season_year)
        )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_sg_stat_cat ON sg_stat_values(stat_category, player_name COLLATE NOCASE)")
    con.commit(); con.close()

def _upsert(rows_to_insert):
    """rows_to_insert: list of (player_name, player_id, sport, stat_category, season_year, value)"""
    if not rows_to_insert: return 0
    con = _db()
    inserted = 0
    for row in rows_to_insert:
        try:
            con.execute(
                "INSERT OR IGNORE INTO sg_stat_values "
                "(player_name, player_id, sport, stat_category, season_year, value) "
                "VALUES (?,?,?,?,?,?)", row)
            inserted += con.execute("SELECT changes()").fetchone()[0]
        except Exception as e:
            print(f"    DB error: {e} — row: {row}")
    con.commit(); con.close()
    return inserted

# ═══════════════════════════════════════════════════════════════════════════════
#  NFL — Pro Football Reference (requires undetected_chromedriver)
# ═══════════════════════════════════════════════════════════════════════════════

DEFENSE_TABLE_IDS = ['defense', 'defense_stats', 'all_defense']

def scrape_nfl(start_year=1970, end_year=None):
    if not HAS_UC:
        print("Skipping NFL — install undetected-chromedriver to enable")
        return
    end_year = end_year or CURRENT_YEAR
    print(f"\n[NFL] ({start_year}-{end_year})")
    total = 0
    for year in range(start_year, end_year + 1):
        # ── PASSING ──────────────────────────────────────────────────────────
        url = f"https://www.pro-football-reference.com/years/{year}/passing.htm"
        print(f"  {year} passing...", end=' ', flush=True)
        html = _cached_get_chrome(url, sport='nfl')
        if not html:
            print("SKIP")
        else:
            rows = _dedup_multi_team(parse_table(html, 'passing'), team_key='team_name_abbr')
            batch = []
            for row in rows:
                pid = _player_id(row); name = _name(row)
                if not pid or not name: continue
                yds = _int(row.get('pass_yds', 0))
                td  = _int(row.get('pass_td',  0))
                if yds > 0: batch.append((name, pid, 'nfl', 'nfl_passing_yards',       year, float(yds)))
                if td  > 0: batch.append((name, pid, 'nfl', 'nfl_passing_touchdowns',  year, float(td)))
            ins = _upsert(batch); total += ins
            print(f"{len(batch)} rows, {ins} new")

        # ── RUSHING ──────────────────────────────────────────────────────────
        url = f"https://www.pro-football-reference.com/years/{year}/rushing.htm"
        print(f"  {year} rushing...", end=' ', flush=True)
        html = _cached_get_chrome(url, sport='nfl')
        if not html:
            print("SKIP")
        else:
            rows = _dedup_multi_team(parse_table(html, 'rushing'), team_key='team_name_abbr')
            batch = []
            for row in rows:
                pid = _player_id(row); name = _name(row)
                if not pid or not name: continue
                yds = _int(row.get('rush_yds', 0))
                td  = _int(row.get('rush_td',  0))
                if yds > 0: batch.append((name, pid, 'nfl', 'nfl_rushing_yards',       year, float(yds)))
                if td  > 0: batch.append((name, pid, 'nfl', 'nfl_rushing_touchdowns',  year, float(td)))
            ins = _upsert(batch); total += ins
            print(f"{len(batch)} rows, {ins} new")

        # ── RECEIVING ─────────────────────────────────────────────────────────
        url = f"https://www.pro-football-reference.com/years/{year}/receiving.htm"
        print(f"  {year} receiving...", end=' ', flush=True)
        html = _cached_get_chrome(url, sport='nfl')
        if not html:
            print("SKIP")
        else:
            rows = _dedup_multi_team(parse_table(html, 'receiving'), team_key='team_name_abbr')
            batch = []
            for row in rows:
                pid = _player_id(row); name = _name(row)
                if not pid or not name: continue
                yds = _int(row.get('rec_yds', 0))
                rec = _int(row.get('rec',     0))
                td  = _int(row.get('rec_td',  0))
                if yds > 0: batch.append((name, pid, 'nfl', 'nfl_receiving_yards',        year, float(yds)))
                if rec > 0: batch.append((name, pid, 'nfl', 'nfl_receptions',              year, float(rec)))
                if td  > 0: batch.append((name, pid, 'nfl', 'nfl_receiving_touchdowns',    year, float(td)))
            ins = _upsert(batch); total += ins
            print(f"{len(batch)} rows, {ins} new")

        # ── DEFENSE (sacks + interceptions) ──────────────────────────────────
        url = f"https://www.pro-football-reference.com/years/{year}/defense.htm"
        print(f"  {year} defense...", end=' ', flush=True)
        html = _cached_get_chrome(url, sport='nfl')
        if not html:
            print("SKIP (fetch failed — defense page not retrieved)")
        else:
            # Try every known table id for the defense page across PFR eras
            rows = []
            for tid in DEFENSE_TABLE_IDS:
                rows = parse_table(html, tid)
                if rows:
                    break
            rows = _dedup_multi_team(rows, team_key='team_name_abbr')
            if not rows:
                print("no defense table parsed — check table id / page contents")
            else:
                batch = []
                for row in rows:
                    pid = _player_id(row); name = _name(row)
                    if not pid or not name: continue
                    # sacks official since 1982; interceptions go back to the 1940s
                    sacks   = _flt(row.get('sacks', 0), row.get('def_sacks', 0))
                    def_int = _int(row.get('def_int', 0), row.get('def_interceptions', 0),
                                   row.get('interceptions', 0))
                    if sacks   > 0: batch.append((name, pid, 'nfl', 'nfl_sacks',          year, sacks))
                    if def_int > 0: batch.append((name, pid, 'nfl', 'nfl_interceptions',  year, float(def_int)))
                ins = _upsert(batch); total += ins
                print(f"{len(batch)} rows, {ins} new")

    print(f"  NFL total new rows: {total}")
    _close_driver()

# ═══════════════════════════════════════════════════════════════════════════════
#  NBA — Basketball Reference
# ═══════════════════════════════════════════════════════════════════════════════

def scrape_nba(start_year=1950, end_year=None):
    end_year = end_year or CURRENT_YEAR
    print(f"\n[NBA] ({start_year}-{end_year})")
    total = 0
    for year in range(start_year, end_year + 1):
        url  = f"https://www.basketball-reference.com/leagues/NBA_{year}_per_game.html"
        print(f"  {year}...", end=' ', flush=True)
        html = _cached_get_http(url, 'nba')
        if not html:
            print("SKIP"); continue
        rows = parse_table(html, 'per_game_stats')
        rows = _dedup_multi_team(rows, team_key='team_name_abbr')
        batch = []
        for row in rows:
            pid  = _player_id(row)
            name = _name(row)
            if not pid or not name: continue
            g = _int(row.get('games', 0) or row.get('g', 0))
            if g < NBA_MIN_GAMES: continue
            ppg   = _flt(row.get('pts_per_g', 0))
            apg   = _flt(row.get('ast_per_g', 0))
            rpg   = _flt(row.get('trb_per_g', 0))
            spg   = _flt(row.get('stl_per_g', 0))
            bpg   = _flt(row.get('blk_per_g', 0))
            fg3pg = _flt(row.get('fg3_per_g', 0))
            if ppg   > 0: batch.append((name, pid, 'nba', 'nba_points_per_game',   year, ppg))
            if apg   > 0: batch.append((name, pid, 'nba', 'nba_assists_per_game',  year, apg))
            if rpg   > 0: batch.append((name, pid, 'nba', 'nba_rebounds_per_game', year, rpg))
            if spg   > 0: batch.append((name, pid, 'nba', 'nba_steals_per_game',   year, spg))
            if bpg   > 0: batch.append((name, pid, 'nba', 'nba_blocks_per_game',   year, bpg))
            # Season totals: per_game_rate × games_played
            tot_pts = round(ppg   * g) if ppg   > 0 and g > 0 else 0
            tot_ast = round(apg   * g) if apg   > 0 and g > 0 else 0
            tot_reb = round(rpg   * g) if rpg   > 0 and g > 0 else 0
            fg3m    = round(fg3pg * g) if fg3pg > 0 and g > 0 else 0
            if tot_pts > 0: batch.append((name, pid, 'nba', 'nba_total_points',    year, float(tot_pts)))
            if tot_ast > 0: batch.append((name, pid, 'nba', 'nba_total_assists',   year, float(tot_ast)))
            if tot_reb > 0: batch.append((name, pid, 'nba', 'nba_total_rebounds',  year, float(tot_reb)))
            if fg3m    > 0: batch.append((name, pid, 'nba', 'nba_3pointers_made',  year, float(fg3m)))
        ins = _upsert(batch)
        print(f"{len(batch)} rows, {ins} new")
        total += ins
    print(f"  NBA total new rows: {total}")

# ═══════════════════════════════════════════════════════════════════════════════
#  MLB — Baseball Reference
# ═══════════════════════════════════════════════════════════════════════════════

MLB_BATTING_TABLE_IDS  = ['players_standard_batting', 'players_batting', 'batting']
MLB_PITCHING_TABLE_IDS = ['players_standard_pitching', 'players_pitching', 'pitching']
MLB_MIN_IP = 10  # minimum innings pitched to count a pitching season

def scrape_mlb(start_year=1900, end_year=None):
    end_year = end_year or CURRENT_YEAR
    print(f"\n[MLB batting] ({start_year}-{end_year})")
    total = 0
    for year in range(start_year, end_year + 1):
        url  = f"https://www.baseball-reference.com/leagues/majors/{year}-standard-batting.shtml"
        print(f"  {year} batting...", end=' ', flush=True)
        html = _cached_get_http(url, 'mlb')
        if not html:
            print("SKIP"); continue
        rows = None
        for tid in MLB_BATTING_TABLE_IDS:
            rows = parse_table(html, tid)
            if rows: break
        if not rows:
            print("no table"); continue
        rows = _dedup_multi_team(rows, team_key='team_name_abbr')
        batch = []
        for row in rows:
            pid  = _player_id(row)
            name = _name(row)
            if not pid or not name: continue
            pa  = _int(row.get('b_pa', 0) or row.get('PA', 0))
            if pa < MLB_MIN_PA: continue
            hr      = _int(row.get('b_hr',  0) or row.get('HR',  0))
            rbi     = _int(row.get('b_rbi', 0) or row.get('RBI', 0))
            h       = _int(row.get('b_h',   0) or row.get('H',   0))
            sb      = _int(row.get('b_sb',  0) or row.get('SB',  0))
            ops_plus = _flt(row.get('b_onbase_plus_slugging_plus', 0) or
                            row.get('onbase_plus_slugging_plus', 0))
            if hr       > 0: batch.append((name, pid, 'mlb', 'mlb_home_runs',    year, float(hr)))
            if rbi      > 0: batch.append((name, pid, 'mlb', 'mlb_rbi',          year, float(rbi)))
            if h        > 0: batch.append((name, pid, 'mlb', 'mlb_hits',         year, float(h)))
            if sb       > 0: batch.append((name, pid, 'mlb', 'mlb_stolen_bases', year, float(sb)))
            if ops_plus > 0: batch.append((name, pid, 'mlb', 'mlb_ops_plus',     year, float(ops_plus)))
        ins = _upsert(batch)
        print(f"{len(batch)} rows, {ins} new")
        total += ins
    print(f"  MLB batting total new rows: {total}")

def scrape_mlb_pitching(start_year=1900, end_year=None):
    end_year = end_year or CURRENT_YEAR
    print(f"\n[MLB pitching] ({start_year}-{end_year})")
    total = 0
    for year in range(start_year, end_year + 1):
        url  = f"https://www.baseball-reference.com/leagues/majors/{year}-standard-pitching.shtml"
        print(f"  {year} pitching...", end=' ', flush=True)
        # Cache separately under mlb/pitching subdirectory key
        html = _cached_get_http(url, 'mlb')
        if not html:
            print("SKIP"); continue
        rows = None
        for tid in MLB_PITCHING_TABLE_IDS:
            rows = parse_table(html, tid)
            if rows: break
        if not rows:
            print("no table"); continue
        rows = _dedup_multi_team(rows, team_key='team_name_abbr')
        batch = []
        for row in rows:
            pid  = _player_id(row)
            name = _name(row)
            if not pid or not name: continue
            # Filter by minimum innings pitched
            ip_str = row.get('p_ip', '') or row.get('IP', '') or row.get('p_ipouts', '')
            try:
                ip = float(str(ip_str).replace(',', '')) if ip_str else 0
            except ValueError:
                ip = 0
            if ip < MLB_MIN_IP: continue
            so  = _int(row.get('p_so',  0) or row.get('SO', 0))
            sv  = _int(row.get('p_sv',  0) or row.get('SV', 0))
            w   = _int(row.get('p_w',   0) or row.get('W',  0))
            if so > 0: batch.append((name, pid, 'mlb', 'mlb_strikeouts_pitching', year, float(so)))
            if sv > 0: batch.append((name, pid, 'mlb', 'mlb_saves',              year, float(sv)))
            if w  > 0: batch.append((name, pid, 'mlb', 'mlb_wins_pitching',      year, float(w)))
        ins = _upsert(batch)
        print(f"{len(batch)} rows, {ins} new")
        total += ins
    print(f"  MLB pitching total new rows: {total}")

# ═══════════════════════════════════════════════════════════════════════════════
#  NHL — Hockey Reference
# ═══════════════════════════════════════════════════════════════════════════════

NHL_TABLE_IDS = ['player_stats', 'stats', 'skaters']

def scrape_nhl(start_year=1970, end_year=None):
    end_year = end_year or CURRENT_YEAR
    print(f"\n[NHL] ({start_year}-{end_year})")
    total = 0
    for year in range(start_year, end_year + 1):
        url  = f"https://www.hockey-reference.com/leagues/NHL_{year}_skaters.html"
        print(f"  {year}...", end=' ', flush=True)
        html = _cached_get_http(url, 'nhl')
        if not html:
            print("SKIP"); continue
        rows = None
        for tid in NHL_TABLE_IDS:
            rows = parse_table(html, tid)
            if rows: break
        if not rows:
            print("no table"); continue
        rows = _dedup_multi_team(rows, team_key='team_name_abbr')
        batch = []
        for row in rows:
            pid  = _player_id(row)
            name = _name(row)
            if not pid or not name: continue
            g = _int(row.get('games', 0) or row.get('games_played', 0))
            if g < NHL_MIN_GAMES: continue
            goals   = _int(row.get('goals',   0))
            points  = _int(row.get('points',  0))
            assists = _int(row.get('assists', 0))
            pim     = _int(row.get('pen_min', 0) or row.get('pim', 0))
            if goals   > 0: batch.append((name, pid, 'nhl', 'nhl_goals',            year, float(goals)))
            if points  > 0: batch.append((name, pid, 'nhl', 'nhl_points',           year, float(points)))
            if assists > 0: batch.append((name, pid, 'nhl', 'nhl_assists',           year, float(assists)))
            if pim     > 0: batch.append((name, pid, 'nhl', 'nhl_penalty_minutes',  year, float(pim)))
        ins = _upsert(batch)
        print(f"{len(batch)} rows, {ins} new")
        total += ins
    print(f"  NHL total new rows: {total}")

# ── CAREER TOTALS ─────────────────────────────────────────────────────────────

# Maps career category → list of season categories whose values are summed per player.
# For nba_career_assists: computed separately via apg * derived_games.
CAREER_MAPPINGS = [
    ('nfl_career_passing_yards',       ['nfl_passing_yards']),
    ('nfl_career_rushing_yards',       ['nfl_rushing_yards']),
    ('nfl_career_receiving_yards',     ['nfl_receiving_yards']),
    # scored TDs (rushing + receiving); excludes passing TDs which are "thrown", not "scored"
    ('nfl_career_touchdowns',          ['nfl_rushing_touchdowns', 'nfl_receiving_touchdowns']),
    ('nfl_career_sacks',               ['nfl_sacks']),
    ('nba_career_points',              ['nba_total_points']),
    ('nba_career_assists',             ['nba_total_assists']),
    ('nba_career_rebounds',            ['nba_total_rebounds']),
    ('nba_career_3pointers',           ['nba_3pointers_made']),
    ('mlb_career_home_runs',           ['mlb_home_runs']),
    ('mlb_career_hits',                ['mlb_hits']),
    ('mlb_career_rbi',                 ['mlb_rbi']),
    ('mlb_career_strikeouts_pitching', ['mlb_strikeouts_pitching']),
    ('mlb_career_saves',               ['mlb_saves']),
    ('nhl_career_goals',               ['nhl_goals']),
    ('nhl_career_points',              ['nhl_points']),
    ('nhl_career_assists',             ['nhl_assists']),
]

def compute_career_stats():
    """
    Aggregate season rows into career totals (season_year=9999).
    Existing career rows are replaced so this is safe to re-run.
    """
    print("\n[Career totals]")
    con = _db()
    total_inserted = 0

    for career_cat, season_cats in CAREER_MAPPINGS:
        sport = career_cat.split('_')[0]
        placeholders = ','.join('?' * len(season_cats))
        rows = con.execute(f"""
            SELECT player_name, player_id, sport, SUM(value) AS career_val
            FROM sg_stat_values
            WHERE stat_category IN ({placeholders})
              AND season_year != 9999
            GROUP BY player_id
            HAVING career_val > 0
        """, season_cats).fetchall()

        inserted = 0
        for player_name, player_id, _, career_val in rows:
            try:
                con.execute(
                    "INSERT OR REPLACE INTO sg_stat_values "
                    "(player_name, player_id, sport, stat_category, season_year, value) "
                    "VALUES (?,?,?,?,9999,?)",
                    (player_name, player_id, sport, career_cat, round(career_val))
                )
                inserted += 1
            except Exception as e:
                print(f"    error {player_id}: {e}")
        con.commit()
        total_inserted += inserted
        print(f"  {career_cat}: {inserted} players")

    con.close()
    print(f"  Career totals complete — {total_inserted} total rows upserted")

# ── CROSS-SPORT LEADERS ─────────────────────────────────────────────────────────
# Career games played is the one stat that is BOTH comparable across all four
# leagues AND published as a career-leaders page on every Sports-Reference site.
# (Seasons-played pages 404 everywhere; WAR / win shares / minutes / penalty minutes
#  exist but are sport-specific and meaningless to pool cross-sport.)
CROSS_SPORT_LEADERS = {
    'nba': dict(url='https://www.basketball-reference.com/leaders/g_career.html',
                tables=['nba', 'tot'],            fetch='http'),
    'mlb': dict(url='https://www.baseball-reference.com/leaders/G_career.shtml',
                tables=['leader_standard_G'],     fetch='http'),
    'nhl': dict(url='https://www.hockey-reference.com/leaders/games_played_career.html',
                tables=['stats_career_NHL'],      fetch='http'),
    'nfl': dict(url='https://www.pro-football-reference.com/leaders/g_career.htm',
                tables=['g_leaders'], fetch='chrome'),
}

def _find_leader_table(html, table_ids):
    """Locate a leaders table by id, searching the DOM and HTML comments."""
    soup = BeautifulSoup(html, 'lxml')
    for tid in table_ids:
        t = soup.find('table', id=tid)
        if t:
            return t
    for comment in soup.find_all(string=lambda x: isinstance(x, Comment)):
        cs = str(comment)
        for tid in table_ids:
            if tid in cs:
                t = BeautifulSoup(cs, 'lxml').find('table', id=tid)
                if t:
                    return t
    return None

def _clean_leader_name(raw):
    """Leaders tables append junk to names: nbsp, a trailing season count '(24)',
    and HOF/active markers (* + #)."""
    raw = raw.replace('\xa0', ' ').replace('’', "'")
    raw = unicodedata.normalize('NFC', raw)
    raw = re.sub(r'\s*\(.*?\)\s*$', '', raw)   # trailing "(24)" season count
    return raw.rstrip('*#+ ').strip()

def scrape_cross_sport(sports=('nba', 'mlb', 'nhl', 'nfl'), dry_run=False):
    """Scrape career games-played leaders for every league into career_games_played.
    Replaces the small curated seed list with the authoritative scraped leaderboards.
    Player IDs come straight from the /players/ links, so they unify with the
    season data already scraped under the same Sports-Reference IDs.

    dry_run=True fetches and parses every page and prints what WOULD be written,
    without deleting the seed rows or inserting anything."""
    print("\n[Cross-sport: career games played]" + ("  (DRY RUN — no DB writes)" if dry_run else ""))
    # Wipe the curated seed rows first so the scrape is the single source of truth
    if not dry_run:
        con = _db()
        con.execute("DELETE FROM sg_stat_values WHERE stat_category='career_games_played'")
        con.commit(); con.close()

    total = 0
    for sport in sports:
        cfg = CROSS_SPORT_LEADERS[sport]
        print(f"  {sport.upper()}...", end=' ', flush=True)
        if cfg['fetch'] == 'chrome':
            if not HAS_UC:
                print("SKIP (needs undetected-chromedriver)"); continue
            html = _cached_get_chrome(cfg['url'], sport=sport)
        else:
            html = _cached_get_http(cfg['url'], sport)
        if not html:
            print("SKIP (fetch failed)"); continue
        table = _find_leader_table(html, cfg['tables'])
        if not table:
            print(f"no leader table parsed (tried ids {cfg['tables']})"); continue

        body = table.find('tbody') or table
        batch = []
        for tr in body.find_all('tr'):
            link = tr.find('a', href=re.compile(r'/players/'))
            if not link:
                continue
            href = link['href']
            pid  = re.sub(r'\.s?html?$', '', href.rstrip('/').split('/')[-1])
            name = _clean_leader_name(link.get_text(strip=True))
            if not pid or not name:
                continue
            # Games = first plain-integer cell AFTER the name cell. This rule skips
            # NHL's "1997-21" year-span column and MLB's bats/throws letter cell.
            name_cell = link.find_parent(['td', 'th'])
            games, started = 0, False
            for cell in tr.find_all(['td', 'th']):
                if cell is name_cell:
                    started = True; continue
                if not started:
                    continue
                txt = cell.get_text(strip=True).replace(',', '')
                if re.fullmatch(r'\d+', txt):
                    games = int(txt); break
            if games > 0:
                batch.append((name, pid, sport, 'career_games_played', 9999, float(games)))
        if dry_run:
            print(f"{len(batch)} rows parsed (not written)")
            for name, pid, _s, _c, _y, val in batch[:5]:
                print(f"      {name} ({pid}) = {val:.0f}")
        else:
            ins = _upsert(batch); total += ins
            print(f"{len(batch)} rows, {ins} new")
    _close_driver()
    if not dry_run:
        print(f"  Cross-sport total new rows: {total}")

# ── CLEANUP ───────────────────────────────────────────────────────────────────

def _close_driver():
    global DRIVER
    if DRIVER:
        try: DRIVER.quit()
        except: pass
        DRIVER = None

# ── MAIN ──────────────────────────────────────────────────────────────────────

DEFAULT_START = {'nfl': 1970, 'nba': 1950, 'mlb': 1900, 'nhl': 1918}

def main():
    parser = argparse.ArgumentParser(description='Scrape season stats into StatGolf DB')
    parser.add_argument('--sport', nargs='+', choices=['nfl','nba','mlb','nhl','all','career','cross','dedupe'],
                        default=['all'],
                        help='Sport(s) to scrape; "career" aggregates career totals, '
                             '"cross" scrapes cross-sport games-played leaders, '
                             '"dedupe" merges seed-typo duplicate player IDs')
    parser.add_argument('--start', type=int, default=None,
                        help='Start year (overrides per-sport default)')
    parser.add_argument('--end',   type=int, default=None,
                        help='End year (default: current year)')
    parser.add_argument('--dry-run', action='store_true',
                        help='For "cross": fetch + parse and print what would be written, without touching the DB')
    args = parser.parse_args()

    sports = args.sport
    if 'all' in sports:
        sports = ['nfl', 'nba', 'mlb', 'nhl', 'career', 'cross']

    _ensure_schema()

    try:
        if 'nfl' in sports:
            scrape_nfl(
                start_year=args.start or DEFAULT_START['nfl'],
                end_year=args.end
            )
        if 'nba' in sports:
            scrape_nba(
                start_year=args.start or DEFAULT_START['nba'],
                end_year=args.end
            )
        if 'mlb' in sports:
            scrape_mlb(
                start_year=args.start or DEFAULT_START['mlb'],
                end_year=args.end
            )
            scrape_mlb_pitching(
                start_year=args.start or DEFAULT_START['mlb'],
                end_year=args.end
            )
        if 'nhl' in sports:
            scrape_nhl(
                start_year=args.start or DEFAULT_START['nhl'],
                end_year=args.end
            )
        if 'career' in sports:
            compute_career_stats()
        if 'cross' in sports:
            scrape_cross_sport(dry_run=args.dry_run)
        if 'dedupe' in sports:
            import models
            merged, moved, deleted = models.dedupe_players()
            print(f"\n[Dedupe] merged {merged} duplicate player ID(s) — {moved} rows moved, {deleted} removed.")
    finally:
        _close_driver()

    con = _db()
    total = con.execute("SELECT COUNT(*) FROM sg_stat_values").fetchone()[0]
    con.close()
    print(f"\nDone. sg_stat_values total rows: {total}")

if __name__ == '__main__':
    main()
