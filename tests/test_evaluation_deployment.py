import copy
import pytest
from core.evaluation import evaluate
from utils.instance_lock import InstanceLock


def test_instance_lock_excludes_duplicate_and_recovers(tmp_path):
    path=tmp_path/"agent.lock"
    with InstanceLock(path):
        with pytest.raises(RuntimeError):
            with InstanceLock(path):pass
    with InstanceLock(path):pass


def test_walk_forward_rejects_leakage_and_unfair_baseline():
    a={"period":"2026-Q3","capital":10000,"risk_settings":"v1","exit_policy":"v1","execution_cost_model":"spread/slippage/fees-v1",
        "decision_at":"2026-09-10T14:00:00+00:00","feature_cutoff":"2026-09-09T20:00:00+00:00",
        "outcome_available_at":"2026-09-20T20:00:00+00:00","net_return":.01,"execution_costs_known":True}
    pairs=[{"discovery":a,"baseline":dict(a,net_return=.005)}]
    folds=[{"train_end":"2026-08-31T00:00:00+00:00","test_start":"2026-09-01T00:00:00+00:00","test_end":"2026-10-01T00:00:00+00:00"}]
    r=evaluate(pairs,folds)
    assert r["folds"][0]["paired_test_samples"]==1 and r["folds"][0]["approx_95pct_interval"] is None
    bad=copy.deepcopy(pairs);bad[0]["baseline"]["capital"]=20000
    with pytest.raises(ValueError):evaluate(bad,folds)
    bad=copy.deepcopy(pairs);bad[0]["discovery"]["feature_cutoff"]="2026-09-11T00:00:00+00:00"
    with pytest.raises(ValueError):evaluate(bad,folds)
