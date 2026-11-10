import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.evaluation import evaluate
from utils.persistence import atomic_json

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("dataset");p.add_argument("--output",default="logs/discovery_evaluation.json")
    a=p.parse_args();dataset=json.loads(Path(a.dataset).read_text())
    report=evaluate(dataset["pairs"],dataset["folds"])
    atomic_json(Path(a.output),report)
    print(json.dumps(report,indent=2))
