"""Write a nonsecret baseline manifest; run before functional implementation."""
import json
import platform
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
manifest = {
    "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
    "python": platform.python_version(), "os": platform.platform(),
    "environment_variable_names": sorted({"ANTHROPIC_API_KEY", "FINNHUB_API_KEY"}),
    "test_command": "python -m pytest -q", "baseline_passed": 55,
    "external_model_tests_deselected": 2,
    "configuration": json.loads((root / "config/settings.json").read_text(encoding="utf-8")),
}
(root / "docs/BASELINE.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
