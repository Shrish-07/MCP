"""Register periodic jobs and define the clock times shared with catch-up."""

from datetime import time

from models.decisions import CycleType


def register_jobs(agent):
    settings = agent.config
    scheduler, backend = agent.scheduler, agent.backend
    options = {"replace_existing": True, "max_instances": 1, "coalesce": True}
    et = "America/New_York"
    scheduler.add_job(
        backend.heartbeat, "interval", id="heartbeat", seconds=30, **options
    )
    scheduler.add_job(
        backend.nightly, "cron", id="nightly", hour=20, minute=0, timezone=et, **options
    )
    if backend.discovery:
        discovery = settings["discovery"]["schedule"]
        scheduler.add_job(
            backend.refresh_discovery_features,
            "cron",
            id="discovery_features",
            hour=discovery["feature_hour_et"],
            minute=0,
            timezone=et,
            **options,
        )
        scheduler.add_job(
            backend.weekly,
            "cron",
            id="discovery_weekly",
            day_of_week=discovery["rank_day"],
            hour=discovery["rank_hour_et"],
            minute=0,
            timezone=et,
            **options,
        )

    schedule = settings["schedule"]
    timezone = schedule["timezone"]
    cycles = [
        (
            cycle,
            time(
                schedule[f"{cycle.value.lower()}_hour"],
                schedule[f"{cycle.value.lower()}_minute"],
            ),
        )
        for cycle in (CycleType.OPEN, CycleType.MID, CycleType.CLOSE)
    ]
    for cycle, when in cycles:
        scheduler.add_job(
            agent.run_cycle,
            "cron",
            args=[cycle],
            id=f"decision_{cycle.value.lower()}",
            day_of_week="mon-fri",
            hour=when.hour,
            minute=when.minute,
            timezone=timezone,
            **options,
        )
    monitor = settings["monitor"]
    if monitor["enabled"]:
        interval = monitor["interval_seconds"]
        scheduler.add_job(
            agent.run_price_monitor,
            "interval",
            id="position_monitor",
            seconds=interval,
            **options,
        )
        agent.logger.info("monitor_registered interval_seconds=%d", interval)
    reporting = settings["reporting"]
    if reporting["attribution_enabled"]:
        scheduler.add_job(
            agent._run_attribution_report,
            "cron",
            id="attribution",
            day_of_week="mon-fri",
            hour=reporting["attribution_hour"],
            minute=reporting["attribution_minute"],
            timezone=timezone,
            **options,
        )
    return timezone, tuple(when for _, when in cycles)
