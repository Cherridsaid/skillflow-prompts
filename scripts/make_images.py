"""Generate the decorative images of the README through OpenRouter, and price each one.

`uv run python scripts/make_images.py [name ...]` writes `docs/images/<name>.png` for each
prompt below and prints what it cost. With no argument it generates every image that is
missing, and never regenerates one that is already there: an image costs money, so the
default is to spend nothing.

Three rules these prompts exist to keep, and the reason for each:

1. **Neutral subjects.** Nothing about anyone's trade or domain. An illustration that
   evokes a profession makes a claim about who the tool is for.
2. **No readable text.** An image model spells badly, and a typo in a page about checking
   precision argues against the product better than any critic.
3. **Decorative, and nothing else.** No receipt, no table of statuses, no invented screen.
   A reader who sees something that looks like output will read it as output, and an image
   cannot be held to a test the way a sentence can.

The key is read from `OPENROUTER_API_KEY` in the environment and never written anywhere.
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "docs" / "images"
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
# `openai/gpt-image-2` was asked for and does not exist in the OpenRouter catalogue, which
# lists `openai/gpt-5-image`, `openai/gpt-5-image-mini` and `openai/gpt-5.4-image-2`. The
# mini model is the cheapest of that family and decoration asks nothing more of it.
MODEL = "openai/gpt-5-image-mini"
PALETTE = "muted sage green, warm grey and a single terracotta accent on an off-white ground"
NO_TEXT = "absolutely no text, no letters, no numbers, no symbols, no logo, no user interface"

PROMPTS = {
    "paper-rule": (
        "Minimal still life photograph, soft daylight: a few pale paper sheets layered on a "
        f"calm surface, the top one aligned exactly against a thin metal rule. {PALETTE}. "
        f"Shallow depth of field, quiet and precise, {NO_TEXT}."
    ),
    "vellum-pleats": (
        "Abstract macro photograph: translucent vellum folded into even pleats, light passing "
        f"through the folds, regular geometry, {PALETTE}. Studio light, calm, {NO_TEXT}."
    ),
    "stone-grid": (
        "Minimal still life photograph: one smooth grey stone resting on a pale sheet ruled "
        f"into a faint square grid, soft shadow, {PALETTE}. Plain and unhurried, {NO_TEXT}."
    ),
}


def generate(prompt):
    """(png bytes, usage) for one prompt. Raises SystemExit with the reason if it fails."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY is not in the environment; nothing was spent")
    body = json.dumps(
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "modalities": ["image", "text"],
            "usage": {"include": True},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            answer = json.load(response)
    except urllib.error.HTTPError as exc:  # the body says why, and the key must not leak
        raise SystemExit(
            f"OpenRouter refused: {exc.code} {exc.read()[:400].decode(errors='replace')}"
        ) from exc
    images = answer["choices"][0]["message"].get("images") or []
    if not images:
        raise SystemExit(f"no image came back: {json.dumps(answer)[:400]}")
    data = images[0]["image_url"]["url"].partition(",")[2]
    return base64.b64decode(data), answer.get("usage", {})


def main(names):
    FOLDER.mkdir(parents=True, exist_ok=True)
    wanted = names or list(PROMPTS)
    for name in wanted:
        if name not in PROMPTS:
            raise SystemExit(f"unknown image: {name}")
        target = FOLDER / f"{name}.png"
        if target.exists() and not names:
            print(f"{name}: already there, nothing spent")
            continue
        png, usage = generate(PROMPTS[name])
        target.write_bytes(png)
        print(f"{name}: {len(png):,} bytes, usage {json.dumps(usage)}")


if __name__ == "__main__":
    main(sys.argv[1:])
