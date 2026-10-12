"""Runnable public CLI; operator approvals are an explicit separate channel."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import __version__
from .util import FlowError, canonical, strict_json, uid


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def deliver(client, case_id, delivery_id, *, wait=30):
    """Operator command: commit an approved delivery and wait for the export to land.

    Until 2026-09-19 a case opened by an ordinary session could be approved and then sat
    at READY forever: the only committer was the service poller, which skips bindings
    without automatic delivery, and no command existed to do it by hand. The operator's
    hand is the trigger here; the authority still decides. Every guard lives server-side:
    the operator credential, the current approval, the frozen snapshot, the preconditions.
    A second call finds the delivery already committed and reports it without exporting
    anything twice.
    """
    import time

    packet = client.call("delivery_read", {"case_id": case_id, "delivery_id": delivery_id})
    if not packet["ok"]:
        return packet
    if packet["result"]["state"] in ("COMMITTED", "DELIVERED"):
        return {
            "ok": True,
            "delivery_id": delivery_id,
            "state": packet["result"]["state"],
            "already_committed": True,
            "export_directory": packet["result"].get("export_directory"),
            "files": packet["result"].get("files"),
        }
    status = client.call("flow_status", {"case_id": case_id})
    if not status["ok"]:
        return status
    committed = client.call(
        "delivery_commit",
        {"case_id": case_id, "delivery_id": delivery_id},
        status["result"]["revision"],
        uid(),
    )
    if not committed["ok"]:
        return committed
    # The service drains the outbox on its own tick and observes the bytes before it
    # says DELIVERED. Wait a bounded time for that observation; report what was seen.
    deadline = time.monotonic() + max(0, wait)
    state = committed["result"].get("state")
    # A contract without outputs closes in the ledger as COMPLETED: there is no file to
    # observe, and the commit answer already says so. Only an artifact delivery waits.
    while state != "COMPLETED" and time.monotonic() < deadline:
        packet = client.call("delivery_read", {"case_id": case_id, "delivery_id": delivery_id})
        state = packet["result"]["state"] if packet["ok"] else state
        if state == "DELIVERED":
            break
        time.sleep(0.5)
    return {
        "ok": True,
        "delivery_id": delivery_id,
        "state": state,
        "already_committed": False,
        "export_directory": packet["result"].get("export_directory") if packet["ok"] else None,
        "files": packet["result"].get("files") if packet["ok"] else None,
        "observed": state in ("DELIVERED", "COMPLETED"),
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["chatgpt"]:
        # The ChatGPT launcher owns its arguments and always answers with one JSON object.
        from .chatgpt import main as chatgpt_main

        return chatgpt_main(argv[1:])
    parser = argparse.ArgumentParser(prog="skillflow-prompts")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "chatgpt", help="ChatGPT launcher: one command, one process (see chatgpt --help)"
    )
    for name in ("init-demo", "demo"):
        item = commands.add_parser(name)
        if name == "init-demo":
            item.add_argument("--port", type=int, default=8765)
        item.add_argument("--root", type=Path, required=True)
    server = commands.add_parser("serve")
    server.add_argument("--root", type=Path, required=True)
    server.add_argument("--port", type=int, default=8765)
    server.add_argument(
        "--qualification",
        action="store_true",
        help="Explicit preliminary Windows qualification; never emits protected receipts",
    )
    hook = commands.add_parser("client-hook")
    hook.add_argument("--config", type=Path, required=True)
    adapter = commands.add_parser("mcp-client")
    adapter.add_argument("--config", type=Path, required=True)
    installer = commands.add_parser("install-client")
    installer.add_argument("--client", choices=["codex", "claude"], required=True)
    installer.add_argument("--config", type=Path, required=True)
    installer.add_argument("--executable")
    installer.add_argument("--apply", action="store_true")
    controlled = commands.add_parser("controlled-client")
    controlled.add_argument("--manifest", type=Path, required=True)
    controlled.add_argument("--evidence", type=Path, required=True)
    controlled.add_argument("--prompt-file", type=Path, required=True)
    proxy = commands.add_parser("network-proxy")
    proxy.add_argument("--config", type=Path, required=True)
    proxy.add_argument("--log", type=Path, required=True)
    native_demo = commands.add_parser("prepare-client-demo")
    native_demo.add_argument("--client", choices=["codex", "claude"], required=True)
    native_demo.add_argument("--root", type=Path, required=True)
    native_demo.add_argument("--workspace", type=Path, required=True)
    native_demo.add_argument("--skill", default="inventory")
    native_demo.add_argument("--port", type=int, default=8765)
    for name in ("mcp", "call", "status", "approve", "deliver", "review", "review-codex"):
        item = commands.add_parser(name)
        item.add_argument("--url", default="http://127.0.0.1:8765")
        item.add_argument("--token-file", type=Path, required=True)
        item.add_argument("--ca")
        if name == "call":
            item.add_argument("action")
            item.add_argument("--arguments", type=Path, required=True)
            item.add_argument("--revision", type=int)
            item.add_argument("--key")
        if name in ("status", "approve", "deliver", "review", "review-codex"):
            item.add_argument("case_id")
        if name == "status":
            item.add_argument("--json", action="store_true")
            item.add_argument("--output", type=Path)
        if name in ("approve", "deliver"):
            item.add_argument("delivery_id")
        if name == "deliver":
            item.add_argument("--wait", type=int, default=30, help="seconds to wait for the export")
        if name in ("review", "review-codex"):
            item.add_argument("review_id")
            item.add_argument("--model", required=True)
            item.add_argument("--timeout", type=int, default=120)
            item.add_argument("--allow-model-call", action="store_true", required=True)
        if name == "review":
            # The engine is named in the provenance and must be accepted by the contract.
            item.add_argument("--engine", choices=("claude-cli", "codex"), required=True)
            item.add_argument("--response-out", type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("receipt", type=Path)
    verify.add_argument("artifact", type=Path)
    verify.add_argument("--trust", type=Path, required=True)
    verify.add_argument("--case")
    verify.add_argument("--profile")
    verify.add_argument("--revocations", type=Path)
    validate = commands.add_parser("validate-contract")
    validate.add_argument("contract", type=Path)
    schemas = commands.add_parser("schemas")
    schemas.add_argument("--output", type=Path, required=True)
    schemas.add_argument("--check", action="store_true")
    backup = commands.add_parser("backup")
    backup.add_argument("--root", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    bundle = commands.add_parser("snapshot-installation")
    bundle.add_argument("--root", type=Path, required=True)
    bundle.add_argument("--output", type=Path, required=True)
    bundle.add_argument("--authority", default="authority")
    restore = commands.add_parser("restore-copy")
    restore.add_argument("--snapshot", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    restore.add_argument("--manifest-sha256", required=True)
    diagnostic = commands.add_parser("diagnose")
    diagnostic.add_argument("--output", type=Path)
    bench = commands.add_parser("bench")
    bench.add_argument("operation", choices=["run", "validate", "aggregate", "readme", "check"])
    bench.add_argument(
        "--suite", choices=["controller", "integration", "agents"], default="controller"
    )
    bench.add_argument("--output", type=Path, default=Path("benchmarks/local/campaign"))
    bench.add_argument("--repetitions", type=int, default=3)
    bench.add_argument("--repo", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    if args.command == "client-hook":
        from .client_hook import main as hook_main

        return hook_main(args.config)
    try:
        result = execute(args)
        if result is not None:
            emit(result)
            return 0 if not isinstance(result, dict) or result.get("ok", True) else 1
        return 0
    except (FlowError, OSError, ValueError) as exc:
        emit(
            exc.result()
            if isinstance(exc, FlowError)
            else {"ok": False, "error": {"code": type(exc).__name__, "message": str(exc)}}
        )
        return 1


def execute(args):
    if args.command == "network-proxy":
        from .network_proxy import serve

        serve(args.config, args.log)
        return None
    if args.command == "snapshot-installation":
        from .recovery import snapshot

        return snapshot(args.root, args.output, args.authority)
    if args.command == "restore-copy":
        from .recovery import restore_copy

        return restore_copy(args.snapshot, args.output, args.manifest_sha256)
    if args.command == "controlled-client":
        from .controlled_entry import run

        return run(args.manifest, args.prompt_file.read_text(encoding="utf-8"), args.evidence)
    if args.command == "prepare-client-demo":
        from .bootstrap import prepare_client_demo

        return prepare_client_demo(args.root, args.workspace, args.client, args.skill, args.port)
    if args.command == "install-client":
        from .client_install import install

        return install(args.client, args.config, executable=args.executable, apply=args.apply)
    if args.command == "mcp-client":
        from .client_hook import connection
        from .util import require

        config = connection(strict_json(args.config.read_bytes()), Path.cwd())
        require(config, "CLIENT_SCOPE", "No installed MCP connection for this workspace")
        from .server import serve_mcp

        return asyncio.run(serve_mcp(config["url"], config["token_file"], config.get("ca")))
    if args.command == "init-demo":
        from .bootstrap import create_demo

        root = create_demo(args.root, port=args.port)
        return {
            "root": str(root),
            "mode": "demo",
            "protected": False,
            "auto_connections": [
                str(root / f"auto-connections-{c}.json") for c in ("codex", "claude")
            ],
        }
    if args.command == "demo":
        from .demo import run_demo

        result = run_demo(args.root)
        return {k: v for k, v in result.items() if k != "trace"} | {
            "report": str(args.root / "demo-report.json")
        }
    if args.command == "serve":
        import uvicorn

        from .server import create_app

        config = strict_json((args.root / "config.json").read_bytes())
        tls = config.get("tls", {})
        uvicorn.run(
            create_app(args.root, qualification=args.qualification),
            host="127.0.0.1",
            port=args.port,
            access_log=False,
            proxy_headers=False,
            ssl_certfile=tls.get("certificate"),
            ssl_keyfile=tls.get("private_key"),
            timeout_keep_alive=5,
            limit_concurrency=16,
            ws="wsproto",
        )
        return None
    if args.command in ("mcp", "call", "status", "approve", "deliver", "review", "review-codex"):
        from .server import Client, serve_mcp

        client = Client(args.url, args.token_file, args.ca)
        if args.command == "mcp":
            asyncio.run(serve_mcp(args.url, args.token_file, args.ca))
            return None
        if args.command == "call":
            return client.call(
                args.action, strict_json(args.arguments.read_bytes()), args.revision, args.key
            )
        if args.command == "deliver":
            return deliver(client, args.case_id, args.delivery_id, wait=args.wait)
        if args.command in ("review", "review-codex"):
            from .reviewer import run_external_review

            return run_external_review(
                client,
                args.case_id,
                args.review_id,
                engine="codex" if args.command == "review-codex" else args.engine,
                model=args.model,
                timeout=args.timeout,
                response_out=getattr(args, "response_out", None),
            )
        status = client.call("flow_status", {"case_id": args.case_id})
        if not status["ok"]:
            return status
        if args.command == "status":
            if args.output:
                args.output.write_bytes(canonical(status["result"]))
            if args.json:
                return status
            value = status["result"]
            progress = value["progress_percent"]
            print(
                f"{value['state']} | revision {value['revision']} | "
                f"{value['validated']}/{value['required_total']} | "
                f"{progress if progress is not None else 'non calculable'} %"
            )
            for skill in value["skills"]:
                print(
                    f"  {skill['contract_id']} {skill['validated']}/{skill['required_total']} "
                    f"attempts={skill['attempts']} corrections={skill['corrections']}"
                )
            print("Missing: " + ", ".join(value["delivery"]["missing"]))
            return None
        packet = client.call(
            "delivery_read", {"case_id": args.case_id, "delivery_id": args.delivery_id}
        )
        if not packet["ok"]:
            return packet
        emit(packet["result"])
        phrase = "APPROVE " + packet["result"]["snapshot_hash"]
        answer = input(
            "Review the exact document, original review and destination above. Type "
            + phrase
            + ": "
        )
        if answer != phrase:
            return {
                "ok": False,
                "error": {"code": "APPROVAL_CANCELLED", "message": "No approval recorded"},
            }
        return client.call(
            "approval_submit",
            {
                "case_id": args.case_id,
                "delivery_id": args.delivery_id,
                "snapshot_hash": packet["result"]["snapshot_hash"],
                "decision": "approve",
            },
            status["result"]["revision"],
            uid(),
        )
    if args.command == "verify":
        from .receipts import verify

        return verify(
            strict_json(args.receipt.read_bytes()),
            args.artifact.read_bytes(),
            strict_json(args.trust.read_bytes()),
            expected_case=args.case,
            expected_profile=args.profile,
            revocations=strict_json(args.revocations.read_bytes()) if args.revocations else None,
        )
    if args.command == "validate-contract":
        from .models import Contract
        from .util import sha256

        contract = Contract.model_validate(strict_json(args.contract.read_bytes()))
        return {
            "ok": True,
            "contract": f"{contract.id}@{contract.version}",
            "sha256": sha256(canonical(contract.model_dump())),
            "uncovered": [
                o.id for s in contract.steps for o in s.obligations if o.coverage == "absent"
            ],
        }
    if args.command == "backup":
        from .storage import Store, state_path

        config = strict_json((args.root / "config.json").read_bytes())
        Store(state_path(args.root, config)).backup(args.output)
        return {"ok": True, "backup": str(args.output)}
    if args.command == "diagnose":
        from .diagnostics import diagnose

        result = diagnose()
        if args.output:
            args.output.write_bytes(canonical(result))
        return result
    if args.command == "schemas":
        from .schema_export import export

        return export(args.output, check=args.check)
    if args.command == "bench":
        from .benchmarks import command

        return command(args)


if __name__ == "__main__":
    sys.exit(main())
