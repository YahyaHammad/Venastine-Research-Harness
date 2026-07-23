import prompts.system_prompts
import core.client
from core.loop import RunAgentLoop 

research_current_pass_index = 0
research_current_pass_id = list(prompts.system_prompts.passes_prompts)[research_current_pass_index]
is_deep_research_mode = false

def CallAgent(user_goal: str, model: str, pass_id: str, max_steps: int):
    if is_deep_research_mode: 
        RunAgentLoop.run_deep_research_mode(user_goal, model, pass_id, max_steps)
    else:
        RunAgentLoop.run_agent_conversation    
