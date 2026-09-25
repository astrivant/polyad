#!/usr/bin/env python3
"""
Record local cluster integration commands without accessing real infrastructure.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    """
    Simulate the small set of command outputs consumed by the lifecycle script.

    Returns:
        int: Requested failure code, or zero after recording a successful command.
    """
    command = [Path(sys.argv[0]).name, *sys.argv[1:]]
    record = {
        "command": command,
        "repository_config": os.environ.get("HELM_REPOSITORY_CONFIG"),
        "repository_cache": os.environ.get("HELM_REPOSITORY_CACHE"),
        "kind_provider": os.environ.get("KIND_EXPERIMENTAL_PROVIDER"),
    }
    if command[0] == "kubectl" and "create" in command and command[-2:] == ["-f", "-"]:
        record["stdin"] = sys.stdin.read()
    with Path(os.environ["COMMAND_LOG"]).open("a") as stream:
        stream.write(json.dumps(record) + "\n")

    # Fail after recording so tests can prove no later mutation was attempted.
    if (failure := os.environ.get("FAIL_COMMAND")) and failure in " ".join(command):
        return 43
    if command[:3] == ["docker", "image", "inspect"]:
        print("sha256:" + "a" * 64)
    elif command[:3] == ["kind", "get", "clusters"]:
        print(os.environ.get("EXISTING_CLUSTERS", ""), end="")
    elif command[:3] == ["kind", "get", "nodes"]:
        print(os.environ.get("KIND_NODES", "test-control-plane\ntest-worker\ntest-worker2"), end="")
    elif command[:3] == ["kind", "export", "kubeconfig"]:
        Path(command[command.index("--kubeconfig") + 1]).touch()
    elif command[0] == "kubectl":
        if "jsonpath={.spec.replicas}" in command:
            print("1")
        elif "jsonpath={.metadata.name}" in command:
            print("polyad-kind-smoke-abc12" if "--kubeconfig" in command else "polyad-minikube-smoke-abc12")
        elif "get" in command and "graphs,polygraphs,replicagroups,compositions,rewrites" in command:
            print(os.environ.get("EXISTING_BOUNDARIES", ""), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
