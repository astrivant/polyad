# Recognize only this installation's reserved definitions, then follow actual
# controller-owner UIDs. An internal label alone never authorizes deletion.
if (.items | type) != "array" then error("expected a Kubernetes resource list") else . end
| .items as $items
| [$items[]
    | select(.metadata.labels["polyad.astrivant.com/internal"] == "true")
    | select((try (.metadata.annotations["polyad.astrivant.com/root-owner"] | fromjson) catch null)
        == [$cluster, $namespace, "Deployment", "polyad-polyad"])
    | .metadata.uid] as $definitions
| def expand:
    . as $known
    | (. + [$items[]
        | select(any(.metadata.ownerReferences[]?;
            .controller == true and (.uid as $uid | $known | index($uid) != null)))
        | .metadata.uid] | unique);
  ($definitions | until(. == expand; expand)) as $managed
| {
    managed: [$items[] | select(.metadata.uid as $uid | $managed | index($uid) != null)],
    applications: [$items[] | select(.kind != "Daemon")
        | select(.metadata.uid as $uid | $managed | index($uid) == null)]
  }
