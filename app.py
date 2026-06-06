import os
import re
import json
import random
import secrets
import sqlite3
from datetime import date
from flask import Flask, jsonify, request, render_template, session
from dotenv import load_dotenv
import models
load_dotenv()

# Scorecard number-overlay layout lives in a root file you can edit (see scorecard_layout.json).
_CARD_LAYOUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scorecard_layout.json")
_CARD_LAYOUT_DEFAULT = {
    "width": 2200, "height": 537,
    "ink": "#1a2a1e", "diffPos": "#C0392B", "diffNeg": "#1F6FB0",
    "size": 34, "totSize": 38,
    "cols": {"label": 281, "h1": 639, "h2": 997, "h3": 1356, "tot": 1714, "rarity": 2022},
    "rows": {"date": 80, "dist": 245, "par": 322, "score": 400, "sub": 473},
}

def _load_card_layout():
    """Read scorecard_layout.json fresh each request (edits apply on refresh). Falls back
    to a built-in default if the file is missing or invalid, so the card never breaks."""
    try:
        with open(_CARD_LAYOUT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return _CARD_LAYOUT_DEFAULT

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

_USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{2,20}$')

# ── AUTH HELPERS ──────────────────────────────────────────────────────────────

def _get_claims():
    """Extract and verify caller identity from session or Bearer token.
    Returns dict with user_id and username, or None."""
    if session.get("user_id"):
        return {"user_id": session["user_id"], "username": session["username"]}
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return models.verify_token(app.secret_key, auth[7:])
    token = request.json.get("token") if request.is_json else None
    if token:
        return models.verify_token(app.secret_key, token)
    return None

def _require_auth():
    claims = _get_claims()
    if not claims:
        return None, jsonify({"error": "Authentication required"}), 401
    return claims, None, None

def _require_admin():
    claims = _get_claims()
    if not claims:
        return None, jsonify({"error": "Authentication required"}), 401
    if not models.is_admin(claims["username"]):
        return None, jsonify({"error": "Admin access required"}), 403
    return claims, None, None

def _get_claims_or_guest():
    """Return real auth claims, or auto-create/reuse a guest session. Never returns None."""
    claims = _get_claims()
    if claims:
        return claims
    if not session.get("guest_id"):
        session["guest_id"] = random.randint(8_000_000_000, 8_999_999_999)
    return {"user_id": session["guest_id"], "username": "Guest", "is_guest": True}

def _today():
    return date.today().isoformat()

# ── AUTH ROUTES ───────────────────────────────────────────────────────────────

@app.post("/api/login")
def api_login():
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400
    user = models.get_statcheck_user(username)
    if not user or not models.verify_password(user["password_hash"], password):
        return jsonify({"error": "Invalid username or password"}), 401
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    token = models.make_token(app.secret_key, user["id"], user["username"])
    return jsonify({
        "user_id": user["id"],
        "username": user["username"],
        "token": token,
        "is_admin": models.is_admin(user["username"]),
    })

@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})

@app.post("/api/register")
def api_register():
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not _USERNAME_RE.match(username):
        return jsonify({"error": "Username must be 2–20 alphanumeric characters or underscores"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    user = models.create_statcheck_user(username, password)
    if not user:
        return jsonify({"error": "Username already taken"}), 409
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    token = models.make_token(app.secret_key, user["id"], user["username"])
    return jsonify({
        "user_id": user["id"],
        "username": user["username"],
        "token": token,
        "is_admin": models.is_admin(user["username"]),
    })

@app.get("/api/me")
def api_me():
    claims = _get_claims()
    if not claims:
        return jsonify({"user": None})
    return jsonify({
        "user": {
            "user_id": claims["user_id"],
            "username": claims["username"],
            "is_admin": models.is_admin(claims["username"]),
        }
    })

# ── PUZZLE ────────────────────────────────────────────────────────────────────

@app.get("/api/statgolf/puzzle")
def api_puzzle():
    puzzle_date = request.args.get("date", _today())
    holes = models.get_puzzle(puzzle_date)
    if not holes:
        return jsonify({"error": "No puzzle found for this date"}), 404
    # Strip creator info from public response
    public = []
    for h in holes:
        public.append({
            "hole_number": h["hole_number"],
            "target_distance": h["target_distance"],
            "par": h["par"],
            "club_bag": h["club_bag"],
            "hazard_bands": h["hazard_bands"],
        })
    return jsonify({"date": puzzle_date, "holes": public})

# ── ROUND ─────────────────────────────────────────────────────────────────────

@app.post("/api/statgolf/start")
def api_start():
    claims = _get_claims_or_guest()
    data = request.get_json(force=True)
    difficulty = data.get("difficulty", "easy")
    if difficulty not in ("easy", "hard"):
        return jsonify({"error": "difficulty must be 'easy' or 'hard'"}), 400
    puzzle_date  = data.get("date", _today())
    is_raincheck = bool(data.get("is_raincheck", False))
    holes = models.get_puzzle(puzzle_date)
    if not holes:
        return jsonify({"error": "No puzzle available for this date"}), 404
    existing = models.get_round(claims["user_id"], puzzle_date)
    if existing:
        return jsonify({"error": "Round already started for this date"}), 409
    rnd = models.create_round(claims["user_id"], puzzle_date, difficulty, is_raincheck)
    return jsonify(_round_response(rnd, holes))

@app.get("/api/statgolf/round")
def api_get_round():
    claims = _get_claims_or_guest()
    puzzle_date = request.args.get("date", _today())
    rnd = models.get_round(claims["user_id"], puzzle_date)
    if not rnd:
        return jsonify({"round": None})
    holes = models.get_puzzle(puzzle_date)
    return jsonify(_round_response(rnd, holes))

@app.post("/api/statgolf/shot")
def api_shot():
    claims = _get_claims_or_guest()
    data = request.get_json(force=True)
    puzzle_date = data.get("date", _today())

    rnd = models.get_round(claims["user_id"], puzzle_date)
    if not rnd:
        return jsonify({"error": "No active round. Call /api/statgolf/start first"}), 404
    if rnd["completed"]:
        return jsonify({"error": "Round already completed"}), 400

    holes = models.get_puzzle(puzzle_date)
    hole_idx = rnd["current_hole"] - 1
    if hole_idx >= len(holes):
        return jsonify({"error": "All holes completed"}), 400
    hole = holes[hole_idx]

    state = rnd["state"]

    if state.get("pending_water"):
        return jsonify({"error": "Resolve water hazard choice first via /api/statgolf/water"}), 400

    club_name  = data.get("club")
    player_id  = data.get("player_id")
    season_year = data.get("season_year")

    if not club_name or not player_id or not season_year:
        return jsonify({"error": "club, player_id, and season_year are required"}), 400

    # Validate club exists in this hole's bag
    bag = {c["club"]: c["stat_category"] for c in hole["club_bag"]}
    if club_name not in bag:
        return jsonify({"error": f"Club '{club_name}' not in this hole's bag"}), 400

    # Driver must be first shot
    if not state["clubs_used"] and club_name != "Driver":
        return jsonify({"error": "First shot must use the Driver"}), 400
    if state["clubs_used"] and club_name == "Driver":
        return jsonify({"error": "Driver can only be used for the tee shot"}), 400

    # A player can only be used once per club within a hole
    for past in state["shot_history"]:
        if past.get("club") == club_name and str(past.get("player_id")) == str(player_id):
            return jsonify({"error": f"You've already used that player with the {club_name} on this hole"}), 400

    stat_category = bag[club_name]
    raw_value, player_name_resolved = models.get_stat(stat_category, player_id, int(season_year))
    if raw_value is None:
        return jsonify({"error": "No stat found for this player/season/category"}), 404

    distance = models.apply_rounding(raw_value, stat_category)

    # Apply bunker debuff (halve distance, rounded down)
    debuff_applied = False
    if state["bunker_debuff"]:
        distance = distance // 2
        debuff_applied = True
        state["bunker_debuff"] = False

    pre_shot_total = state["running_total"]
    was_overshot = pre_shot_total > hole["target_distance"]
    if was_overshot:
        state["running_total"] = max(0, pre_shot_total - distance)
    else:
        state["running_total"] += distance
    state["strokes"] += 1
    remaining = abs(hole["target_distance"] - state["running_total"])
    overshot_now = state["running_total"] > hole["target_distance"]

    # Check hazards — only on the approach side (ball short of the pin). Past the pin
    # you're beyond the fairway hazards, so overshooting by N yards must NOT trigger a
    # hazard that sits N yards short of the green.
    hazard = None if overshot_now else models.check_hazards(remaining, hole["hazard_bands"])
    effect = None

    shot_record = {
        "club": club_name,
        "stat_category": stat_category,
        "player_name": player_name_resolved,
        "player_id": player_id,
        "season_year": int(season_year),
        "raw_value": raw_value,
        "distance": distance,
        "debuff_applied": debuff_applied,
        "effect": None,
    }

    if hazard:
        if hazard["type"] == "bunker":
            state["bunker_debuff"] = True
            effect = "bunker"
        elif hazard["type"] == "water":
            state["pending_water"] = {
                "band": hazard,
                "pre_shot_total": pre_shot_total,
                "drop_position": models.drop_position(hazard),
            }
            effect = "water"

    shot_record["effect"] = effect
    state["clubs_used"].append(club_name)
    state["shot_history"].append(shot_record)

    gimme = models.STATGOLF_GIMME[rnd["difficulty"]]
    hole_complete = False

    if hazard and hazard["type"] == "water":
        # Can't auto-complete while water is pending
        pass
    elif remaining <= gimme:
        hole_complete = True

    if hole_complete:
        result = _complete_hole(claims["user_id"], puzzle_date, rnd, hole, state)
        return jsonify(result)

    models.update_round_state(claims["user_id"], puzzle_date, state)
    return jsonify({
        "shot": shot_record,
        "state": _public_state(state, hole, rnd["difficulty"]),
        "hole_complete": False,
    })

@app.post("/api/statgolf/water")
def api_water():
    claims = _get_claims_or_guest()
    data = request.get_json(force=True)
    puzzle_date = data.get("date", _today())
    choice = data.get("choice")  # "drop" or "rehit"

    if choice not in ("drop", "rehit"):
        return jsonify({"error": "choice must be 'drop' or 'rehit'"}), 400

    rnd = models.get_round(claims["user_id"], puzzle_date)
    if not rnd:
        return jsonify({"error": "No active round"}), 404

    state = rnd["state"]
    pending = state.get("pending_water")
    if not pending:
        return jsonify({"error": "No pending water hazard"}), 400

    holes = models.get_puzzle(puzzle_date)
    hole = holes[rnd["current_hole"] - 1]
    gimme = models.STATGOLF_GIMME[rnd["difficulty"]]

    if choice == "drop":
        state["running_total"] = hole["target_distance"] - pending["drop_position"]
        state["strokes"] += 1  # penalty stroke
        state["pending_water"] = None
        effect = "drop"
    else:  # rehit
        # Revert running_total, free the club
        state["running_total"] = pending["pre_shot_total"]
        last_shot = state["shot_history"][-1]
        state["clubs_used"].remove(last_shot["club"])
        state["pending_water"] = None
        effect = "rehit"

    remaining = abs(hole["target_distance"] - state["running_total"])
    hole_complete = remaining <= gimme

    if hole_complete:
        result = _complete_hole(claims["user_id"], puzzle_date, rnd, hole, state)
        result["water_choice"] = effect
        return jsonify(result)

    models.update_round_state(claims["user_id"], puzzle_date, state)
    return jsonify({
        "water_choice": effect,
        "state": _public_state(state, hole, rnd["difficulty"]),
        "hole_complete": False,
    })

@app.get("/api/statgolf/result")
def api_result():
    claims = _get_claims_or_guest()
    puzzle_date = request.args.get("date", _today())
    rnd = models.get_round(claims["user_id"], puzzle_date)
    if not rnd or not rnd["completed"]:
        return jsonify({"error": "Round not completed"}), 404
    results    = models.get_hole_results(claims["user_id"], puzzle_date)
    total      = sum(r["par_diff"] for r in results)
    rarity     = models.calculate_rarity(claims["user_id"], puzzle_date)
    hole_stats = models.get_hole_difficulty_stats(puzzle_date)
    callouts   = models.get_landmark_callouts(claims["user_id"], puzzle_date)
    community  = models.get_community_stats(puzzle_date)
    streak     = models.get_streak(claims["user_id"]) if not claims.get("is_guest") else None
    return jsonify({
        "date": puzzle_date,
        "difficulty": rnd["difficulty"],
        "holes": results,
        "total_par_diff": total,
        "score_label": models.score_label(total),
        "share_string": models.share_string(puzzle_date, results, rarity),
        "rarity": rarity,
        "hole_difficulty": hole_stats,
        "landmark_callouts": callouts,
        "community": community,
        "streak": streak,
    })

@app.get("/api/statgolf/streak")
def api_streak():
    claims, err, code = _require_auth()
    if err:
        return err, code
    streak   = models.get_streak(claims["user_id"])
    raincheck = models.get_raincheck_info(claims["user_id"])
    return jsonify({"streak": streak, "raincheck": raincheck})

@app.get("/api/statgolf/hints")
def api_hints():
    return jsonify({"hints": models.get_category_hints()})

@app.get("/api/statgolf/community")
def api_community():
    # Admin-only: players-finished / avg-score is not public information.
    claims, err, code = _require_admin()
    if err:
        return err, code
    puzzle_date = request.args.get("date", _today())
    return jsonify(models.get_community_stats(puzzle_date))

@app.get("/api/statgolf/history")
def api_history():
    claims, err, code = _require_auth()
    if err:
        return err, code
    history = models.get_history(claims["user_id"])
    return jsonify({"history": history})

# ── AUTOCOMPLETE ──────────────────────────────────────────────────────────────

@app.get("/api/statgolf/autocomplete")
def api_autocomplete():
    category = request.args.get("category", "")
    query = request.args.get("q", "")
    if not category:
        return jsonify({"error": "category required"}), 400
    if len(query) < 2:
        return jsonify({"players": []})
    players = models.search_players(category, query)
    return jsonify({"players": players})

@app.get("/api/statgolf/seasons")
def api_seasons():
    category = request.args.get("category", "")
    player_id = request.args.get("player_id", "")
    if not category or not player_id:
        return jsonify({"error": "category and player_id required"}), 400
    seasons = models.get_player_seasons(category, player_id)
    return jsonify({"seasons": seasons})

# ── ADMIN ─────────────────────────────────────────────────────────────────────

@app.get("/api/admin/landmarks")
def api_admin_landmarks():
    claims, err, code = _require_admin()
    if err:
        return err, code
    return jsonify({"landmarks": models.get_all_landmarks()})

@app.post("/api/admin/landmarks/detect")
def api_admin_detect_landmarks():
    claims, err, code = _require_admin()
    if err:
        return err, code
    inserted = models.detect_landmark_candidates()
    return jsonify({"ok": True, "inserted": inserted})

@app.post("/api/admin/landmarks/toggle")
def api_admin_toggle_landmark():
    claims, err, code = _require_admin()
    if err:
        return err, code
    data = request.get_json(force=True)
    lm_id     = data.get("id")
    raw_active = data.get("is_active")
    desc       = data.get("description")
    if not lm_id:
        return jsonify({"error": "id required"}), 400
    if raw_active is not None:
        models.set_landmark_active(lm_id, bool(raw_active))
    if desc is not None:
        models.update_landmark_description(lm_id, desc)
    return jsonify({"ok": True})

@app.post("/api/admin/generate")
def api_admin_generate():
    claims, err, code = _require_admin()
    if err:
        return err, code
    data = request.get_json(force=True)
    driver_cats = data.get("drivers", [])
    n = int(data.get("n", 5))
    if len(driver_cats) != 3:
        return jsonify({"error": "Exactly 3 driver categories required"}), 400
    for cat in driver_cats:
        if cat not in models.STATGOLF_CATEGORIES:
            return jsonify({"error": f"Unknown category: {cat}"}), 400
    if len(set(driver_cats)) != 3:
        return jsonify({"error": "Each hole must have a different driver category"}), 400
    packages = models.generate_puzzle_candidates(driver_cats, n)
    return jsonify({"packages": packages})

@app.get("/api/admin/categories")
def api_categories():
    claims, err, code = _require_admin()
    if err:
        return err, code
    return jsonify({"categories": models.STATGOLF_CATEGORIES, "clubs": models.STATGOLF_CLUBS})

@app.get("/api/admin/puzzle")
def api_admin_get_puzzle():
    claims, err, code = _require_admin()
    if err:
        return err, code
    puzzle_date = request.args.get("date", _today())
    holes = models.get_puzzle_any(puzzle_date)
    return jsonify({"date": puzzle_date, "holes": holes})

@app.post("/api/admin/puzzle")
def api_admin_save_puzzle():
    claims, err, code = _require_admin()
    if err:
        return err, code
    data = request.get_json(force=True)
    puzzle_date = data.get("date")
    holes = data.get("holes", [])
    publish = data.get("publish", False)

    if not puzzle_date:
        return jsonify({"error": "date required"}), 400
    if not holes or len(holes) != 3:
        return jsonify({"error": "exactly 3 holes required"}), 400

    for hole in holes:
        hn = hole.get("hole_number")
        if hn not in (1, 2, 3):
            return jsonify({"error": "hole_number must be 1, 2, or 3"}), 400
        bag = hole.get("club_bag", [])
        if not bag:
            return jsonify({"error": f"Hole {hn} has no clubs"}), 400
        clubs_in_bag = [c["club"] for c in bag]
        if "Driver" not in clubs_in_bag:
            return jsonify({"error": f"Hole {hn} must include a Driver"}), 400
        for club in bag:
            if club.get("stat_category") not in models.STATGOLF_CATEGORIES:
                return jsonify({"error": f"Unknown stat_category: {club.get('stat_category')}"}), 400

    if publish:
        gimme = models.STATGOLF_GIMME.get("hard", 10)
        hole_dicts = [{"hole_number": h["hole_number"],
                       "target_distance": h["target_distance"],
                       "club_bag": h["club_bag"]} for h in holes]
        ok, msg = models.validate_solvability(hole_dicts, gimme)
        if not ok:
            return jsonify({"error": f"Solvability check failed: {msg}"}), 400

    for hole in holes:
        models.upsert_puzzle_hole(
            puzzle_date=puzzle_date,
            hole_number=hole["hole_number"],
            target_distance=hole["target_distance"],
            par=hole["par"],
            club_bag=hole["club_bag"],
            hazard_bands=hole.get("hazard_bands", []),
            created_by=claims["username"],
            published=1 if publish else 0,
        )

    return jsonify({"ok": True, "date": puzzle_date, "published": publish})

@app.post("/api/admin/publish")
def api_admin_publish():
    claims, err, code = _require_admin()
    if err:
        return err, code
    data = request.get_json(force=True)
    puzzle_date = data.get("date")
    if not puzzle_date:
        return jsonify({"error": "date required"}), 400
    holes = models.get_puzzle_any(puzzle_date)
    if len(holes) != 3:
        return jsonify({"error": "Need all 3 holes before publishing"}), 400
    gimme = models.STATGOLF_GIMME.get("hard", 10)
    ok, msg = models.validate_solvability(holes, gimme)
    if not ok:
        return jsonify({"error": f"Solvability check failed: {msg}"}), 400
    models.publish_puzzle(puzzle_date)
    return jsonify({"ok": True, "date": puzzle_date})

@app.post("/api/admin/validate")
def api_admin_validate():
    claims, err, code = _require_admin()
    if err:
        return err, code
    data = request.get_json(force=True)
    holes = data.get("holes", [])
    difficulty = data.get("difficulty", "hard")
    gimme = models.STATGOLF_GIMME.get(difficulty, 10)
    ok, msg = models.validate_solvability(holes, gimme)
    suggestions = []
    for hole in holes:
        suggestions.append({
            "hole_number": hole.get("hole_number"),
            "suggested_par": models.suggest_par(hole),
        })
    return jsonify({"ok": ok, "message": msg, "suggestions": suggestions})

@app.get("/api/admin/upcoming")
def api_admin_upcoming():
    claims, err, code = _require_admin()
    if err:
        return err, code
    from_date = request.args.get("from", _today())
    rows = models.list_upcoming_puzzles(from_date)
    return jsonify({"puzzles": rows})

@app.post("/api/admin/playtest")
def api_admin_playtest():
    """Admin can start a playtest round even if a puzzle isn't published."""
    claims, err, code = _require_admin()
    if err:
        return err, code
    data = request.get_json(force=True)
    puzzle_date = data.get("date", _today())
    difficulty = data.get("difficulty", "easy")

    holes = models.get_puzzle_any(puzzle_date)
    if not holes:
        return jsonify({"error": "No puzzle for this date"}), 404

    # Delete any existing round so admin can restart
    con = sqlite3.connect(models.DB_PATH)
    con.execute("DELETE FROM sg_round WHERE user_id=? AND puzzle_date=?",
                (claims["user_id"], puzzle_date))
    con.execute("DELETE FROM sg_hole_result WHERE user_id=? AND puzzle_date=?",
                (claims["user_id"], puzzle_date))
    con.commit()
    con.close()

    rnd = models.create_round(claims["user_id"], puzzle_date, difficulty)
    return jsonify(_round_response(rnd, holes))

# ── GAME INTERNAL HELPERS ─────────────────────────────────────────────────────

def _complete_hole(user_id, puzzle_date, rnd, hole, state):
    """Finalize the current hole, advance to next, and return response."""
    models.save_hole_result(
        user_id, puzzle_date, hole["hole_number"],
        state["strokes"], hole["par"], state["shot_history"]
    )
    next_hole = rnd["current_hole"] + 1
    all_done = next_hole > 3

    if all_done:
        models.update_round_state(user_id, puzzle_date, {}, current_hole=next_hole, completed=True)
    else:
        fresh = models._fresh_hole_state(next_hole)
        models.update_round_state(user_id, puzzle_date, fresh, current_hole=next_hole)

    hole_result = {
        "hole_number": hole["hole_number"],
        "strokes": state["strokes"],
        "par": hole["par"],
        "par_diff": state["strokes"] - hole["par"],
        "score_label": models.score_label(state["strokes"] - hole["par"]),
        "shot_history": state["shot_history"],
    }

    resp = {
        "shot": state["shot_history"][-1],
        "state": _public_state(state, hole, rnd["difficulty"]),
        "hole_complete": True,
        "hole_result": hole_result,
        "round_complete": all_done,
    }
    if all_done:
        results = models.get_hole_results(user_id, puzzle_date)
        total = sum(r["par_diff"] for r in results)
        resp["round_result"] = {
            "holes": results,
            "total_par_diff": total,
            "score_label": models.score_label(total),
            "share_string": models.share_string(puzzle_date, results),
        }
    return resp

def _public_state(state, hole, difficulty):
    gimme = models.STATGOLF_GIMME[difficulty]
    remaining = abs(hole["target_distance"] - state["running_total"])
    overshoot = state["running_total"] > hole["target_distance"]
    pending_w = state.get("pending_water")
    return {
        "hole_number": state["hole_number"],
        "running_total": state["running_total"],
        "remaining": remaining,
        "overshoot": overshoot,
        "strokes": state["strokes"],
        "clubs_used": state["clubs_used"],
        "bunker_debuff": state["bunker_debuff"],
        "pending_water": {"drop_remaining": pending_w["drop_position"]} if pending_w else None,
        "gimme_threshold": gimme,
        "shot_history": state["shot_history"],
    }

def _round_response(rnd, holes):
    state = rnd["state"]
    if rnd["completed"] or not holes:
        current_hole_data = None
    else:
        idx = rnd["current_hole"] - 1
        if idx < len(holes):
            h = holes[idx]
            current_hole_data = {
                "hole_number": h["hole_number"],
                "target_distance": h["target_distance"],
                "par": h["par"],
                "club_bag": h["club_bag"],
                "hazard_bands": h["hazard_bands"],
            }
        else:
            current_hole_data = None
    return {
        "round": {
            "puzzle_date": rnd["puzzle_date"],
            "difficulty": rnd["difficulty"],
            "completed": bool(rnd["completed"]),
            "current_hole": rnd["current_hole"],
        },
        "state": _public_state(state, current_hole_data, rnd["difficulty"]) if current_hole_data and state else None,
        "current_hole": current_hole_data,
    }

# ── PAGES ─────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return render_template("index.html", card_layout=_load_card_layout())

@app.get("/admin")
def admin():
    return render_template("admin.html")

# ── STARTUP ───────────────────────────────────────────────────────────────────

models.init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5052)
