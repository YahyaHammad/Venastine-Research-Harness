import json
import os

# #24. Resolved at IMPORT time, exactly like its four siblings --
# config.DB_PATH / OUTPUT_DIR / WORKSPACE_DIR and logging_setup's log
# file are all `os.environ.get(NAME, default)` evaluated once -- so a
# test can still redirect it by monkeypatching the attribute, and a
# user can point it anywhere with AGENT_PROVIDERS_FILE before launch.
# The default stays CWD-relative on purpose: for a repo-local tool,
# per-checkout state is what you want, and repo-relative would share
# one key file across every clone on the machine.
LLM_PROVIDERS_FILE = os.environ.get("AGENT_PROVIDERS_FILE", "providers.json")


def no_providers_message() -> str:
    """The error for "the file itself is not there" (#24).

    ONE wording, used by both raisers -- api_initialization (the one
    every user actually meets) and load_credentials below -- so the two
    cannot drift apart the way their old messages already had.
    """
    return (
        f"No providers configured: {LLM_PROVIDERS_FILE} was not found in "
        f"{os.getcwd()!r}. Copy providers.json.example to "
        f"{LLM_PROVIDERS_FILE} and add your API key."
    )


def unknown_provider_message(provider_name: str, providers: dict) -> str:
    """The error for "the file is there, this provider is not in it" (#24).

    Names the configured set exactly like /model's refusal does, so the
    typo is answered where it was made rather than two layers later.
    """
    configured = ", ".join(sorted(providers)) or "none"
    return (
        f"Unknown provider: {provider_name}. Configured: {configured} "
        f"(file: {LLM_PROVIDERS_FILE})."
    )


def load_provider_data() -> dict:
    if not os.path.exists(LLM_PROVIDERS_FILE):
        return {}
    with open(LLM_PROVIDERS_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


# 0600 -- owner read/write, nobody else. This file holds every LLM
# provider's API key in cleartext, and a bare open(path, "w") left its
# protection to the process umask: 0644 under the common default, i.e.
# readable by every other account on the machine (#19).
#
# The blast radius of a credential file is not the file; it is the paid
# API account behind it. Every comparable tool treats one as 0600
# (~/.aws/credentials, ~/.docker/config.json, ~/.ssh/id_*), and OpenSSH
# goes further and REFUSES a group-readable private key.
_SECRET_FILE_MODE = 0o600


def _write_secret_json(path: str, data: dict) -> None:
    """Write JSON to a file created 0600, never world-readable even briefly.

    os.open with the mode, NOT open() followed by os.chmod. A post-write
    chmod leaves a window in which the key is on disk with the umask's
    permissions -- short, but it is exactly the window an attacker with
    local read access is waiting for, and closing it costs one line.

    WINDOWS: POSIX mode bits do not apply, and os.open's mode argument
    only controls the read-only flag there. This is a no-op on Windows
    rather than a wrong-op; the file's protection comes from the ACL on
    the user profile directory instead. The tests skip accordingly, and
    the CI container is where the assertion actually runs.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                 _SECRET_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)


def _write_provider_data(provider_data: dict) -> None:
    _write_secret_json(LLM_PROVIDERS_FILE, provider_data)


def save_credentials(
    provider_name: str,
    api_key: str,
    api_url: str = "",
    is_v1_compatible: bool = True,
    supports_stream_usage: bool = False,
) -> None:
    provider_data = load_provider_data()

    if provider_name in provider_data:
        provider_data[provider_name]["API_KEY"] = api_key
        if api_url:
            provider_data[provider_name]["API_URL"] = api_url
        provider_data[provider_name]["is_v1_compatible"] = is_v1_compatible
        provider_data[provider_name]["supports_stream_usage"] = supports_stream_usage
    else:
        provider_data[provider_name] = {
            "API_KEY": api_key,
            "API_URL": api_url,
            "is_v1_compatible": is_v1_compatible,
            "supports_stream_usage": supports_stream_usage,
        }

    _write_provider_data(provider_data)


def load_credentials(provider_name: str) -> tuple[str, str]:
    provider_data = load_provider_data()
    # #24. The two failure modes say different things now -- "the file is
    # not there" is a setup problem with a one-line remedy; "the provider
    # is not in it" is a typo. Same builders api_initialization uses.
    if not provider_data and not os.path.exists(LLM_PROVIDERS_FILE):
        raise ValueError(no_providers_message())
    if provider_name not in provider_data:
        raise ValueError(unknown_provider_message(provider_name, provider_data))

    entry = provider_data[provider_name]
    return entry["API_KEY"], entry.get("API_URL", "")