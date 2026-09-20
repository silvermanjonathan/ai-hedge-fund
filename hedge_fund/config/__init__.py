"""Configuration and credentials — the non-presentation settings layer.

Anything every entry point needs before it can do work, and that has nothing
to do with how output looks. Credentials live here rather than in
hedge_fund.tui because the headless CLIs — `aihf`, `aihf-ledger`,
`aihf-universe`, and the weekly LaunchAgent run — need them and must not
import the interactive app's package to get them.

Like paths.py, this layer is import-light and imports no UI framework;
test_entry_points.py enforces that from the other direction.
"""

from hedge_fund.config.credentials import (
    apply_credentials,
    ENV_PATH,
    env_var_for,
    masked,
    missing_key,
    PROVIDER_ENV_VARS,
    save_credential,
)

__all__ = [
    "ENV_PATH",
    "PROVIDER_ENV_VARS",
    "apply_credentials",
    "env_var_for",
    "masked",
    "missing_key",
    "save_credential",
]
