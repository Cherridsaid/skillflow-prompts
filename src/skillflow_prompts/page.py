"""One local page for the whole protocol: it shows, and it decides nothing.

Five commands in a terminal is a tool Said does not use, and a tool nobody runs checks
nothing. So the launcher gets a door that needs no terminal: paste the prompt, read what is
checked and what is not, copy the prompt into ChatGPT, paste the answer, confirm a
candidate, deliver, verify a receipt.

Every route here builds its own arguments and calls `chatgpt.run`. The page holds no
reading of a prompt, no status and no verdict of its own: what it returns is the launcher's
own JSON, plus the exit code the command line would have returned. A second
implementation would be a second behaviour to keep in step, and one of the two would be
wrong without anyone noticing.

What is pasted is text, never an argument: it is written to a file under the state, and
only values this module checks itself -- an identifier, a name -- ever reach a command
line. The page listens on the loopback, refuses any other Host and any other origin.
"""

import os
import re
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .chatgpt import run
from .util import FlowError

# The page is for the machine it runs on. Nothing here is an authority, so there is no
# token to hand out: what protects the state is that no other host can reach it.
HOST = "127.0.0.1"
# 8770, et pas 8765 : celui-là est le port de démonstration de skillflow-mcp, hérité par
# accident, et une démo lancée pendant que la page est ouverte se heurterait à elle. Le
# changer ne coûte rien avant la publication, et coûterait une migration après.
PORT = 8770
LOCAL = ("127.0.0.1", "localhost", "::1", "testserver")
# A prompt and an answer are texts a person writes, not a corpus: the body is read in
# chunks and refused past this size, as the control server reads its own.
MAX_REQUEST = 1_000_000
POLICY = (
    "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    # The page carries one empty data: icon, which is how a browser is told not to go and
    # ask for /favicon.ico. Without it the console keeps a 404 that is not an error, and a
    # console one learns to ignore is a console that hides the next real line.
    "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'none'; "
    "base-uri 'none'"
)
# Les trois fins possibles de `serve`, nommées, parce que deux d'entre elles se sont
# confondues : un port pris et un arrêt demandé avant l'écoute rendaient la même réponse, et
# le lanceur annonçait un port pris sur un port libre.
SERVED, TAKEN, CANCELLED = "served", "taken", "cancelled"
# What may travel to a command line: the launcher's own identifiers and a plain name.
NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$", re.I)
# A folder identifier, as the launcher writes them: thirty-two hex characters.
CASE = re.compile(r"^[0-9a-f]{32}$", re.I)


def refusal(code, message, status):
    return JSONResponse(FlowError(code, message).result(), status)


def create_page(root):
    """The page, ready to be opened: the state is created here, so nothing needs a terminal.

    `init` is idempotent, so opening the page twice on the same folder keeps the same
    signing key and the same history.
    """
    root = Path(root)
    scratch = root / "page"
    code, started = run(["init", "--root", str(root)])
    app = FastAPI(title="skillflow-prompts — protocole", docs_url=None, redoc_url=None)
    app.state.root = root
    app.state.started = {**started, "exit_code": code}

    def write(name, text):
        """What is pasted, on disk, under the state: the launcher reads files, not strings."""
        scratch.mkdir(parents=True, exist_ok=True)
        path = scratch / name
        path.write_text(text, encoding="utf-8", newline="")
        return str(path)

    def launched(argv):
        code, result = run(argv)
        return JSONResponse({**result, "exit_code": code})

    def moved(asked):
        """Refuse an action whose folder is no longer the one the page was showing.

        One state, several tabs. Compiling in a second tab cancels the first tab's folder,
        and the first tab would then deliver a receipt for a prompt it never displayed --
        the whole point of this tool, inverted. So an action that acts on a folder carries
        the identifier the page is showing, and it is refused when the launcher's current
        folder is another one. The page decides nothing here either: it compares an
        identifier the launcher itself gave it, and the answer says what to do.
        """
        shown, rounds, kept = asked.get("case_id"), asked.get("rounds"), asked.get("receipt")
        if not shown and rounds is None and not kept:
            return None
        odd = (
            (shown and not CASE.match(str(shown)))
            or (rounds is not None and not isinstance(rounds, int))
            or (kept is not None and not isinstance(kept, str))
        )
        if odd:
            return refusal("CASE_NAME", "Identifiant ou compte de tours inattendu.", 400)
        _code, state = run(["status", "--root", str(root)])
        now = state.get("current") or {}
        # Le compte de tours est le numéro de version du dossier. L'identifiant seul ne
        # suffit pas : deux réponses successives le gardent, donc un onglet pouvait livrer
        # un reçu sur la réponse contrôlée dans l'autre. Trouvé par la revue produit.
        if (
            (shown and now.get("case_id") != shown)
            or (rounds is not None and now.get("rounds_remaining") != rounds)
            # La page reprend un reçu, et elle dit LEQUEL. Un dossier ouvert ailleurs, et
            # « Livrer » remettrait celui-là ; une remise faite ailleurs depuis, et le
            # dernier reçu n'est plus celui que la page affiche. Reprendre un reçu n'est pas
            # livrer, et ce n'est pas prendre le reçu de quelqu'un d'autre.
            or (kept and (now or state.get("last_receipt") != kept))
        ):
            return refusal(
                "CASE_MOVED",
                "Le dossier a changé ailleurs — un autre onglet ? Recharge la page avant d'agir.",
                409,
            )
        return None

    @app.middleware("http")
    async def boundary(request, call_next):
        if request.url.hostname not in LOCAL:
            return refusal("HOST", "Cette page n'écoute que la boucle locale.", 403)
        origin = request.headers.get("origin")
        scheme = "https" if request.url.scheme in ("https", "wss") else "http"
        if origin and origin != scheme + "://" + request.url.netloc:
            return refusal("ORIGIN", "Appel d'une autre origine : refusé.", 403)
        if request.method == "POST":
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_REQUEST:
                    return refusal(
                        "REQUEST_SIZE",
                        "Texte trop long pour cette page : garde un prompt qu'on relit.",
                        413,
                    )
            request._body = bytes(body)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def opened():
        return HTMLResponse(
            (Path(__file__).parent / "page.html").read_text(encoding="utf-8"),
            headers={
                "Cache-Control": "no-store",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": POLICY,
            },
        )

    @app.get("/state")
    async def state():
        return launched(["status", "--root", str(root)])

    @app.post("/prompt")
    async def prompt(request: Request):
        text = (await request.json()).get("text", "")
        code, result = run(["prompt", "--root", str(root), "--file", write("prompt.txt", text)])
        # The text to paste into ChatGPT travels with its reading: a prompt one has to go
        # and fetch in a file is a prompt one pastes wrong.
        return JSONResponse({**result, "prompt_text": text, "exit_code": code})

    @app.post("/answer")
    async def answer(request: Request):
        asked = await request.json()
        elsewhere = moved(asked)
        if elsewhere:
            return elsewhere
        text = asked.get("text", "")
        code, result = run(["answer", "--root", str(root), "--file", write("answer.txt", text)])
        # La réponse contrôlée revient avec son verdict, comme le prompt revient avec sa
        # lecture : la page compare ce qu'elle affiche au texte qui est allé dans le fichier
        # que le lanceur a lu, et elle ne livre pas un reçu sur un autre texte.
        return JSONResponse({**result, "answer_text": text, "exit_code": code})

    @app.post("/confirm")
    async def confirm(request: Request):
        asked = await request.json()
        elsewhere = moved(asked)
        if elsewhere:
            return elsewhere
        by = str(asked.get("by", ""))
        raised = [str(one) for one in asked.get("ids", [])]
        lowered = [str(one) for one in asked.get("refuse", [])]
        if not NAME.match(by) or not all(NAME.match(one) for one in raised + lowered):
            return refusal("NAME", "Nom ou identifiant inattendu : rien n'a été confirmé.", 400)
        argv = ["confirm", "--root", str(root), "--by", by]
        for one in raised:
            argv += ["--id", one]
        for one in lowered:
            argv += ["--refuse", one]
        return launched(argv)

    @app.post("/deliver")
    async def deliver(request: Request):
        elsewhere = moved(await request.json())
        if elsewhere:
            return elsewhere
        code, result = run(["deliver", "--root", str(root)])
        # The three files a verification replays, handed over with the receipt: the page
        # offers them to save, so no path has to be copied anywhere.
        files = {}
        folder = Path(result["receipt_folder"]) if result.get("receipt_folder") else None
        if folder and folder.is_dir():
            for name in ("receipt.json", "answer.txt", "prompt.txt"):
                kept = folder / name
                if kept.is_file():
                    files[name] = kept.read_text(encoding="utf-8")
        return JSONResponse({**result, "files": files, "exit_code": code})

    @app.post("/verify")
    async def verify(request: Request):
        given = await request.json()
        argv = ["verify"]
        for flag, name in (("--receipt", "receipt.json"), ("--answer", "answer.txt")):
            argv += [flag, write("verify-" + name, given.get(name, ""))]
        if given.get("prompt.txt"):
            argv += ["--prompt", write("verify-prompt.txt", given["prompt.txt"])]
        return launched(argv)

    return app


def serve(root, port=PORT, opened=None):
    """Open the page on the loopback and nowhere else, and say so only once it listens.

    The server starts in a thread and this waits for it to be LISTENING before anything is
    announced or opened. Two reasons, both measured as failures of the first version: a port
    already taken has to be a refusal that names itself instead of a traceback nobody reads;
    and the address handed to a browser must be the one actually served -- otherwise a
    double-click shows whatever else holds this port, and the page it shows is not this one.

    `opened` is called with the address once it is live. The tests pass their own, so nothing
    reaches a real browser; the launcher passes the browser.
    """
    import socket
    import threading

    import uvicorn

    # Le port est essaye ICI, avant qu'uvicorn soit lance : sinon sa propre ligne ERROR
    # arrive au-dessus du refus, et Said lit une trace anglaise avant la phrase qui lui dit
    # quoi faire. Un essai libere le port aussitot, donc il reste une course : si quelqu un
    # le prend entre-temps, `started` restera faux et le refus sera le meme.
    tried = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name != "nt":
            # Exactement ce que fait asyncio, et donc uvicorn : SO_REUSEADDR sur POSIX, pas
            # sous Windows. Sans lui, un port en TIME_WAIT après un vrai arrêt était
            # annoncé PRIS alors que le serveur, lui, aurait su le prendre — la sonde
            # mentait sur ce qu'elle sonde. Avec lui sous Windows, à l'inverse, elle
            # réussirait à se poser sur un port qu'un autre écoute vraiment.
            tried.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tried.bind((HOST, port))
    except OSError:
        return TAKEN, None
    finally:
        tried.close()

    page = uvicorn.Server(
        uvicorn.Config(create_page(root), host=HOST, port=port, log_level="warning")
    )
    listening = threading.Thread(target=page.run, daemon=True)
    where = None
    # Ctrl-C arrive dans le fil principal, pas dans celui du serveur, et il peut arriver à
    # N'IMPORTE QUEL moment : pendant que le serveur démarre, pendant que le navigateur
    # s'ouvre, ou pendant l'attente. L'arrêt est donc dans un `finally` et nulle part
    # ailleurs. Il était attaché à la seule attente : une interruption au démarrage
    # remontait alors en trace, sans que le serveur ait jamais reçu l'ordre de s'arrêter.
    # Refusé par la revue.
    try:
        # Le démarrage lui-même est DANS le try : une interruption tombée entre le start et
        # le try laissait un fil vivant sans jamais recevoir l'ordre de s'arrêter. La
        # fenêtre est minuscule, et c'est précisément pourquoi elle n'a aucune raison de
        # rester ouverte. Sixième refus de la revue.
        listening.start()
        while not page.started and listening.is_alive():
            time.sleep(0.05)
        if not page.started:
            return TAKEN, None
        where = f"http://{HOST}:{port}/"
        if opened:
            try:
                opened(where)
            except Exception:
                # Un navigateur qui refuse de s'ouvrir n'est pas une raison de tuer la
                # page : l'adresse est déjà écrite à l'écran, et elle suffit.
                pass
        while listening.is_alive():
            listening.join(0.2)
    except KeyboardInterrupt:
        # Interrompu AVANT d'avoir écouté : ce n'est pas un port pris, c'est un arrêt
        # demandé. Les deux rendaient la même réponse, et le lanceur annonçait alors un
        # port pris sur un port libre — un diagnostic faux, refusé par la revue.
        if where is None:
            return CANCELLED, None
    finally:
        # L'attente n'a PAS de délai. Elle en avait un de cinq secondes, et au-delà cette
        # fonction rendait la main : le lanceur écrivait alors « Page arrêtée » pendant que
        # le serveur tenait encore le port. Un message faux sur l'état de sa propre page,
        # refusé par la revue aussi. Un deuxième Ctrl-C est absorbé, parce qu'il ne rendrait
        # l'arrêt ni plus vrai ni plus rapide ; s'il ne finissait jamais, il reste à fermer
        # la fenêtre, et le lanceur le dit.
        page.should_exit = True
        while listening.is_alive():
            try:
                listening.join(0.2)
            except KeyboardInterrupt:
                continue
    return SERVED, where
