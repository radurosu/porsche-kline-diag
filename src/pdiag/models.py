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
    dme_addr: int = 0x11  # 2002 996 ME7.8 (2026-10-15 capture); assumed for the others
    over_rev_from: tuple[int, ...] = PLACEHOLDER_OVER_REV

    def profile(self) -> dict:
        d = asdict(self)
        d["dme_addr"] = f"0x{self.dme_addr:02X}"
        return d


MODELS = {m.code: m for m in [
    Model("986", "Boxster (986)", (1997, 2004)),
    Model("996", "911 (996)", (1998, 2005)),  # MY1998 in Europe, MY1999 in the US
    Model("987", "Boxster / Cayman (987.1)", (2005, 2008)),
    Model("997", "911 (997.1)", (2005, 2008)),
]}
DEFAULT = MODELS["996"]

# VIN position 10. Letters I, O, Q, U, Z and the digit 0 are never used. The
# codes repeat every 30 years; read them in the 1997-2026 window, since every
# car here is a 1997 or later model (so "A" is 2010, not 1980).
_YEARS = {c: 2010 + i for i, c in enumerate("ABCDEFGHJKLMNPRST")}
_YEARS.update({"V": 1997, "W": 1998, "X": 1999, "Y": 2000})
_YEARS.update({str(d): 2000 + d for d in range(1, 10)})


def vin_year(vin: str) -> int | None:
    """Model year from VIN position 10, read in the 1997-2026 window."""
    return _YEARS.get(vin[9]) if len(vin) == 17 else None


# VIN positions 7-8 -> the generations with that type code, newest first. MY2005
# is in both 911 ranges: the 996 Turbo/GT2/GT3 ran into 2005 alongside the first
# 997s. Most are 997s; pass --model 996 for the others.
_FAMILIES = {"98": ("987", "986"), "99": ("997", "996")}


def from_vin(vin: str | None) -> Model | None:
    """Porsche cars (WMI WP0) carry the type in VIN positions 7-8: 98 = Boxster
    and Cayman, 99 = 911. The model year picks the generation from MODELS. None
    for anything else, e.g. an air-cooled 993 (MY1997 "99") or the CAN-era
    987.2 / 997.2 (MY2009 on)."""
    if not vin or len(vin) != 17 or not vin.startswith("WP0"):
        return None
    year = vin_year(vin)
    if year is None:
        return None
    for code in _FAMILIES.get(vin[6:8], ()):
        first, last = MODELS[code].years
        if first <= year <= last:
            return MODELS[code]
    return None
