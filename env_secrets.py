"""
env_secrets.py

Miscellaneous API keys for individual TOOLS (e.g. a GitHub token, an
NVD/NIST API key) -- NOT LLM provider keys. Loaded from a .env file via
python-dotenv.

This is deliberately separate from credentials.py. The distinction:

  credentials.py  -- LLM PROVIDER keys (OpenAI, Anthropic, ...). Structured
                     per-provider (URL + key + compatibility flag), stored
                     in providers.json, read/written through
                     load_provider_data()/save_credentials().

  env_secrets.py  -- Flat, single-value secrets that one specific TOOL
                     needs to call some third-party API. No structure
                     beyond name -> value, stored in .env (never committed
                     -- see .gitignore), read through get_env_secret().

If you're adding a new tool that needs its own API key (a weather API, a
GitHub token, an NVD key, etc.), it belongs here, not in credentials.py or
providers.json -- those are exclusively for the LLM provider abstraction
in core/client.py.
"""

import os

from dotenv import dotenv_values, find_dotenv

# Batch 35. Resolved at IMPORT time like credentials.LLM_PROVIDERS_FILE and
# for the same reason -- a test can monkeypatch the attribute, and a user can
# point it anywhere before launch. Reading the variable is all that happens
# at import; the FILE is read by `_file_values()`, at the point of use.
#
# THE usecwd=True IS LOAD-BEARING, not a style choice. A bare find_dotenv()
# is find_dotenv(usecwd=False), which walks up from THE CALLING FRAME'S
# FILE -- this module -- and not from the working directory. In a checkout
# those are the same directory, which is the only reason this has ever looked
# like cwd resolution. Install the harness anywhere else (npm puts it under
# node_modules/) and the walk starts in the wrong tree: it never reaches the
# user's .env, and it can pick up a stray one sitting above the install
# prefix. Measured before the fix, with a .env in the working directory and
# the harness imported from elsewhere: get_env_secret() saw None.
#
# Do not "simplify" this back to a bare call. The defect is invisible from
# a checkout and presents as a tool reporting a missing key that is sitting
# in the .env right next to you.
#
# AGENT_ENV_FILE is the escape hatch, named to match AGENT_PROVIDERS_FILE.
# There is deliberately NO user-level fallback to ~/.config/venastine/ here,
# though providers.json has one: a provider key is machine-wide by nature,
# while .env holds the per-project tool tokens described above, and the two
# stores are kept apart on purpose. find_dotenv returns "" when it finds
# nothing, and that reads as "no file", so the unconfigured case costs one
# directory walk and returns nothing.
ENV_FILE = os.environ.get("AGENT_ENV_FILE")

# A .env MAY NOT SUPPLY THE HARNESS'S OWN NAMES (TECHNICAL_DEBT 57, batch
# 160). Until then this module called `load_dotenv()` AT IMPORT, which wrote
# every name in the file into `os.environ` for the whole process -- and a
# .env is an ordinary file in a repository, inside no trust boundary. Five
# security-relevant knobs are read from `os.environ`, two of them at call
# time: `AGENT_SECRETS_FILE` moves the encrypted store, `AGENT_LOG_FILE`
# the log, and `VENASTINE_REDACT_OFF` turns pattern redaction off -- the
# switch kept out of settings.json precisely so a cloned repository cannot
# flip it (G7's reason). Nothing imported this module yet, so none of that
# had happened; the first tool to import it would have done all of it.
#
# So the file is now READ, never loaded: `dotenv_values()` returns a mapping
# and touches nothing, `get_env_secret` is the only reader, and the process
# environment still wins, as `load_dotenv`'s default `override=False` made
# it. The filter is the second half and the belt: no harness module reads a
# knob through `get_env_secret`, but a tool asking for one of these names
# must get the environment's answer or none, never the repository's.
_HARNESS_PREFIXES = ("AGENT_", "VENASTINE_")


def _file_values() -> dict:
    """The tool keys the .env holds, minus every harness name.

    Read at the call, from the file `AGENT_ENV_FILE` names or the nearest
    .env walking up from the working directory. A name with no value (a
    bare `KEY` line) is absent rather than None, so the caller's
    "missing" message stays the one it gives.
    """
    path = ENV_FILE or find_dotenv(usecwd=True)
    if not path:
        return {}
    return {
        name: value for name, value in dotenv_values(path).items()
        if value is not None
        and not name.upper().startswith(_HARNESS_PREFIXES)
    }


def get_env_secret(key: str, required: bool = True) -> str:
    """
    Fetch a misc API key/secret from the environment or .env.

    The process environment answers first, then the .env file; nothing is
    written back to `os.environ` (TECHNICAL_DEBT 57).

    Raises a clear, actionable error if a REQUIRED secret is missing,
    rather than letting a tool proceed with an empty string and fail
    later with a confusing 401 from some third-party API. Pass
    required=False for genuinely optional keys (e.g. a key that only
    unlocks a higher rate limit but isn't needed for the tool to work
    at all).
    """
    value = os.environ.get(key)
    if value is None:
        value = _file_values().get(key)
    if required and not value:
        raise ValueError(
            f"Missing required environment variable '{key}'. "
            f"Copy .env.example to .env and fill in {key}."
        )
    return value or ""
