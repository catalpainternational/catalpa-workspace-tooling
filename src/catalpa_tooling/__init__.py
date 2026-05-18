"""Deploy and development CLIs driven by a repo-root ``tooling.yaml`` manifest."""

from catalpa_tooling.dev_cli import main as main_dev
from catalpa_tooling.dk_cli import main as main_dk
from catalpa_tooling.env_yaml import _credentials_to_env

# Backward compatibility: ``from catalpa_tooling import main`` is ``dk``.
main = main_dk

__all__ = ["main", "main_dev", "main_dk", "_credentials_to_env"]
