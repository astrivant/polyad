#!/usr/bin/env python3
"""
Record local cluster integration commands without accessing real infrastructure.
"""

from __future__ import annotations

import json
import os
import signal
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
    if command == ["uname", "-s"]:
        print(os.environ.get("MINIKUBE_TEST_HOST_OS", "Linux"))
    elif command == ["uname", "-m"]:
        print(os.environ.get("MINIKUBE_TEST_HOST_ARCH", "x86_64"))
    elif command[0].startswith("qemu-system-") and command[1:] == ["-accel", "help"]:
        print(os.environ.get("MINIKUBE_TEST_ACCELERATORS", "Accelerators supported in QEMU binary:\nhvf\ntcg"))
    elif command[0] == "minikube" and command[3:5] == ["profile", "list"]:
        profile = command[command.index("--profile") + 1]
        default_driver = "qemu2" if os.environ.get("MINIKUBE_TEST_HOST_OS") == "Darwin" else "kvm2"
        print(
            json.dumps(
                {
                    "valid": [
                        {"Name": "unrelated", "Config": {"Driver": "docker"}},
                        {
                            "Name": profile,
                            "Config": {
                                "Driver": os.environ.get("MINIKUBE_TEST_PROFILE_DRIVER", default_driver),
                                "Network": os.environ.get("MINIKUBE_TEST_PROFILE_NETWORK", "socket_vmnet"),
                            },
                        },
                    ]
                }
            )
        )
    elif command[0] == "kubectl" and "port-forward" in command:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        print(f"Forwarding from 127.0.0.1:{command[-1].split(':')[0]} -> 5000", flush=True)
        try:
            signal.pause()
        finally:
            with Path(os.environ["COMMAND_LOG"]).open("a") as stream:
                stream.write(json.dumps({**record, "forward_stopped": True}) + "\n")
    elif command[:3] == ["docker", "image", "inspect"]:
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
