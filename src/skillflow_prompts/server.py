"""Authenticated HTTP authority with an official SDK stdio MCP adapter."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from . import __version__, api
from .core import Controller
from .delivery import drain_outbox
from .registry import authenticate
from .util import FlowError, canonical, require, strict_json

MAX_REQUEST = 1_600_000


class DaemonLock:
    def __init__(self, root):
        self.path = Path(root) / "daemon.lock"
        self.file = None

    def __enter__(self):
        self.file = self.path.open("a+b")
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt

            if not self.file.read(1):
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self

    def __exit__(self, *_):
        if self.file:
            self.file.close()


def create_app(root, *, qualification=False):
    controller = Controller(root, qualification=qualification)
    from .integration import ClientBridge

    bridge = ClientBridge(controller)

    @asynccontextmanager
    async def lifespan(app):
        with DaemonLock(controller.store.path.parent):
            stop = asyncio.Event()

            async def worker():
                while not stop.is_set():
                    await asyncio.to_thread(bridge.poll_deliveries)
                    await asyncio.to_thread(drain_outbox, controller)
                    try:
                        await asyncio.wait_for(stop.wait(), 1)
                    except TimeoutError:
                        pass

            task = asyncio.create_task(worker())
            try:
                yield
            finally:
                stop.set()
                await task

    app = FastAPI(
        title="SkillFlow authority",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.controller = controller

    def same_origin(connection):
        scheme = "https" if connection.url.scheme in ("https", "wss") else "http"
        return connection.headers.get("origin") == scheme + "://" + connection.url.netloc

    def principal_for(request):
        authorization = request.headers.get("authorization", "")
        require(authorization.startswith("Bearer "), "UNAUTHENTICATED", "Bearer required")
        principal = authenticate(authorization[7:], controller.config)
        if request.headers.get("origin"):
            from .control import operator

            operator(principal)
        return principal

    @app.middleware("http")
    async def boundary(request, call_next):
        if request.headers.get("origin") and not (
            same_origin(request)
            and request.url.path in ("/v1/control", "/v1/call", "/v1/overview", "/v1/catalog")
        ):
            return JSONResponse(
                FlowError("ORIGIN", "Browser-origin calls are not supported").result(), 403
            )
        if request.url.hostname not in ("127.0.0.1", "localhost", "::1", "testserver"):
            return JSONResponse(FlowError("HOST", "Unexpected Host header").result(), 403)
        if request.url.path in ("/v1/call", "/v1/client-event", "/v1/control"):
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > MAX_REQUEST:
                    return JSONResponse(
                        FlowError("REQUEST_SIZE", "Request too large").result(), 413
                    )
            request._body = bytes(data)
        return await call_next(request)

    @app.get("/health")
    async def health():
        return {
            "service": "skillflow-prompts",
            "version": __version__,
            "mode": "qualification" if controller.qualification else controller.config["mode"],
        }

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard():
        return HTMLResponse(
            (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8"),
            headers={
                "Cache-Control": "no-store",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline' https://cdn.jsdelivr.net; connect-src 'self'; frame-ancestors 'none'; form-action 'none'; base-uri 'none'",
            },
        )

    @app.get("/v1/overview")
    @app.post("/v1/control")
    async def operator_control(request: Request):
        from .control import overview, set_state

        try:
            principal = principal_for(request)
            if request.method == "GET":
                result = {
                    "ok": True,
                    "result": await asyncio.to_thread(overview, controller, principal),
                }
            else:
                result = await asyncio.to_thread(
                    set_state, controller, principal, strict_json(await request.body())
                )
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except FlowError as exc:
            return JSONResponse(exc.result(), 403)
        except (OSError, ValueError, TypeError, KeyError):
            return JSONResponse(
                FlowError(
                    "CONTROL_UNAVAILABLE", "Inspect authority state; outcome unknown"
                ).result(),
                503,
            )

    @app.websocket("/v1/live")
    async def live(socket: WebSocket):
        from .control import operator, overview

        if socket.url.hostname not in (
            "127.0.0.1",
            "localhost",
            "::1",
            "testserver",
        ) or not same_origin(socket):
            await socket.close(code=1008)
            return
        await socket.accept()
        try:
            # The token is never put in URLs, browser storage, or websocket replies.
            token = await asyncio.wait_for(socket.receive_text(), 5)
            require(0 < len(token) <= 256, "UNAUTHENTICATED", "Invalid token size")
            principal = authenticate(token, controller.config)
            operator(principal)
            while True:
                result = await asyncio.to_thread(overview, controller, principal)
                await socket.send_json({"ok": True, "result": result})
                await asyncio.sleep(1)
        except WebSocketDisconnect:
            pass
        except (FlowError, OSError, ValueError, TypeError, KeyError, TimeoutError):
            await socket.close(code=1008)

    @app.get("/v1/catalog")
    async def catalog_status(request: Request):
        try:
            authorization = request.headers.get("authorization", "")
            require(authorization.startswith("Bearer "), "UNAUTHENTICATED", "Bearer required")
            principal = authenticate(authorization[7:], controller.config)
            require(
                principal.role == "operator" and "*" in principal.case_keys,
                "FORBIDDEN",
                "Global catalog requires the global operator",
            )
            from .catalog import report

            return {"ok": True, "result": await asyncio.to_thread(report, controller)}
        except FlowError as exc:
            return JSONResponse(exc.result(), 403)
        except (OSError, ValueError, KeyError, TypeError):
            return JSONResponse(
                FlowError("CATALOG_UNAVAILABLE", "Pinned catalog unavailable").result(), 503
            )

    @app.post("/v1/call")
    @app.post("/v1/client-event")
    async def call(request: Request):
        try:
            principal = principal_for(request)
            envelope = strict_json(await request.body())
            handler = bridge.handle if request.url.path == "/v1/client-event" else controller.call
            result = await asyncio.to_thread(handler, principal, envelope)
            return JSONResponse(result)
        except FlowError as exc:
            return JSONResponse(exc.result(), 401 if exc.code == "UNAUTHENTICATED" else 400)
        except (ValueError, TypeError, UnicodeError):
            return JSONResponse(
                FlowError("INVALID_JSON", "Strict JSON object required").result(), 400
            )

    return app


class Client:
    def __init__(self, url, token_file, ca=None, *, timeout=15):
        parsed = urlsplit(url)
        require(
            parsed.scheme in ("http", "https")
            and parsed.hostname in ("127.0.0.1", "localhost", "::1")
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment,
            "CONTROLLER_URL",
            "Use a loopback HTTP(S) controller URL",
        )
        self.url = url.rstrip("/")
        self.token_file = Path(token_file)
        self.ca = ca
        self.timeout = timeout

    def call(self, action, arguments, revision=None, idempotency_key=None):
        return self.request(
            "/v1/call",
            {
                "action": action,
                "arguments": arguments,
                "revision": revision,
                "idempotency_key": idempotency_key,
            },
        )

    def request(self, path, envelope):
        require(path in ("/v1/call", "/v1/client-event"), "CONTROLLER_PATH", "Unknown API path")
        try:
            import ssl

            verify = ssl.create_default_context(cafile=self.ca) if self.ca else True
            token = self.token_file.read_text(encoding="utf-8").strip()
            with httpx.Client(
                timeout=self.timeout, trust_env=False, verify=verify, follow_redirects=False
            ) as client:
                response = client.post(
                    self.url + path,
                    headers={"Authorization": "Bearer " + token},
                    content=canonical(envelope),
                )
            require(
                response.status_code == 200,
                "CONTROLLER_REJECTED",
                "Controller authentication/transport rejected request",
            )
            result = strict_json(response.content)
            require(
                isinstance(result, dict) and type(result.get("ok")) is bool,
                "CONTROLLER_RESPONSE",
                "Invalid controller response",
            )
            return result
        except FlowError as exc:
            return exc.result()
        except (OSError, ValueError, httpx.HTTPError):
            return FlowError(
                "CONTROLLER_UNAVAILABLE",
                "Controller unavailable; the operation outcome is unknown. Inspect persisted state and retry with the same key",
            ).result()


async def serve_mcp(url, token_file, ca=None):
    client = Client(url, token_file, ca)
    server = Server(
        "skillflow-prompts",
        version=__version__,
        instructions="Use the provisioned case. Only exact gate-exported artifacts have receipts. "
        "Writer tools cannot start cases, assert connector observations, review or approve.",
    )

    @server.list_tools()
    async def list_tools():
        tools = []
        for name in sorted(api.WRITER):
            schema = api.schema(name)
            if name not in api.READ:
                schema["properties"] |= {
                    "revision": {"type": "integer", "minimum": 0},
                    "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 128},
                }
                schema["required"] += ["revision", "idempotency_key"]
            tools.append(
                types.Tool(
                    name=name, description="SkillFlow " + name.replace("_", " "), inputSchema=schema
                )
            )
        return tools

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        if name not in api.WRITER:
            result = FlowError("FORBIDDEN", "Tool is outside the writer adapter").result()
        else:
            args = dict(arguments)
            revision, key = args.pop("revision", None), args.pop("idempotency_key", None)
            result = await asyncio.to_thread(client.call, name, args, revision, key)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result))],
            structuredContent=result,
            isError=not result["ok"],
        )

    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())
