# LLM API Wrapper Script
import os
from .. import config

from openai import OpenAI
from google import genai
from anthropic import Anthropic

# Anthropic and Google API endpoints managed natively by their respective SDKs

v1_provider_endpoints = {
    # Direct drop-in OpenAI SDK compatibility
    "OpenAI": "https://api.openai.com/v1",
    "DeepSeek": "https://api.deepseek.com/v1",
    "Grok": "https://api.x.ai/v1",
    "Mistral": "https://api.mistral.ai/v1",
    "Groq": "https://api.groq.com/openai/v1",
    "TogetherAI": "https://api.together.ai/v1",
    "Perplexity": "https://api.perplexity.ai",
    "Fireworks": "https://api.fireworks.ai/inference/v1",
    "Qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "Z.AI": "https://open.bigmodel.cn/api/paas/v4",
    "Cohere": "https://api.cohere.ai/compatibility/v1",
    "OpenRouter": "https://openrouter.ai/api/v1"
}


provider = config.provider
provider_api_key = config.api_key

selected_model = ""
loaded_model = ""

# Credential initialization
def credential_initialization():
    if provider in v1_provider_endpoints:
        client = OpenAI(
            base_url=v1_provider_endpoints[provider],
            api_key=provider_api_key) # capitalize provider name and concatenate the string for standardization
    elif provider == "Google":
        client = genai.Client(api_key=provider_api_key)
    elif provider == "Anthropic":
        client = Anthropic(api_key=provider_api_key)
    else:
        raise ValueError("Provider Selection Error")
    return client

def list_models():
    models = client.models.list()
    available_models = models.data
    return available_models

def select_model():
    # Get input for model selection
    if selected_model in available_models:
        loaded_model = selected_model
    else:
        raise ValueError("Model Selection Error")
    return loaded_model