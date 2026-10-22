"""Cancel an in-flight REPL request, then render a follow-up answer."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tests._helpers.polling import poll_until
from tests.e2e.conftest import get_mock_requests
from tests.e2e.omnigent._pexpect_harness import (
    await_turn_complete,
    clean_exit,
    spawn_omnigent_run,
    strip_ansi,
    submit_prompt,
)
from tests.e2e.omnigent._snapshot import compare_snapshot
from tests.e2e.omnigent.conftest import configure_mock_llm

# Visible turn-synchronization markers (see test_repl_smoke).
# ``working`` is the streaming activity line; ``❯ `` is the idle
# input prompt. Both survive PTY truncation, unlike the far-right
# ``state:`` badge the test originally synchronized on.
_RUNNING_MARKER = r"working"
_COMPLETION_MARKER = r"❯ "

_MODEL = "mock-model"
_HARNESS = "openai-agents"

# The mock holds this request until the user cancels it.
_LONG_PROMPT = (
    "Count slowly from 1 to 100. Print one number per line, "
    "with a short verbal description after each number "
    "explaining what the number could mean. Take your time."
)
_FOLLOW_UP_PROMPT = "say hi"
_FOLLOW_UP_ANSWER = "Hi there! How can I help you today?"

# Cancellation acknowledgement rendered by the REPL's run loop
# when the in-flight turn is cancelled: the muted ``cancelled``
# line emitted right after ``session.cancel()`` in the Escape
# handler path (repl/_repl.py). Matching it proves the cancel
# gesture actually interrupted the streaming turn rather than
# being silently dropped.
_CANCEL_ACK_MARKER = r"cancelled"

# The ``◆`` diamond the formatter commits in front of an assistant
# message (``_DiamondMarkdown`` in omnigent_ui_sdk; ``◆ <model>`` on
# the resume path). It is committed to scrollback only when the model
# actually returns text, and never appears in the user-prompt echo
# (``❯``) or toolbar chrome — so it is an assistant-ONLY signal, not
# satisfiable by the submitted prompt's echo.
_ASSISTANT_HEADER_GLYPH = "◆"

# Minimum prose length (after the ``◆`` header) required to count the
# follow-up as a real assistant response. A bare header with no body
# — or the prompt echo alone — must not pass. Two chars clears those
# while staying robust to a terse reply like "Hi".
_MIN_ASSISTANT_BODY_CHARS = 2

_SPAWN_TIMEOUT = 60.0
_BOOT_TIMEOUT = 30.0
_RUNNING_TIMEOUT = 20.0
# Initial turn must be long-lived enough for cancellation to
# land mid-stream. Setting a generous ceiling lets a slow LLM
# still hit the cancel path; if the turn finishes too fast we
# still verify cancellation was attempted via the status line.
_INITIAL_RUNNING_BUDGET = 30.0
_CANCEL_ACK_TIMEOUT = 30.0
_FOLLOWUP_RUNNING_TIMEOUT = 30.0
_FOLLOWUP_COMPLETION_TIMEOUT = 60.0
_EXIT_TIMEOUT = 15.0


def test_repl_cancel_re_arms_for_next_turn(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """Escape cancels a blocked provider call; the next answer reaches the terminal."""
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": "The interrupted answer", "block": True},
            {"text": _FOLLOW_UP_ANSWER},
        ],
        key=_MODEL,
    )

    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"

    child = spawn_omnigent_run(
        omnigent_python=omnigent_python,
        yaml_path=yaml_path,
        model=_MODEL,
        harness=_HARNESS,
        env=mock_credentials_env,
        cwd=omnigent_repo_root,
        timeout=_SPAWN_TIMEOUT,
    )
    try:
        child.expect(_COMPLETION_MARKER, timeout=_BOOT_TIMEOUT)
        submit_prompt(child, _LONG_PROMPT)
        # Wait for the turn to actually start streaming — the
        # visible ``⠹ working`` activity line marks the moment the
        # executor accepted the prompt and is producing output.
        child.expect(_RUNNING_MARKER, timeout=_INITIAL_RUNNING_BUDGET)
        poll_until(
            lambda: any(
                _LONG_PROMPT in str(request)
                for request in get_mock_requests(mock_llm_server_url, key=_MODEL)
            ),
            timeout_s=_INITIAL_RUNNING_BUDGET,
            interval_s=0.1,
        )
        # Press Escape — the REPL's live mid-turn cancel gesture.
        child.send("\x1b")
        # The muted ``cancelled`` line is the observable proof the
        # gesture actually interrupted the streaming turn.
        child.expect(_CANCEL_ACK_MARKER, timeout=_CANCEL_ACK_TIMEOUT)
        # Follow-up prompt — proves the input area still accepts
        # text and the streaming consumer re-armed.
        submit_prompt(child, _FOLLOW_UP_PROMPT)
        followup_turn = await_turn_complete(
            child,
            running_timeout=_FOLLOWUP_RUNNING_TIMEOUT,
            completion_timeout=_FOLLOWUP_COMPLETION_TIMEOUT,
            running_marker=_RUNNING_MARKER,
            completion_pattern=re.escape(_FOLLOW_UP_ANSWER),
        )
        child.expect(_COMPLETION_MARKER, timeout=_FOLLOWUP_COMPLETION_TIMEOUT)
        clean_exit(child, timeout=_EXIT_TIMEOUT)
        exit_code = child.exitstatus
    finally:
        if not child.closed:
            child.close(force=True)

    # Merge the captured turn with the post-exit before-buffer.
    combined_stripped = followup_turn.stripped + "\n" + strip_ansi(child.before or "")

    # Assistant-only signal: the ``◆`` diamond header.
    diamond_idx = combined_stripped.find(_ASSISTANT_HEADER_GLYPH)
    assistant_body = (
        combined_stripped[diamond_idx + len(_ASSISTANT_HEADER_GLYPH) :]
        if diamond_idx != -1
        else ""
    )

    observed: dict[str, Any] = {
        "exit_code": exit_code,
        "follow_up_assistant_response_rendered": diamond_idx != -1
        and len(assistant_body.strip()) >= _MIN_ASSISTANT_BODY_CHARS,
        "follow_up_user_prompt_echoed": "❯" in combined_stripped
        and _FOLLOW_UP_PROMPT in combined_stripped,
    }
    diffs = compare_snapshot("test_repl_ctrl_c_interrupt", observed)
    assert diffs == [], (
        "Snapshot mismatch for cancellation re-arm:\n"
        + "\n".join(diffs)
        + f"\n\nfollow-up turn + tail stripped (last 2000):\n"
        f"{combined_stripped[-2000:]}"
    )
