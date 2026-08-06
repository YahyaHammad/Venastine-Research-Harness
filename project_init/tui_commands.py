"""
project_init/tui_commands.py

ROADMAP_v2 §24: `/init`, registered into §16's slash registry.

Mirrors memories/tui_commands.py, skills/tui_commands.py and
agents/tui_commands.py -- each section registers its own commands rather
than editing a match statement in the app.

THIN BY CONSTRUCTION. Every decision -- which set, whether to overwrite,
whether to re-grant trust -- lives in project_init/generator.py, so the CLI
makes the same ones. What is here is the three things only a TUI can do:
run the work off the UI thread, render a diff into the transcript, and put
a modal in front of a human.
"""

from tui.commands import SlashCommand, registry as commands


def _cmd_init(app, args: str) -> None:
    """Scaffold this project's documentation set.

    On a worker, because it is a model call over a project's documents, and
    announced before it starts: a call the user pays for is never silent,
    the rule /compact and /summary already follow.
    """
    from project_init import doc_sets

    flag = args.strip().lower().lstrip("-")
    kind = None
    if flag:
        if flag not in doc_sets.PROJECT_KINDS:
            app._transcript.write_error(
                f"Unknown option {args.strip()!r}. Usage: /init "
                f"[--software|--research].")
            return
        kind = flag

    if app._busy:
        app._transcript.write_error(
            "Still working — wait for this turn to finish.")
        return

    app._busy = True
    transcript = app._transcript  # held before any modal opens (§27's note)

    def _work() -> None:
        from project_init.generator import InitError, generate
        try:
            notice = generate(
                model=app.model,
                provider_name=app.provider_name,
                kind=kind,
                confirm=lambda summary: app.ask_confirm_blocking(
                    "Write these files?", summary, confirm_label="Write"),
                notify=lambda text: app.call_from_thread(
                    transcript.write_system, text),
                choose_kind=app.ask_project_kind_blocking,
            )
        except InitError as e:
            app.call_from_thread(transcript.write_error, str(e))
            return
        except Exception as e:  # noqa: BLE001 — same containment as /compact
            app.call_from_thread(transcript.write_error, f"/init failed: {e}")
            return
        finally:
            app.call_from_thread(setattr, app, "_busy", False)
        app.call_from_thread(transcript.write_system, notice["text"])

    app.run_worker(_work, thread=True, exit_on_error=False, name="init")


def register_init_commands() -> None:
    """Idempotent -- registering by name overwrites, matching §16's
    register_builtin_commands() and every section registrar since."""
    commands.register(SlashCommand(
        "init", "generate this project's documentation set", _cmd_init,
        "[--software|--research]"))
