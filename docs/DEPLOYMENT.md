# Deployment preparation

Reviewed November 12, 2026. These are preparation instructions. No service was provisioned or deployed during the audit.

## Provider and region

Use a single DigitalOcean Basic Droplet in NYC3. Start with 4 vCPUs, 8 GiB RAM and 160 GiB SSD for the configured local sentiment workload. The published base is $48/month. Daily backups add 30%, making $62.40 before tax and optional monitoring. Budget approximately $62–75/month for this host and modest monitoring. A core-only 2-vCPU, 4-GiB, 80-GiB host is $24/month, or $31.20 with daily backups, but the configured sentiment path needs separate capacity validation. These ranges exclude Claude and market-data usage. [Droplet pricing](https://www.digitalocean.com/pricing/droplets)

NYC3 offers a US East location. The strategy's multi-day horizons do not justify selecting a host by microsecond latency. Reliable network access, persistent storage and an operator who can respond to alerts matter more. Robinhood connectivity from that host still needs a read-only test. [Regional availability](https://docs.digitalocean.com/platform/regional-availability/)

The second choice is AWS Lightsail in us-east-1, Northern Virginia. Its Linux bundle provides 2 vCPUs, 8 GB RAM and 160 GB SSD for $44/month, or 2 vCPUs, 4 GB RAM and 80 GB SSD for $24/month. Allow roughly $50–65/month for the 8-GB host plus snapshot retention and monitoring, depending on stored snapshot size. Burst CPU limits should be checked for sustained local inference. [Bundle prices](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-bundles.html), [Regions](https://docs.aws.amazon.com/lightsail/latest/userguide/understanding-regions-and-availability-zones-in-amazon-lightsail.html), [CPU baseline](https://docs.aws.amazon.com/en_en/lightsail/latest/userguide/baseline-cpu-performance.html)

These resource recommendations are engineering estimates. The audit did not measure live model memory or steady-state CPU. Neither provider choice establishes application readiness.

## Current container contract

| Item | Implemented behavior and required validation |
| --- | --- |
| Process | Python 3.13.6, one `python main.py` process, non-root `agent` user. The image's numeric UID/GID must be obtained from the actual built image. |
| State | Bind mounts for `config`, `data`, `logs` and `journals`. Positions, order/fill ledger and costs live below logs. Data includes derived feature and shortlist state. |
| Authentication | Named `oauth-cache` volume at `/app/oauth`, with `ROBINHOOD_TOKEN_CACHE=/app/oauth/token.json` set before import. Windows credentials are excluded from the image. |
| Models | Named `sentiment-cache` volume at `/home/agent/.cache/huggingface`. The build defaults to `INSTALL_SENTIMENT=false`, while the checked-in settings enable legacy sentiment selection. Resolve the installed-dependency mismatch before expecting that path to work. |
| Ownership | Image directories are owned by `agent`. Host bind mounts replace their image ownership, so verify actual host directories are writable as the built container user. |
| OAuth writes | Atomic cache replacement needs write and rename access in the parent directory. A read-only file mount cannot persist token rotation. |
| Restart | `restart: unless-stopped`, `init: true` and 45-second stop grace. The restart policy responds to process exits. A health failure alone needs an external response. |
| Application health | Container executes `scripts/healthcheck.py`. During market hours it needs a recent successful monitor and no broker/quote error. A parseable health file is accepted outside market hours without a heartbeat-age check. Add an independent watchdog for continuous liveness. |
| Logging | Application diagnostic files rotate at 10 MiB with five backups. Diagnostic trade events use `trade_events.jsonl`, separate from the canonical `trades.json` execution results. Compose currently specifies no Docker stdout log rotation. Configure host log retention before running. Canonical ledgers need retention and backup without truncation. |
| Lock | CLI holds `logs/agent.lock`. Two processes sharing the same lock path are excluded. Separate hosts or copied state directories have independent locks and can still submit duplicates. |
| Schedule | Exchange logic uses America/New_York and calendar timestamps. Set host clock synchronization and use UTC for diagnostics. |
| Shutdown | Scheduler and review cancellation are implemented. Test SIGTERM, timeout interruption and an unresolved order on an isolated fixture before deploying. Broker orders can survive process shutdown. |

The default Compose file has no callback port mapping and the callback listener binds localhost. Browser approval on a Windows host cannot normally reach that listener inside this container. Existing authentication must be privately provisioned into the persistent volume under separate owner authorization. Do not attempt a browser login inside this deployment configuration. See [authentication details](AUTHENTICATION.md).

Docker health status and restart policies have different responsibilities. Arrange a host watchdog that detects stale heartbeat and alerts an operator. An unhealthy container needs explicit operator or supervisor handling. [Docker restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/)

## Ordered preparation steps

1. Preserve the current schema-2 account binding and canonical execution history. The owner-authorized November 12 local reset already removed the four legacy positions; November 15 preflight again reports zero local positions and zero unresolved intents. Current broker state still requires fresh matching-account reconciliation. Do not repeat the reset as a deployment step.
2. Preserve the reset's journal exclusion baseline. Old canonical fills remain retained but do not rebuild discarded prompt context. The November 12 reset already cleared the old journal context; new operational journals must persist across deployment. See [the November 16 operating audit](MONDAY_READINESS.md).
3. Install Docker Engine and Compose on the selected Linux host after provisioning is authorized. Keep the working tree and its current local changes reviewable. Use one stable Compose project name and one state directory.
4. Verify the code, configuration and container build with dry run enabled and live release approval disabled. The commands below are preparation commands to run later, after secret handling is authorized. Compose can read dotenv and service environment files, so do not publish its expanded configuration.
5. Build the current sentiment configuration with `INSTALL_SENTIMENT=true` when installation is authorized. Provision approved model weights to the model volume and verify inference locally before enabling any research work. Do not rely on automatic downloads during trading hours.
6. Create host storage directories and set ownership to the built image's user. Test atomic file replacement as that user on isolated files in each mount. Retain logs, account state, fills, costs and attribution across container recreation.
7. Privately provision the approved OAuth cache and API settings. Never put them in the image or source control. Stop other clients using the same refresh credentials. Set the cache path in the process environment before Python imports.
8. Verify the eligible account identity against the owner's approved account, then perform the permitted balance, holdings and order reads. Do not silently accept a new identity because the legacy state is unbound.
9. Use one container and perform dry-run restart, reboot, crash and network-failure checks under separate operational authorization. Verify that a second process sharing the state directory is refused, and that unresolved fixture orders cannot be resubmitted.
10. Add external alerts for process exit, stale heartbeat, stale quotes, broker failures, unresolved orders, disk space, memory pressure and failed token persistence. Verify alert delivery from outside the host. Keep an operator response procedure for software-managed stops during downtime.
11. Keep encrypted, consistent backups of the state, cost and attribution ledgers, and protect OAuth backups as credentials. Test restore with execution disabled. Do not include the removed research journal history in a new archive.
12. Review remaining release blockers. Live activation is a separate owner-controlled step.

```bash
# Preparation commands only. No commands in this block ran during the audit.
docker compose config --quiet
INSTALL_SENTIMENT=true docker compose build agent
docker compose run --rm --no-deps --entrypoint id agent
# Starting the service invokes the actual scheduler and requires separate authorization.
```

The read-only audit could parse Compose locally using PyYAML, but Docker was unavailable. Image build, dependency installation, mounted-file ownership, callback behavior on a host, reboot recovery and long-running health have not been validated. A provider change does not resolve those application and operating requirements.
