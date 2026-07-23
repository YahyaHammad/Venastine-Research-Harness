import core.client 
import prompts.system_prompts


class RunAgentLoop:


    
    
    @staticmethod
    def run_agent_conversation(user_goal: str, model: str, max_steps: int = 20):
        for step in range(max_steps):
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools_schema
            )
        pass

    
    @staticmethod
    def run_deep_research_mode(user_goal: str, model: str, pass_id: str, max_steps: int = 20):
        messages = [
            {"role": "system", "content": prompts.system_prompts.passes_prompts[pass_id]},
            {"role": "user", "content": user_goal}
        ]
        
        for step in range(max_steps):
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools_schema
            )