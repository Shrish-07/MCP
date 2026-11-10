"""Compare an exact request-window dashboard export with the version-2 ledger."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from utils.cost_tracker import reconcile_window
from utils.persistence import atomic_json

if __name__ == "__main__":
    p=argparse.ArgumentParser()
    p.add_argument("dashboard",help="JSON: request_ids, usd, optional rounding_tolerance_usd")
    p.add_argument("--start",required=True);p.add_argument("--end",required=True)
    p.add_argument("--records",default="logs/cost_records_v2.jsonl")
    p.add_argument("--output",default="logs/cost_reconciliation.json")
    a=p.parse_args()
    rows=[json.loads(line) for line in Path(a.records).read_text().splitlines()]
    report=reconcile_window(rows,json.loads(Path(a.dashboard).read_text()),a.start,a.end)
    atomic_json(Path(a.output),report)
    print(json.dumps(report,indent=2))
