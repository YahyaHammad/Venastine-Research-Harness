"""
main.py

Interactive CLI entry point (ROADMAP §1). Two modes:

  $ python main.py                                    # new chat thread
  $ python main.py --thread <uuid>                    # resume a thread
  $ python main.py --mode research "some query"       # deep-research pipeline
  $ python main.py --provider OPENAI --model gpt-5.1  # override defaults

Chat mode runs an input()/print() loop with tool use. Ctrl+C or Ctrl+D
to exit. The thread id is printed after the first response so it can be
resumed later with --thread.

Research mode runs the full ten-pass pipeline and prints the final
report + trace log.

A feature-rich Textual TUI is planned as a separate effort; this CLI is
the permanent fallback entry point.
"""

import argparse
import sys
from uuid import UUID

import config
from core.loop import RunAgentLoop, DEFAULT_PROVIDER
from database import create_db_and_tables
from logging_setup import configure_logging


def _uuid_type(value: str) -> UUID:
    """Argparse type validator for --thread. Produces a clear CLI error
    for a malformed UUID instead of a traceback from deep in storage."""
    try:
        return UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"'{value}' is not a valid UUID. "
            f"Thread ids look like: 550e8400-e29b-41d4-a716-446655440000"
        )


def run_chat(
    thread_id: UUID | None,
    provider_name: str,
    model: str,
    first_message: str | None = None,
) -> None:
    """Interactive chat loop. If first_message is given (positional arg
    in chat mode), it's sent as the first user turn before dropping into
    the interactive loop."""
    current_thread_id = thread_id
    printed_thread_id = False

    print("Venastine Research Harness — chat mode.")
    print(f"Provider: {provider_name} | Model: {model}")
    if current_thread_id:
        print(f"Resuming thread: {current_thread_id}")
    else:
        print("Starting a new conversation thread.")
    print("Ctrl+C or Ctrl+D to exit.\n")

    while True:
        if first_message is not None:
            user_input = first_message
            first_message = None
        else:
            try:
                user_input = input("You: ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nExiting.")
                break

        if not user_input:
            continue

        response = RunAgentLoop.run_agent_conversation(
            user_goal=user_input,
            model=model,
            provider_name=provider_name,
            thread_id=current_thread_id,
        )
        current_thread_id = response.thread_id

        if not printed_thread_id and current_thread_id:
            print(f"[thread: {current_thread_id}]")
            printed_thread_id = True

        print(f"\nAgent: {response.text}")
        if response.stop_reason != "complete":
            print(f"[stopped early: {response.stop_reason}]")
        print()


def run_research(query: str, provider_name: str, model: str) -> None:
    """Run the full deep-research pipeline and print the results."""
    from core.reasoning.orchestrator import run_deep_research_pipeline

    print(f"Running deep-research pipeline on: {query!r}")
    print(f"Provider: {provider_name} | Model: {model}\n")

    run = run_deep_research_pipeline(
        user_query=query, model=model, provider_name=provider_name,
    )

    print(run.final_report)

    print("\n--- Trace ---")
    for line in run.trace:
        print(f"- {line}")

    if run.run_id:
        print(f"\n[run id: {run.run_id}]")


def build_parser() -> argparse.ArgumentParser:
    """Builds the CLI argument parser. Separated from main() so tests
    can exercise argument parsing without side effects."""
    parser = argparse.ArgumentParser(
        description="Venastine Research Harness — interactive CLI",
    )
    parser.add_argument(
        "query",
        nargs="?",
        default=None,
        help="Research query (required for --mode research). "
             "In chat mode, if given, sent as the first message.",
    )
    parser.add_argument(
        "--thread",
        type=_uuid_type,
        default=None,
        metavar="UUID",
        help="Resume an existing conversation thread by its UUID.",
    )
    parser.add_argument(
        "--mode",
        choices=["chat", "research"],
        default="chat",
        help="chat (default) or research (deep-research pipeline).",
    )
    parser.add_argument(
        "--provider",
        default=DEFAULT_PROVIDER,
        help=f"LLM provider name (default: {DEFAULT_PROVIDER}).",
    )
    parser.add_argument(
        "--model",
        default=config.MODEL_NAME,
        help=f"Model name (default: {config.MODEL_NAME}).",
    )
    return parser


if __name__ == "__main__":
    configure_logging()
    create_db_and_tables()

    parser = build_parser()
    args = parser.parse_args()

    if args.mode == "research":
        if not args.query:
            parser.error("--mode research requires a positional query argument.")
        run_research(args.query, args.provider, args.model)
    else:
        run_chat(args.thread, args.provider, args.model, first_message=args.query)
