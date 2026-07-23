import json
import os

LLM_PROVIDERS_FILE = "providers.json"


def load_provider_data() -> dict:
    if not os.path.exists(LLM_PROVIDERS_FILE):
        return {}
    with open(LLM_PROVIDERS_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


def _write_provider_data(provider_data: dict) -> None:
    with open(LLM_PROVIDERS_FILE, "w", encoding="utf-8") as file:
        json.dump(provider_data, file, indent=2)


def save_credentials(
    provider_name: str,
    api_key: str,
    api_url: str = "",
    is_v1_compatible: bool = True,
) -> None:
    provider_data = load_provider_data()

    if provider_name in provider_data:
        provider_data[provider_name]["API_KEY"] = api_key
        if api_url:
            provider_data[provider_name]["API_URL"] = api_url
        provider_data[provider_name]["is_v1_compatible"] = is_v1_compatible
    else:
        provider_data[provider_name] = {
            "API_KEY": api_key,
            "API_URL": api_url,
            "is_v1_compatible": is_v1_compatible,
        }

    _write_provider_data(provider_data)


def load_credentials(provider_name: str) -> tuple[str, str]:
    provider_data = load_provider_data()
    if provider_name not in provider_data:
        raise ValueError(f"Provider '{provider_name}' not found")

    entry = provider_data[provider_name]
    return entry["API_KEY"], entry.get("API_URL", "")