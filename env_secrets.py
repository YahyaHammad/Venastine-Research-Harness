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

from dotenv import load_dotenv

load_dotenv()  # populates os.environ from a local .env file; a no-op if
                # .env doesn't exist, so this is safe to import even
                # before anyone has set one up.


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
