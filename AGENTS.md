# Repository Guidelines

## Project Structure & Module Organization

This local Python POC runs Clara through LiveKit: Deepgram speech recognition → OpenAI Responses → ElevenLabs speech synthesis. `src/agent.py` configures the `AgentServer`, providers, and one `AgentSession` per conversation. `src/clara_prompt.txt` holds interview instructions; `src/observability.py` handles logs, metrics, and session reports. `pyproject.toml` declares dependencies, `uv.lock` locks versions, and `.python-version` pins Python 3.13.2. Generated files belong in ignored `outputs/` and `console-recordings/`. There is no dedicated test directory or custom frontend.

## Build, Test, and Development Commands

Run from the repository root in PowerShell:

- `uv sync --locked --python 3.13.2`: install the locked dependencies.
- `Copy-Item .env.example .env`: initialize configuration only when `.env` is absent.
- `lk agent console --list-devices`: list audio devices without provider credentials.
- `lk agent console`: run a local terminal conversation.
- `lk agent console --text`: inspect prompt and context through text.
- `lk agent console --record`: retain CLI audio and reports.
- `lk agent dev`: connect the local worker to LiveKit for browser Agent Console sessions; add `--no-reload` for longer conversations.

There is no separate build command or configured automated test runner.

## Coding Style & Naming Conventions

Follow existing Python style: four-space indentation, `snake_case` functions and variables, `PascalCase` classes, and uppercase constants. Add type annotations consistent with adjacent code. Keep session orchestration in `agent.py` and reporting logic in `observability.py`. No formatter or linter is configured. Preserve pinned SDK/plugin versions unless deliberately updating dependencies and the lockfile together.

## Testing Guidelines

Use the README's manual scenarios: normal conversation, thinking pauses, long answers, interruptions, noise, Spanish speech, and continuous sessions. Record mode, session ID, configuration, observations, and report location. Evaluate terminal and WebRTC separately. Confirm normal closure produces `outputs/<session-id>/report.json`. No coverage target or automated test naming convention is established; report unverified scenarios explicitly.

## Commit & Pull Request Guidelines

Git history is unavailable in this directory, so no existing commit convention can be confirmed. Use concise imperative messages, such as `Improve session report diagnostics`. PRs should explain the behavior change, link relevant issues, list validation performed and remaining gaps, and include sanitized evidence for audio or reporting changes.

## Security & Configuration

Keep credentials in the root `.env`; existing environment variables take precedence. Never commit secrets, candidate transcripts, recordings, or generated reports. Maintain `.env.example` when adding configuration. Keep the agent local; deployment and Runway integration require separate scope.
