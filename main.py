"""
main.py

Entry point. Uncomment whichever mode you want to try.
"""

import config
from database import create_db_and_tables
from core.loop import RunAgentLoop
from core.reasoning.orchestrator import run_deep_research_pipeline

if __name__ == "__main__":
    create_db_and_tables()

    # --- Regular conversation ---
    # response = RunAgentLoop.run_agent_conversation(
    #     user_goal="What's the capital of France?",
    #     model=config.MODEL_NAME,
    # )
    # print(response.text)

    # --- Full deep-research pipeline ---
    run = run_deep_research_pipeline(
        user_query="What are the main risks of quantum computing to current encryption standards?",
        model=config.MODEL_NAME,
    )

    print(run.final_report)

    print("\n--- Trace log ---")
    for line in run.trace:
        print(f"- {line}")

    print("\n--- Claims ---")
    for c in run.claims:
        print(f"{c.id} [{c.confidence_tier}] {c.final_text or c.text}")
