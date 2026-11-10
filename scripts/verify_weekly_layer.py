"""Offline verification of selection contracts and failure retention. No live I/O."""
import subprocess
import sys
from pathlib import Path

if __name__=="__main__":
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run([sys.executable,"-m","pytest","-q","tests/test_ticker_selector.py",
        "tests/test_weekly_e2e.py","tests/test_discovery.py","tests/test_sentiment_unit.py"],cwd=root)
    raise SystemExit(result.returncode)
