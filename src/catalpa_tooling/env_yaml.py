"""YAML credential / env mapping for deploy."""


def _yaml_mapping_to_env(data: dict, *, skip_sops: bool = True) -> dict[str, str]:
    """Map a flat YAML mapping to process env (uppercased keys, string values). Skips nested dict/list."""
    out: dict[str, str] = {}
    for key, value in data.items():
        if skip_sops and key == "sops":
            continue
        if not isinstance(key, str):
            continue
        if isinstance(value, (dict, list)):
            continue
        env_key = key.upper()
        if value is None:
            out[env_key] = ""
        else:
            out[env_key] = str(value)
    return out


def _credentials_to_env(creds: dict) -> dict[str, str]:
    """SOPS-decrypted credentials YAML (skips `sops` metadata)."""
    return _yaml_mapping_to_env(creds, skip_sops=True)
