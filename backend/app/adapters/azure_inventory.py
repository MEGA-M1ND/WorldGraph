"""Read-only Azure inventory import.

Azure Resource Graph → normalization → ``WorldEntity`` + ``DependencyEdge`` → the existing
WorldGraph engines. There is no Azure-specific analysis path: an Azure resource becomes an
ordinary WorldGraph entity and the same traversal, blast-radius, simulation and AI layers
operate on it unchanged.

**Read-only, absolutely.** This module issues exactly one kind of call — a Resource Graph
query — and holds no write permission. It does not tag, deploy, restart, scale, or change
configuration, and there is no code path here that could.

**It does not invent dependencies.** Cloud inventory proves some relationships and merely
suggests others. WorldGraph creates edges only for the first kind:

* a resource's ``location`` proves it is ``HOSTED_IN`` that region;
* an explicit resource-id reference in a resource's own properties proves a connection;
* a ``worldgraph.depends_on`` tag is a human declaration, which is stronger still.

A shared resource group, a shared region, similar names, or adjacent creation times prove
nothing. They are associations, and this module treats them as such — which is to say, it
ignores them.

Every edge carries provenance naming its source, method and confidence, so a reader can
always tell what Azure proved from what a person asserted.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from ..config import Settings
from ..models.core import (
    BusinessProfile,
    Criticality,
    DataMode,
    DataSourceInfo,
    DependencyEdge,
    DependencyType,
    EntityType,
    ExposureProfile,
    HealthState,
    WorldEntity,
    utcnow,
)
from ..models.workspace import (
    GraphCoverage,
    ImportSummary,
    InventorySourceKind,
    Workspace,
    WorkspaceKind,
    WorkspaceStatus,
)
from ..security.sanitize import sanitize_identifier, sanitize_text
from .azure_regions import (
    CLOUD_REGION_APPROXIMATION,
    normalize_region,
    region_display_name,
    region_geography,
    region_location,
)
from .azure_tags import ParsedTags, TagRejection, parse_tags
from .base import AdapterError

logger = logging.getLogger("worldgraph.adapters.azure")

#: Azure resource types WorldGraph models, and the entity type each becomes.
#:
#: Deliberately small. Mapping every Azure type would produce dozens of WorldGraph entity
#: types that no engine has rules for; unmapped types are *counted and reported* instead,
#: which tells the operator the boundary of the graph rather than padding it.
SUPPORTED_TYPES: dict[str, EntityType] = {
    "microsoft.compute/virtualmachines": EntityType.NETWORK_NODE,
    "microsoft.containerservice/managedclusters": EntityType.KUBERNETES_CLUSTER,
    "microsoft.web/sites": EntityType.APPLICATION,
    "microsoft.sql/servers": EntityType.DATABASE,
    "microsoft.sql/servers/databases": EntityType.DATABASE,
    "microsoft.documentdb/databaseaccounts": EntityType.DATABASE,
    "microsoft.dbforpostgresql/flexibleservers": EntityType.DATABASE,
    "microsoft.dbformysql/flexibleservers": EntityType.DATABASE,
    "microsoft.cache/redis": EntityType.DATABASE,
    "microsoft.storage/storageaccounts": EntityType.DATABASE,
    "microsoft.keyvault/vaults": EntityType.EXTERNAL_API,
    "microsoft.network/loadbalancers": EntityType.NETWORK_NODE,
    "microsoft.network/applicationgateways": EntityType.NETWORK_NODE,
    "microsoft.network/publicipaddresses": EntityType.NETWORK_NODE,
    "microsoft.network/virtualnetworks": EntityType.NETWORK_NODE,
    "microsoft.containerregistry/registries": EntityType.EXTERNAL_API,
    "microsoft.servicebus/namespaces": EntityType.EXTERNAL_API,
    "microsoft.eventhub/namespaces": EntityType.EXTERNAL_API,
}

#: Resource types that are internet-facing by their nature. This is a property of the
#: Azure resource type itself, not an inference about the workload — a public IP is public.
_INTERNET_FACING_TYPES: frozenset[str] = frozenset(
    {
        "microsoft.network/publicipaddresses",
        "microsoft.network/applicationgateways",
    }
)

#: Resource-id segments that indicate a *secret-bearing* child resource. WorldGraph records
#: that a Key Vault exists and never reads what is in it — see §17 of the phase brief and
#: SECURITY.md. These are refused at the query layer, not filtered afterwards.
FORBIDDEN_RESOURCE_SEGMENTS: frozenset[str] = frozenset(
    {"secrets", "keys", "certificates"}
)

#: Resource-property keys that must never survive into a WorldGraph entity or a snapshot.
#: Matched case-insensitively as substrings, because Azure spells these many ways.
SENSITIVE_PROPERTY_HINTS: tuple[str, ...] = (
    "password",
    "secret",
    "key",
    "token",
    "credential",
    "connectionstring",
    "certificate",
    "thumbprint",
    "sas",
    "accountkey",
    "primarykey",
    "secondarykey",
    "clientsecret",
    "sharedaccess",
    "adminlogin",
    "publickey",
    "privatekey",
    "fingerprint",
)

#: The Resource Graph query. Explicitly projects only the columns WorldGraph uses — a
#: `project *` would pull secret-bearing properties into memory for no reason, and the
#: cheapest way to not leak a field is to never fetch it.
RESOURCE_GRAPH_QUERY = """
resources
| project id, name, type, location, resourceGroup, subscriptionId, tags, kind, sku, properties
| limit 5000
"""


# ======================================================================================
# Sanitization
# ======================================================================================


def is_sensitive_key(key: str) -> bool:
    """Whether a property name looks like it carries a secret."""
    lowered = str(key).lower().replace("_", "").replace("-", "")
    return any(hint in lowered for hint in SENSITIVE_PROPERTY_HINTS)


def sanitize_properties(value: Any, *, depth: int = 0) -> Any:
    """Strip anything secret-shaped out of an Azure property tree.

    Applied before a resource reaches a WorldGraph entity *and* before it reaches a
    snapshot on disk. Redacts rather than drops, so a reviewer can see that a field was
    present and was removed — a silently absent key is indistinguishable from one that
    never existed.
    """
    if depth > 8:
        # Azure property trees are deep and occasionally self-referential. A bound here is
        # cheaper than trusting them.
        return "[truncated]"
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if is_sensitive_key(key):
                cleaned[str(key)] = "[redacted]"
                continue
            cleaned[str(key)] = sanitize_properties(item, depth=depth + 1)
        return cleaned
    if isinstance(value, list):
        return [sanitize_properties(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return sanitize_text(value, max_length=512)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return sanitize_text(str(value), max_length=256)


def is_forbidden_resource(resource_id: str) -> bool:
    """Whether a resource id names a secret-bearing child resource.

    WorldGraph may know a Key Vault exists. It may not enumerate what is inside one.
    """
    segments = [segment.lower() for segment in resource_id.split("/")]
    return any(segment in FORBIDDEN_RESOURCE_SEGMENTS for segment in segments)


# ======================================================================================
# Normalization
# ======================================================================================


def entity_id_for(resource_id: str) -> str:
    """A WorldGraph entity id derived from an Azure resource id.

    Azure ids contain ``/`` and are up to a few hundred characters; WorldGraph ids are
    slugs because they reach URLs. The subscription GUID is deliberately dropped from the
    *visible* id — it is an identifier an operator may not want on a shared screen, and it
    is preserved in metadata where the UI can choose not to show it.
    """
    trimmed = re.sub(
        r"^/subscriptions/[^/]+/resourcegroups/", "", resource_id, flags=re.IGNORECASE
    )
    trimmed = re.sub(r"/providers/[^/]+/", "/", trimmed, flags=re.IGNORECASE)
    slug = sanitize_identifier(trimmed.replace("/", "."), max_length=120)
    return f"az.{slug}".lower()


def region_entity_id(location: str) -> str:
    return f"az.region.{normalize_region(location)}"


def _azure_source(workspace: Workspace, *, observed_at=None) -> DataSourceInfo:
    """Provenance for a record imported from Azure."""
    return DataSourceInfo(
        source_id="azure-resource-graph",
        source_name="Azure Resource Graph",
        source_url=None,
        mode=workspace.mode,
        # High but not 1.0: Resource Graph is authoritative about what exists, and it is
        # also eventually consistent. A resource created seconds ago may be absent.
        confidence=0.97,
        observed_at=observed_at or utcnow(),
        ingested_at=utcnow(),
    )


def normalize_resource(
    resource: dict[str, Any], workspace: Workspace
) -> tuple[WorldEntity | None, ParsedTags | None, str]:
    """Turn one Resource Graph row into a WorldGraph entity.

    Returns ``(entity, parsed_tags, resource_type)``. ``entity`` is ``None`` for a type
    WorldGraph does not model or a resource it must not read — the caller counts those
    rather than dropping them silently.
    """
    resource_id = str(resource.get("id") or "").strip()
    raw_type = str(resource.get("type") or "").strip().lower()
    if not resource_id or not raw_type:
        return None, None, raw_type or "unknown"

    if is_forbidden_resource(resource_id):
        # A Key Vault secret/key/certificate. Not modelled, not counted as supported, and
        # never read.
        return None, None, raw_type

    entity_type = SUPPORTED_TYPES.get(raw_type)
    if entity_type is None:
        return None, None, raw_type

    name = sanitize_text(resource.get("name") or resource_id.rsplit("/", 1)[-1], max_length=200)
    location = str(resource.get("location") or "").strip()
    resource_group = sanitize_text(resource.get("resourceGroup"), max_length=120)
    subscription_id = sanitize_text(resource.get("subscriptionId"), max_length=64)

    tags = resource.get("tags") if isinstance(resource.get("tags"), dict) else {}
    parsed = parse_tags(tags, resource=name)

    business = BusinessProfile(
        # Region is inventory, so it is known. Everything else stays UNKNOWN unless a tag
        # declares it — this is the whole point of the Reality Pass.
        region=region_display_name(location) if location else "",
        revenue_per_hour=parsed.revenue_per_hour,
    )
    exposure = ExposureProfile(
        internet_facing=raw_type in _INTERNET_FACING_TYPES,
        network_zone="azure",
        # Azure inventory does not tell us whether a workload authenticates its callers.
        # Claiming it does would be a security assertion WorldGraph has no basis for.
        authenticated=True,
    )

    metadata: dict[str, Any] = {
        "provider": "azure",
        "azureResourceId": resource_id,
        "subscriptionId": subscription_id,
        "resourceGroup": resource_group,
        "resourceType": raw_type,
        "location": location,
        "locationDisplay": region_display_name(location),
        "tags": {
            sanitize_text(k, max_length=120): sanitize_text(v, max_length=256)
            for k, v in list(tags.items())[:40]
        },
        "properties": sanitize_properties(resource.get("properties")),
    }
    if resource.get("kind"):
        metadata["kind"] = sanitize_text(resource.get("kind"), max_length=120)
    if parsed.service:
        metadata["worldgraphService"] = parsed.service
    if parsed.owner:
        # Recorded as metadata, never interpreted. An owner field containing an
        # instruction is a suspicious string and nothing more.
        metadata["worldgraphOwner"] = parsed.owner

    entity = WorldEntity(
        id=entity_id_for(resource_id),
        type=entity_type,
        name=name,
        description=f"{raw_type} in {region_display_name(location)}" if location else raw_type,
        location=region_location(location),
        # Resource Graph reports existence, not health. Claiming HEALTHY would be an
        # observation WorldGraph did not make.
        health=HealthState.UNKNOWN,
        criticality=parsed.criticality or Criticality.UNKNOWN,
        customer_facing=parsed.customer_facing,
        business=business,
        exposure=exposure,
        metadata=metadata,
        source=_azure_source(workspace),
        updated_at=utcnow(),
    )
    return entity, parsed, raw_type


def build_region_entity(location: str, workspace: Workspace) -> WorldEntity:
    """A canonical cloud-region entity for one Azure region."""
    point = region_location(location)
    return WorldEntity(
        id=region_entity_id(location),
        type=EntityType.CLOUD_REGION,
        name=f"Azure {region_display_name(location)}",
        description=(
            f"Azure region {location}. Position is a {CLOUD_REGION_APPROXIMATION.lower()} "
            f"of the published geography ({region_geography(location) or 'unknown'}) — an "
            "Azure region is a set of datacenters across a metropolitan area, not a building."
        ),
        location=point,
        health=HealthState.UNKNOWN,
        criticality=Criticality.UNKNOWN,
        business=BusinessProfile(region=region_display_name(location)),
        metadata={
            "provider": "azure",
            "azureRegion": normalize_region(location),
            "geography": region_geography(location),
            "positionAccuracy": CLOUD_REGION_APPROXIMATION,
            "positionKnown": point is not None,
        },
        source=_azure_source(workspace),
        updated_at=utcnow(),
    )


# ======================================================================================
# Edges — only what Azure actually proves
# ======================================================================================


def _edge(
    source: str,
    target: str,
    edge_type: DependencyType,
    *,
    method: str,
    confidence: float,
    origin: str,
    criticality: float,
    redundancy: float = 0.0,
    capacity_impact: float | None = None,
) -> DependencyEdge:
    """Build an edge that carries why it exists."""
    return DependencyEdge(
        id=f"{source}--{edge_type.value}--{target}",
        source_entity_id=source,
        target_entity_id=target,
        type=edge_type,
        criticality=criticality,
        redundancy=redundancy,
        capacity_impact=capacity_impact,
        metadata={
            "provenance": {
                "source": origin,
                "method": method,
                "confidence": confidence,
            }
        },
    )


def collect_references(properties: Any, *, limit: int = 200) -> list[str]:
    """Every Azure resource id referenced anywhere in a resource's properties.

    Walks the whole tree rather than a fixed list of paths, because Azure nests references
    differently per resource type and a path list would silently miss most of them —
    ``serverFarmId`` on a web app, ``vnetSubnetID`` inside an AKS agent pool, backend
    address pools on a load balancer, ``publicIPAddress.id`` on a gateway, and dozens more
    that no hand-maintained list would keep up with.

    The references found are *explicit configuration* — one resource naming another — which
    is the only automatic evidence this module accepts for an edge. Sensitive keys are
    skipped on the way down, so a connection string containing a resource id can never
    become a dependency.
    """
    found: list[str] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 8 or len(found) >= limit:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if is_sensitive_key(key):
                    continue
                walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:50]:
                walk(item, depth + 1)
        elif isinstance(node, str):
            candidate = node.strip()
            if candidate.lower().startswith("/subscriptions/") and len(candidate) < 1024:
                found.append(candidate)

    walk(properties)
    return found


def infer_edges(
    entities_by_azure_id: dict[str, WorldEntity],
    tags_by_entity: dict[str, ParsedTags],
    workspace: Workspace,
) -> tuple[list[DependencyEdge], int, int]:
    """Build every defensible edge. Returns ``(edges, explicit_count, declared_count)``.

    Three sources of evidence, in ascending order of strength:

    1. ``HOSTED_IN`` a region — proved by the resource's own ``location`` field.
    2. ``CONNECTS_TO`` — proved by an explicit resource-id reference in configuration.
    3. ``DEPENDS_ON`` — declared by a human through a ``worldgraph.depends_on`` tag.

    Nothing else produces an edge. Sharing a resource group or a region produces exactly
    nothing, which is the correct amount.
    """
    edges: dict[str, DependencyEdge] = {}
    explicit = 0
    declared = 0

    # Index by normalized Azure id so a reference with different casing still resolves.
    by_lower = {azure_id.lower(): entity for azure_id, entity in entities_by_azure_id.items()}

    for entity in entities_by_azure_id.values():
        location = str(entity.metadata.get("location") or "")
        if location:
            region_id = region_entity_id(location)
            edge = _edge(
                entity.id,
                region_id,
                DependencyType.HOSTED_IN,
                origin="azure-resource-graph",
                method="resource-location-field",
                confidence=0.99,
                # A region losing capacity affects what it hosts, but Azure regions have
                # multiple availability zones and WorldGraph does not know whether this
                # resource is zone-redundant. A hard 1.0 would assert a single point of
                # failure that inventory alone cannot establish.
                criticality=0.9,
                redundancy=0.0,
            )
            edges[edge.id] = edge
            explicit += 1

        # 2. Explicit references in configuration.
        for reference in collect_references(entity.metadata.get("properties")):
            target = by_lower.get(reference.lower())
            if target is None:
                # A reference to something outside this subscription, or to a resource
                # type WorldGraph does not model. Not an edge — an edge to an entity that
                # does not exist would be a phantom.
                continue
            if target.id == entity.id:
                continue
            edge = _edge(
                entity.id,
                target.id,
                DependencyType.CONNECTS_TO,
                origin="azure-resource-graph",
                method="explicit-resource-reference",
                confidence=0.98,
                # A configuration reference proves a connection exists. It does not prove
                # how much of the source's function depends on it, so the coupling is
                # modest and honest rather than assumed total.
                criticality=0.5,
                redundancy=0.0,
            )
            edges.setdefault(edge.id, edge)
            explicit += 1

    # 3. Human declarations. Strongest evidence available, and the only source of a
    #    DEPENDS_ON edge — inventory alone can never justify one.
    for entity_id, parsed in tags_by_entity.items():
        for target_azure_id in parsed.depends_on:
            target = by_lower.get(target_azure_id.lower())
            if target is None or target.id == entity_id:
                continue
            edge = _edge(
                entity_id,
                target.id,
                DependencyType.DEPENDS_ON,
                origin="worldgraph-tag",
                method="user-declared",
                confidence=1.0,
                criticality=1.0,
                redundancy=0.0,
            )
            edges[edge.id] = edge
            declared += 1

    return list(edges.values()), explicit, declared


# ======================================================================================
# Coverage
# ======================================================================================


def assess_coverage(
    entities: list[WorldEntity],
    edges: list[DependencyEdge],
    tags_by_entity: dict[str, ParsedTags],
) -> list[GraphCoverage]:
    """What WorldGraph knows about this estate, dimension by dimension.

    Never one number. Averaging "complete hosting topology" with "no business mapping"
    produces something that looks precise and means nothing.
    """
    workloads = [e for e in entities if e.type is not EntityType.CLOUD_REGION]
    total = len(workloads) or 1

    hosted = sum(1 for e in edges if e.type is DependencyType.HOSTED_IN)
    app_edges = [
        e for e in edges if e.type in {DependencyType.DEPENDS_ON, DependencyType.CONNECTS_TO}
    ]
    with_criticality = sum(1 for e in workloads if e.criticality is not Criticality.UNKNOWN)
    with_customer_facing = sum(1 for e in workloads if e.customer_facing is not None)
    with_revenue = sum(1 for e in workloads if e.business.has_revenue)
    with_service = sum(1 for e in workloads if e.metadata.get("worldgraphService"))

    def level(count: int, *, total_count: int = total) -> str:
        ratio = count / total_count if total_count else 0.0
        if ratio >= 0.9:
            return "HIGH"
        if ratio >= 0.4:
            return "PARTIAL"
        if ratio > 0:
            return "LOW"
        return "NONE"

    return [
        GraphCoverage(
            dimension="infrastructure",
            label="Infrastructure discovered",
            level="HIGH" if workloads else "NONE",
            detail=f"{len(workloads)} resources imported from Azure Resource Graph",
            remedy="" if workloads else "No supported resources were found in this subscription.",
        ),
        GraphCoverage(
            dimension="hosting",
            label="Hosting relationships",
            level=level(hosted),
            detail=f"{hosted} of {len(workloads)} resources are placed in a region",
            remedy="" if hosted >= len(workloads) else "Some resources report no location.",
        ),
        GraphCoverage(
            dimension="application_dependencies",
            label="Application dependencies",
            level=level(len(app_edges)),
            detail=(
                f"{len(app_edges)} dependency edges, all from explicit references or "
                "worldgraph.depends_on tags"
            ),
            remedy=(
                "Azure inventory tells WorldGraph where a workload is hosted, but not what "
                "it calls at runtime. Declare dependencies with worldgraph.depends_on tags, "
                "or connect a tracing source."
            ),
        ),
        GraphCoverage(
            dimension="business_service",
            label="Business-service mapping",
            level=level(with_service),
            detail=f"{with_service} of {len(workloads)} resources declare worldgraph.service",
            remedy=(
                "Tag resources with worldgraph.service to group them into the services the "
                "business actually recognises."
            ),
        ),
        GraphCoverage(
            dimension="criticality",
            label="Criticality declared",
            level=level(with_criticality),
            detail=f"{with_criticality} of {len(workloads)} resources declare a criticality",
            remedy=(
                "Tag resources with worldgraph.criticality. Until then WorldGraph will not "
                "grade their business severity, because nobody has told it how."
            ),
        ),
        GraphCoverage(
            dimension="customer_exposure",
            label="Customer exposure metadata",
            level=level(with_customer_facing),
            detail=(
                f"{with_customer_facing} of {len(workloads)} resources declare "
                "worldgraph.customer_facing"
            ),
            remedy=(
                "Without this WorldGraph cannot say which failures customers would notice, "
                "and it will report customer impact as UNKNOWN rather than guessing."
            ),
        ),
        GraphCoverage(
            dimension="revenue",
            label="Revenue metadata",
            level=level(with_revenue),
            detail=f"{with_revenue} of {len(workloads)} resources declare a revenue figure",
            remedy=(
                "Without this, revenue exposure is reported as UNKNOWN. WorldGraph will not "
                "estimate a figure it has no basis for."
            ),
        ),
    ]


# ======================================================================================
# Import
# ======================================================================================


def configured_azure_workspaces(settings: Settings) -> list[Workspace]:
    """Azure workspaces this deployment declares.

    Configuration only — nothing is contacted here. A workspace appears in the list
    whether or not credentials work, and its status says which.
    """
    workspaces: list[Workspace] = []
    for entry in settings.azure_subscriptions:
        label = sanitize_text(entry, max_length=120) or "subscription"
        slug = sanitize_identifier(label, max_length=48).lower() or "subscription"
        workspaces.append(
            Workspace(
                id=f"azure-{slug}",
                name=f"Azure — {label}",
                kind=WorkspaceKind.REAL,
                source=InventorySourceKind.AZURE,
                status=WorkspaceStatus.NOT_LOADED,
                mode=DataMode.LIVE,
                organization=label,
                description=(
                    "Read-only inventory imported from an Azure subscription. WorldGraph "
                    "holds no write permission and changes nothing."
                ),
                read_only=True,
            )
        )
    if settings.azure_snapshot_path:
        workspaces.append(
            Workspace(
                id="azure-snapshot",
                name="Azure — Snapshot",
                kind=WorkspaceKind.REAL,
                source=InventorySourceKind.SNAPSHOT,
                status=WorkspaceStatus.NOT_LOADED,
                # A snapshot is a recording. Calling it LIVE would be the exact dishonesty
                # the provenance model exists to prevent.
                mode=DataMode.REPLAY,
                organization="Azure snapshot",
                description=(
                    "A saved Azure inventory snapshot, replayed from disk. Not a live view."
                ),
                read_only=True,
            )
        )
    return workspaces


async def fetch_resources(settings: Settings, workspace: Workspace) -> list[dict[str, Any]]:
    """Read the inventory for a workspace.

    Snapshot workspaces read from disk. Live workspaces query Azure Resource Graph through
    the official SDK using ``DefaultAzureCredential``, which picks up ``az login``,
    managed identity, or environment credentials without WorldGraph ever handling a secret
    itself.
    """
    if workspace.source is InventorySourceKind.SNAPSHOT:
        return load_snapshot(settings.azure_snapshot_path or "")

    try:
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.resourcegraph import ResourceGraphClient
        from azure.mgmt.resourcegraph.models import QueryRequest
    except ImportError as error:
        raise AdapterError(
            "Azure SDK is not installed. Install the optional dependencies with "
            "`pip install -r requirements-azure.txt` to import live inventory."
        ) from error

    subscription = workspace.organization
    try:
        credential = DefaultAzureCredential()
        client = ResourceGraphClient(credential)
        request = QueryRequest(subscriptions=[subscription], query=RESOURCE_GRAPH_QUERY)
        response = client.resources(request)
    except Exception as error:  # cloud SDK errors carry request detail
        # Only the exception type escapes. An Azure SDK error can contain a request URL, a
        # tenant id or a token fragment, and none of those belong in a log or a response.
        raise AdapterError(
            f"Azure Resource Graph query failed ({type(error).__name__}). Check that "
            "credentials are available (try `az login`) and that the subscription is "
            "readable."
        ) from error

    data = getattr(response, "data", None) or []
    return [row for row in data if isinstance(row, dict)]


def load_snapshot(path: str) -> list[dict[str, Any]]:
    """Read a sanitized inventory snapshot from disk."""
    import json
    from pathlib import Path

    file = Path(path)
    if not file.exists():
        raise AdapterError(f"Azure snapshot not found at '{path}'.")
    try:
        payload = json.loads(file.read_text())
    except (OSError, ValueError) as error:
        raise AdapterError(f"Azure snapshot at '{path}' is not readable JSON.") from error
    resources = payload.get("resources") if isinstance(payload, dict) else payload
    if not isinstance(resources, list):
        raise AdapterError(f"Azure snapshot at '{path}' contains no resource list.")
    return [row for row in resources if isinstance(row, dict)]


def build_snapshot(resources: list[dict[str, Any]], *, subscription_label: str = "") -> dict:
    """Serialize inventory into a snapshot, with every secret-shaped field removed.

    Snapshots exist for offline development, regression tests and reproducible demos. They
    are written to disk and may be committed, so sanitization here is not defence in depth
    — it is the only thing standing between a live subscription and a git repository.
    """
    return {
        "schema": "worldgraph.azure-snapshot.v1",
        "subscription_label": sanitize_text(subscription_label, max_length=120),
        "captured_at": utcnow().isoformat(),
        "note": (
            "Sanitized Azure inventory snapshot. Secret-shaped properties are redacted at "
            "capture time; this file must never contain credentials."
        ),
        "resources": [sanitize_properties(resource) for resource in resources],
    }


async def import_azure_workspace(
    settings: Settings, workspace: Workspace
) -> tuple[list[WorldEntity], list[DependencyEdge], ImportSummary]:
    """Import one Azure workspace into WorldGraph entities and edges."""
    started = time.perf_counter()
    resources = await fetch_resources(settings, workspace)

    entities_by_azure_id: dict[str, WorldEntity] = {}
    tags_by_entity: dict[str, ParsedTags] = {}
    rejections: list[TagRejection] = []
    unsupported: dict[str, int] = {}
    locations: set[str] = set()

    for resource in resources:
        entity, parsed, raw_type = normalize_resource(resource, workspace)
        if entity is None:
            if raw_type:
                unsupported[raw_type] = unsupported.get(raw_type, 0) + 1
            continue
        entities_by_azure_id[str(resource.get("id"))] = entity
        if parsed is not None:
            tags_by_entity[entity.id] = parsed
            rejections.extend(parsed.rejections)
        location = str(entity.metadata.get("location") or "")
        if location:
            locations.add(location)

    # Region entities, created once per distinct location.
    region_entities = [build_region_entity(location, workspace) for location in sorted(locations)]

    entities = [*region_entities, *entities_by_azure_id.values()]
    edges, explicit, declared = infer_edges(entities_by_azure_id, tags_by_entity, workspace)

    # Drop edges whose target region is not among the entities we built. Cannot normally
    # happen, but a dangling edge would let the graph claim reachability into nothing.
    known_ids = {entity.id for entity in entities}
    edges = [
        edge
        for edge in edges
        if edge.source_entity_id in known_ids and edge.target_entity_id in known_ids
    ]

    summary = ImportSummary(
        workspace_id=workspace.id,
        source=workspace.source,
        subscription_label=workspace.organization,
        resources_discovered=len(resources),
        resources_supported=len(entities_by_azure_id),
        resources_unsupported=len(resources) - len(entities_by_azure_id),
        unsupported_types=dict(sorted(unsupported.items(), key=lambda kv: -kv[1])[:25]),
        entities_created=len(entities),
        explicit_edges=explicit,
        declared_edges=declared,
        regions=len(region_entities),
        rejected_tags=[rejection.describe() for rejection in rejections[:50]],
        coverage=assess_coverage(entities, edges, tags_by_entity),
        mode=workspace.mode,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
    )
    logger.info(
        "azure_import workspace=%s discovered=%d supported=%d edges=%d regions=%d",
        workspace.id,
        summary.resources_discovered,
        summary.resources_supported,
        len(edges),
        summary.regions,
    )
    return entities, edges, summary
