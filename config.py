import os
from dataclasses import dataclass


@dataclass
class APICredentials:
    provider_name: str # Pick one of the following (ensure correct spelling & capitalization) {OPENAI, ANTHROPIC, GOOGLE, OPENROUTER, DEEPSEEK, GROK, MISTRAL, GROQ, TOGETHERAI, PERPLEXITY, FIREWORKS, QWEN, Z.AI, COHERE]
    api_key: str # Add your api key to the system environment variables then insert the name of the variable you created between the double quotations 
    
    # api_url: str


@dataclass
class ToolPermissions:
    web_search: bool = True
    get_time: bool = True
    calculator: bool = True
    read: bool = False
    write: bool = False
    edit: bool = False
    shell: bool = False

@dataclass
class ToolApprovals:
    web_search: bool = False
    get_time: bool = False
    calculator: bool = False
    read: bool = False
    write: bool = False
    edit: bool = False
    shell: bool = True
