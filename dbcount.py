"""
Quick row-count helper — avoids PowerShell quoting pain.

Usage:
    python dbcount.py                      # every category, with row counts
    python dbcount.py career_games_played  # one category, broken out by sport
"""
import sqlite3
import sys
import models

con = sqlite3.connect(models.DB_PATH)
cat = sys.argv[1] if len(sys.argv) > 1 else None

if cat:
    total = con.execute(
        "SELECT COUNT(*) FROM sg_stat_values WHERE stat_category=?", (cat,)
    ).fetchone()[0]
    players = con.execute(
        "SELECT COUNT(DISTINCT player_id) FROM sg_stat_values WHERE stat_category=?", (cat,)
    ).fetchone()[0]
    print(f"{cat}: {total} rows, {players} players")
    for sport in ("nfl", "nba", "mlb", "nhl"):
        n = con.execute(
            "SELECT COUNT(*) FROM sg_stat_values WHERE stat_category=? AND sport=?",
            (cat, sport),
        ).fetchone()[0]
        if n:
            print(f"  {sport}: {n}")
else:
    rows = con.execute(
        "SELECT stat_category, COUNT(*) FROM sg_stat_values GROUP BY stat_category ORDER BY stat_category"
    ).fetchall()
    for c, n in rows:
        print(f"{c:34s} {n}")

con.close()
