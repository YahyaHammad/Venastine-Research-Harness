import os


"""
Model provider selection
"""
provider = "OpenAI" # Pick one of the following (ensure correct spelling & capitalization) {OpenAI, Anthropic, Google, OpenRouter, DeepSeek, Grok, Mistral, Groq, TogetherAI, Perplexity, Fireworks, Qwen, Z.AI, Cohere]
api_key = os.environ.get("") # Add your api key to the system environment variables then insert the name of the variable you created between the double quotations 

context_window = 0 # leave at 0 for setting by provider

"""
Agent tool permission selection
"""
web_search = true
get_time = true
read = false
write = false
edit = false
shell = false

"""
Maximum number of steps for each reasoning pass
"""