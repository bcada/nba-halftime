#!/usr/bin/env python3
"""NBA halftime notifier (version 2).

Watches ESPN's public NBA scoreboard and sends a push notification to your
iPhone (through the free ntfy app) when a game reaches halftime.

Settings come from environment variables (set in GitHub, see SETUP.md):
  NTFY_TOPIC    required  your private ntfy topic name
  TEAMS         optional  comma-separated team abbreviations, e.g. "NY,BOS,LAL"
                          (leave empty for ALL games)
  NTFY_TOKEN    optional  ntfy access token (only needed if ntfy.sh starts
                          rejecting messages sent from GitHub's servers)
  RUN_MINUTES   optional  longest a single run keeps watching (default 330)
  POLL_SECONDS  optional  seconds between checks (default 20)

Command line:
  python notify.py --test     send a test notification and exit
  python notify.py --once     check once and exit
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SCOREBOARD_URL = os.environ.get(
    "SCOREBOARD_URL", "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
)
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
UPCOMING_WINDOW = timedelta(minutes=90)  # keep watching if a game starts within this long
IGNORED_STATUSES = {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED"}


def log(msg):
    print(f"[{datetime.now(ET).strftime('%a %H:%M:%S ET')}] {msg}", flush=True)


# ---------------------------------------------------------------- score feed

def http_get(url):
    req = urllib.request.Request(url, headers=auth_headers({"User-Agent": "halftime-notifier/2.0"}))
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", "replace")


def fetch_events():
    """Fetch yesterday's and today's slates (Eastern time) so a game that runs
    past midnight never drops off the feed. Returns a list of raw events."""
    today = datetime.now(ET).date()
    events, errors = {}, []
    for day in (today - timedelta(days=1), today):
        sep = "&" if "?" in SCOREBOARD_URL else "?"
        url = f"{SCOREBOARD_URL}{sep}dates={day.strftime('%Y%m%d')}"
        try:
            data = json.loads(http_get(url))
        except Exception as exc:
            errors.append(f"{day}: {exc}")
            continue
        for ev in data.get("events", []):
            events[str(ev.get("id"))] = ev
    if len(errors) == 2:
        raise RuntimeError("; ".join(errors))
    if errors:
        log(f"Partial feed problem (continuing): {errors[0]}")
    return list(events.values())


def is_halftime(status):
    """True for halftime however ESPN happens to label it."""
    t = status.get("type") or {}
    name, state = t.get("name", ""), t.get("state", "")
    period = status.get("period") or 0
    text = " ".join(str(t.get(k, "")) for k in ("description", "detail", "shortDetail")).lower()
    if name == "STATUS_HALFTIME" or "halftime" in text:
        return True
    # Some games show "End of 2nd Quarter" and never get a separate halftime label.
    if state == "in" and period == 2 and (name == "STATUS_END_PERIOD" or status.get("clock") == 0):
        return True
    return False


def parse_time(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def parse_game(event):
    comp = (event.get("competitions") or [{}])[0]
    status = comp.get("status") or event.get("status") or {}
    stype = status.get("type") or {}
    teams = {}
    for c in comp.get("competitors", []):
        team = c.get("team") or {}
        teams[c.get("homeAway", "?")] = {
            "abbr": team.get("abbreviation", "???"),
            "name": team.get("displayName", "Unknown"),
            "score": c.get("score", "0"),
        }
    blank = {"abbr": "???", "name": "Unknown", "score": "0"}
    return {
        "id": str(event.get("id")),
        "state": stype.get("state", ""),
        "name": stype.get("name", ""),
        "period": status.get("period") or 0,
        "halftime": is_halftime(status),
        "start": parse_time(event.get("date") or comp.get("date")),
        "away": teams.get("away", blank),
        "home": teams.get("home", blank),
    }


def label(g):
    if g["state"] == "pre":
        return "not started"
    if g["state"] == "post":
        return "final"
    if g["halftime"]:
        return "HALFTIME"
    return f"Q{g['period']}" if g["period"] <= 4 else "OT"


def matchup(g):
    return f"{g['away']['abbr']} @ {g['home']['abbr']}"


def margin_emoji(g):
    """Green square: tied or within 6. Yellow: 7-11. Red: 12 or more."""
    try:
        diff = abs(int(g["away"]["score"]) - int(g["home"]["score"]))
    except (TypeError, ValueError):
        return ""
    if diff <= 6:
        return "\U0001F7E9"  # green square
    if diff <= 11:
        return "\U0001F7E8"  # yellow square
    return "\U0001F7E5"  # red square


def score_line(g):
    text = f"{g['away']['name']} {g['away']['score']} - {g['home']['name']} {g['home']['score']}"
    emoji = margin_emoji(g)
    return f"{emoji} {text}" if emoji else text


def team_keys(g):
    """Abbreviations and full names of both teams, uppercased, for matching."""
    return {g[side][key].upper() for side in ("away", "home") for key in ("abbr", "name")}


def is_idle(games):
    """True when nothing is live and nothing starts soon (run can stop early)."""
    now = datetime.now(timezone.utc)
    for g in games:
        if g["state"] == "post" or g["name"] in IGNORED_STATUSES:
            continue
        if g["state"] == "pre" and g["start"] and g["start"] - now > UPCOMING_WINDOW:
            continue
        return False
    return True


# ------------------------------------------------------------------- ntfy

def auth_headers(headers):
    token = os.environ.get("NTFY_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def already_sent(topic, title):
    """Ask ntfy whether a message with this exact title already went out
    (guards against duplicates when one run hands over to the next)."""
    try:
        url = f"{NTFY_SERVER}/{topic}/json?poll=1&since=12h&title={urllib.parse.quote(title)}"
        for line in http_get(url).splitlines():
            try:
                if json.loads(line).get("event") == "message":
                    return True
            except ValueError:
                pass
    except Exception as exc:
        log(f"Could not check ntfy history ({exc}); sending anyway.")
    return False


def send_push(topic, title, body):
    """Send one notification. Returns ntfy's id for the message (for the log)."""
    headers = auth_headers({"Title": title, "Priority": "high"})
    last = "unknown error"
    for attempt, wait in enumerate((0, 3, 10), start=1):
        if wait:
            time.sleep(wait)
        try:
            req = urllib.request.Request(
                f"{NTFY_SERVER}/{topic}", data=body.encode("utf-8"), headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
            try:
                return json.loads(raw).get("id")
            except ValueError:
                return None
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code} {exc.read()[:200].decode('utf-8', 'replace')}"
        except Exception as exc:
            last = str(exc)
        log(f"Send attempt {attempt} of 3 failed: {last}")
    raise RuntimeError(last)


# ------------------------------------------------------------------- main

def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        sys.exit("NTFY_TOPIC is not set. See SETUP.md.")

    if "--test" in sys.argv:
        send_push(topic, "Test: halftime alerts are working", "If you can read this, setup is complete.")
        log("Test notification sent.")
        return

    teams_filter = {t.strip().upper() for t in os.environ.get("TEAMS", "").split(",") if t.strip()}
    # Games involving these teams never alert. Default: the Knicks.
    ignore_filter = {t.strip().upper() for t in os.environ.get("IGNORE_TEAMS", "").split(",") if t.strip()}
    ignore_filter = ignore_filter or {"NY", "NEW YORK KNICKS"}
    run_minutes = float(os.environ.get("RUN_MINUTES") or 330)
    poll_seconds = float(os.environ.get("POLL_SECONDS") or 20)
    once = "--once" in sys.argv

    notified, seen_first_half, warned, last_label = set(), set(), set(), {}
    deadline = time.time() + run_minutes * 60
    log(f"Watching NBA games ({'all teams' if not teams_filter else ', '.join(sorted(teams_filter))}).")

    def ignored(g):
        return bool(team_keys(g) & ignore_filter)

    def wanted(g):
        if ignored(g):
            return False
        return not teams_filter or bool(team_keys(g) & teams_filter)

    def alert(g, late=False):
        normal_title = f"Halftime: {matchup(g)}"
        if already_sent(topic, normal_title):
            notified.add(g["id"])
            log(f"{matchup(g)}: alert was already sent earlier; skipping.")
            return
        title = f"Halftime (late alert): {matchup(g)}" if late else normal_title
        body = score_line(g)
        if late:
            body += " (the halftime alert was delayed; second half is underway)"
        msg_id = send_push(topic, title, body)
        notified.add(g["id"])
        log(f"SENT {'late ' if late else ''}alert (accepted by ntfy, message id {msg_id}): {title} | {body}")

    while True:
        idle = False
        try:
            games = []
            for event in fetch_events():
                try:
                    games.append(parse_game(event))
                except Exception as exc:
                    if event.get("id") not in warned:  # say it once, not every check
                        warned.add(event.get("id"))
                        log(f"Skipping one unreadable game (id {event.get('id')}): {exc}")

            for g in games:
                lab = label(g)
                if last_label.get(g["id"]) != lab:  # log only when something changes
                    log(f"{matchup(g)}: {lab}" + (" (ignored team, no alert)" if ignored(g) else ""))
                    last_label[g["id"]] = lab
                if not wanted(g) or g["id"] in notified:
                    continue
                if g["state"] == "in" and g["period"] <= 2 and not g["halftime"]:
                    seen_first_half.add(g["id"])
                try:
                    if g["halftime"] and g["state"] != "post":
                        alert(g)
                    elif g["state"] == "in" and g["period"] >= 3 and g["id"] in seen_first_half:
                        log(f"{matchup(g)}: reached the second half with no halftime alert; sending late alert.")
                        alert(g, late=True)
                except Exception as exc:
                    log(f"Could not send alert for {matchup(g)}; will retry: {exc}")

            idle = is_idle(games)
        except Exception as exc:  # keep going on any network hiccup
            log(f"Check failed, will retry: {exc}")

        if idle:
            log("No live games and none starting within 90 minutes. Stopping; the next scheduled run will check again.")
            break
        if once or time.time() >= deadline:
            break
        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
