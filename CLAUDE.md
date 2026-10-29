# IA Gate

MCP server (Python) running on a PC of the private network that lets Claude Code / Claude Desktop
install and test apps on Android devices through adb, addressing each device by a stable gate ID
(e.g. `VAL-042`).

- Spec: `docs/superpowers/specs/2026-10-29-ia-gate-design.md`
- Plan: `docs/superpowers/plans/2026-10-29-ia-gate-v1.md`

## Layout

- `gate/` — uv project, package `ia_gate` in `gate/src/ia_gate/`, tests in `gate/tests/`.
- `.claude/agents/` — dev agents (`gate-implementer`, `spec-reviewer`, `gate-code-reviewer`) and the runtime agent `device-tester`.
- `.claude/workflows/` — orchestrators: `implement-plan` (plan execution with reviews), `device-test` (multi-device test run).

## Commands (from `gate/`)

- `uv sync` — install
- `uv run pytest -q` — tests (no real device needed)
- `uv run ruff check .` — lint
- `uv run ia-gate serve --transport http` — run the gate

## Rules

- TDD; never call a real `adb` in tests — use `tests/fakes.py::FakeRunner`.
- Every argument sent to `adb shell` goes through `AdbClient` (which `shlex.quote`s it).
- User-facing error messages in French and actionable; code and comments in English.
- MCP SDK is v2: `from mcp.server.mcpserver import MCPServer` (not `FastMCP`).
- Do not change an interface listed in the plan's **Interfaces** blocks without updating the plan.
