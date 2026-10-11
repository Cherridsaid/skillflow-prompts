# skillflow-prompts

[Français](README.fr.md)

skillflow-prompts runs the SkillFlow controller from a **prompt** instead of a skill,
so that ChatGPT (work mode) can compile a prompt into a contract, check an answer
against it and hand back a signed receipt.

**Status: under construction.** This repository currently contains the SkillFlow core
imported from skillflow-mcp (commit `c26ff68`) and renamed to `skillflow_prompts`.
The prompt layer and the ChatGPT launcher are not available yet. Nothing in this
README describes a feature that is not yet tested.

## Local development

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```console
uv sync --locked
uv run python -m pytest
uv run skillflow-prompts --help
```

## License

Apache-2.0. Derived from skillflow-mcp by the same author.
