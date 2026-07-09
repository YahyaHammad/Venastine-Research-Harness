import os

from types import SimpleNamespace

passes_source_files = {
    "Pass 1": "initial_generation.md", # Initial response seed
    "Pass 2": "claim_extraction.md", # Extract a JSON list of falsifiable claims
    "Pass 3a": "source_grounding.md", # Source grounding using web sources, calculations, and information from user-provided files
    "Pass 3b": "critic_pass.md", # Checking for fallacies and contradictions
    "Pass 3c": "completeness.md", # Checking for expectation coverage relative to the user's query (making sure the response is relevant to what the user asked)
    "Pass 5": "assumption_audit.md", # (Come before pass 4 intentionally) ensures the user's query does notmake subtle invalid assumptions or contain implicit biases
    "Pass 4": "confidence_tiers.md", # Labels the confidence of each claim, values are HIGH, MEDIUM, LOW, UNVERIFIED, UNVERIFIED_COVERAGE
    "Pass 6a": "revise.md", # Rewrite claims that have confidence below accepted threshhold  
    "Pass 6b": "annotate.md", # Add user-facing confidence tags
    "Pass 6c": "revalidate.md" # Run passes 3a, 3b, and 4 on rewritten claims
}

passes_prompts = {}



def GetSystemPrompts():
    passes_prompts = {}
    # Get the absolute directory of the current python script
    current_dir = os.path.dirname(os.path.abspath(__file__))

    for pass_id, pass_filename in passes_source_files.items():
        # Construct the absolute path to your markdown file
        md_file_path = os.path.join(current_dir, pass_filename)

        # Open and read the file safely, "r" is read mode
        with open(md_file_path, "r", encoding="utf-8") as file:
            passes_prompts[pass_id] = file.read()
    return passes_prompts
