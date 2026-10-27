#!/usr/bin/env python3
"""NBA halftime notifier.

Checks ESPN's public NBA scoreboard every few seconds and sends a push
notification to your iPhone (through the free ntfy app) the moment a game
reaches halftime. Each game only notifies once.

Settings come from environment variables (set in GitHub, see SETUP.md):
  NTFY_TOPIC    required  your private ntfy topic name
  TEAMS         optional  comma-separated team abbreviations, e.g. "NY,BOS,LAL"
                          (leave empty for ALL games)
  RUN_MINUTES   optional  how long to keep checking before stopping (default 350)
  POLL_SECONDS  optional  seconds between checks (default 30)

Command line:
  python notify.py --test     send a test notification and exit
  python notify.py --once     check once and exit
"""

import json
import os
import sys
import time
import urllib.request

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"


def fetch_scoreboard():
    req = urllib.request.Request(SCOREBOARD_URL, headers={"User-Agent": "halftime-notifier/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def halftime_games(data, teams_filter=None):
    """Return a list of (game_id, title, body) for games currently at halftime."""
    found = []
    for event in data.get("events", []):
        comps = event.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        status_type = (comp.get("status") or event.get("status") or {}).get("type", {})
        if status_type.get("name") != "STATUS_HALFTIME":
            continue

        teams = {}
        for c in comp.get("competitors", []):
            team = c.get("team", {})
            teams[c.get("homeAway", "?")] = {
                "abbr": team.get("abbreviation", "???"),
                "name": team.get("displayName", "Unknown"),
                "score": c.get("score", "0"),
            }
        away = teams.get("away", {"abbr": "???", "name": "Away", "score": "0"})
        home = teams.get("home", {"abbr": "???", "name": "Home", "score": "0"})

        if teams_filter and not ({away["abbr"].upper(), home["abbr"].upper()} & teams_filter):
            continue

        title = f"Halftime: {away['abbr']} @ {home['abbr']}"
        body = f"{away['name']} {away['score']} - {home['name']} {home['score']}"
        found.append((str(event.get("id")), title, body))
    return found


def send_push(topic, title, body):
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=body.encode("utf-8"),
        headers={
            "Title": title,
            "Tags": "basketball",
            "Priority": "high",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        sys.exit("NTFY_TOPIC is not set. See SETUP.md.")

    if "--test" in sys.argv:
        send_push(topic, "Test: halftime alerts are working", "If you can read this, setup is complete.")
        print("Test notification sent.")
        return

    teams_filter = {t.strip().upper() for t in os.environ.get("TEAMS", "").split(",") if t.strip()}
    run_minutes = float(os.environ.get("RUN_MINUTES") or 350)
    poll_seconds = float(os.environ.get("POLL_SECONDS") or 30)
    once = "--once" in sys.argv

    notified = set()
    deadline = time.time() + run_minutes * 60
    print(f"Watching NBA games ({'all teams' if not teams_filter else ', '.join(sorted(teams_filter))})...", flush=True)

    while True:
        try:
            data = fetch_scoreboard()
            for game_id, title, body in halftime_games(data, teams_filter):
                if game_id in notified:
                    continue
                send_push(topic, title, body)
                notified.add(game_id)
                print(f"Sent: {title} | {body}", flush=True)
        except Exception as exc:  # keep going on any network hiccup
            print(f"Check failed, will retry: {exc}", flush=True)

        if once or time.time() >= deadline:
            break
        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
