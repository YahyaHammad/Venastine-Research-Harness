from openai import OpenAI
from google import genai
from anthropic import Anthropic

from credentials import load_provider_data

# Caches initialized clients so repeated calls for the same provider don't
# rebuild the client (and re-read the credentials file) every time.
_client_cache: dict[str, object] = {}


def api_initialization(provider_name: str):
    if provider_name in _client_cache:
        return _client_cache[provider_name]

    provider_data = load_provider_data()

    if provider_name not in provider_data:
        raise ValueError(f"Unknown provider: {provider_name}")

    entry = provider_data[provider_name]

    if entry.get("is_v1_compatible", False):
        client = OpenAI(base_url=entry["API_URL"], api_key=entry["API_KEY"])
    elif provider_name == "GOOGLE":
        client = genai.Client(api_key=entry["API_KEY"])
    elif provider_name == "ANTHROPIC":
        client = Anthropic(api_key=entry["API_KEY"])
        # Add further proprietary API formats here
    else:
        raise ValueError(f"Provider {provider_name} is not currently supported")

    _client_cache[provider_name] = client
    return client


def list_models(client) -> list[str]:
    models = client.models.list()
    return [model.id for model in models.data]


def select_model(selected_model: str, available_models: list[str]) -> str:
    if selected_model in available_models:
        return selected_model
    raise ValueError(f"Model '{selected_model}' is not in the available models list")

