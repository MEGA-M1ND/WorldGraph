"""Azure region → approximate coordinates.

**These are approximations, and WorldGraph says so everywhere it uses them.**

An Azure region is not a building. It is a set of datacenters distributed across a
metropolitan area — sometimes tens of kilometres apart, sometimes more. Microsoft does not
publish datacenter coordinates, and it would be a security problem if it did.

So each entry below is the *published geography* of the region (the city or area Microsoft
names in its own region metadata), used for one purpose only: putting a marker somewhere
defensible on a globe so an operator can see roughly where their estate lives.

What that means for correlation, and it matters:

> An earthquake near a region's map point does **not** prove Azure has suffered an outage.
> It establishes *potential geographic exposure* — a reason to check, not a finding. Only
> official service-health information is operational evidence, and WorldGraph keeps the
> two apart (see ``docs/REALITY_PASS_REPORT.md``).

Coordinates are city-level, rounded to two decimals (~1 km), deliberately. Any more
precision would imply knowledge nobody has.
"""

from __future__ import annotations

from ..models.core import GeoPoint

#: The label WorldGraph attaches to every entity positioned from this table. It appears in
#: the entity's metadata and in the UI, so a marker is never mistaken for a facility.
CLOUD_REGION_APPROXIMATION = "CLOUD REGION APPROXIMATION"

#: Azure region name → (latitude, longitude, display name, geography).
#:
#: Sourced from Microsoft's published region geography (the city or area each region is
#: named for). Where a region names a country rather than a city, the coordinates are that
#: country's primary commercial centre, which is where the capacity actually is.
AZURE_REGIONS: dict[str, tuple[float, float, str, str]] = {
    # -- Asia Pacific ------------------------------------------------------------------
    "southeastasia": (1.35, 103.82, "Southeast Asia", "Singapore"),
    "eastasia": (22.32, 114.17, "East Asia", "Hong Kong SAR"),
    "centralindia": (18.52, 73.86, "Central India", "Pune, India"),
    "southindia": (13.08, 80.27, "South India", "Chennai, India"),
    "westindia": (19.08, 72.88, "West India", "Mumbai, India"),
    "jioindiawest": (22.31, 70.80, "Jio India West", "Jamnagar, India"),
    "jioindiacentral": (21.15, 79.09, "Jio India Central", "Nagpur, India"),
    "japaneast": (35.68, 139.65, "Japan East", "Tokyo, Japan"),
    "japanwest": (34.69, 135.50, "Japan West", "Osaka, Japan"),
    "koreacentral": (37.57, 126.98, "Korea Central", "Seoul, South Korea"),
    "koreasouth": (35.18, 129.08, "Korea South", "Busan, South Korea"),
    "australiaeast": (-33.87, 151.21, "Australia East", "New South Wales, Australia"),
    "australiasoutheast": (-37.81, 144.96, "Australia Southeast", "Victoria, Australia"),
    "australiacentral": (-35.31, 149.13, "Australia Central", "Canberra, Australia"),
    "australiacentral2": (-35.31, 149.13, "Australia Central 2", "Canberra, Australia"),
    "newzealandnorth": (-36.85, 174.76, "New Zealand North", "Auckland, New Zealand"),
    "indonesiacentral": (-6.21, 106.85, "Indonesia Central", "Jakarta, Indonesia"),
    "malaysiawest": (3.14, 101.69, "Malaysia West", "Kuala Lumpur, Malaysia"),
    # -- Europe / Middle East / Africa -------------------------------------------------
    "westeurope": (52.37, 4.90, "West Europe", "Netherlands"),
    "northeurope": (53.35, -6.26, "North Europe", "Ireland"),
    "uksouth": (51.51, -0.13, "UK South", "London, United Kingdom"),
    "ukwest": (51.48, -3.18, "UK West", "Cardiff, United Kingdom"),
    "francecentral": (48.86, 2.35, "France Central", "Paris, France"),
    "francesouth": (43.30, 5.37, "France South", "Marseille, France"),
    "germanywestcentral": (50.11, 8.68, "Germany West Central", "Frankfurt, Germany"),
    "germanynorth": (52.52, 13.40, "Germany North", "Berlin, Germany"),
    "switzerlandnorth": (47.38, 8.54, "Switzerland North", "Zurich, Switzerland"),
    "switzerlandwest": (46.20, 6.14, "Switzerland West", "Geneva, Switzerland"),
    "norwayeast": (59.91, 10.75, "Norway East", "Oslo, Norway"),
    "norwaywest": (58.97, 5.73, "Norway West", "Stavanger, Norway"),
    "swedencentral": (60.67, 17.14, "Sweden Central", "Gävle, Sweden"),
    "polandcentral": (52.23, 21.01, "Poland Central", "Warsaw, Poland"),
    "italynorth": (45.46, 9.19, "Italy North", "Milan, Italy"),
    "spaincentral": (40.42, -3.70, "Spain Central", "Madrid, Spain"),
    "uaenorth": (25.20, 55.27, "UAE North", "Dubai, United Arab Emirates"),
    "uaecentral": (24.45, 54.38, "UAE Central", "Abu Dhabi, United Arab Emirates"),
    "qatarcentral": (25.29, 51.53, "Qatar Central", "Doha, Qatar"),
    "israelcentral": (32.08, 34.78, "Israel Central", "Israel"),
    "southafricanorth": (-26.20, 28.05, "South Africa North", "Johannesburg, South Africa"),
    "southafricawest": (-33.92, 18.42, "South Africa West", "Cape Town, South Africa"),
    # -- Americas ----------------------------------------------------------------------
    "eastus": (37.43, -78.66, "East US", "Virginia, United States"),
    "eastus2": (36.67, -78.39, "East US 2", "Virginia, United States"),
    "centralus": (41.59, -93.62, "Central US", "Iowa, United States"),
    "northcentralus": (41.88, -87.63, "North Central US", "Illinois, United States"),
    "southcentralus": (29.42, -98.49, "South Central US", "Texas, United States"),
    "westcentralus": (41.14, -104.82, "West Central US", "Wyoming, United States"),
    "westus": (37.78, -122.42, "West US", "California, United States"),
    "westus2": (47.61, -122.33, "West US 2", "Washington, United States"),
    "westus3": (33.45, -112.07, "West US 3", "Arizona, United States"),
    "canadacentral": (43.65, -79.38, "Canada Central", "Toronto, Canada"),
    "canadaeast": (46.81, -71.21, "Canada East", "Quebec City, Canada"),
    "brazilsouth": (-23.55, -46.63, "Brazil South", "São Paulo, Brazil"),
    "brazilsoutheast": (-22.91, -43.17, "Brazil Southeast", "Rio de Janeiro, Brazil"),
    "mexicocentral": (20.59, -100.39, "Mexico Central", "Querétaro, Mexico"),
    "chilecentral": (-33.45, -70.67, "Chile Central", "Santiago, Chile"),
}


def normalize_region(name: str | None) -> str:
    """Canonicalise an Azure region identifier.

    Azure returns ``southeastasia`` in some APIs and ``Southeast Asia`` in others; both
    must resolve to the same region entity or the same location would produce two.
    """
    if not name:
        return ""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def region_location(name: str | None) -> GeoPoint | None:
    """Approximate coordinates for an Azure region, or ``None`` if unrecognised.

    ``None`` is a real answer. A new or sovereign-cloud region WorldGraph does not know
    gets no marker rather than a guessed one — an entity at the wrong place on the globe
    is worse than an entity with no place, because it invites a conclusion.
    """
    entry = AZURE_REGIONS.get(normalize_region(name))
    if entry is None:
        return None
    lat, lon, _display, _geography = entry
    return GeoPoint(lat=lat, lon=lon)


def region_display_name(name: str | None) -> str:
    """Human name for a region, falling back to the identifier Azure gave us."""
    entry = AZURE_REGIONS.get(normalize_region(name))
    return entry[2] if entry else (name or "unknown region")


def region_geography(name: str | None) -> str:
    """The published geography a region is named for, for the provenance panel."""
    entry = AZURE_REGIONS.get(normalize_region(name))
    return entry[3] if entry else ""


def is_known_region(name: str | None) -> bool:
    return normalize_region(name) in AZURE_REGIONS
