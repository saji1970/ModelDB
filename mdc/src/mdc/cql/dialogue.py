"""Human-facing text for the clarification dialogue (CLAUDE.md section 24).

Pure formatting - `mdc.mce.ambiguity` decides what the question *is*
(subject, options); this module decides how it *reads* on screen. Kept
separate so the CLI's presentation can change without touching the
resolution logic.
"""

from __future__ import annotations

from mdc.mce.ambiguity import ClarificationQuestion


def format_question(question: ClarificationQuestion) -> list[str]:
    lines = [f'I need to clarify what you mean by "{question.subject}".']
    for option in question.options:
        lines.append(f"  {option.index}. {option.label}")
    return lines


def format_unresolved(question: ClarificationQuestion) -> list[str]:
    lines = ["I didn't understand that answer. Please choose one:"]
    for option in question.options:
        lines.append(f"  {option.index}. {option.label}")
    return lines
