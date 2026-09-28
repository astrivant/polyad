#!/usr/bin/env python3
"""
Model an empty or existing HA lab without touching real Kubernetes or credentials.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    """
    Record bootstrap commands and reject namespace, CRD or Helm ordering mistakes.

    Returns:
        int: Zero for a simulated success, otherwise the requested failure status.
    """
    command = [Path(sys.argv[0]).name, *sys.argv[1:]]
    state = Path(os.environ["FULL_TEST_STATE"])
    mode = os.environ["FULL_TEST_NAMESPACE"]
    with (state / "commands.jsonl").open("a") as log:
        log.write(json.dumps(command) + "\n")
    namespace = state / "namespace"
    if command[0] == "kubectl":
        args = command[5:]
        if args[:2] == ["get", "nodes"]:
            print(json.dumps({"items": [{"metadata": {"name": "base", "labels": {}}}]}))
        elif args[:2] == ["get", "node"]:
            print('{"metadata":{"labels":{}}}')
        elif args[:2] == ["get", "namespace"]:
            if mode == "forbidden":
                print("namespace lookup forbidden", file=sys.stderr)
                return 41
            if mode == "terminating":
                print('{"metadata":{"deletionTimestamp":"2026-09-28T00:00:00Z"}}')
            elif namespace.exists():
                print('{"metadata":{"name":"polyad","labels":{"customer":"preserve"}},"status":{"phase":"Active"}}')
        elif args[:2] == ["create", "namespace"]:
            if mode == "create-failed" or namespace.exists():
                return 42
            namespace.touch()
        elif args[:2] == ["wait", "namespace/polyad"]:
            if not namespace.exists() or mode == "wait-failed":
                return 43
        elif args[:2] == ["get", "secret"]:
            if not namespace.exists():
                raise AssertionError("Secret queried before namespace creation")
            secret = state / f"secret-{args[2]}"
            if not secret.exists():
                return 1
            print(f"secret/{args[2]}")
        elif args[:2] == ["create", "secret"]:
            if not namespace.exists():
                raise AssertionError("Secret created before namespace creation")
            secret = state / f"secret-{args[3]}"
            if secret.exists():
                raise AssertionError("Existing Secret was replaced")
            secret.touch()
        elif args[0] == "apply" and args[-1].endswith("charts/polyad-crds/crds"):
            (state / "crds-applied").touch()
        elif args[0] == "wait" and args[-1].endswith("charts/polyad-crds/crds"):
            assert (state / "crds-applied").exists()
            (state / "crds-ready").touch()
        elif args[:3] == ["create", "-f", "-"] or args[:3] == ["apply", "--server-side", "--field-manager=polyad-minikube-full"]:
            if args[-1] == "-":
                sys.stdin.read()
        elif args[:2] == ["create", "-f"] and "workload.yaml" in " ".join(args):
            print("polyad-minikube-smoke-fixture")
        elif args[:2] == ["get", "deployment"] and "availableReplicas" in args[-1]:
            print("2")
        elif args[:2] == ["get", "dragonfly"]:
            print("2")
    elif command[0] == "helm":
        if command[1] == "template":
            print("# Mock prerequisite CRDs")
        elif command[1] == "upgrade":
            assert namespace.exists(), "Helm called before namespace creation"
            assert (state / "crds-ready").exists(), "Helm called before Polyad CRDs were established"
            release_name = command[3] if command[2] == "--install" else command[2]
            release = state / f"release-{release_name}"
            if not release.exists() and "--install" not in command:
                raise AssertionError("First installation attempted upgrade without --install")
            release.touch()
    elif command[0] == "openssl":
        # These are synthetic fixtures, not generated credentials or live keys.
        if "-out" in command:
            Path(command[command.index("-out") + 1]).write_text("synthetic-test-key")
        if "-keyout" in command:
            Path(command[command.index("-keyout") + 1]).write_text("synthetic-test-key")
        if command[1] == "rand":
            print("synthetic-test-token")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
