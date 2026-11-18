# Verification archive

The dated reports and outputs in this directory describe their original
runs. The main walkthrough covers the current architecture and operating
paths.

- [November 16 operating audit](../MONDAY_READINESS.md) and [offline suite](pytest_monday_2026-11-15.txt)
- [November 15 remediation](../MASTER_REMEDIATION.md) and [offline suite](pytest_master_2026-11-15.txt)
- [Earlier readiness audits](../PRODUCTION_READINESS_AUDIT.md) and [November 12 offline suite](pytest_clean_reset_verified_2026-11-12.txt)

The dated artifacts retain their original paths. For current verification,
run the guarded suite and focused selection harness from the repository root.

```sh
python -B scripts/run_offline_tests.py
python -B scripts/verify_weekly_layer.py
```
