import os
import sqlite3
import json
import time
import secrets
import unicodedata
import hmac as _hmac
import hashlib
from datetime import date, datetime
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.environ.get("STATGOLF_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "statgolf.db"))
STATCHECK_USERS_DB = os.environ.get("STATCHECK_USERS_DB", "")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "Kelp")

_TOKEN_LIFETIME = 86400 * 30  # 30 days

# ── SCHEMA ────────────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS sg_stat_values (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        player_name   TEXT    NOT NULL,
        player_id     TEXT    NOT NULL,
        sport         TEXT    NOT NULL,
        stat_category TEXT    NOT NULL,
        season_year   INTEGER NOT NULL,
        value         REAL    NOT NULL,
        UNIQUE(player_id, stat_category, season_year)
    );
    CREATE INDEX IF NOT EXISTS idx_sg_stat_cat
        ON sg_stat_values(stat_category, player_name COLLATE NOCASE);

    CREATE TABLE IF NOT EXISTS sg_puzzle (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        puzzle_date     TEXT    NOT NULL,
        hole_number     INTEGER NOT NULL,
        target_distance INTEGER NOT NULL,
        par             INTEGER NOT NULL,
        club_bag        TEXT    NOT NULL,
        hazard_bands    TEXT    NOT NULL DEFAULT '[]',
        published       INTEGER NOT NULL DEFAULT 0,
        created_by      TEXT    NOT NULL DEFAULT '',
        created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(puzzle_date, hole_number)
    );

    CREATE TABLE IF NOT EXISTS sg_round (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER NOT NULL,
        puzzle_date  TEXT    NOT NULL,
        difficulty   TEXT    NOT NULL,
        completed    INTEGER NOT NULL DEFAULT 0,
        current_hole INTEGER NOT NULL DEFAULT 1,
        state        TEXT    NOT NULL DEFAULT '{}',
        is_raincheck INTEGER NOT NULL DEFAULT 0,
        created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, puzzle_date)
    );

    CREATE TABLE IF NOT EXISTS sg_hole_result (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        puzzle_date TEXT    NOT NULL,
        hole_number INTEGER NOT NULL,
        strokes     INTEGER NOT NULL,
        par         INTEGER NOT NULL,
        par_diff    INTEGER NOT NULL,
        shots       TEXT    NOT NULL DEFAULT '[]',
        UNIQUE(user_id, puzzle_date, hole_number)
    );

    CREATE TABLE IF NOT EXISTS sg_landmark_stats (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        player_name   TEXT    NOT NULL,
        player_id     TEXT    NOT NULL,
        sport         TEXT    NOT NULL,
        stat_category TEXT    NOT NULL,
        season_year   INTEGER NOT NULL,
        value         REAL    NOT NULL,
        landmark_type TEXT    NOT NULL DEFAULT 'candidate',
        description   TEXT    NOT NULL DEFAULT '',
        is_active     INTEGER NOT NULL DEFAULT 0,
        created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(player_id, stat_category, season_year)
    );
    CREATE INDEX IF NOT EXISTS idx_landmark_cat
        ON sg_landmark_stats(stat_category, is_active);
    """)
    con.commit()
    # Migration: add is_raincheck for databases created before this column existed
    try:
        con.execute("ALTER TABLE sg_round ADD COLUMN is_raincheck INTEGER NOT NULL DEFAULT 0")
        con.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    con.close()

# ── AUTH / STATCHECK USERS ────────────────────────────────────────────────────

def _statcheck_con():
    if not STATCHECK_USERS_DB or not os.path.exists(STATCHECK_USERS_DB):
        return None
    return sqlite3.connect(f"file:{STATCHECK_USERS_DB}?mode=rw", uri=True)

def get_statcheck_user(username):
    con = _statcheck_con()
    if not con:
        return None
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT id, username, password_hash FROM users WHERE LOWER(username)=LOWER(?)",
        (username,)
    ).fetchone()
    con.close()
    return dict(row) if row else None

def create_statcheck_user(username, password):
    con = _statcheck_con()
    if not con:
        return None
    try:
        con.execute(
            "INSERT INTO users (username, password_hash, nfl_mascot, mlb_mascot, nba_mascot, nhl_mascot) "
            "VALUES (?, ?, 'KC', 'NYY', 'LAL', 'BOS')",
            (username, generate_password_hash(password))
        )
        con.commit()
    except sqlite3.IntegrityError:
        con.close()
        return None
    con.close()
    return get_statcheck_user(username)

def verify_password(stored_hash, password):
    return check_password_hash(stored_hash, password)

def is_admin(username):
    if not username:
        return False
    return username.strip().lower() == ADMIN_USERNAME.strip().lower()

# ── TOKENS ────────────────────────────────────────────────────────────────────

def make_token(secret_key, user_id, username):
    expiry = int(time.time()) + _TOKEN_LIFETIME
    payload = f"{user_id}:{username}:{expiry}"
    sig = _hmac.new(secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"

def verify_token(secret_key, token):
    try:
        parts = token.split(":", 3)
        if len(parts) != 4:
            return None
        user_id, username, expiry, sig = parts
        if int(expiry) < int(time.time()):
            return None
        payload = f"{user_id}:{username}:{expiry}"
        expected = _hmac.new(secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not secrets.compare_digest(sig, expected):
            return None
        return {"user_id": int(user_id), "username": username}
    except Exception:
        return None

# ── STAT VALUES ───────────────────────────────────────────────────────────────

def _fold(s):
    """Accent-fold + lowercase, so 'pena' matches 'Peña' and 'nunez' matches 'Núñez'."""
    if not s:
        return ""
    return unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()

def search_players(stat_category, query, limit=20):
    con = sqlite3.connect(DB_PATH)
    con.create_function("sg_fold", 1, _fold)   # accent/case-insensitive matching
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT DISTINCT player_name, player_id, sport
           FROM sg_stat_values
           WHERE stat_category = ?
             AND sg_fold(player_name) LIKE ?
           ORDER BY player_name COLLATE NOCASE
           LIMIT ?""",
        (stat_category, f"%{_fold(query)}%", limit)
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]

def get_player_seasons(stat_category, player_id):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT season_year, value
           FROM sg_stat_values
           WHERE stat_category = ? AND player_id = ?
           ORDER BY season_year DESC""",
        (stat_category, player_id)
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]

def _round_sig(value, sig=2):
    """Round to `sig` significant figures, nearest. e.g. 17364→17000, 5351→5400, 43.2→43."""
    if value <= 0:
        return 0
    import math
    digits = sig - int(math.floor(math.log10(abs(value)))) - 1
    r = round(value, digits)
    return int(r) if r == int(r) else r

# "Upper echelon" reach: the elite value an engaged player actually aims a pick at.
# A category's full population has a huge low tail (every journeyman who ever recorded
# the stat), so any percentage-of-population basis is dragged down to mediocrity.
# Instead we take the best record of each of the top REACH_TOP_N *distinct players* —
# the calibre of athlete a fan names first — and average them. Every place that sizes a
# hole, suggests par, or hints an aim point uses THIS so they stay coherent.
REACH_TOP_N = 5
# Clubs are never built from categories whose reach is below this — tiny rate stats like
# steals/blocks per game (reach ~3–5) that players don't know or use. Bigger, variable
# stats (points per game, home runs, …) still let a low pick fine-tune near the pin.
MIN_CLUB_REACH = 10

def category_reach(con, stat_category, n=REACH_TOP_N):
    """Average of the top-n distinct players' best value in a category. 0 if no data.
    Dedupes by player so one athlete's multiple elite seasons can't dominate."""
    row = con.execute(
        "SELECT AVG(v) FROM ("
        "  SELECT MAX(value) AS v FROM sg_stat_values WHERE stat_category=?"
        "  GROUP BY player_id ORDER BY v DESC LIMIT ?)",
        (stat_category, n)
    ).fetchone()
    return row[0] if row and row[0] else 0

def get_category_hints():
    """Returns {stat_category: hint_value} — the value a knowledgeable player should
    aim a top pick at, shown on each club. Uses the same upper-echelon reach the hole
    was sized to (see category_reach / REACH_TOP_N), rounded to 2 significant figures,
    so the aim shown to the player matches how the target was actually built."""
    con = sqlite3.connect(DB_PATH)
    cats = [r[0] for r in con.execute(
        "SELECT DISTINCT stat_category FROM sg_stat_values"
    ).fetchall()]
    result = {}
    for cat in cats:
        reach = category_reach(con, cat)
        if reach:
            result[cat] = _round_sig(reach, 2)
    con.close()
    return result

def _norm_name(name):
    """Accent-fold + casefold a player name for collision detection."""
    import unicodedata, re
    n = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode()
    return re.sub(r'\s+', ' ', n).strip().lower()

def dedupe_players():
    """Merge seed-typo duplicate player IDs into their canonical scraped ID.

    The scraper derives player_id from the Sports-Reference URL, so it never
    duplicates a real player. Duplicates only arise when seed.py hand-enters a
    player_id that doesn't match the scraped one (e.g. 'SmiiBr00' vs 'SmitBr00').

    Safe rule: within a (normalized-name, sport) group, if exactly ONE id has
    season-level rows ('rich' = scraped) and the rest have only career rows
    ('thin' = seed), merge the thin ids into the rich one. Groups with two or
    more rich ids are left untouched — those are genuinely different people who
    share a name (there are several 'Steve Smith' / 'Chris Johnson' in the NFL).

    Idempotent. Returns (groups_merged, rows_moved, rows_deleted)."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT player_id, player_name, sport FROM sg_stat_values GROUP BY player_id"
    ).fetchall()
    groups = {}
    for pid, name, sport in rows:
        groups.setdefault((_norm_name(name), sport), []).append((pid, name))

    def n_seasons(pid):
        return con.execute(
            "SELECT COUNT(*) FROM sg_stat_values WHERE player_id=? AND season_year!=9999", (pid,)
        ).fetchone()[0]

    merged = moved = deleted = 0
    for (_nm, _sport), members in groups.items():
        if len(members) < 2:
            continue
        rich = [(p, n) for p, n in members if n_seasons(p) > 0]
        thin = [(p, n) for p, n in members if n_seasons(p) == 0]
        if len(rich) != 1 or not thin:
            continue  # ambiguous (multiple real players, or all seed-only) — skip
        rid, rname = rich[0]
        for tid, _tn in thin:
            for rowid, cat, yr in con.execute(
                "SELECT id, stat_category, season_year FROM sg_stat_values WHERE player_id=?", (tid,)
            ).fetchall():
                clash = con.execute(
                    "SELECT 1 FROM sg_stat_values WHERE player_id=? AND stat_category=? AND season_year=?",
                    (rid, cat, yr)
                ).fetchone()
                if clash:
                    con.execute("DELETE FROM sg_stat_values WHERE id=?", (rowid,)); deleted += 1
                else:
                    con.execute("UPDATE sg_stat_values SET player_id=?, player_name=? WHERE id=?",
                                (rid, rname, rowid)); moved += 1
            merged += 1
    con.commit(); con.close()
    return merged, moved, deleted

def dedupe_duplicate_player_ids():
    """Merge player_id variants that are the SAME real person split across two ids by a
    scraper/seed typo — e.g. 'SmiEm00'/'SmitEm00', 'TomLa00'/'TomlLa00', 'JackLa00'/'JackLa02',
    'espophi01'/'esposph01'. These make a player show up twice (one copy often holding just a
    season or two), which is the 'duplicated but only for one season' symptom.

    SAFETY: two ids in the same (normalized-name, sport) group are merged ONLY when they share
    an IDENTICAL (stat_category, season_year, value) line that is distinctive — a value >= 10,
    or two-or-more shared lines. Different real namesakes (Sports-Reference disambiguates them
    with a trailing NUMBER, e.g. AlleMa00 vs AlleMa03) never produce identical stat lines, so
    they are left untouched. The id with the most rows wins; the other's rows are reassigned,
    or dropped on a key clash. Idempotent. Returns (clusters_merged, rows_moved, rows_deleted)."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT player_id, player_name, sport, stat_category, season_year, value FROM sg_stat_values"
    ).fetchall()

    from collections import defaultdict
    group_lines = defaultdict(lambda: defaultdict(set))   # (norm_name, sport) -> pid -> {(cat,yr,val)}
    rowcount    = defaultdict(int)
    name_of     = {}
    for pid, name, sport, cat, yr, val in rows:
        group_lines[(_norm_name(name), sport)][pid].add((cat, yr, round(val, 3)))
        rowcount[pid] += 1
        name_of.setdefault(pid, name)

    def same_person(a_lines, b_lines):
        shared = a_lines & b_lines
        return any(v >= 10 for _, _, v in shared) or len(shared) >= 2

    merged = moved = deleted = 0
    for (_nm, _sport), pids in group_lines.items():
        if len(pids) < 2:
            continue
        ids = list(pids)
        parent = {i: i for i in ids}
        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]; x = parent[x]
            return x
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                if same_person(pids[ids[i]], pids[ids[j]]):
                    parent[find(ids[i])] = find(ids[j])
        clusters = defaultdict(list)
        for i in ids:
            clusters[find(i)].append(i)

        for cluster in clusters.values():
            if len(cluster) < 2:
                continue
            canon = max(cluster, key=lambda p: rowcount[p])
            cname = name_of[canon]
            canon_keys = set(con.execute(
                "SELECT stat_category, season_year FROM sg_stat_values WHERE player_id=?", (canon,)
            ).fetchall())
            for dup in cluster:
                if dup == canon:
                    continue
                for rowid, cat, yr in con.execute(
                    "SELECT id, stat_category, season_year FROM sg_stat_values WHERE player_id=?", (dup,)
                ).fetchall():
                    if (cat, yr) in canon_keys:
                        con.execute("DELETE FROM sg_stat_values WHERE id=?", (rowid,)); deleted += 1
                    else:
                        con.execute("UPDATE sg_stat_values SET player_id=?, player_name=? WHERE id=?",
                                    (canon, cname, rowid)); moved += 1
                        canon_keys.add((cat, yr))
            merged += 1
    con.commit(); con.close()
    return merged, moved, deleted

def get_stat(stat_category, player_id, season_year):
    """Returns (value, player_name) or (None, player_id_str) if not found."""
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT value, player_name FROM sg_stat_values WHERE stat_category=? AND player_id=? AND season_year=?",
        (stat_category, player_id, season_year)
    ).fetchone()
    con.close()
    return (row[0], row[1]) if row else (None, str(player_id))

def get_max_value_per_club(puzzle_date):
    """For solvability check: max stat value available per stat_category in today's puzzle."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    puzzles = con.execute(
        "SELECT hole_number, club_bag FROM sg_puzzle WHERE puzzle_date=? AND published=1",
        (puzzle_date,)
    ).fetchall()
    result = {}
    for p in puzzles:
        bag = json.loads(p["club_bag"])
        for club in bag:
            cat = club["stat_category"]
            if cat in result:
                continue
            row = con.execute(
                "SELECT MAX(value) FROM sg_stat_values WHERE stat_category=?", (cat,)
            ).fetchone()
            result[cat] = row[0] or 0
    con.close()
    return result

# ── PUZZLES ───────────────────────────────────────────────────────────────────

def get_puzzle(puzzle_date):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM sg_puzzle WHERE puzzle_date=? AND published=1 ORDER BY hole_number",
        (puzzle_date,)
    ).fetchall()
    con.close()
    return [_parse_puzzle_row(r) for r in rows]

def get_puzzle_any(puzzle_date):
    """Admin: get puzzle regardless of published state."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM sg_puzzle WHERE puzzle_date=? ORDER BY hole_number",
        (puzzle_date,)
    ).fetchall()
    con.close()
    return [_parse_puzzle_row(r) for r in rows]

def _parse_puzzle_row(r):
    d = dict(r)
    d["club_bag"] = json.loads(d["club_bag"])
    d["hazard_bands"] = json.loads(d["hazard_bands"])
    return d

def upsert_puzzle_hole(puzzle_date, hole_number, target_distance, par,
                       club_bag, hazard_bands, created_by, published=0):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """INSERT INTO sg_puzzle
               (puzzle_date, hole_number, target_distance, par, club_bag, hazard_bands, created_by, published)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(puzzle_date, hole_number) DO UPDATE SET
               target_distance=excluded.target_distance,
               par=excluded.par,
               club_bag=excluded.club_bag,
               hazard_bands=excluded.hazard_bands,
               created_by=excluded.created_by,
               published=excluded.published""",
        (puzzle_date, hole_number, target_distance, par,
         json.dumps(club_bag), json.dumps(hazard_bands), created_by, published)
    )
    con.commit()
    con.close()

def publish_puzzle(puzzle_date):
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE sg_puzzle SET published=1 WHERE puzzle_date=?", (puzzle_date,))
    con.commit()
    con.close()

def list_upcoming_puzzles(from_date=None):
    if from_date is None:
        from_date = date.today().isoformat()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT puzzle_date, hole_number, target_distance, par, published
           FROM sg_puzzle WHERE puzzle_date >= ? ORDER BY puzzle_date, hole_number""",
        (from_date,)
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]

# ── ROUNDS ────────────────────────────────────────────────────────────────────

def get_round(user_id, puzzle_date):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT * FROM sg_round WHERE user_id=? AND puzzle_date=?",
        (user_id, puzzle_date)
    ).fetchone()
    con.close()
    if not row:
        return None
    d = dict(row)
    d["state"] = json.loads(d["state"])
    return d

def create_round(user_id, puzzle_date, difficulty, is_raincheck=False):
    initial_state = _fresh_hole_state(1)
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute(
            "INSERT INTO sg_round (user_id, puzzle_date, difficulty, state, is_raincheck) VALUES (?,?,?,?,?)",
            (user_id, puzzle_date, difficulty, json.dumps(initial_state), 1 if is_raincheck else 0)
        )
        con.commit()
    except sqlite3.IntegrityError:
        con.close()
        return None
    con.close()
    return get_round(user_id, puzzle_date)

def update_round_state(user_id, puzzle_date, state, current_hole=None, completed=None):
    con = sqlite3.connect(DB_PATH)
    updates = ["state=?", "updated_at=CURRENT_TIMESTAMP"]
    params = [json.dumps(state)]
    if current_hole is not None:
        updates.append("current_hole=?")
        params.append(current_hole)
    if completed is not None:
        updates.append("completed=?")
        params.append(1 if completed else 0)
    params += [user_id, puzzle_date]
    con.execute(
        f"UPDATE sg_round SET {', '.join(updates)} WHERE user_id=? AND puzzle_date=?",
        params
    )
    con.commit()
    con.close()

def save_hole_result(user_id, puzzle_date, hole_number, strokes, par, shots):
    par_diff = strokes - par
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """INSERT OR REPLACE INTO sg_hole_result
               (user_id, puzzle_date, hole_number, strokes, par, par_diff, shots)
           VALUES (?,?,?,?,?,?,?)""",
        (user_id, puzzle_date, hole_number, strokes, par, par_diff, json.dumps(shots))
    )
    con.commit()
    con.close()

def get_hole_results(user_id, puzzle_date):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT h.*, p.target_distance
           FROM sg_hole_result h
           LEFT JOIN sg_puzzle p
             ON p.puzzle_date = h.puzzle_date
            AND p.hole_number = h.hole_number
           WHERE h.user_id=? AND h.puzzle_date=?
           ORDER BY h.hole_number""",
        (user_id, puzzle_date)
    ).fetchall()
    con.close()
    results = []
    for r in rows:
        d = dict(r)
        d["shots"] = json.loads(d["shots"])
        results.append(d)
    return results

def get_history(user_id, limit=30):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT r.puzzle_date, r.difficulty, r.completed,
                  SUM(h.par_diff) AS total_par_diff,
                  COUNT(h.hole_number) AS holes_completed
           FROM sg_round r
           LEFT JOIN sg_hole_result h ON h.user_id=r.user_id AND h.puzzle_date=r.puzzle_date
           WHERE r.user_id=?
           GROUP BY r.puzzle_date
           ORDER BY r.puzzle_date DESC
           LIMIT ?""",
        (user_id, limit)
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]

# ── GAME LOGIC HELPERS ────────────────────────────────────────────────────────

STATGOLF_CLUBS = ["Driver", "3-Wood", "5-Iron", "7-Iron", "9-Iron", "Sand Wedge", "Putter"]

STATGOLF_CATEGORIES = {
    # ── NFL single-season ──────────────────────────────────────────────────────
    "nfl_passing_yards":              {"label": "Passing Yards (NFL)",              "sport": "nfl", "unit": "yds"},
    "nfl_rushing_yards":              {"label": "Rushing Yards (NFL)",              "sport": "nfl", "unit": "yds"},
    "nfl_receiving_yards":            {"label": "Receiving Yards (NFL)",            "sport": "nfl", "unit": "yds"},
    "nfl_passing_touchdowns":         {"label": "Passing TDs (NFL)",               "sport": "nfl", "unit": "td"},
    "nfl_rushing_touchdowns":         {"label": "Rushing TDs (NFL)",               "sport": "nfl", "unit": "td"},
    "nfl_receiving_touchdowns":       {"label": "Receiving TDs (NFL)",             "sport": "nfl", "unit": "td"},
    "nfl_receptions":                 {"label": "Receptions (NFL)",                "sport": "nfl", "unit": "rec"},
    "nfl_sacks":                      {"label": "Sacks (NFL)",                     "sport": "nfl", "unit": "sk"},
    "nfl_interceptions":              {"label": "Interceptions (NFL)",             "sport": "nfl", "unit": "int"},
    # ── NFL career ─────────────────────────────────────────────────────────────
    "nfl_career_rushing_yards":       {"label": "Career Rushing Yards (NFL)",      "sport": "nfl", "unit": "yds", "career": True},
    "nfl_career_receiving_yards":     {"label": "Career Receiving Yards (NFL)",    "sport": "nfl", "unit": "yds", "career": True},
    "nfl_career_passing_yards":       {"label": "Career Passing Yards (NFL)",      "sport": "nfl", "unit": "yds", "career": True},
    "nfl_career_touchdowns":          {"label": "Career Touchdowns (NFL)",         "sport": "nfl", "unit": "td",  "career": True},
    "nfl_career_sacks":               {"label": "Career Sacks (NFL)",              "sport": "nfl", "unit": "sk",  "career": True},
    # ── NBA single-season ──────────────────────────────────────────────────────
    "nba_points_per_game":            {"label": "Points Per Game (NBA)",           "sport": "nba", "unit": "pts"},
    "nba_total_points":               {"label": "Total Points in Season (NBA)",    "sport": "nba", "unit": "pts"},
    "nba_assists_per_game":           {"label": "Assists Per Game (NBA)",          "sport": "nba", "unit": "ast"},
    "nba_total_assists":              {"label": "Total Assists in Season (NBA)",   "sport": "nba", "unit": "ast"},
    "nba_rebounds_per_game":          {"label": "Rebounds Per Game (NBA)",         "sport": "nba", "unit": "reb"},
    "nba_total_rebounds":             {"label": "Total Rebounds in Season (NBA)",  "sport": "nba", "unit": "reb"},
    "nba_3pointers_made":             {"label": "3-Pointers Made in Season (NBA)", "sport": "nba", "unit": "3pm"},
    "nba_steals_per_game":            {"label": "Steals Per Game (NBA)",           "sport": "nba", "unit": "stl"},
    "nba_blocks_per_game":            {"label": "Blocks Per Game (NBA)",           "sport": "nba", "unit": "blk"},
    # ── NBA career ─────────────────────────────────────────────────────────────
    "nba_career_points":              {"label": "Career Points (NBA)",             "sport": "nba", "unit": "pts", "career": True},
    "nba_career_assists":             {"label": "Career Assists (NBA)",            "sport": "nba", "unit": "ast", "career": True},
    "nba_career_rebounds":            {"label": "Career Rebounds (NBA)",           "sport": "nba", "unit": "reb", "career": True},
    "nba_career_3pointers":           {"label": "Career 3-Pointers Made (NBA)",    "sport": "nba", "unit": "3pm", "career": True},
    # ── MLB single-season ──────────────────────────────────────────────────────
    "mlb_home_runs":                  {"label": "Home Runs (MLB)",                 "sport": "mlb", "unit": "hr"},
    "mlb_rbi":                        {"label": "RBI (MLB)",                       "sport": "mlb", "unit": "rbi"},
    "mlb_hits":                       {"label": "Hits (MLB)",                      "sport": "mlb", "unit": "h"},
    "mlb_stolen_bases":               {"label": "Stolen Bases (MLB)",              "sport": "mlb", "unit": "sb"},
    "mlb_strikeouts_pitching":        {"label": "Strikeouts Pitching (MLB)",       "sport": "mlb", "unit": "k"},
    "mlb_saves":                      {"label": "Saves (MLB)",                     "sport": "mlb", "unit": "sv"},
    "mlb_wins_pitching":              {"label": "Wins Pitching (MLB)",             "sport": "mlb", "unit": "w"},
    "mlb_ops_plus":                   {"label": "OPS+ (MLB)",                      "sport": "mlb", "unit": "ops+"},
    # ── MLB career ─────────────────────────────────────────────────────────────
    "mlb_career_home_runs":           {"label": "Career Home Runs (MLB)",          "sport": "mlb", "unit": "hr",  "career": True},
    "mlb_career_hits":                {"label": "Career Hits (MLB)",               "sport": "mlb", "unit": "h",   "career": True},
    "mlb_career_rbi":                 {"label": "Career RBI (MLB)",                "sport": "mlb", "unit": "rbi", "career": True},
    "mlb_career_strikeouts_pitching": {"label": "Career Strikeouts Pitching (MLB)","sport": "mlb", "unit": "k",   "career": True},
    "mlb_career_saves":               {"label": "Career Saves (MLB)",              "sport": "mlb", "unit": "sv",  "career": True},
    # ── NHL single-season ──────────────────────────────────────────────────────
    "nhl_goals":                      {"label": "Goals (NHL)",                     "sport": "nhl", "unit": "g"},
    "nhl_points":                     {"label": "Points (NHL)",                    "sport": "nhl", "unit": "pts"},
    "nhl_assists":                    {"label": "Assists (NHL)",                   "sport": "nhl", "unit": "a"},
    "nhl_penalty_minutes":            {"label": "Penalty Minutes (NHL)",           "sport": "nhl", "unit": "pim"},
    # ── NHL career ─────────────────────────────────────────────────────────────
    "nhl_career_goals":               {"label": "Career Goals (NHL)",              "sport": "nhl", "unit": "g",   "career": True},
    "nhl_career_points":              {"label": "Career Points (NHL)",             "sport": "nhl", "unit": "pts", "career": True},
    "nhl_career_assists":             {"label": "Career Assists (NHL)",            "sport": "nhl", "unit": "a",   "career": True},
    # ── Crossover (multi-sport) — search returns players from all four sports ──
    "career_games_played":            {"label": "Career Games Played",             "sport": "multi","unit": "gp",    "career": True},
}

STATGOLF_GIMME = {"easy": 20, "hard": 10}

def _fresh_hole_state(hole_number):
    return {
        "hole_number": hole_number,
        "running_total": 0,
        "strokes": 0,
        "clubs_used": [],
        "bunker_debuff": False,
        "pending_water": None,
        "shot_history": [],
    }

def apply_rounding(value, stat_category):
    """Convert a raw float stat value to an integer distance in yards."""
    return round(value)

def check_hazards(remaining, hazard_bands):
    """Return the hazard band dict if remaining distance falls inside one, else None."""
    for band in hazard_bands:
        lo, hi = min(band["start"], band["end"]), max(band["start"], band["end"])
        if lo <= remaining <= hi:
            return band
    return None

def drop_position(band):
    """
    Drop point: just outside the TEE-side edge of the water band (the higher 'remaining'
    value), so a drop never moves the ball closer to the hole than where it entered.
    Returned value is the yards-remaining-to-pin after the drop. E.g. band 2500–3000 →
    drop at 3010 (behind the hazard, toward the tee).
    """
    tee_side = max(band["start"], band["end"])
    return int(round((tee_side + 10) / 10) * 10)   # just behind the hazard, rounded to 10

def score_label(par_diff):
    labels = {-3: "Albatross", -2: "Eagle", -1: "Birdie", 0: "Par",
              1: "Bogey", 2: "Double Bogey", 3: "Triple Bogey"}
    if par_diff in labels:
        return labels[par_diff]
    if par_diff < -3:
        return f"{abs(par_diff)}-under"
    return f"+{par_diff}"

def share_string(puzzle_date, hole_results, rarity=None):
    from datetime import datetime
    try:
        dt = datetime.strptime(puzzle_date, "%Y-%m-%d")
        date_str = dt.strftime("%B %d, %Y")
    except Exception:
        date_str = puzzle_date

    total_par   = sum(h["par"]     for h in hole_results)
    total_score = sum(h["strokes"] for h in hole_results)
    total_diff  = total_score - total_par

    def _sign(n):
        return f"+{n}" if n > 0 else str(n)

    # Scorecard grid: hole numbers as column headers, row labels on the left, numbers only in cells
    LW = 10   # label column width
    CW = 8    # per-hole column width

    def _col(val, width=CW):
        return f"{val:>{width}}"

    header   = " " * LW + "".join(_col(f"H{h['hole_number']}") for h in hole_results)
    dist_row = f"{'Distance':<{LW}}" + "".join(
        _col(f"{int(h['target_distance']):,}" if h.get("target_distance") else "-")
        for h in hole_results
    )
    par_row   = f"{'Par':<{LW}}"   + "".join(_col(h["par"])          for h in hole_results)
    score_row = f"{'Score':<{LW}}" + "".join(_col(h["strokes"])      for h in hole_results)
    diff_row  = f"{'+/-':<{LW}}"   + "".join(_col(_sign(h["par_diff"])) for h in hole_results)

    label    = score_label(total_diff)
    diff_str = _sign(total_diff)

    lines = [
        f"⛳ StatGolf | {date_str}",
        "",
        header,
        dist_row,
        par_row,
        score_row,
        diff_row,
        "",
        f"{label.upper()}  ({diff_str})",
    ]
    if rarity is not None:
        lines.append(f"Rarity: {round(rarity)}%")
    lines += ["", "statgolf.com"]
    return "\n".join(lines)

def suggest_par(hole):
    """Par = optimal-path strokes + 1 (clamped 3–6), the same basis the generator uses,
    so an admin who hand-edits a hole gets a par consistent with auto-generated ones.
    The optimal line is Driver + reuse of the largest approach club (see
    optimal_path_strokes); the smaller clubs are fixers, not part of the line."""
    bag = hole.get("club_bag", [])
    if not bag:
        return 4
    target = hole["target_distance"]
    con = sqlite3.connect(DB_PATH)
    driver_reach = 0
    approach_reaches = []
    for club in bag:
        reach = category_reach(con, club["stat_category"])
        if not reach:
            continue
        if club["club"] == "Driver":
            driver_reach = reach
        else:
            approach_reaches.append(reach)
    con.close()
    if not driver_reach:
        driver_reach = max(approach_reaches) if approach_reaches else 0
    strokes = optimal_path_strokes(target, driver_reach, approach_reaches, STATGOLF_GIMME["hard"])
    return max(3, min(6, strokes + 1))

def get_streak(user_id):
    """Count consecutive days with a completed round, ending on today or yesterday."""
    from datetime import date, timedelta
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT DISTINCT puzzle_date FROM sg_round WHERE user_id=? AND completed=1 ORDER BY puzzle_date DESC",
        (user_id,)
    ).fetchall()
    con.close()
    if not rows:
        return 0
    played = {r[0] for r in rows}
    today = date.today()
    check = today if today.isoformat() in played else today - timedelta(days=1)
    if check.isoformat() not in played:
        return 0
    streak = 0
    while check.isoformat() in played:
        streak += 1
        check -= timedelta(days=1)
    return streak

def get_raincheck_info(user_id):
    """Return whether the user missed yesterday and can play a raincheck round."""
    from datetime import date, timedelta
    today     = date.today()
    yesterday = today - timedelta(days=1)
    con = sqlite3.connect(DB_PATH)
    yesterday_row = con.execute(
        "SELECT id FROM sg_round WHERE user_id=? AND puzzle_date=?",
        (user_id, yesterday.isoformat())
    ).fetchone()
    con.close()
    if yesterday_row:
        return {"available": False, "date": None}
    # Only offer raincheck if yesterday actually has a published puzzle
    yesterday_puzzle = get_puzzle(yesterday.isoformat())
    if not yesterday_puzzle:
        return {"available": False, "date": None}
    return {"available": True, "date": yesterday.isoformat()}

def get_community_stats(puzzle_date):
    """Completed round count and average par_diff for a puzzle date."""
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        """SELECT COUNT(*) as cnt, AVG(total_diff) as avg_diff
           FROM (
             SELECT h.user_id, SUM(h.par_diff) as total_diff
             FROM sg_hole_result h
             JOIN sg_round r ON r.user_id=h.user_id AND r.puzzle_date=h.puzzle_date
             WHERE h.puzzle_date=? AND r.completed=1
             GROUP BY h.user_id
             HAVING COUNT(h.hole_number)=3
           )""",
        (puzzle_date,)
    ).fetchone()
    con.close()
    count = row[0] or 0
    avg   = round(row[1], 1) if row[1] is not None else None
    return {"completed_count": count, "avg_diff": avg}

def get_hole_difficulty_stats(puzzle_date):
    """Per-hole average par_diff across all completed rounds for a date."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        """SELECT h.hole_number, AVG(h.par_diff) as avg_diff, COUNT(*) as cnt
           FROM sg_hole_result h
           JOIN sg_round r ON r.user_id=h.user_id AND r.puzzle_date=h.puzzle_date
           WHERE h.puzzle_date=? AND r.completed=1
           GROUP BY h.hole_number
           ORDER BY h.hole_number""",
        (puzzle_date,)
    ).fetchall()
    con.close()
    return [{"hole_number": r[0], "avg_diff": round(r[1], 2), "count": r[2]} for r in rows]

def calculate_rarity(user_id, puzzle_date):
    """
    Average percentage of other completed players who chose the same athletes.
    Lower = rarer picks = more bragging rights.
    Returns None if fewer than 2 completed players.
    """
    con = sqlite3.connect(DB_PATH)
    try:
        total = con.execute(
            "SELECT COUNT(DISTINCT user_id) FROM sg_round WHERE puzzle_date=? AND completed=1",
            (puzzle_date,)
        ).fetchone()[0]
        if total <= 1:
            return None

        user_holes = con.execute(
            "SELECT shots FROM sg_hole_result WHERE user_id=? AND puzzle_date=?",
            (user_id, puzzle_date)
        ).fetchall()
        user_picks = set()
        for (sj,) in user_holes:
            for shot in json.loads(sj):
                user_picks.add((shot["player_id"], shot["stat_category"]))
        if not user_picks:
            return None

        # Build pick→user_ids map for all other completed players
        other_results = con.execute(
            """SELECT h.user_id, h.shots FROM sg_hole_result h
               JOIN sg_round r ON r.user_id=h.user_id AND r.puzzle_date=h.puzzle_date
               WHERE h.puzzle_date=? AND r.completed=1 AND h.user_id!=?""",
            (puzzle_date, user_id)
        ).fetchall()
        pick_users = {}
        for (uid, sj) in other_results:
            for shot in json.loads(sj):
                key = (shot["player_id"], shot["stat_category"])
                pick_users.setdefault(key, set()).add(uid)

        rarity_pcts = [
            len(pick_users.get(pick, set())) / (total - 1) * 100
            for pick in user_picks
        ]
        return round(sum(rarity_pcts) / len(rarity_pcts), 1)
    finally:
        con.close()

def detect_landmark_candidates():
    """Auto-flag top 2% values per stat_category as landmark candidates."""
    con = sqlite3.connect(DB_PATH)
    categories = [r[0] for r in con.execute(
        "SELECT DISTINCT stat_category FROM sg_stat_values"
    ).fetchall()]
    inserted = 0
    for cat in categories:
        rows = con.execute(
            """SELECT player_name, player_id, sport, season_year, value
               FROM sg_stat_values WHERE stat_category=?
               ORDER BY value DESC""",
            (cat,)
        ).fetchall()
        cutoff = max(1, int(len(rows) * 0.02))
        for pname, pid, sport, yr, val in rows[:cutoff]:
            try:
                con.execute(
                    """INSERT OR IGNORE INTO sg_landmark_stats
                       (player_name, player_id, sport, stat_category, season_year,
                        value, landmark_type, is_active)
                       VALUES (?,?,?,?,?,?,'candidate',0)""",
                    (pname, pid, sport, cat, yr, val)
                )
                inserted += 1
            except Exception:
                pass
    con.commit()
    con.close()
    return inserted

def get_all_landmarks():
    """Return all landmark records ordered by category then value desc."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT * FROM sg_landmark_stats
           ORDER BY is_active DESC, stat_category, value DESC"""
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]

def set_landmark_active(landmark_id, is_active):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE sg_landmark_stats SET is_active=? WHERE id=?",
        (1 if is_active else 0, landmark_id)
    )
    con.commit()
    con.close()

def update_landmark_description(landmark_id, description):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE sg_landmark_stats SET description=? WHERE id=?",
        (description, landmark_id)
    )
    con.commit()
    con.close()

def get_landmark_callouts(user_id, puzzle_date):
    """Check if any of the user's shots matched or cleared an active landmark value."""
    con = sqlite3.connect(DB_PATH)
    active = con.execute(
        """SELECT stat_category, value, player_name, description, season_year
           FROM sg_landmark_stats WHERE is_active=1""",
    ).fetchall()
    if not active:
        con.close()
        return []
    landmark_map = {}
    for cat, val, pname, desc, yr in active:
        landmark_map.setdefault(cat, []).append(
            {"value": val, "player": pname, "desc": desc, "year": yr}
        )
    hole_results = con.execute(
        "SELECT shots FROM sg_hole_result WHERE user_id=? AND puzzle_date=?",
        (user_id, puzzle_date)
    ).fetchall()
    con.close()
    callouts = []
    seen = set()
    for (sj,) in hole_results:
        for shot in json.loads(sj):
            cat  = shot.get("stat_category", "")
            dist = shot.get("distance", 0)
            if cat not in landmark_map:
                continue
            for lm in landmark_map[cat]:
                lm_val = round(lm["value"])
                if abs(dist - lm_val) <= max(5, int(lm_val * 0.04)):
                    key = (lm["player"], lm_val)
                    if key in seen:
                        continue
                    seen.add(key)
                    label = lm["desc"] if lm["desc"] else f"{lm['player']} ({lm['year']})"
                    callouts.append({
                        "shot_player": shot.get("player_name", ""),
                        "distance": dist,
                        "landmark_player": lm["player"],
                        "landmark_value": lm_val,
                        "label": label,
                        "cleared": dist >= lm_val,
                    })
    return callouts

def optimal_path_strokes(target, driver_reach, approach_reaches, gimme):
    """Fewest shots a knowledgeable player needs to hole out.

    Because a player may pick ANY athlete (any value from ~0 up to a club's reach),
    the optimal line is: the Driver first (cover up to its reach without overshooting),
    then reuse the single LARGEST approach club, tuning the final shot to land within
    the gimme. The smaller clubs are never required for the optimal line — they exist to
    fix a misjudged shot and to cover short distances. Returns the stroke count, or a
    large sentinel (99) if the hole cannot be closed at all.
    """
    import math
    remaining = max(0.0, target - min(driver_reach, target))   # after the tee shot
    if remaining <= gimme:
        return 1
    big = max(approach_reaches) if approach_reaches else 0
    if big <= 0:
        return 99
    return 1 + math.ceil(remaining / big)

def generate_puzzle_candidates(driver_cats, n=5):
    """Generate n 3-hole puzzle packages — distance-first with optimal-path par.

    driver_cats: list of 3 stat_category keys chosen by the admin, one per hole.

    For each hole (given the admin's Driver category):
      1. Pick a 3-Wood (primary approach) whose reach is a fraction of the Driver's —
         this sets how many shots the optimal line takes.
      2. Build 5-Iron / 9-Iron / Putter as a descending, log-spaced ladder of correction
         clubs down to a genuinely SHORT Putter (smallest-reach category available) that
         can nudge the ball within the gimme. A big stat like OPS+ is never the Putter.
      3. Choose the target from the optimal line: Driver + a whole number of 3-Wood
         shots (+ a partial finish). The target always sits ABOVE the Driver's reach, so
         the forced tee shot never auto-overshoots.
      4. Par = optimal-path strokes + 1, clamped to [3, 6].
    Every club in the bag is smaller than the hole and useful: the big ones drive the
    optimal line, the small ones fix mistakes and close out short distances.
    """
    import random, math

    con = sqlite3.connect(DB_PATH)
    cat_info = {}
    for cat, meta in STATGOLF_CATEGORIES.items():
        row = con.execute(
            "SELECT COUNT(*) FROM sg_stat_values WHERE stat_category=?", (cat,)
        ).fetchone()
        if row and row[0] >= 5:
            cat_info[cat] = {"reach": category_reach(con, cat),
                             "sport": meta["sport"], "label": meta["label"]}
    con.close()

    by_reach = sorted(cat_info.items(), key=lambda x: x[1]["reach"])   # ascending
    reach_of = lambda c: cat_info[c]["reach"]
    CLUB_NAMES = ["Driver", "3-Wood", "5-Iron", "9-Iron", "Putter"]
    gimme = STATGOLF_GIMME["hard"]

    def round_target(x):
        if x >= 50_000: return max(10_000, round(x / 5_000) * 5_000)
        if x >= 10_000: return max(2_000,  round(x / 1_000) * 1_000)
        if x >= 1_000:  return max(200,    round(x / 100)   * 100)
        if x >= 100:    return max(100,    round(x / 50)    * 50)
        return max(50, round(x / 10) * 10)

    packages = []
    for _ in range(n * 10):
        try:
            holes = []
            for hn, driver_cat in enumerate(driver_cats):
                if driver_cat not in cat_info:
                    raise ValueError(f"Category {driver_cat} not in database")
                D = reach_of(driver_cat)
                # Pool of categories smaller than the Driver — every approach club comes
                # from here, so no club is ever bigger than the hole. Excludes tiny-reach
                # stats (reach < MIN_CLUB_REACH, e.g. steals/blocks per game) which players
                # don't engage with; the remaining stats have enough range that a low pick
                # still fine-tunes near the pin (like points per game). Need 4 for a full bag.
                below = [c for c, info in by_reach
                         if MIN_CLUB_REACH <= info["reach"] < D and c != driver_cat]
                if len(below) < 4:
                    raise ValueError("driver too small for a full 5-club bag")

                # Putter: the most useful short club — random among the 3 smallest eligible
                # below the Driver (a recognizable, variable stat, never a tiny rate stat).
                putter = random.choice(below[:3])
                rest = [c for c in below if c != putter]            # ascending, >= 3 left

                # 3-Wood: the primary approach that drives the optimal line — one of the two
                # largest below the Driver (keeps maximum ladder room beneath it).
                three = random.choice(rest[-2:])
                mids = [c for c in rest if c != three]              # >= 2 left
                rp, r1 = reach_of(putter), reach_of(three)

                # 5-Iron / 9-Iron: two mid clubs, log-spaced between Putter and 3-Wood so the
                # bag has a correction club at every scale between the pin and the tee.
                if len(mids) >= 2 and r1 > rp > 0:
                    t5 = rp * (r1 / rp) ** (2 / 3)
                    t9 = rp * (r1 / rp) ** (1 / 3)
                    five = min(mids, key=lambda c: abs(reach_of(c) - t5))
                    nine = min([c for c in mids if c != five], key=lambda c: abs(reach_of(c) - t9))
                else:
                    five, nine = mids[-1], mids[0]

                approaches = sorted({three, five, nine, putter}, key=reach_of, reverse=True)
                if len(approaches) < 4:
                    raise ValueError("could not build a full 5-club bag")
                r1 = reach_of(approaches[0])     # biggest approach drives the optimal line

                # ── Target (distance-first): Driver + A whole 3-Wood shots + a finish ──
                A = random.choices([1, 2, 3], weights=[3, 4, 2])[0]   # approach shots
                remaining = r1 * (A - 1) + r1 * random.uniform(0.40, 0.92)
                target = round_target(D + remaining)
                if target <= D:                  # must sit above the Driver
                    target = round_target(D + r1 * 0.6)

                # ── Par = optimal-path strokes + 1 (clamped 3–6) ──
                strokes = optimal_path_strokes(target, D, [reach_of(a) for a in approaches], gimme)
                par = max(3, min(6, strokes + 1))

                club_bag = ([{"club": CLUB_NAMES[0], "stat_category": driver_cat}] +
                            [{"club": CLUB_NAMES[i + 1], "stat_category": approaches[i]}
                             for i in range(4)])

                # ── Hazards (band edges always rounded to a multiple of 10) ──
                r10 = lambda v: max(0, int(round(v / 10.0) * 10))
                hazards = []
                landing = target - round(D)      # remaining after a strong drive
                if landing > 30:
                    buf = max(15, int(round(D) * 0.08))
                    hazards.append({"type": "bunker",
                                    "start": r10(landing + buf), "end": max(10, r10(landing - buf))})
                water_center = round(target * random.uniform(0.25, 0.70))
                water_buf = max(20, round(target * 0.05))
                if abs(water_center - landing) > water_buf + 15:
                    hazards.append({"type": "water",
                                    "start": r10(water_center + water_buf), "end": max(10, r10(water_center - water_buf))})

                holes.append({
                    "hole_number":      hn + 1,
                    "target_distance":  target,
                    "par":              par,
                    "club_bag":         club_bag,
                    "hazard_bands":     hazards,
                    "sport":            cat_info[driver_cat]["sport"],
                    "primary_category": driver_cat,
                })

            ok, _ = validate_solvability(holes, gimme)
            if not ok:
                continue

            # Score package for variety
            pars    = [h["par"] for h in holes]
            targets = [h["target_distance"] for h in holes]
            score   = len(set(pars)) * 20
            if min(targets) > 0:
                ratio = max(targets) / min(targets)
                score += 20 if ratio >= 3 else 10 if ratio >= 1.5 else 0
            score += sum(1 for h in holes if len(h["hazard_bands"]) >= 2) * 15
            if len(set(pars)) == 1:
                score -= 10

            packages.append({"holes": holes, "score": score})
            if len(packages) >= n:
                break

        except Exception as _e:
            if os.environ.get("SG_GEN_DEBUG"):
                import sys; print("gen-fail:", repr(_e), file=sys.stderr)
            continue

    packages.sort(key=lambda p: p["score"], reverse=True)
    return packages[:n]

def validate_solvability(holes, gimme):
    """
    Check each hole is (a) reachable — the clubs' max values can sum to the target — and
    (b) not auto-overshot — the target sits at/above the Driver's reach, so the forced
    tee shot (best pick) doesn't blow past the pin on swing one.
    Returns (ok: bool, message: str).
    """
    con = sqlite3.connect(DB_PATH)
    for hole in holes:
        target = hole["target_distance"]
        bag = hole["club_bag"]
        total_max = 0
        driver_reach = 0
        for club in bag:
            row = con.execute(
                "SELECT MAX(value) FROM sg_stat_values WHERE stat_category=?",
                (club["stat_category"],)
            ).fetchone()
            total_max += apply_rounding(row[0] or 0, club["stat_category"])
            if club["club"] == "Driver":
                driver_reach = category_reach(con, club["stat_category"])
        # Reachable if max possible total >= target - gimme
        if total_max < target - gimme:
            con.close()
            return False, (
                f"Hole {hole['hole_number']}: max possible distance {total_max} "
                f"cannot reach target {target} (need {target - gimme} with gimme {gimme})"
            )
        # The Driver's best pick must not overshoot the pin on the opening shot.
        if driver_reach and target < driver_reach - gimme:
            con.close()
            return False, (
                f"Hole {hole['hole_number']}: target {target} is below the Driver's reach "
                f"{round(driver_reach)} — the forced tee shot would overshoot the pin"
            )
    con.close()
    return True, "ok"
