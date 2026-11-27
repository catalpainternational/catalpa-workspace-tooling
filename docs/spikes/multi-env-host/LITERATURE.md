# Literature review: several envs and colours per host (2026-11-28)

What published practice says about the design in [../../MULTI_ENV_HOST.md](../../MULTI_ENV_HOST.md).
It comes from three research passes (multi-account rootless hosts, blue/green with a shared
database, the host level).

Marked "checked" are the claims this session confirmed itself with `gh api`: podman#28435's
maintainer reply, MinIO archived, podman#29419 open. Everything else is as the sources state
it. The Zabbix 8.0 Podman template and the RustFS 1.0 date were not checked.

## Confirmed

| Our design | What the sources say |
|---|---|
| One Linux account per env or colour | No comparable tool does this. Kamal routes many apps on one Docker through one proxy ([kamal proxy](https://kamal-deploy.org/docs/configuration/proxy/)); Dokku runs everything as `dokku` ([dokku users](https://dokku.com/docs/deployment/user-management/)). The closest precedent is NixOS's `oci-containers.<name>.podman.user`, with a static uid and subuid ranges ([NixOS wiki](https://wiki.nixos.org/wiki/Talk:Podman)). Red Hat's examples use a dedicated service account with linger, and warn against `sudo`/`su` for rootless sessions ([Red Hat](https://www.redhat.com/en/blog/sudo-rootless-podman)). |
| Cross-account traffic via `Network=pasta:--map-host-loopback,169.254.1.3` | The maintainer-endorsed workaround. `host.containers.internal` not reaching loopback is "by design, exposing services on localhost by default to all container could be a security issue" ([podman#28435](https://github.com/podman-container-tools/podman/issues/28435), checked). `--map-guest-addr` also goes stale when the host's address changes ([podman#24970](https://github.com/podman-container-tools/podman/issues/24970)). Mapped traffic arrives from 127.0.0.1 ([passt(1)](https://passt.top/builds/latest/web/passt.1.html)). |
| Socket-activated Caddy edge | Keeps the client IP and avoids the host-wide `ip_unprivileged_port_start` ([podman-caddy-socket-activation](https://github.com/eriksjolund/podman-caddy-socket-activation)). Avoid Caddy 2.11.0–2.11.1 (ACME regression with socket activation, caddy#7525). |
| Same-named files in two `--application` folders: one silently wins | By design. `--application` doesn't rename files, so names must be unique per account ([podman#26941](https://github.com/podman-container-tools/podman/issues/26941)). The reviewer's "put the version in every name" is for a shared account. One account per env or colour avoids the clash (measured, `EVIDENCE.md` part 1 (d)). |
| `.timer` accepted by `quadlet install` but never loaded | By design: non-Quadlet files are stored as assets ([podman-quadlet-install(1)](https://docs.podman.io/en/latest/markdown/podman-quadlet-install.1.html)). |
| Freeze-and-copy deploy with a short outage | The "maintenance window" alternative to expand/contract ([Fowler, BlueGreenDeployment](https://martinfowler.com/bliki/BlueGreenDeployment.html), [ParallelChange](https://martinfowler.com/bliki/ParallelChange.html)). Like Heroku, Render and Fly, migrate runs as its own step, and a failure keeps the old release serving ([Heroku release phase](https://devcenter.heroku.com/articles/release-phase), [Render](https://render.com/docs/deploys), [Fly](https://docs.fly.io/reference/configuration)). Keeping the untouched database for a failed deploy goes beyond most practice. |
| One pgBackRest stanza and one restic repository per env | Several stanzas in one repo is the common pgBackRest layout ([pgBackRest multi-stanza](https://docs.pgedge.com/pgbackrest/v2-59-0/user-guide/multi-stanza/)). For restic, separate repos are the security boundary ([restic forum](https://forum.restic.net/t/what-are-the-implications-of-storing-data-from-different-hosts-in-one-repository/589)). |
| Committed render plus a check before apply | Matches Talos (`apply-config --dry-run`, drift as the named problem: [Talos](https://docs.siderolabs.com/talos/v1.9/configure-your-talos-cluster/system-configuration/reproducible-machine-configuration)) and NixOS (`mutableUsers=false`: [NixOS users](https://wiki.nixos.org/wiki/User_management)). |

## Changes it asks for

1. **Ignition is first-boot only, so re-provision rather than drift.** FCOS changes a machine
   by replacing it ([Ignition rationale](https://coreos.github.io/ignition/rationale/)), and
   re-running Ignition isn't supported
   ([FCOS list](https://lists.fedoraproject.org/archives/list/coreos@lists.fedoraproject.org/thread/MV4DCX45M5COOQ4HVSA43RFHQFAUFFEE/)).
   - Adding accounts over SSH is acceptable only if a fresh `host create` from the same
     `host.yaml` gives the same result.
   - `dk host check` should read the live state (`/etc/passwd`, subuid/subgid, AllowUsers,
     linger), not just compare the commit with a fresh render.
2. **Slots never reused.** Each account's uid, subordinate-ID range and ports follow from a
   slot number that is never handed out again (NixOS keeps a uid map:
   [NixOS users](https://wiki.nixos.org/wiki/User_management)). Subordinate ranges must not
   overlap
   ([ADMIN](https://www.admin-magazine.com/Articles/Run-rootless-Podman-containers-as-systemd-services/(offset)/3)).
3. **Removing an account, in order.** `userdel` refuses while the account has processes
   ([userdel(8)](https://man7.org/linux/man-pages/man8/userdel.8.html)), and linger is not
   reliably cleaned up, so first `disable-linger`, then `terminate-user`. Its man page never
   says it removes subordinate ranges; `usermod --del-subuids` does
   ([usermod(8)](https://man.archlinux.org/man/usermod.8.fr.raw)).
4. **Loopback exposure.** Every container given `--map-host-loopback` can reach everything on
   the host's loopback (inferred from [passt(1)](https://passt.top/builds/latest/web/passt.1.html)).
   `Network=host` is worse, because it also exposes abstract Unix sockets
   ([podman-networking-docs](https://github.com/eriksjolund/podman-networking-docs)). Limit who
   may connect to each port (our own idea: an nftables `skuid` rule, to test).
5. **Per-user Quadlet folders.**
   - Never a symlink ([podman#26250](https://github.com/podman-container-tools/podman/issues/26250))
     and never named by username
     ([podman#29838](https://github.com/podman-container-tools/podman/issues/29838)). Either
     leaks the units to every account.
   - Podman 6.0–6.1 also load `/usr/share/containers/systemd/users/<uid>/` for everyone; fixed
     by PR 30120 ([podman#30116](https://github.com/podman-container-tools/podman/issues/30116)).
   - `podman quadlet list` walking other accounts' folders appears in no upstream report. The
     reviewer read it in `getAllQuadlets` (to test: restrictive folder permissions).
6. **Deploy steps.**
   - **Trial the next release's migrations on production-sized data** before the outage. GitLab
     runs every merge request's migrations on a thin clone of production. Its time budgets are
     3 min for regular migrations, 10 min for post-deploy ones and 1 h per deploy
     ([GitLab migration pipeline](https://docs.gitlab.com/development/database/database_migration_pipeline/),
     [style guide](https://docs.gitlab.com/development/migration_style_guide/)).
   - **Log what will run** with `migrate --plan` and `sqlmigrate`. `sqlmigrate` needs a copy of
     the target database ([django-admin](https://docs.djangoproject.com/en/5.2/ref/django-admin/)).
   - **Cap each statement** with a per-session `statement_timeout` on the migrate run, not a
     server-wide one
     ([PostgreSQL client config](https://www.postgresql.org/docs/current/runtime-config-client.html)).
     For comparison, strong_migrations suggests a 10 s lock timeout and a 1 h statement timeout
     ([strong_migrations](https://github.com/ankane/strong_migrations)).
   - **Don't adopt django-pg-zero-downtime-migrations under the freeze.** Its migrations aren't
     transactional
     ([README](https://github.com/tbicr/django-pg-zero-downtime-migrations)), which would weaken
     the clean put-back.
   - **`CREATE DATABASE … TEMPLATE` refuses while anyone is connected to the template.** The
     default `WAL_LOG` strategy writes the whole copy to the WAL. `FILE_COPY` forces
     checkpoints instead
     ([CREATE DATABASE](https://www.postgresql.org/docs/current/sql-createdatabase.html)). So
     the freeze's cost grows with the database and with WAL archiving: measure it.
   - **Fail fast,** not on a timeout. Kamal checks health every second up to `deploy_timeout`
     and then drains the old container
     ([Kamal proxy](https://kamal-deploy.org/docs/configuration/proxy/)).
   - **Give Caddy a finite `grace_period`** on reload
     ([Caddy options](https://caddyserver.com/docs/caddyfile/options)).
   - **Restoring a snapshot throws away writes made since.** Teams allow it only in a short
     window, then fix forward
     ([Octopus](https://octopus.com/docs/deployments/databases/common-patterns/backups-rollbacks),
     [Redgate](https://www.red-gate.com/hub/product-learning/flyway/failed-flyway-database-deployments-roll-back-or-fix-forward)).
7. **The local S3 store.**
   - MinIO's repository was archived on 2026-04-24 ([minio/minio](https://github.com/minio/minio),
     checked), and its Docker Hub tags no longer resolve (measured).
   - **versitygw** stores objects as plain files, so restic can back them up directly
     ([versitygw POSIX namespace](https://github.com/versity/versitygw/wiki/POSIX-Transparent-Namespace)).
   - **SeaweedFS** needs its filer metadata backed up as well as its volumes
     ([SeaweedFS async backup](https://github.com/seaweedfs/seaweedfs/wiki/Async-Backup)).
   - **Garage** lacks object ACLs, bucket policies and versioning
     ([Garage S3 compatibility](https://garagehq.deuxfleurs.fr/documentation/reference-manual/s3-compatibility/)).
8. **Monitoring several accounts.** The Zabbix agent 2 Docker plugin takes one socket. The
   options are an agent per socket, a Podman API per account on loopback (Zabbix 8.0 template:
   not checked), or narrow sudo rules
   ([Zabbix forum](https://www.zabbix.com/forum/zabbix-help/458623-monitoring-podman-rootless-containers-with-zabbix),
   [Zabbix Podman integration](https://www.zabbix.com/integrations/podman)).
9. **To watch.**
   - Rootless network teardown fails while a user manager shuts down, once per lingering
     account at reboot ([podman#29419](https://github.com/podman-container-tools/podman/issues/29419),
     open, checked).
   - Fedora 45's image mode (bootc), as a later path for host changes
     ([FCOS 43 bootc thread](https://discussion.fedoraproject.org/t/fcos-43-transition-to-bootc-questions/170698)).
