"""Time-ordered paired strategy evaluation, without retuning or claiming an edge."""
from datetime import datetime
import math
import statistics

MATCHED=("period","capital","risk_settings","exit_policy","execution_cost_model")


def evaluate(pairs,folds):
    """Each pair has discovery/baseline outcomes from identical execution rules.

    Required outcomes: decision_at, feature_cutoff, outcome_available_at,
    net_return, MATCHED fields, and execution_costs_known. Outcomes can include
    delayed/unfilled orders as cash outcomes. Folds are explicit chronological
    train-end/test-start/test-end boundaries; labels unavailable at train end
    are never admitted to that fold's training sample.
    """
    validated=[]
    for pair in pairs:
        a,b=pair["discovery"],pair["baseline"]
        if any(a.get(key)!=b.get(key) or key not in a for key in MATCHED):
            raise ValueError("unmatched capital/risk/period/exit/execution assumptions")
        for row in (a,b):
            decision=datetime.fromisoformat(row["decision_at"])
            features=datetime.fromisoformat(row["feature_cutoff"])
            available=datetime.fromisoformat(row["outcome_available_at"])
            if not all(t.tzinfo for t in (decision,features,available)) or features>decision or available<decision:
                raise ValueError("future features/invalid availability")
            if not row.get("execution_costs_known") or not math.isfinite(row["net_return"]):
                raise ValueError("known execution costs and finite net outcomes required")
        if a["decision_at"]!=b["decision_at"]:raise ValueError("decision windows differ")
        validated.append(pair)
    results=[];previous_end=None
    for fold in folds:
        train_end,start,end=(datetime.fromisoformat(fold[key]) for key in ("train_end","test_start","test_end"))
        if not train_end<start<end or previous_end and start<previous_end:
            raise ValueError("folds must be ordered and nonoverlapping")
        previous_end=end
        train=[p for p in validated if max(datetime.fromisoformat(p[s]["outcome_available_at"]) for s in ("discovery","baseline"))<=train_end]
        test=[p for p in validated if start<=datetime.fromisoformat(p["discovery"]["decision_at"])<end]
        differences=[p["discovery"]["net_return"]-p["baseline"]["net_return"] for p in test]
        n=len(differences);mean=statistics.mean(differences) if n else None
        half=1.96*statistics.stdev(differences)/math.sqrt(n) if n>=2 else None
        results.append({**fold,"training_outcomes_available":len(train),"paired_test_samples":n,
            "mean_paired_net_difference":mean,"approx_95pct_interval":[mean-half,mean+half] if half is not None else None})
    return {"folds":results,"paired_samples":len(validated),"scoring_weights_changed":False,
        "conclusion":"No profitability or durable discovery edge established. Inspect sample size, regimes, execution assumptions and dependence; approximate intervals assume independent pairs.",
        "kill_switch":"Set discovery.enabled=false when evidence does not support useful lift; deterministic exits remain active."}
