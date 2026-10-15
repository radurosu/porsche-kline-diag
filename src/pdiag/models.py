"""Car models pdiag targets, and how to tell them apart from the VIN.

Model years and VIN layout are public. Every DME-specific value (address,
over-rev range boundaries, dial redline) is a PLACEHOLDER until confirmed from
a capture of that model; see docs/PROTOCOL.md.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# PLACEHOLDER lower bounds of over-rev ranges 1-6 (not verified on any DME).
PLACEHOLDER_OVER_REV = (7300, 7600, 7900, 8200, 8500, 8800)


@dataclass(frozen=True)
class Model:
    code: str
    name: str
    years: tuple[int, int]  # model years with a K-line DME
    redline: int = 7200  # where the dial's red arc starts; approximate
    dme_addr: int = 0x10  # unverified guess
    over_rev_from: tuple[int, ...] = PLACEHOLDER_OVER_REV

    def profile(self) -> dict:
        d = asdict(self)
        d["dme_addr"] = f"0x{self.dme_addr:02X}"
        return d


MODELS = {m.code: m for m in [
    Model("986", "Boxster (986)", (1997, 2004)),
    Model("996", "911 (996)", (1999, 2005)),
    Model("987", "Boxster / Cayman (987.1)", (2005, 2008)),
    Model("997", "911 (997.1)", (2005, 2008)),
]}
DEFAULT = MODELS["996"]

# VIN position 10. Letters I, O, Q, U, Z are never used.
_YEARS = {c: 1980 + i for i, c in enumerate("ABCDEFGHJKLMNPRSTVWXY")}
_YEARS.update({str(d): 2001 + d - 1 for d in range(1, 10)})


def vin_year(vin: str) -> int | None:
    """Model year from VIN position 10, assuming the 1980-2009 cycle (all K-line cars)."""
    return _YEARS.get(vin[9]) if len(vin) == 17 else None


def from_vin(vin: str | None) -> Model | None:
    """Porsche cars (WMI WP0) carry the type in VIN positions 7-8: 98 = Boxster
    and Cayman, 99 = 911. The model year splits the generations. Returns None for
    anything else, including the CAN-era 987.2 / 997.2 (model year 2009 on)."""
    if not vin or len(vin) != 17 or not vin.startswith("WP0"):
        return None
    year = vin_year(vin)
    if year is None:
        return None
    family = vin[6:8]
    if family == "98":
        return MODELS["986"] if year <= 2004 else MODELS["987"] if year <= 2008 else None
    if family == "99":
        # MY2005 is ambiguous: the 996 Turbo/GT2/GT3 ran into 2005 alongside the
        # first 997s. Pass --model 996 for those cars.
        return MODELS["996"] if year <= 2004 else MODELS["997"] if year <= 2008 else None
    return None
