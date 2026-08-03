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
import logging
import os
import sys
from uuid import UUID

import config
from core import config_loader, workspace_trust
from core.loop import RunAgentLoop, DEFAULT_PROVIDER
from database import create_db_and_tables
from logging_setup import configure_logging

logger = logging.getLogger(__name__)


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

        try:
            response = RunAgentLoop.run_agent_conversation(
                user_goal=user_input,
                model=model,
                provider_name=provider_name,
                thread_id=current_thread_id,
            )
        except Exception as e:
            logger.exception("Chat turn failed")
            print(f"\n[Error: {e}]")
            continue

        current_thread_id = response.thread_id

        if not printed_thread_id and current_thread_id:
            print(f"[thread: {current_thread_id}]")
            printed_thread_id = True

        print(f"\nAgent: {response.text}")
        if response.stop_reason != "complete":
            print(f"[stopped early: {response.stop_reason}]")
        print()


def run_research(
    query: str,
    provider_name: str,
    model: str,
    ensemble_mode: bool | None = None,
    ensemble_n: int | None = None,
) -> None:
    """Run the full deep-research pipeline, write artifacts to disk,
    and print the results. ensemble_mode/ensemble_n come from
    settings.json (§14); None means the pipeline falls back to the
    config.py defaults."""
    from core.reasoning.orchestrator import run_deep_research_pipeline
    from core.reasoning.output_writer import write_run_artifacts

    print(f"Running deep-research pipeline on: {query!r}")
    print(f"Provider: {provider_name} | Model: {model}\n")

    try:
        run = run_deep_research_pipeline(
            user_query=query, model=model, provider_name=provider_name,
            ensemble_mode=ensemble_mode, ensemble_n=ensemble_n,
        )
    except Exception as e:
        logger.exception("Research pipeline failed")
        print(f"\nPipeline failed: {e}")
        print("If any partial results were persisted, they can be "
              "inspected via the database.")
        raise SystemExit(1)

    output_dir = None
    try:
        output_dir = write_run_artifacts(run)
    except Exception as e:
        logger.exception("Could not write artifacts")
        print(f"\n[Warning: could not write artifacts: {e}]")

    print(run.final_report)

    print("\n--- Trace ---")
    for line in run.trace:
        print(f"- {line}")

    if run.run_id:
        print(f"\n[run id: {run.run_id}]")
    if output_dir:
        print(f"Full artifacts written to: {output_dir}")


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
    # Defaults are None so that after parsing we can tell an explicit
    # CLI choice from argparse fill-in; resolution happens in main via
    # resolve_runtime_defaults (CLI > settings.json > config.py).
    parser.add_argument(
        "--provider",
        default=None,
        help=f"LLM provider name (default: settings.json, else "
             f"{DEFAULT_PROVIDER}).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"Model name (default: settings.json, else "
             f"{config.MODEL_NAME}).",
    )
    parser.add_argument(
        "--trust-project",
        action="store_true",
        help="Grant workspace trust (D17) for the current directory's "
             ".venastine/ content -- intended for scripts/CI where the "
             "interactive prompt is unavailable.",
    )
    return parser


def resolve_runtime_defaults(args, settings: dict) -> tuple[str, str]:
    """Provider/model precedence: explicit CLI flag > settings.json
    (trusted project tier already beat user tier inside the loader) >
    config.py constants."""
    provider = args.provider or settings.get("default_provider") or DEFAULT_PROVIDER
    model = args.model or settings.get("default_model") or config.MODEL_NAME
    return provider, model


def _ensure_workspace_trust(project_path: str, trust_flag: bool) -> None:
    """D17 one-time confirmation. Shows what would load (file list plus
    settings.json verbatim -- a project's settings can pick the provider
    and multiply pipeline cost via ensemble_n, so approval must be
    informed). Interactive y/N on a TTY; --trust-project grants for
    scripts/CI; non-TTY without the flag skips project content with a
    notice instead of hanging on input()."""
    if workspace_trust.is_trusted(project_path):
        return
    summary = config_loader.describe_project_content(project_path)
    if trust_flag:
        print(summary)
        workspace_trust.grant_trust(project_path)
        print("Trust granted for this project.\n")
        return
    if sys.stdin.isatty():
        print(summary)
        answer = input("Trust this project's .venastine/ content? [y/N]: ")
        if answer.strip().lower() in ("y", "yes"):
            workspace_trust.grant_trust(project_path)
            print("Trust granted for this project.\n")
        else:
            print("Proceeding without project-level content.\n")
    else:
        print("[notice] .venastine/ present but not trusted; skipping "
              "project-level content. Run once with --trust-project to "
              "grant trust.\n")


if __name__ == "__main__":
    configure_logging()
    create_db_and_tables()

    parser = build_parser()
    args = parser.parse_args()

    project_path = os.getcwd()
    _ensure_workspace_trust(project_path, args.trust_project)
    config_loader.initialize(project_path)
    settings = config_loader.get_settings()
    provider, model = resolve_runtime_defaults(args, settings)

    if args.mode == "research":
        if not args.query:
            parser.error("--mode research requires a positional query argument.")
        run_research(
            args.query, provider, model,
            ensemble_mode=settings.get("ensemble_mode"),
            ensemble_n=settings.get("ensemble_n"),
        )
    else:
        run_chat(args.thread, provider, model, first_message=args.query)
