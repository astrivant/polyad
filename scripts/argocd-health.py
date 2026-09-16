"""
Render Polyad health customizations for an existing Argo CD ConfigMap or Helm release.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from polyad.compiler.registry import DEFINITION_KINDS, RESOURCE_TYPES
from polyad_types.resources import GROUP


def main() -> None:
    """
    Print a merge patch, local ConfigMap, or argo-cd Helm values to standard output.

    Returns:
        None: Writes the configuration to standard output.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("patch", "configmap", "helm"), default="patch")
    parser.add_argument("--namespace", default="argocd", help="Namespace for configmap output.")
    args = parser.parse_args()
    source = (Path(__file__).resolve().parents[1] / "integrations/argocd/health.lua").read_text()
    definitions = ", ".join(f"{kind} = true" for kind in sorted(DEFINITION_KINDS))
    source = source.replace("-- REGISTRY_DEFINITIONS", f"local definitions = {{{definitions}}}")
    data = {
        f"resource.customizations.health.{GROUP}_{kind}": source for kind, resource in sorted(RESOURCE_TYPES.items()) if resource.polyad
    }
    if args.format == "helm":
        document = {"configs": {"cm": data}}
    elif args.format == "configmap":
        document = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "argocd-cm", "namespace": args.namespace},
            "data": data,
        }
    else:
        document = {"data": data}
    yaml.SafeDumper.add_representer(
        str, lambda dumper, value: dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)
    )
    print(yaml.safe_dump(document, sort_keys=False))


if __name__ == "__main__":
    main()
