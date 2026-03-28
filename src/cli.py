from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .main import TradingSystem

DEFAULT_STATE_PATH = Path("logs/analysis_state.json")
DEFAULT_SESSION_DIR = Path("logs/analysis_sessions")


def _read_json_file(path: Path) -> dict:
    return json.loads(path.read_text())


def _read_text_input(path_value: str) -> str:
    if path_value == "-":
        return sys.stdin.read()
    return Path(path_value).read_text()


def _write_session(payload: dict, session_file: Path) -> None:
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text(json.dumps(payload))


def _print(payload: dict) -> int:
    print(json.dumps(payload, indent=2))
    return 0


def _analysis_prompt_payload(system: TradingSystem, session_file: Path) -> dict:
    state = system.export_analysis_state()
    event = state.get("pendingAnalysisEvent") or {}
    return {
        "sessionFile": str(session_file),
        "stepType": "analysis",
        "cycleNumber": state.get("cycleNumber", 0),
        "symbols": state.get("symbols", []),
        "systemPrompt": event.get("system_prompt", ""),
        "userPrompt": event.get("user_prompt", ""),
        "renderedPrompt": event.get("rendered_prompt", ""),
    }


def cmd_prepare(args: argparse.Namespace) -> int:
    state = _read_json_file(Path(args.state_file))
    session_file = Path(args.session_file or _default_session_file())
    _write_session(state, session_file)
    system = TradingSystem.from_analysis_state(state)
    payload = _analysis_prompt_payload(system, session_file)
    if args.symbols:
        payload["requestedSymbols"] = args.symbols
    return _print(payload)


def cmd_prepare_position(args: argparse.Namespace) -> int:
    session_file = Path(args.session_file)
    state = _read_json_file(session_file)
    system = TradingSystem.from_analysis_state(state)

    analysis_json = _read_text_input(args.analysis_file)
    positions_json = _read_text_input(args.positions_file)
    prompt = system.get_position_prompt(analysis_json, positions_json)
    _write_session(system.export_analysis_state(), session_file)

    if prompt is None:
        return _print(
            {
                "sessionFile": str(session_file),
                "stepType": "analysis",
                "submitAnalysisDirectly": True,
            }
        )

    state = system.export_analysis_state()
    event = state.get("pendingPositionEvent") or {}
    return _print(
        {
            "sessionFile": str(session_file),
            "stepType": "position_management",
            "submitAnalysisDirectly": False,
            "cycleNumber": state.get("cycleNumber", 0),
            "symbols": state.get("symbols", []),
            "systemPrompt": event.get("system_prompt", ""),
            "userPrompt": event.get("user_prompt", ""),
            "renderedPrompt": event.get("rendered_prompt", ""),
        }
    )


def cmd_submit(args: argparse.Namespace) -> int:
    session_file = Path(args.session_file)
    state = _read_json_file(session_file)
    system = TradingSystem.from_analysis_state(state)
    response_json = _read_text_input(args.response_file)
    result = system.submit_decision(response_json)
    _write_session(system.export_analysis_state(), session_file)
    return _print(
        {
            "sessionFile": str(session_file),
            "result": result,
        }
    )


def _default_session_file() -> str:
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return str(DEFAULT_SESSION_DIR / f"{timestamp}.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Trading analysis CLI")
    top = parser.add_subparsers(dest="command", required=True)

    analyze = top.add_parser("analyze", help="Single analysis command surface")
    analyze_sub = analyze.add_subparsers(dest="analyze_command", required=True)

    prepare = analyze_sub.add_parser("prepare", help="Create an analysis session from daemon state")
    prepare.add_argument("--state-file", default=str(DEFAULT_STATE_PATH))
    prepare.add_argument("--session-file")
    prepare.add_argument("--symbols", nargs="*")
    prepare.set_defaults(func=cmd_prepare)

    prepare_position = analyze_sub.add_parser(
        "prepare-position",
        help="Create a position-management prompt from analysis + positions",
    )
    prepare_position.add_argument("--session-file", required=True)
    prepare_position.add_argument("--analysis-file", required=True)
    prepare_position.add_argument("--positions-file", required=True)
    prepare_position.set_defaults(func=cmd_prepare_position)

    submit = analyze_sub.add_parser("submit", help="Validate and persist a decision response")
    submit.add_argument("--session-file", required=True)
    submit.add_argument("--response-file", required=True)
    submit.set_defaults(func=cmd_submit)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
