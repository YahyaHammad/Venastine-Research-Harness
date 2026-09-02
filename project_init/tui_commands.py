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


def _split_init_flags(args: str) -> tuple:
    """(kind, scaffold_config, error) for `/init`'s leading flags.

    A LOOP over tokens, modelled on `/research`'s `_split_research_flags`,
    so the flags compose in either written order -- `/init --config
    --software` and `/init --software --config` are one command written
    two ways, and handling one flag then the other in a fixed sequence is
    exactly how that stopped being true there.

    STRICTER THAN WHAT IT REPLACES, on purpose. The old parser read a
    single token through `.lstrip("-")`, which also accepted `/init
    software` and `/init ---software`. A command that writes files into
    someone's project should not be reachable by a typo that happens to
    normalise, and there is no muscle memory to protect: the documented
    spellings are the only ones that ever appeared.

    The kind flags are derived from `doc_sets.PROJECT_KINDS` rather than
    typed out, so a third document set is spelled once.
    """
    from project_init import doc_sets

    usage = "Usage: /init [--software|--research] [--config]."
    kinds = {"--" + name: name for name in doc_sets.PROJECT_KINDS}
    kind = None
    scaffold_config = False
    for token in (args or "").split():
        low = token.lower()
        if low == "--config":
            scaffold_config = True
        elif low in kinds:
            if kind is not None and kind != kinds[low]:
                return None, False, (
                    f"/init takes at most one of "
                    f"{', '.join(sorted(kinds))} -- got both, and guessing "
                    f"which you meant would scaffold the wrong document "
                    f"set. {usage}")
            kind = kinds[low]
        else:
            return None, False, f"Unknown option {token!r}. {usage}"
    return kind, scaffold_config, None


def _cmd_init(app, args: str) -> None:
    """Scaffold this project's documentation set, its configuration, or both.

    On a worker, because it is a model call over a project's documents, and
    announced before it starts: a call the user pays for is never silent,
    the rule /compact and /summary already follow.

    `--config` ALONE runs no model call -- the worker is still where it
    belongs, because the one consent opens a modal and the thread that
    waits on the answer cannot be the UI thread.
    """
    kind, scaffold_config, problem = _split_init_flags(args)
    if problem:
        app._transcript.write_error(problem)
        return
    # `--config` on its own is the configuration and nothing else; named
    # with a kind it is both, under one consent. §24 I17.
    scaffold_docs = not scaffold_config or kind is not None

    if app._busy:
        app._transcript.write_error(
            "Still working — wait for this turn to finish.")
        return

    app._busy = True
    transcript = app._transcript  # held before any modal opens (§27's note)

    def _work() -> None:
        from core import interaction
        from project_init import doc_sets
        from project_init.generator import InitError, generate

        # §23: generate()'s confirm/choose_kind parameters are unchanged --
        # they are a shell-agnostic API the CLI implements too. What moved
        # is the plumbing beneath them: both now go down the same channel
        # as every other question, so the fail-safe decoding is the shared
        # one rather than a guard written by hand here.
        channel = app.response_channel()

        def _confirm(summary: str) -> bool:
            return interaction.ask(channel, interaction.Request(
                kind=interaction.CONFIRM,
                payload={"title": "Write these files?", "body": summary,
                         "confirm_label": "Write"}))

        def _choose_kind(proposal, reason, blank):
            return interaction.ask(channel, interaction.Request(
                kind=interaction.CHOICE,
                payload={"options": list(doc_sets.PROJECT_KINDS),
                         "proposal": proposal, "reason": reason,
                         "blank": blank}))

        try:
            notice = generate(
                model=app.model,
                provider_name=app.provider_name,
                kind=kind,
                confirm=_confirm,
                notify=lambda text: app.call_from_thread(
                    transcript.write_system, text),
                choose_kind=_choose_kind,
                scaffold_docs=scaffold_docs,
                scaffold_config=scaffold_config,
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
        "[--software|--research] [--config]"))
