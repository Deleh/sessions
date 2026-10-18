import argparse
import os
import shutil
import signal
import sys
import time
from collections.abc import Callable
from enum import StrEnum
from typing import NoReturn

import libtmux
import yaml

STOP_TIMEOUT = 30  # [s]
COMMAND_PANE_OPTION = "@sessions-pane"


SessionsConfig = dict[str, dict]
Notes = dict[str, str]


class Format(StrEnum):
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    CLEAR = "\r\033[K"


server = libtmux.Server()


def error(message: str) -> NoReturn:
    sys.exit(f"{Format.BOLD}{Format.RED}error{Format.RESET} {message}")


def get_configured_sessions(config_path: str) -> SessionsConfig:
    try:
        with open(config_path) as f:
            config = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as ex:
        error(f"failed to load config {Format.BOLD}{config_path}{Format.RESET}: {ex}")

    if not isinstance(config, dict):
        error(
            f"config {Format.BOLD}{config_path}{Format.RESET} must map session names "
            "to settings"
        )

    for session_name, session_config in config.items():
        if (
            not isinstance(session_name, str)
            or not session_name
            or any(c in session_name for c in ".:")
        ):
            error(
                f"invalid session name {session_name!r}, must be a non-empty string "
                "without '.' or ':'"
            )

        if not isinstance(session_config, dict) or not session_config.get("command"):
            error(
                f"session {Format.BOLD}{session_name}{Format.RESET} has no configured "
                f"{Format.BOLD}command{Format.RESET}"
            )

        directory = session_config.get("directory")
        if directory:
            path = os.path.expanduser(os.path.expandvars(directory))
            if not os.path.isdir(path):
                error(
                    f"session {Format.BOLD}{session_name}{Format.RESET} has a "
                    f"non-existent directory {Format.BOLD}{directory}{Format.RESET}"
                )
            session_config["directory"] = path

    return config


def match_sessions(sessions: SessionsConfig, prefix: str) -> set[str]:
    if prefix in sessions:
        return {prefix}

    return {s for s in sessions if s.startswith(prefix)}


def select_sessions(sessions: SessionsConfig, prefixes: list[str]) -> SessionsConfig:
    if not prefixes:
        return sessions

    selected = set()
    for prefix in prefixes:
        matches = match_sessions(sessions, prefix)
        if not matches:
            error(
                f"no session matches {Format.BOLD}{prefix}{Format.RESET}, available "
                f"sessions: {', '.join(sessions)}"
            )
        selected |= matches

    return {name: cfg for name, cfg in sessions.items() if name in selected}


def get_command_pane(session: libtmux.Session) -> libtmux.Pane | None:
    try:
        pane_id = session.show_option(COMMAND_PANE_OPTION)
    except libtmux.exc.InvalidOption:
        return None

    return session.panes.get(pane_id=pane_id, default=None)


def get_session_state(session: libtmux.Session | None) -> tuple[Format, str]:
    if not session:
        return Format.DIM, "○ stopped"

    pane = get_command_pane(session)
    if pane is None:
        return Format.DIM, "● unmanaged"
    if pane.pane_dead != "1":
        return Format.GREEN, "● running"
    if pane.pane_dead_status == "0":
        return Format.YELLOW, "● idle"

    if pane.pane_dead_status is not None:
        return Format.RED, f"● error (exit {pane.pane_dead_status})"
    if pane.pane_dead_signal is not None:
        try:
            return (
                Format.RED,
                f"● error ({signal.Signals(int(pane.pane_dead_signal)).name})",
            )
        except ValueError:
            return Format.RED, f"● error (signal {pane.pane_dead_signal})"
    return Format.RED, "● error (?)"


def is_session_dead(session_name: str) -> bool:
    session = server.sessions.get(session_name=session_name, default=None)
    if session is None:
        return True

    pane = get_command_pane(session)
    return pane is None or pane.pane_dead == "1"


def signal_pane(pane: libtmux.Pane, sig: signal.Signals) -> None:
    try:
        os.killpg(os.getpgid(int(pane.pane_pid)), sig)
    except ProcessLookupError:
        pass


def start_sessions(sessions: SessionsConfig) -> Notes:
    notes = {}

    running_sessions = {s.name: s for s in server.sessions}
    for session_name, session_config in sessions.items():
        session = running_sessions.get(session_name)

        # Start a new session
        if not session:
            session = server.new_session(
                session_name=session_name,
                start_directory=session_config.get("directory"),
            )

            # Mark the managed pane
            pane = session.windows[0].panes[0]
            session.set_option(COMMAND_PANE_OPTION, pane.pane_id)

            # Start the actual command after setting the "remain-on-exit" option
            pane.set_option("remain-on-exit", "on")
            pane.respawn(kill=True, shell=session_config["command"])

        # Handle unmanaged sessions
        elif (pane := get_command_pane(session)) is None:
            notes[session_name] = "not managed by sessions"

        # Restart the command if it terminated or failed
        elif pane.pane_dead == "1":
            pane.respawn(
                shell=session_config["command"],
                start_directory=session_config.get("directory"),
            )

        else:
            notes[session_name] = "was already running"

    return notes


def stop_sessions(sessions: SessionsConfig) -> Notes:
    notes = {}

    # Send SIGTERM to processes in managed sessions
    managed_sessions = {}
    for session_name in sessions:
        session = server.sessions.get(session_name=session_name, default=None)

        if not session:
            notes[session_name] = "was not running"
        elif (pane := get_command_pane(session)) is None:
            notes[session_name] = "not managed by sessions"
        else:
            managed_sessions[session_name] = session
            if pane.pane_dead != "1":
                signal_pane(pane, signal.SIGTERM)

    # Gather not terminated sessions
    pending_sessions = set(managed_sessions)
    deadline = time.monotonic() + STOP_TIMEOUT
    while pending_sessions and time.monotonic() < deadline:
        pending_sessions = {s for s in pending_sessions if not is_session_dead(s)}

        if not pending_sessions:
            break

        # Show a "waiting for" note in interactive terminals
        if sys.stderr.isatty():
            status = (
                f"waiting for {', '.join(sorted(pending_sessions))} "
                f"({int(deadline - time.monotonic())}s)"
            )
            status = status[: shutil.get_terminal_size().columns - 1]
            print(
                f"{Format.CLEAR}{Format.DIM}{status}{Format.RESET}",
                end="",
                file=sys.stderr,
                flush=True,
            )
        time.sleep(0.5)

    if sys.stderr.isatty():
        print(Format.CLEAR, end="", file=sys.stderr, flush=True)

    # Cleanup
    for session_name, session in managed_sessions.items():
        try:
            # Kill not terminated sessions after timeout
            if session_name in pending_sessions:
                if pane := get_command_pane(session):
                    signal_pane(pane, signal.SIGKILL)
                notes[session_name] = f"killed after {STOP_TIMEOUT}s"

            session.kill()
        except libtmux.exc.LibTmuxException:
            # Session is already gone
            pass

    return notes


def restart_sessions(sessions: SessionsConfig) -> Notes:
    return stop_sessions(sessions) | start_sessions(sessions)


def list_sessions(sessions: SessionsConfig, notes: Notes) -> None:
    running_sessions = {s.name: s for s in server.sessions}

    rows = [(name, *get_session_state(running_sessions.get(name))) for name in sessions]
    name_width = max((len(name) for name, _, _ in rows), default=0)
    state_width = max((len(text) for _, _, text in rows), default=0)

    for name, color, text in rows:
        line = f"{name:<{name_width}}  {color}{text:<{state_width}}{Format.RESET}"
        if note := notes.get(name):
            line += f"  {Format.DIM}{note}{Format.RESET}"
        print(line)


COMMANDS: dict[str, Callable[[SessionsConfig], Notes]] = {
    "ls": lambda _: {},
    "list": lambda _: {},
    "start": start_sessions,
    "restart": restart_sessions,
    "stop": stop_sessions,
}


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="sessions",
        description="Run configured commands in detached tmux sessions.",
    )
    parser.add_argument(
        "command",
        choices=COMMANDS,
        help="Sessions command",
    )
    parser.add_argument(
        "sessions",
        nargs="*",
        help="Session names or prefixes (default: all configured)",
    )
    parser.add_argument(
        "--sessions-config",
        "-c",
        help="Path to sessions config (default ~/.config/sessions.yaml)",
        default="~/.config/sessions.yaml",
    )
    args = parser.parse_args()

    # Check if tmux is installed
    if not shutil.which("tmux"):
        error("tmux not found, please install it")

    configured_sessions = get_configured_sessions(
        os.path.expanduser(args.sessions_config)
    )

    selected_sessions = select_sessions(configured_sessions, args.sessions)

    notes = COMMANDS[args.command](selected_sessions)
    list_sessions(selected_sessions, notes)


if __name__ == "__main__":
    main()
