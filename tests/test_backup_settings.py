"""`backups:` and `restore_from` decide what an env does with backups; credentials must match
(#204, tooling 2.0)."""

from __future__ import annotations

import argparse

import pytest

from catalpa_tooling import backup_settings as bs


@pytest.fixture
def envs(minimal_project):
    def make(**infos: str):
        for env, text in infos.items():
            (minimal_project.deploy_envs_dir / env).mkdir(parents=True, exist_ok=True)
            (minimal_project.deploy_envs_dir / env / "info.yaml").write_text(f"name: {env}\n{text}")
        return minimal_project

    return make


def test_settings_and_what_a_restore_reads(envs) -> None:
    config = envs(prod="backups: [db, files]\n", staging="restore_from: prod\n", dev="")
    prod = bs.backup_settings(config, "prod", bs.load_info(config, "prod"))
    assert prod.writes("db") and prod.writes("files") and not prod.restores
    staging = bs.backup_settings(config, "staging", bs.load_info(config, "staging"))
    assert staging.backups == frozenset() and staging.restores == {"db", "files"}
    dev = bs.backup_settings(config, "dev", {})
    assert not dev.backups and not dev.restores


@pytest.mark.parametrize(
    "infos,env,message",
    [
        ({"prod": "backups: db\n"}, "prod", "a list of kinds"),
        ({"prod": "backups: [db, media]\n"}, "prod", "unknown media"),
        ({"prod": "", "staging": "restore_from: prod\n"}, "staging", "prod backs nothing up"),
    ],
)
def test_bad_settings(envs, infos, env, message) -> None:
    config = envs(**infos)
    with pytest.raises(bs.BackupSettingsError, match=message):
        bs.backup_settings(config, env, bs.load_info(config, env))


def test_credentials_must_match_the_settings(envs) -> None:
    config = envs(prod="backups: [db]\n", staging="restore_from: prod\n")
    prod = bs.backup_settings(config, "prod", bs.load_info(config, "prod"))
    assert bs.credential_problems(prod, {"PGBR_S3_WRITE_BUCKET"}) == []
    (p,) = bs.credential_problems(prod, {"PGBR_S3_WRITE_BUCKET", "RESTIC_WRITE_REPOSITORY"})
    assert "doesn't back up files" in p and "`backups: [db, files]`" in p
    (p,) = bs.credential_problems(prod, {"RESTIC_REPOSITORY"})  # restic's old names count too
    assert "restic write credentials" in p
    staging = bs.backup_settings(config, "staging", bs.load_info(config, "staging"))
    assert bs.credential_problems(staging, {"PGBR_S3_READ_KEY"}) == []
    (p,) = bs.credential_problems(staging, {"RESTIC_READ_PASSWORD"})
    assert "restore_from: prod doesn't back up files" in p
    nothing = bs.BackupSettings(env="dev", backups=frozenset())
    (p,) = bs.credential_problems(nothing, {"PGBR_S3_READ_KEY"})
    assert "it has no restore_from" in p
    # A setting without credentials is fine here: the commands that need them make them.
    assert bs.credential_problems(prod, set()) == []


def test_refuse_write(envs) -> None:
    config = envs(prod="backups: [db, files]\n", staging="restore_from: prod\n")
    staging = bs.backup_settings(config, "staging", bs.load_info(config, "staging"))
    why = bs.refuse_write(staging, "db", "dk staging db backup")
    assert "doesn't back up db" in why and "restores prod's backups instead" in why
    prod = bs.backup_settings(config, "prod", bs.load_info(config, "prod"))
    assert bs.refuse_write(prod, "db", "x") is None


# --- compose envs ---------------------------------------------------------------------------


def test_compose_env_with_credentials_but_no_setting_stops(envs, capsys) -> None:
    from catalpa_tooling.env_handlers import _backup_roles

    config = envs(prod="")
    assert _backup_roles(config, "prod", {}, {"PGBR_S3_WRITE_BUCKET": "b"}) is None
    assert "add `backups: [db]`" in capsys.readouterr().err
    config = envs(prod="backups: [db]\n")
    assert _backup_roles(config, "prod", {"backups": ["db"]}, {"PGBR_S3_WRITE_BUCKET": "b"}) is not None


@pytest.mark.parametrize(
    "sub,extra",
    [("backup", {"backup_type": "incr"}), ("configure", {"configure_mode": "stanza-create"}),
     ("install-systemd", {})],
)  # fmt: skip
def test_compose_db_commands_that_back_up_are_refused_without_backups(envs, monkeypatch, capsys, sub, extra) -> None:
    from catalpa_tooling import doctl_spaces_provision, stack_commands
    from catalpa_tooling.stack_backend import ComposeBackend

    config = envs(staging="")
    monkeypatch.setattr(
        doctl_spaces_provision, "ensure_spaces_backup_credentials", lambda *a, **k: pytest.fail("offered to provision")
    )
    ns = argparse.Namespace(db_command=sub, yes=False, **extra)
    b = ComposeBackend.with_env(ComposeBackend.from_parts(config, "staging", "compose.yml", {}).ctx, {})
    rc = stack_commands.run(b, "db", ns, dry_run=False)
    assert rc == 2
    assert "staging doesn't back up db" in capsys.readouterr().err
