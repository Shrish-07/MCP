import json,sys
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from utils.market_calendar import is_market_open_at

if __name__=="__main__":
    try:
        state=json.loads((Path(__file__).resolve().parents[1]/"logs/health.json").read_text())
        if is_market_open_at():
            stamp=state.get("monitor",{}).get("last_success")
            healthy=bool(stamp) and (datetime.now(timezone.utc)-datetime.fromisoformat(stamp)).total_seconds()<90
            healthy=healthy and not state.get("broker",{}).get("error") and not state.get("quotes",{}).get("error")
        else:healthy=True
    except (OSError,ValueError):healthy=False
    print("healthy" if healthy else "unhealthy")
    sys.exit(0 if healthy else 1)
