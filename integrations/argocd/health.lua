-- Polyad lifecycle: docs/operator.md#graph-instance-status.
-- Uses only the observed resource; no API access or open Lua libraries.
local meta = obj.metadata or {}
local spec = obj.spec or {}
local status = obj.status or {}
local phase = status.phase or "Unknown"
local message = status.message
if message == nil or message == "" then
    message = phase
end
local function health(state, detail)
    return {status = state, message = detail}
end

if meta.deletionTimestamp ~= nil then
    return health("Progressing", "Waiting for owned resources and finalizers to finish cleanup")
end

-- REGISTRY_DEFINITIONS
if definitions[obj.kind] or spec.templateOnly == true then
    return health("Healthy", "Reusable definition; execution is reported by graph instances and their resources")
end
if status.observedGeneration ~= (meta.generation or 1) then
    return health("Progressing", "Waiting for the operator to observe the current generation")
end
if phase == "Invalid" or phase == "Failed" or status.failed == true then
    return health("Degraded", message)
end
if obj.kind == "Activation" and phase == "Superseded" then
    return health("Healthy", "Pulse coalesced into a newer pending request")
end
if obj.kind == "Rewrite" then
    if status.applied == true then
        return health("Healthy", "Rewrite applied; execution is reported by the target graph")
    end
    return health("Progressing", message)
end

-- Composition receipts mirror root lifecycle predicates, rather than graph metrics.
if obj.kind ~= "Composition" and obj.kind ~= "Activation" then
    local metrics = status.metrics or {}
    local rollup = metrics.rollup or {}
    if metrics.observedGeneration ~= (meta.generation or 1)
        or rollup.observedGeneration ~= (meta.generation or 1) then
        return health("Progressing", "Waiting for current graph and descendant observations")
    end
    local phases = rollup.graphsByPhase or {}
    local summary = "graphs " .. tostring(rollup.graphCount or 0)
        .. ", leaves " .. tostring(rollup.leafNodes or 0)
        .. ", pending " .. tostring(rollup.pendingLeafNodes or 0)
        .. ", ready " .. tostring(rollup.readyLeafNodes or 0)
        .. ", completed " .. tostring(rollup.completedLeafNodes or 0)
        .. ", failed " .. tostring(rollup.failedLeafNodes or 0)
    message = message .. "; " .. summary
    if (rollup.failedLeafNodes or 0) > 0 or (phases.Failed or 0) > 0 or (phases.Invalid or 0) > 0 then
        return health("Degraded", message)
    end
    if phase == "Suspended" or phase == "Stopped" then
        return health("Suspended", message)
    end
    if rollup.observationsComplete ~= true or (rollup.unobservedGraphs or 0) > 0 then
        return health("Progressing", "Waiting for missing or stale descendant observations; " .. message)
    end
    if (phases.Suspended or 0) > 0 or (phases.Stopped or 0) > 0 then
        return health("Suspended", message)
    end
    if (phases.Reconciling or 0) > 0 or (phases.Draining or 0) > 0 or (phases.Unknown or 0) > 0 then
        return health("Progressing", message)
    end
end
if phase == "Suspended" or phase == "Stopped" then
    return health("Suspended", message)
end
if phase == "Draining" or phase == "Reconciling" then
    return health("Progressing", message)
end
if phase == "Completed" and status.completed == true then
    return health("Healthy", message)
end
if (phase == "Ready" or phase == "Running") and status.ready == true then
    return health("Healthy", message)
end
if obj.kind == "Feedback" and phase == "Waiting" then
    return health("Healthy", "Waiting between feedback epochs; " .. message)
end
return health("Progressing", message)
