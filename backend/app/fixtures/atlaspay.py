"""AtlasPay — the deterministic synthetic enterprise estate.

AtlasPay is a fictional multinational payments company. Every entity, edge, number and
coordinate in this file is **synthetic** and carries ``DataMode.SYNTHETIC`` so the UI can
badge it. Nothing here is measured, and the product never claims otherwise.

The estate is deliberately small (≈60 entities) and hand-tuned so the hero demo is
reproducible to the digit: the same click sequence always yields the same blast radius,
the same risk score, and the same response plan.

Geography is real (coordinates of the actual cities/regions) because the globe has to look
right; the *facilities* at those coordinates are invented.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..models.core import (
    BusinessProfile,
    Criticality,
    DataMode,
    DataSourceInfo,
    DependencyEdge,
    DependencyType,
    EntityType,
    ExposureProfile,
    GeoPoint,
    HealthState,
    SoftwareComponent,
    WorldEntity,
)

#: Fixed clock for the fixture. A synthetic estate with a moving ``updated_at`` would make
#: snapshot tests flap and make "what changed in the last hour" meaningless.
FIXTURE_EPOCH = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)

ORG_ID = "atlaspay"

SYNTHETIC_SOURCE = DataSourceInfo(
    source_id="atlaspay-fixture",
    source_name="AtlasPay synthetic estate",
    source_url=None,
    mode=DataMode.SYNTHETIC,
    confidence=1.0,
    observed_at=FIXTURE_EPOCH,
    ingested_at=FIXTURE_EPOCH,
)

# Real coordinates; invented facilities.
SITES: dict[str, tuple[float, float]] = {
    "mumbai": (19.0760, 72.8777),
    "singapore": (1.3521, 103.8198),
    "frankfurt": (50.1109, 8.6821),
    "virginia": (38.9445, -77.4558),  # Ashburn, VA
    "tokyo": (35.6762, 139.6503),
    "taiwan": (24.7736, 120.9417),  # Hsinchu Science Park
    "bengaluru": (12.9716, 77.5946),
}


def _entity(
    entity_id: str,
    entity_type: EntityType,
    name: str,
    *,
    description: str = "",
    site: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    criticality: Criticality = Criticality.MEDIUM,
    health: HealthState = HealthState.HEALTHY,
    business: BusinessProfile | None = None,
    exposure: ExposureProfile | None = None,
    software: list[SoftwareComponent] | None = None,
    metadata: dict[str, object] | None = None,
) -> WorldEntity:
    """Build one synthetic entity with the shared source stamp."""
    location: GeoPoint | None = None
    if site is not None:
        lat_, lon_ = SITES[site]
        location = GeoPoint(lat=lat_, lon=lon_)
    elif lat is not None and lon is not None:
        location = GeoPoint(lat=lat, lon=lon)
    return WorldEntity(
        id=entity_id,
        type=entity_type,
        name=name,
        description=description,
        location=location,
        health=health,
        criticality=criticality,
        business=business or BusinessProfile(),
        exposure=exposure or ExposureProfile(),
        software=software or [],
        metadata=metadata or {},
        source=SYNTHETIC_SOURCE,
        observed_at=FIXTURE_EPOCH,
        updated_at=FIXTURE_EPOCH,
    )


def _edge(
    source: str,
    target: str,
    edge_type: DependencyType,
    *,
    criticality: float = 1.0,
    redundancy: float = 0.0,
    capacity_impact: float | None = None,
    note: str = "",
) -> DependencyEdge:
    """Build one dependency edge with a deterministic id."""
    return DependencyEdge(
        id=f"{source}--{edge_type.value}--{target}",
        source_entity_id=source,
        target_entity_id=target,
        type=edge_type,
        criticality=criticality,
        redundancy=redundancy,
        capacity_impact=capacity_impact,
        metadata={"note": note} if note else {},
    )


# --------------------------------------------------------------------------------------
# Software inventory (drives the security scenario)
# --------------------------------------------------------------------------------------

#: The synthetic vulnerability at the centre of hero scenario #2. Clearly fictional — a
#: 2026 "DEMO" identifier that cannot collide with a real CVE.
DEMO_CVE_ID = "CVE-2026-DEMO-001"

#: The vulnerable package. Also fictional.
VULNERABLE_PACKAGE = SoftwareComponent(
    name="atlas-gateway",
    version="3.4.1",
    vendor="AtlasPay Platform",
    cve_ids=[DEMO_CVE_ID],
)

_PATCHED_PACKAGE = SoftwareComponent(
    name="atlas-gateway", version="3.5.0", vendor="AtlasPay Platform", cve_ids=[]
)

_POSTGRES = SoftwareComponent(name="postgresql", version="16.3", vendor="PostgreSQL")
_REDIS = SoftwareComponent(name="redis", version="7.2.5", vendor="Redis")


def build_entities() -> list[WorldEntity]:
    """Every AtlasPay entity. Order is stable so ids and snapshots stay reproducible."""
    entities: list[WorldEntity] = []
    add = entities.append

    # -- organisation ------------------------------------------------------------------
    add(
        _entity(
            ORG_ID,
            EntityType.ORGANIZATION,
            "AtlasPay",
            description="Fictional multinational payments company used for the WorldGraph demo.",
            site="singapore",
            criticality=Criticality.CRITICAL,
            business=BusinessProfile(
                traffic_share=1.0,
                revenue_per_hour=4_200_000.0,
                customer_count=41_500,
                region="GLOBAL",
                sla_tier="TIER-0",
            ),
            metadata={"industry": "payments", "founded": 2014, "synthetic": True},
        )
    )

    # -- cloud regions & datacenters (physical) ----------------------------------------
    regions = [
        ("cloud-region-mumbai", "AWS ap-south-1 (Mumbai)", "mumbai", Criticality.CRITICAL, 0.22),
        ("cloud-region-singapore", "AWS ap-southeast-1 (Singapore)", "singapore", Criticality.CRITICAL, 0.31),
        ("cloud-region-frankfurt", "AWS eu-central-1 (Frankfurt)", "frankfurt", Criticality.HIGH, 0.24),
        ("cloud-region-virginia", "AWS us-east-1 (N. Virginia)", "virginia", Criticality.HIGH, 0.18),
        ("cloud-region-tokyo", "AWS ap-northeast-1 (Tokyo)", "tokyo", Criticality.MEDIUM, 0.05),
    ]
    for rid, name, site, crit, traffic in regions:
        add(
            _entity(
                rid,
                EntityType.CLOUD_REGION,
                name,
                description="Synthetic cloud region footprint.",
                site=site,
                criticality=crit,
                business=BusinessProfile(
                    traffic_share=traffic,
                    region=site.upper(),
                    capacity=1.0,
                    redundancy=3,  # availability zones
                    sla_tier="TIER-1",
                ),
                metadata={"provider": "aws", "availability_zones": 3},
            )
        )

    add(
        _entity(
            "dc-singapore-partner",
            EntityType.DATACENTER,
            "Singapore Datacenter Partner (Jurong)",
            description="Colocation partner hosting AtlasPay's card-network interconnect.",
            lat=1.3329,
            lon=103.7436,
            criticality=Criticality.HIGH,
            business=BusinessProfile(region="APAC", redundancy=1, capacity=1.0, sla_tier="TIER-2"),
            metadata={"operator": "synthetic partner", "racks": 42},
        )
    )
    add(
        _entity(
            "dc-taiwan-hsinchu",
            EntityType.DATACENTER,
            "Hsinchu Edge Facility",
            description="AtlasPay edge/staging facility co-located with the hardware supplier.",
            site="taiwan",
            criticality=Criticality.MEDIUM,
            business=BusinessProfile(region="APAC", redundancy=1, capacity=1.0),
            metadata={"role": "hardware staging"},
        )
    )

    # -- suppliers & factories ---------------------------------------------------------
    add(
        _entity(
            "supplier-taiwan-hardware",
            EntityType.SUPPLIER,
            "Taiwan Hardware Supplier",
            description=(
                "Sole-source supplier of HSM and payment-terminal silicon for APAC. "
                "Single site, no qualified second source."
            ),
            site="taiwan",
            criticality=Criticality.CRITICAL,
            business=BusinessProfile(
                region="APAC",
                redundancy=1,  # single source — the structural risk
                capacity=1.0,
                sla_tier="TIER-2",
            ),
            metadata={
                "supplies": ["HSM modules", "payment terminal SoC"],
                "lead_time_weeks": 14,
                "second_source_qualified": False,
            },
        )
    )
    add(
        _entity(
            "factory-taiwan-assembly",
            EntityType.FACTORY,
            "Hsinchu Assembly Line 3",
            description="Supplier-operated assembly line producing AtlasPay terminal units.",
            lat=24.8020,
            lon=120.9720,
            criticality=Criticality.HIGH,
            business=BusinessProfile(region="APAC", redundancy=1, capacity=1.0),
            metadata={"units_per_week": 2400},
        )
    )
    add(
        _entity(
            "supplier-singapore-datacenter",
            EntityType.SUPPLIER,
            "Singapore Datacenter Partner",
            description="Colocation and interconnect provider for APAC card-network settlement.",
            lat=1.3329,
            lon=103.7436,
            criticality=Criticality.HIGH,
            business=BusinessProfile(region="APAC", redundancy=1, capacity=1.0),
            metadata={"contract": "synthetic", "services": ["colocation", "cross-connect"]},
        )
    )

    # -- offices -----------------------------------------------------------------------
    offices = [
        ("office-bengaluru", "AtlasPay Bengaluru", "bengaluru", 1400, Criticality.MEDIUM),
        ("office-singapore", "AtlasPay Singapore HQ (APAC)", "singapore", 620, Criticality.HIGH),
        ("office-frankfurt", "AtlasPay Frankfurt (EMEA)", "frankfurt", 380, Criticality.MEDIUM),
    ]
    for oid, name, site, headcount, crit in offices:
        add(
            _entity(
                oid,
                EntityType.OFFICE,
                name,
                description="AtlasPay office.",
                site=site,
                criticality=crit,
                business=BusinessProfile(region=site.upper(), redundancy=1),
                metadata={"headcount": headcount},
            )
        )

    # -- kubernetes clusters & data stores ---------------------------------------------
    add(
        _entity(
            "payments-k8s-mumbai",
            EntityType.KUBERNETES_CLUSTER,
            "payments-k8s-mumbai",
            description="Payments workload cluster, Mumbai region.",
            site="mumbai",
            criticality=Criticality.CRITICAL,
            business=BusinessProfile(
                traffic_share=0.18, region="APAC", capacity=1.0, redundancy=2, sla_tier="TIER-1"
            ),
            software=[_PATCHED_PACKAGE],
            metadata={"nodes": 24, "runtime": "kubernetes 1.31"},
        )
    )
    add(
        _entity(
            "payments-k8s-singapore",
            EntityType.KUBERNETES_CLUSTER,
            "payments-k8s-singapore",
            description="Primary APAC payments workload cluster.",
            site="singapore",
            criticality=Criticality.CRITICAL,
            business=BusinessProfile(
                traffic_share=0.26, region="APAC", capacity=1.0, redundancy=2, sla_tier="TIER-1"
            ),
            software=[VULNERABLE_PACKAGE],
            metadata={"nodes": 38, "runtime": "kubernetes 1.31"},
        )
    )
    add(
        _entity(
            "fraud-k8s-singapore",
            EntityType.KUBERNETES_CLUSTER,
            "fraud-k8s-singapore",
            description="Fraud scoring cluster, Singapore.",
            site="singapore",
            criticality=Criticality.HIGH,
            business=BusinessProfile(traffic_share=0.11, region="APAC", capacity=1.0, redundancy=1),
            metadata={"nodes": 12},
        )
    )
    add(
        _entity(
            "portal-k8s-frankfurt",
            EntityType.KUBERNETES_CLUSTER,
            "portal-k8s-frankfurt",
            description="Customer portal cluster, Frankfurt.",
            site="frankfurt",
            criticality=Criticality.HIGH,
            business=BusinessProfile(traffic_share=0.14, region="EMEA", capacity=1.0, redundancy=2),
            software=[_PATCHED_PACKAGE],
            metadata={"nodes": 16},
        )
    )
    add(
        _entity(
            "postgres-singapore",
            EntityType.DATABASE,
            "postgres-singapore (primary)",
            description="Primary transactional store for APAC payments.",
            site="singapore",
            criticality=Criticality.CRITICAL,
            business=BusinessProfile(
                traffic_share=0.30, region="APAC", capacity=1.0, redundancy=1, sla_tier="TIER-0"
            ),
            software=[_POSTGRES],
            metadata={"engine": "postgresql 16", "role": "primary"},
        )
    )
    add(
        _entity(
            "postgres-frankfurt-replica",
            EntityType.DATABASE,
            "postgres-frankfurt (replica)",
            description="Cross-region read replica; promotable to primary.",
            site="frankfurt",
            criticality=Criticality.HIGH,
            business=BusinessProfile(region="EMEA", capacity=1.0, redundancy=1),
            software=[_POSTGRES],
            metadata={
                "engine": "postgresql 16",
                "role": "replica",
                "promotable": True,
                # Headroom relative to the PRIMARY, recorded as a fact rather than as a
                # capacity ceiling: the replica is fully healthy in its own role, and
                # would carry ~80% of primary throughput if promoted.
                "promoted_throughput_ratio": 0.8,
            },
        )
    )
    add(
        _entity(
            "redis-mumbai",
            EntityType.DATABASE,
            "redis-mumbai",
            description="Session and idempotency cache for payments.",
            site="mumbai",
            criticality=Criticality.HIGH,
            business=BusinessProfile(region="APAC", capacity=1.0, redundancy=2),
            software=[_REDIS],
            metadata={"engine": "redis 7.2"},
        )
    )
    add(
        _entity(
            "warehouse-virginia",
            EntityType.DATABASE,
            "warehouse-virginia",
            description="Analytics warehouse. Non-customer-facing.",
            site="virginia",
            criticality=Criticality.LOW,
            business=BusinessProfile(region="AMER", capacity=1.0, redundancy=1),
            metadata={"engine": "columnar warehouse", "customer_facing": False},
        )
    )

    # -- microservices & applications --------------------------------------------------
    add(
        _entity(
            "payments-api",
            EntityType.MICROSERVICE,
            "payments-api",
            description="Authorises and captures card payments. The revenue path.",
            site="singapore",
            criticality=Criticality.CRITICAL,
            business=BusinessProfile(
                traffic_share=0.38,
                revenue_per_hour=1_850_000.0,
                region="APAC",
                capacity=1.0,
                redundancy=2,
                sla_tier="TIER-0",
            ),
            exposure=ExposureProfile(internet_facing=False, network_zone="restricted", authenticated=True),
            software=[VULNERABLE_PACKAGE],
            metadata={"language": "go", "owner": "payments-core"},
        )
    )
    add(
        _entity(
            "fraud-service",
            EntityType.MICROSERVICE,
            "fraud-service",
            description="Real-time fraud scoring. Payments fail closed without it.",
            site="singapore",
            criticality=Criticality.HIGH,
            business=BusinessProfile(traffic_share=0.20, region="APAC", capacity=1.0, redundancy=1),
            exposure=ExposureProfile(internet_facing=False, network_zone="restricted"),
            metadata={"language": "python", "owner": "risk"},
        )
    )
    add(
        _entity(
            "checkout-worker",
            EntityType.MICROSERVICE,
            "checkout-worker",
            description="Asynchronous settlement and retry worker.",
            site="mumbai",
            criticality=Criticality.HIGH,
            business=BusinessProfile(traffic_share=0.12, region="APAC", capacity=1.0, redundancy=2),
            exposure=ExposureProfile(internet_facing=False, network_zone="internal"),
            software=[VULNERABLE_PACKAGE],
            metadata={"language": "go", "owner": "payments-core"},
        )
    )
    add(
        _entity(
            "admin-api",
            EntityType.MICROSERVICE,
            "admin-api",
            description=(
                "Internet-facing back-office API for merchant operations. "
                "The only vulnerable service reachable from the internet."
            ),
            site="singapore",
            criticality=Criticality.HIGH,
            business=BusinessProfile(traffic_share=0.02, region="APAC", capacity=1.0, redundancy=1),
            exposure=ExposureProfile(internet_facing=True, network_zone="dmz", authenticated=True),
            software=[VULNERABLE_PACKAGE],
            metadata={"language": "node", "owner": "merchant-ops"},
        )
    )
    add(
        _entity(
            "internal-auth",
            EntityType.MICROSERVICE,
            "internal-auth",
            description="Issues service-to-service credentials. Trusted by payments-api.",
            site="singapore",
            criticality=Criticality.CRITICAL,
            business=BusinessProfile(traffic_share=0.05, region="APAC", capacity=1.0, redundancy=2),
            exposure=ExposureProfile(internet_facing=False, network_zone="restricted"),
            metadata={"owner": "platform-security"},
        )
    )
    add(
        _entity(
            "analytics-etl",
            EntityType.MICROSERVICE,
            "analytics-etl",
            description="Batch ETL into the warehouse. Safe to pause.",
            site="virginia",
            criticality=Criticality.LOW,
            business=BusinessProfile(traffic_share=0.01, region="AMER", capacity=1.0, redundancy=1),
            metadata={"owner": "data", "pausable": True},
        )
    )

    # -- applications / business services ----------------------------------------------
    add(
        _entity(
            "checkout-platform",
            EntityType.APPLICATION,
            "checkout-platform",
            description="Merchant-facing checkout application.",
            site="singapore",
            criticality=Criticality.CRITICAL,
            business=BusinessProfile(
                traffic_share=0.44,
                revenue_per_hour=2_400_000.0,
                region="GLOBAL",
                capacity=1.0,
                redundancy=2,
                sla_tier="TIER-0",
            ),
            exposure=ExposureProfile(internet_facing=True, network_zone="dmz"),
            metadata={"owner": "checkout"},
        )
    )
    add(
        _entity(
            "customer-portal",
            EntityType.APPLICATION,
            "customer-portal",
            description="Merchant self-service portal.",
            site="frankfurt",
            criticality=Criticality.HIGH,
            business=BusinessProfile(
                traffic_share=0.16, revenue_per_hour=180_000.0, region="EMEA", capacity=1.0, redundancy=2
            ),
            exposure=ExposureProfile(internet_facing=True, network_zone="dmz"),
            metadata={"owner": "merchant-ops"},
        )
    )
    add(
        _entity(
            "analytics-platform",
            EntityType.APPLICATION,
            "analytics-platform",
            description="Internal reporting and reconciliation.",
            site="virginia",
            criticality=Criticality.LOW,
            business=BusinessProfile(traffic_share=0.03, region="AMER", capacity=1.0, redundancy=1),
            metadata={"owner": "data", "customer_facing": False},
        )
    )
    add(
        _entity(
            "customer-facing-payments",
            EntityType.BUSINESS_SERVICE,
            "Customer-facing payments",
            description="The business service AtlasPay sells: accepting a payment.",
            site="singapore",
            criticality=Criticality.CRITICAL,
            business=BusinessProfile(
                traffic_share=0.62,
                revenue_per_hour=3_100_000.0,
                region="GLOBAL",
                capacity=1.0,
                redundancy=2,
                sla_tier="TIER-0",
            ),
            metadata={"customer_facing": True},
        )
    )
    add(
        _entity(
            "merchant-onboarding",
            EntityType.BUSINESS_SERVICE,
            "Merchant onboarding",
            description="Signing up and verifying new merchants.",
            site="frankfurt",
            criticality=Criticality.MEDIUM,
            business=BusinessProfile(traffic_share=0.08, revenue_per_hour=120_000.0, region="GLOBAL", redundancy=1),
            metadata={"customer_facing": True},
        )
    )
    add(
        _entity(
            "settlement-reporting",
            EntityType.BUSINESS_SERVICE,
            "Settlement reporting",
            description="Daily settlement files and merchant reporting.",
            site="virginia",
            criticality=Criticality.MEDIUM,
            business=BusinessProfile(traffic_share=0.04, revenue_per_hour=45_000.0, region="GLOBAL", redundancy=1),
            metadata={"customer_facing": True},
        )
    )

    # -- external APIs & network -------------------------------------------------------
    add(
        _entity(
            "card-network-apac",
            EntityType.EXTERNAL_API,
            "Card network (APAC gateway)",
            description="Third-party card scheme interconnect for APAC settlement.",
            site="singapore",
            criticality=Criticality.CRITICAL,
            business=BusinessProfile(region="APAC", redundancy=1),
            metadata={"third_party": True},
        )
    )
    add(
        _entity(
            "edge-network-apac",
            EntityType.NETWORK_NODE,
            "APAC edge / CDN PoP",
            description="Edge termination for APAC customer traffic.",
            site="singapore",
            criticality=Criticality.HIGH,
            business=BusinessProfile(region="APAC", redundancy=2),
            exposure=ExposureProfile(internet_facing=True, network_zone="edge", authenticated=False),
            metadata={"provider": "synthetic CDN"},
        )
    )
    add(
        _entity(
            "edge-network-emea",
            EntityType.NETWORK_NODE,
            "EMEA edge / CDN PoP",
            description="Edge termination for EMEA customer traffic.",
            site="frankfurt",
            criticality=Criticality.MEDIUM,
            business=BusinessProfile(region="EMEA", redundancy=2),
            exposure=ExposureProfile(internet_facing=True, network_zone="edge", authenticated=False),
            metadata={"provider": "synthetic CDN"},
        )
    )
    add(
        _entity(
            "internet",
            EntityType.NETWORK_NODE,
            "Public internet",
            description="Untrusted origin used as the start of attack-path traces.",
            lat=0.0,
            lon=0.0,
            criticality=Criticality.LOW,
            business=BusinessProfile(),
            exposure=ExposureProfile(internet_facing=True, network_zone="internet", authenticated=False),
            metadata={"synthetic_anchor": True},
        )
    )

    # -- customer regions --------------------------------------------------------------
    customer_regions = [
        ("customers-apac", "APAC customers", "singapore", 0.38, 18_400, 1_610_000.0, Criticality.CRITICAL),
        ("customers-india", "India customers", "mumbai", 0.19, 9_200, 720_000.0, Criticality.HIGH),
        ("customers-emea", "EMEA customers", "frankfurt", 0.24, 8_900, 980_000.0, Criticality.HIGH),
        ("customers-amer", "AMER customers", "virginia", 0.19, 5_000, 640_000.0, Criticality.MEDIUM),
    ]
    for cid, name, site, traffic, count, revenue, crit in customer_regions:
        add(
            _entity(
                cid,
                EntityType.CUSTOMER_REGION,
                name,
                description="Modelled customer population.",
                site=site,
                criticality=crit,
                business=BusinessProfile(
                    traffic_share=traffic,
                    revenue_per_hour=revenue,
                    customer_count=count,
                    region=site.upper(),
                    sla_tier="TIER-1",
                ),
                metadata={"customer_facing": True},
            )
        )

    return entities


def build_edges() -> list[DependencyEdge]:
    """Every AtlasPay dependency edge.

    Read each row as "<source> needs <target>". Redundancy is the share of the source's
    need that survives losing this one target — a cluster with a warm peer in another
    region carries redundancy 0.6, a sole-source supplier carries 0.0.
    """
    e = _edge
    D = DependencyType
    return [
        # organisation ownership (structural, non-propagating in practice because it is
        # modelled as the org DEPENDING ON its business services)
        e(ORG_ID, "customer-facing-payments", D.DEPENDS_ON, criticality=0.7, redundancy=0.0),
        e(ORG_ID, "merchant-onboarding", D.DEPENDS_ON, criticality=0.2, redundancy=0.5),
        e(ORG_ID, "settlement-reporting", D.DEPENDS_ON, criticality=0.1, redundancy=0.5),

        # business services → applications
        e("customer-facing-payments", "checkout-platform", D.DEPENDS_ON, criticality=1.0, redundancy=0.0),
        e("merchant-onboarding", "customer-portal", D.DEPENDS_ON, criticality=0.9, redundancy=0.1),
        e("settlement-reporting", "analytics-platform", D.DEPENDS_ON, criticality=0.8, redundancy=0.2),

        # applications → services
        e("checkout-platform", "payments-api", D.DEPENDS_ON, criticality=1.0, redundancy=0.0),
        e("checkout-platform", "checkout-worker", D.DEPENDS_ON, criticality=0.5, redundancy=0.3),
        e("checkout-platform", "edge-network-apac", D.CONNECTS_TO, criticality=0.6, redundancy=0.5),
        e("customer-portal", "portal-k8s-frankfurt", D.HOSTED_IN, criticality=1.0, redundancy=0.2),
        e("customer-portal", "internal-auth", D.DEPENDS_ON, criticality=0.7, redundancy=0.2),
        e("customer-portal", "edge-network-emea", D.CONNECTS_TO, criticality=0.5, redundancy=0.5),
        e("analytics-platform", "analytics-etl", D.DEPENDS_ON, criticality=0.8, redundancy=0.1),

        # payments-api and friends
        e("payments-api", "payments-k8s-singapore", D.HOSTED_IN, criticality=1.0, redundancy=0.55,
          note="Warm peer in Mumbai carries ~55% of load on failover."),
        e("payments-api", "payments-k8s-mumbai", D.HOSTED_IN, criticality=0.6, redundancy=0.6),
        e("payments-api", "postgres-singapore", D.DEPENDS_ON, criticality=1.0, redundancy=0.15,
          note="Frankfurt replica is promotable but not automatic."),
        e("payments-api", "redis-mumbai", D.DEPENDS_ON, criticality=0.45, redundancy=0.4),
        e("payments-api", "fraud-service", D.DEPENDS_ON, criticality=0.7, redundancy=0.1,
          note="Payments fail closed when fraud scoring is unavailable."),
        e("payments-api", "internal-auth", D.DEPENDS_ON, criticality=0.8, redundancy=0.3),
        e("payments-api", "card-network-apac", D.DEPENDS_ON, criticality=0.9, redundancy=0.2),

        e("fraud-service", "fraud-k8s-singapore", D.HOSTED_IN, criticality=1.0, redundancy=0.0),
        e("checkout-worker", "payments-k8s-mumbai", D.HOSTED_IN, criticality=1.0, redundancy=0.4),
        e("checkout-worker", "redis-mumbai", D.DEPENDS_ON, criticality=0.8, redundancy=0.2),
        e("admin-api", "payments-k8s-singapore", D.HOSTED_IN, criticality=1.0, redundancy=0.2),
        e("admin-api", "internal-auth", D.DEPENDS_ON, criticality=0.9, redundancy=0.1),
        e("internal-auth", "payments-k8s-singapore", D.HOSTED_IN, criticality=1.0, redundancy=0.5),
        e("analytics-etl", "warehouse-virginia", D.DEPENDS_ON, criticality=1.0, redundancy=0.0),

        # data stores → regions
        e("postgres-singapore", "cloud-region-singapore", D.HOSTED_IN, criticality=1.0, redundancy=0.2),
        e("postgres-singapore", "postgres-frankfurt-replica", D.REPLICATES_TO, criticality=0.3, redundancy=0.9),
        e("postgres-frankfurt-replica", "cloud-region-frankfurt", D.HOSTED_IN, criticality=1.0, redundancy=0.2),
        e("redis-mumbai", "cloud-region-mumbai", D.HOSTED_IN, criticality=1.0, redundancy=0.3),
        e("warehouse-virginia", "cloud-region-virginia", D.HOSTED_IN, criticality=1.0, redundancy=0.1),

        # clusters → regions
        e("payments-k8s-singapore", "cloud-region-singapore", D.HOSTED_IN, criticality=1.0, redundancy=0.25),
        e("payments-k8s-mumbai", "cloud-region-mumbai", D.HOSTED_IN, criticality=1.0, redundancy=0.25),
        e("fraud-k8s-singapore", "cloud-region-singapore", D.HOSTED_IN, criticality=1.0, redundancy=0.2),
        e("portal-k8s-frankfurt", "cloud-region-frankfurt", D.HOSTED_IN, criticality=1.0, redundancy=0.25),

        # network & interconnect
        e("edge-network-apac", "cloud-region-singapore", D.CONNECTS_TO, criticality=0.7, redundancy=0.5),
        e("edge-network-emea", "cloud-region-frankfurt", D.CONNECTS_TO, criticality=0.7, redundancy=0.5),
        e("card-network-apac", "dc-singapore-partner", D.HOSTED_IN, criticality=1.0, redundancy=0.1),
        e("dc-singapore-partner", "supplier-singapore-datacenter", D.SUPPLIED_BY,
          criticality=0.9, redundancy=0.1, capacity_impact=0.9,
          note="The partner operates the facility; losing them is losing the site."),

        # supply chain — the Taiwan path.
        # `criticality` is small and `capacity_impact` is large on purpose: losing the
        # supplier does not switch running clusters off today, it removes the ability to
        # replace failed hardware and to grow. See DependencyEdge.capacity_impact.
        e("payments-k8s-singapore", "supplier-taiwan-hardware", D.SUPPLIED_BY,
          criticality=0.12, redundancy=0.10, capacity_impact=0.65,
          note="HSM modules and terminal silicon; no qualified second source."),
        e("payments-k8s-mumbai", "supplier-taiwan-hardware", D.SUPPLIED_BY,
          criticality=0.08, redundancy=0.25, capacity_impact=0.35,
          note="Partially second-sourced; Mumbai holds 6 weeks of spares."),
        e("dc-taiwan-hsinchu", "supplier-taiwan-hardware", D.SUPPLIED_BY,
          criticality=0.90, redundancy=0.0, capacity_impact=0.95,
          note="Co-located staging facility; shares the supplier's site."),
        e("supplier-taiwan-hardware", "factory-taiwan-assembly", D.DEPENDS_ON,
          criticality=0.95, redundancy=0.05),

        # attack surface (security scenario) — "internet reaches admin-api"
        e("admin-api", "internet", D.CONNECTS_TO, criticality=0.1, redundancy=1.0,
          note="Exposure edge: models internet reachability, not an operational dependency."),

        # services → customers
        e("customer-facing-payments", "customers-apac", D.SERVES, criticality=1.0, redundancy=0.0),
        e("customer-facing-payments", "customers-india", D.SERVES, criticality=0.9, redundancy=0.0),
        e("customer-facing-payments", "customers-emea", D.SERVES, criticality=0.8, redundancy=0.0),
        e("customer-facing-payments", "customers-amer", D.SERVES, criticality=0.7, redundancy=0.0),
        e("merchant-onboarding", "customers-emea", D.SERVES, criticality=0.5, redundancy=0.0),
        e("merchant-onboarding", "customers-apac", D.SERVES, criticality=0.4, redundancy=0.0),
        e("settlement-reporting", "customers-amer", D.SERVES, criticality=0.4, redundancy=0.0),

        # offices depend on regional connectivity (people, not revenue path)
        e("office-singapore", "edge-network-apac", D.CONNECTS_TO, criticality=0.3, redundancy=0.6),
        e("office-bengaluru", "cloud-region-mumbai", D.CONNECTS_TO, criticality=0.3, redundancy=0.6),
        e("office-frankfurt", "edge-network-emea", D.CONNECTS_TO, criticality=0.3, redundancy=0.6),
    ]


def build_atlaspay() -> tuple[list[WorldEntity], list[DependencyEdge]]:
    """The complete synthetic estate."""
    return build_entities(), build_edges()


#: Headline counters for the dashboard. Derived, not hardcoded, so they cannot drift from
#: the fixture — the spec's example numbers are a target, the truth is the graph.
def headline_counts() -> dict[str, int]:
    entities, _ = build_atlaspay()
    critical_services = sum(
        1
        for entity in entities
        if entity.criticality is Criticality.CRITICAL
        and entity.type
        in {
            EntityType.BUSINESS_SERVICE,
            EntityType.APPLICATION,
            EntityType.MICROSERVICE,
            EntityType.DATABASE,
        }
    )
    infrastructure = sum(
        1
        for entity in entities
        if entity.type
        in {
            EntityType.KUBERNETES_CLUSTER,
            EntityType.DATABASE,
            EntityType.CLOUD_REGION,
            EntityType.DATACENTER,
            EntityType.OFFICE,
            EntityType.SUPPLIER,
            EntityType.FACTORY,
            EntityType.NETWORK_NODE,
            EntityType.EXTERNAL_API,
            EntityType.MICROSERVICE,
        }
    )
    return {
        "critical_services": critical_services,
        "infrastructure_assets": infrastructure,
        "entities": len(entities),
    }


#: Timestamps used by the replay scenarios, relative to the fixture epoch so a replay
#: always tells the same story.
REPLAY_BASE_TIME = FIXTURE_EPOCH + timedelta(days=4, hours=9, minutes=12)
