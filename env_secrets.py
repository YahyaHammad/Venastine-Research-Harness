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

from dotenv import find_dotenv, load_dotenv

# Batch 35. Resolved at IMPORT time like credentials.LLM_PROVIDERS_FILE and
# for the same reason -- a test can monkeypatch the attribute, and a user can
# point it anywhere before launch.
#
# THE usecwd=True IS LOAD-BEARING, not a style choice. A bare load_dotenv()
# calls find_dotenv(usecwd=False), which walks up from THE CALLING FRAME'S
# FILE -- this module -- and not from the working directory. In a checkout
# those are the same directory, which is the only reason this has ever looked
# like cwd resolution. Install the harness anywhere else (npm puts it under
# node_modules/) and the walk starts in the wrong tree: it never reaches the
# user's .env, and it can pick up a stray one sitting above the install
# prefix. Measured before the fix, with a .env in the working directory and
# the harness imported from elsewhere: get_env_secret() saw None.
#
# Do not "simplify" this back to load_dotenv(). The defect is invisible from
# a checkout and presents as a tool reporting a missing key that is sitting
# in the .env right next to you.
#
# AGENT_ENV_FILE is the escape hatch, named to match AGENT_PROVIDERS_FILE.
# There is deliberately NO user-level fallback to ~/.config/venastine/ here,
# though providers.json has one: a provider key is machine-wide by nature,
# while .env holds the per-project tool tokens described above, and the two
# stores are kept apart on purpose. find_dotenv returns "" when it finds
# nothing, and load_dotenv("") is a no-op, so the unconfigured case is still
# safe to import.
ENV_FILE = os.environ.get("AGENT_ENV_FILE")

load_dotenv(ENV_FILE or find_dotenv(usecwd=True))


def get_env_secret(key: str, required: bool = True) -> str:
    """
    Fetch a misc API key/secret populated from .env.

    Raises a clear, actionable error if a REQUIRED secret is missing,
    rather than letting a tool proceed with an empty string and fail
    later with a confusing 401 from some third-party API. Pass
    required=False for genuinely optional keys (e.g. a key that only
    unlocks a higher rate limit but isn't needed for the tool to work
    at all).
    """
    value = os.environ.get(key)
    if required and not value:
        raise ValueError(
            f"Missing required environment variable '{key}'. "
            f"Copy .env.example to .env and fill in {key}."
        )
    return value or ""
