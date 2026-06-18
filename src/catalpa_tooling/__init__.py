"""Deploy and development CLIs driven by a repo-root ``tooling.yaml`` manifest."""

from catalpa_tooling.native_cli import main as main_native
from catalpa_tooling.dk_cli import main as main_dk
from catalpa_tooling.env_yaml import _credentials_to_env

# Backward compatibility: ``from catalpa_tooling import main`` is ``dk``.
main = main_dk
main_local = main_native  # deprecated alias
main_dev = main_native  # deprecated alias

__all__ = ["main", "main_native", "main_local", "main_dev", "main_dk", "_credentials_to_env"]
