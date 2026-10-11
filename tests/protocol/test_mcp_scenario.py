"""Ten-step synthetic scenario driven through the real stdio MCP bridge.

Level B evidence: every writer operation crosses the MCP protocol (initialize,
tools/list, tools/call) against a live local authority. Connector, reviewer and
operator operations use their own credentials over HTTP, as an operator would
with the CLI. Nothing is validated by a declaration: refusals and counters come
from the server.
"""

import asyncio
import base64
import json
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from skillflow.bootstrap import create_demo
from skillflow.harness import live_server
from skillflow.server import Client
from skillflow.util import canonical, uid


def http(root, role, url, action, args, *, revision=None, key=None):
    return Client(url, root / (role + ".token")).call(action, args, revision, key)


def test_ten_step_scenario_over_real_mcp(tmp_path):
    root = create_demo(tmp_path / "authority")
    observed = []

    def note(step, code, status=None):
        observed.append(
            {
                "step": step,
                "code": code,
                **(
                    {
                        "validated": status["validated"],
                        "required_total": status["required_total"],
                        "attempts": status["attempts"],
                        "invalidated_current": status["invalidated_current"],
                        "refusal_events": status["refusal_events"],
                        "state": status["state"],
                    }
                    if status
                    else {}
                ),
            }
        )

    with live_server(root) as (url, process):
        # 1. Opening the case is a connector operation, never a writer tool.
        case_id = http(
            root, "connector", url, "flow_start", {"case_key": "demo-legal"}, revision=0, key=uid()
        )["result"]["case_id"]
        invocation_id = http(root, "writer", url, "flow_status", {"case_id": case_id})["result"][
            "skills"
        ][0]["invocation_id"]
        session_id = uid()
        for action, args in (
            ("session_bind", {"session_id": session_id}),
            ("skill_enter", {"invocation_id": invocation_id, "session_id": session_id}),
        ):
            revision = http(root, "writer", url, "flow_status", {"case_id": case_id})["result"][
                "revision"
            ]
            assert http(
                root,
                "connector",
                url,
                action,
                {"case_id": case_id, **args},
                revision=revision,
                key=uid(),
            )["ok"]

        async def writer_scenario(url):
            parameters = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-m",
                    "skillflow",
                    "mcp",
                    "--url",
                    url,
                    "--token-file",
                    str(root / "writer.token"),
                ],
            )
            async with (
                stdio_client(parameters) as (reader, writer),
                ClientSession(reader, writer) as client,
            ):
                await client.initialize()
                tools = {t.name for t in (await client.list_tools()).tools}
                assert {"flow_status", "skill_read", "artifact_submit", "flow_check"} <= tools
                assert "approval_submit" not in tools and "flow_start" not in tools

                async def status():
                    result = await client.call_tool("flow_status", {"case_id": case_id})
                    return json.loads(result.content[0].text)["result"]

                async def mutate(name, args):
                    revision = (await status())["revision"]
                    result = await client.call_tool(
                        name,
                        {
                            "case_id": case_id,
                            "revision": revision,
                            "idempotency_key": uid(),
                            **args,
                        },
                    )
                    payload = json.loads(result.content[0].text)
                    return payload, result.isError

                async def submit(text):
                    payload, is_error = await mutate(
                        "artifact_submit",
                        {
                            "invocation_id": invocation_id,
                            "name": "note.txt",
                            "media_type": "text/plain",
                            "content_base64": base64.b64encode(text.encode()).decode(),
                        },
                    )
                    assert not is_error, payload

                async def check(step):
                    payload, is_error = await mutate(
                        "flow_check", {"invocation_id": invocation_id, "step_id": step}
                    )
                    return (payload["error"]["code"] if is_error else "OK"), payload

                # Instructions and resources come from the contract, through MCP.
                read = json.loads(
                    (
                        await client.call_tool(
                            "skill_read", {"case_id": case_id, "invocation_id": invocation_id}
                        )
                    )
                    .content[0]
                    .text
                )
                assert read["ok"] and read["result"]["contract"]["instructions"]
                note("1_open_and_read", "OK", await status())
                # 2. Skipping to the dependent step is refused.
                code, _ = await check("judgment")
                assert code == "INPUTS_MISSING"
                note("2_skip_refused", code, await status())
                # 3. A check without proof is refused.
                code, _ = await check("draft")
                assert code == "INPUTS_MISSING"
                note("3_missing_proof_refused", code, await status())
                # 4. Valid path: submit, mechanical check passes.
                await submit("SYNTHETIC\nConclusion: fictional example, revision one.")
                code, _ = await check("draft")
                assert code == "OK"
                note("4_valid_check", code, await status())
                return session_id

        asyncio.run(writer_scenario(url))

        def review():
            revision = http(root, "writer", url, "flow_status", {"case_id": case_id})["result"][
                "revision"
            ]
            review_id = http(
                root,
                "writer",
                url,
                "review_request",
                {"case_id": case_id},
                revision=revision,
                key=uid(),
            )["result"]["review_id"]
            packet = http(
                root, "reviewer", url, "review_read", {"case_id": case_id, "review_id": review_id}
            )["result"]
            raw = canonical(
                {
                    "verdict": "JUSTE",
                    "reason": "Executed synthetic fixture; no semantic assurance.",
                    "references": [o["sha256"] for o in packet["objects"]],
                    "obligations": {k: "JUSTE" for k in packet["obligations"]},
                }
            ).decode()
            revision = http(root, "writer", url, "flow_status", {"case_id": case_id})["result"][
                "revision"
            ]
            return http(
                root,
                "reviewer",
                url,
                "review_submit",
                {"case_id": case_id, "review_id": review_id, "raw": raw, "provider": "fixture"},
                revision=revision,
                key=uid(),
            )

        # 9a. The judgment step needs the reviewer credential; a fixture verdict is demo-only.
        assert review()["ok"]
        validated = http(root, "writer", url, "flow_status", {"case_id": case_id})["result"]
        assert validated["validated"] == 2 and validated["progress_percent"] == 100
        assert not validated["delivery"]["authorized"]
        note("4_9_validated_after_review", "OK", validated)

        # 5-6. A changed deliverable invalidates dependent checks and moves counters backwards.
        async def change_and_correct(url):
            parameters = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-m",
                    "skillflow",
                    "mcp",
                    "--url",
                    url,
                    "--token-file",
                    str(root / "writer.token"),
                ],
            )
            async with (
                stdio_client(parameters) as (reader, writer),
                ClientSession(reader, writer) as client,
            ):
                await client.initialize()

                async def status():
                    return json.loads(
                        (await client.call_tool("flow_status", {"case_id": case_id}))
                        .content[0]
                        .text
                    )["result"]

                async def mutate(name, args):
                    revision = (await status())["revision"]
                    result = await client.call_tool(
                        name,
                        {
                            "case_id": case_id,
                            "revision": revision,
                            "idempotency_key": uid(),
                            **args,
                        },
                    )
                    return json.loads(result.content[0].text), result.isError

                payload, is_error = await mutate(
                    "artifact_submit",
                    {
                        "invocation_id": invocation_id,
                        "name": "note.txt",
                        "media_type": "text/plain",
                        "content_base64": base64.b64encode(
                            b"SYNTHETIC\nConclusion: fictional example, revision two."
                        ).decode(),
                    },
                )
                assert not is_error, payload
                after = await status()
                assert after["validated"] == 0 and after["invalidated_current"] == 2
                note("5_6_changed_and_invalidated", "OK", after)
                payload, is_error = await mutate("delivery_request", {"destination": "local"})
                assert is_error and payload["error"]["code"] == "DELIVERY_PRECONDITIONS"
                note("6_delivery_refused", payload["error"]["code"], await status())
                # 7. Correction: the same step is re-checked, no new step appears.
                payload, is_error = await mutate(
                    "flow_check", {"invocation_id": invocation_id, "step_id": "draft"}
                )
                assert not is_error, payload
                corrected = await status()
                assert corrected["required_total"] == 2
                note("7_corrected", "OK", corrected)

        asyncio.run(change_and_correct(url))
        before_kill = http(root, "writer", url, "flow_status", {"case_id": case_id})["result"]
        # 8. Interruption: the authority process is killed, then restarted on the same store.
        process.kill()
        process.wait(timeout=10)

    with live_server(root) as (url, _):
        after_kill = http(root, "writer", url, "flow_status", {"case_id": case_id})["result"]
        for key in (
            "validated",
            "attempts",
            "corrections",
            "revision",
            "refusal_events",
            "budget_remaining",
        ):
            assert after_kill[key] == before_kill[key], key
        revision = after_kill["revision"]
        assert http(
            root,
            "connector",
            url,
            "flow_resume",
            {"case_id": case_id, "session_id": "resumed"},
            revision=revision,
            key=uid(),
        )["ok"]
        note("8_resumed_after_kill", "OK", after_kill)
        # 9. Review again on the corrected snapshot, then human approval by the operator.
        assert review()["ok"]
        revision = http(root, "writer", url, "flow_status", {"case_id": case_id})["result"][
            "revision"
        ]
        demand = http(
            root,
            "writer",
            url,
            "delivery_request",
            {"case_id": case_id, "destination": "local"},
            revision=revision,
            key=uid(),
        )["result"]
        revision = http(root, "writer", url, "flow_status", {"case_id": case_id})["result"][
            "revision"
        ]
        assert http(
            root,
            "operator",
            url,
            "approval_submit",
            {
                "case_id": case_id,
                "delivery_id": demand["delivery_id"],
                "snapshot_hash": demand["snapshot_hash"],
                "decision": "approve",
            },
            revision=revision,
            key=uid(),
        )["ok"]
        note(
            "9_reviewed_and_approved",
            "OK",
            http(root, "writer", url, "flow_status", {"case_id": case_id})["result"],
        )
        # 10. Delivery of the exact validated object, observed by the worker.
        revision = http(root, "writer", url, "flow_status", {"case_id": case_id})["result"][
            "revision"
        ]
        assert http(
            root,
            "operator",
            url,
            "delivery_commit",
            {"case_id": case_id, "delivery_id": demand["delivery_id"]},
            revision=revision,
            key=uid(),
        )["ok"]
        deadline = time.monotonic() + 15
        while (
            http(root, "writer", url, "flow_status", {"case_id": case_id})["result"]["state"]
            != "DELIVERED"
        ):
            assert time.monotonic() < deadline, "worker did not deliver"
            time.sleep(0.25)
        final = http(root, "writer", url, "flow_status", {"case_id": case_id})["result"]
        exported = list((root / "outbox" / demand["delivery_id"]).glob("*.receipt.json"))
        assert final["state"] == "DELIVERED" and exported
        artifact = exported[0].with_name(exported[0].name.removesuffix(".receipt.json"))
        assert artifact.read_bytes() == b"SYNTHETIC\nConclusion: fictional example, revision two."
        note("10_delivered_exact_object", "OK", final)

    (tmp_path / "mcp-scenario-observed.json").write_text(
        json.dumps(observed, indent=1), encoding="utf-8"
    )
    assert [o["step"] for o in observed] == [
        "1_open_and_read",
        "2_skip_refused",
        "3_missing_proof_refused",
        "4_valid_check",
        "4_9_validated_after_review",
        "5_6_changed_and_invalidated",
        "6_delivery_refused",
        "7_corrected",
        "8_resumed_after_kill",
        "9_reviewed_and_approved",
        "10_delivered_exact_object",
    ]
