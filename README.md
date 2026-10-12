# skillflow-prompts

[Français](README.fr.md)

skillflow-prompts runs the SkillFlow controller from a **prompt** instead of a skill,
so that ChatGPT (work mode) can compile a prompt into a contract, check an answer
against it and hand back a signed receipt.

**Status: in progress.** The SkillFlow core comes from skillflow-mcp (commit `c26ff68`),
renamed to `skillflow_prompts`. The prompt layer and the ChatGPT launcher are there and
tested: `skillflow-prompts chatgpt` compiles a prompt into a contract, checks an answer
against it, names the obligations it rejects so that only those have to be corrected, and
hands back a signed receipt that `verify` recomputes instead of reading back. One command
is one process, so nothing is left running between two of them. Correcting only what was
rejected is an instruction to the model, not something the tool can enforce.

Still missing: the extra verifiers, the declared coverage and the kernel, the complete
documentation, and a real run inside ChatGPT. The launcher holds every token of that
sandbox, so its approval is **fictive** and both the receipt and `verify` say so: what is
mechanically checked is marked `VERIFIE`, and everything else stays `NON_VERIFIE`. Nothing
in this README describes a feature that is not yet tested.

To drive it from a ChatGPT conversation, see [CHATGPT.md](CHATGPT.md).

## Local development

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```console
uv sync --locked
uv run python -m pytest
uv run skillflow-prompts --help
```

## License

Apache-2.0. Derived from skillflow-mcp by the same author.
