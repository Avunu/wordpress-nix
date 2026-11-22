"""Output and prompts, as frappe-init prints them.

Lines go to stdout, warnings and errors to stderr. A prompt is asked only with a
terminal on both stdin and stdout; without one, ``confirm`` is no.
"""

import sys
from typing import NoReturn


class Die(Exception):
    """Stop: ``ERROR: <message>`` on stderr, exit CODE."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


class Exit(Exception):
    """Stop silently with CODE: a failing step has already said why."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def die(message: str, code: int = 1) -> NoReturn:
    raise Die(message, code)


def out(text: str = "") -> None:
    print(text, flush=True)


def step(text: str) -> None:
    out(f"\n\033[1m{text}\033[0m")


def info(text: str) -> None:
    out(f"  {text}")


def ok(text: str) -> None:
    out(f"  \033[32m✓\033[0m  {text}")


def err(text: str) -> None:
    sys.stdout.flush()
    print(f"  \033[31m✗\033[0m  {text}", file=sys.stderr, flush=True)


def warn_line(text: str) -> None:
    sys.stdout.flush()
    print(f"  \033[33m⚠\033[0m  {text}", file=sys.stderr, flush=True)


def error_line(text: str) -> None:
    sys.stdout.flush()
    print(f"\033[31mERROR:\033[0m {text}", file=sys.stderr, flush=True)


def has_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def confirm(question: str) -> bool:
    """Yes or no; always no without a terminal."""
    if not has_tty():
        return False
    answer = input(f"{question} [Y/n] ").strip().lower()
    return answer in ("", "y", "yes")
