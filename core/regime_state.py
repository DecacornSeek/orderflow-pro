"""
core/regime_state.py — Stabilitaet der Anzeige (Charter §6).

Eine flackernde Anzeige ist schlechter als keine. Drei Mechanismen:

1. Hysterese auf den Regime-Zustand. Umschaltung erst bei Durchbruch ueber
   eine Bandbreite UND Verweildauer. Ohne das flackert die Anzeige am
   staerksten genau am Flip — dort, wo sie am noetigsten ist.
2. Session-Open als Anker. Snapshot bei Sessionstart festhalten, aktuelle
   Werte als Delta dazu. Ohne Anker geht das Gefuehl verloren, ob sich etwas
   bewegt hat oder man sich an den neuen Zustand gewoehnt hat.
3. Aenderungsliste statt Daueranimation. Nur materielle Aenderungen melden.

Der Zustandsautomat trifft keine Richtungsaussage — "Long Gamma" heisst
"Hedging daempft", nicht "Preis steigt".
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import statistics
from typing import Any, Deque, Dict, List, Optional

LONG_GAMMA = "LONG_GAMMA"      # Hedging daempft
SHORT_GAMMA = "SHORT_GAMMA"    # Hedging verstaerkt
TRANSITION = "TRANSITION"      # im Totband oder Verweildauer noch nicht erfuellt
UNKNOWN = "UNKNOWN"            # noch keine Daten

# Totband als Anteil der typischen |Net-GEX|-Groesse. Innerhalb davon wird
# kein Regime behauptet.
DEFAULT_BAND_PCT = 0.10
# Verweildauer jenseits des Bandes, bevor umgeschaltet wird
DEFAULT_DWELL_SECONDS = 120.0
# Fenster fuer die Referenzgroesse (robust ueber den Median)
DEFAULT_REFERENCE_WINDOW = 60

MAX_CHANGES = 40


@dataclass(frozen=True)
class RegimeState:
    """Der veroeffentlichte Zustand samt Begruendung."""

    state: str
    raw_state: str              # was der aktuelle Wert allein sagen wuerde
    net_gex: Optional[float]
    band: Optional[float]       # aktuelle Totbandbreite in USD
    pending_state: Optional[str]      # Kandidat, der die Verweildauer laeuft
    pending_seconds: float            # wie lange schon
    dwell_required: float
    changed_at: Optional[datetime]

    @property
    def is_committed(self) -> bool:
        return self.state in (LONG_GAMMA, SHORT_GAMMA)


class RegimeTracker:
    """
    Fuehrt den Regime-Zustand mit Hysterese.

    Ein Kandidat muss zwei Huerden nehmen: das Totband verlassen UND die
    Verweildauer ueberstehen. Der Weg zurueck ins Totband ist dagegen sofort —
    Unsicherheit einzuraeumen braucht keine Wartezeit, eine Behauptung schon.
    """

    def __init__(
        self,
        band_pct: float = DEFAULT_BAND_PCT,
        dwell_seconds: float = DEFAULT_DWELL_SECONDS,
        reference_window: int = DEFAULT_REFERENCE_WINDOW,
    ) -> None:
        self.band_pct = band_pct
        self.dwell_seconds = dwell_seconds
        self._magnitudes: Deque[float] = deque(maxlen=reference_window)
        self._state: str = UNKNOWN
        self._changed_at: Optional[datetime] = None
        self._pending: Optional[str] = None
        self._pending_since: Optional[datetime] = None

    def _band(self) -> Optional[float]:
        """Totbandbreite aus der typischen Groessenordnung der Beobachtungen."""
        if not self._magnitudes:
            return None
        return statistics.median(self._magnitudes) * self.band_pct

    def update(self, net_gex: Optional[float], now: Optional[datetime] = None) -> RegimeState:
        """Verarbeitet eine neue Netto-GEX-Beobachtung."""
        if now is None:
            now = datetime.now(timezone.utc)

        if net_gex is None:
            # Kein Wert heisst nicht "ausgeglichen" — der Zustand bleibt stehen,
            # aber es wird nichts Neues behauptet.
            return RegimeState(self._state, UNKNOWN, None, self._band(),
                               self._pending, self._pending_age(now),
                               self.dwell_seconds, self._changed_at)

        self._magnitudes.append(abs(net_gex))
        band = self._band() or 0.0

        if net_gex > band:
            raw = LONG_GAMMA
        elif net_gex < -band:
            raw = SHORT_GAMMA
        else:
            raw = TRANSITION

        if raw == TRANSITION:
            # Zurueck ins Totband: sofort, ohne Verweildauer.
            self._pending = None
            self._pending_since = None
            if self._state != TRANSITION:
                self._state = TRANSITION
                self._changed_at = now
        elif raw == self._state:
            # Bereits dort — laufender Kandidat ist hinfaellig.
            self._pending = None
            self._pending_since = None
        else:
            # Neuer Kandidat: Verweildauer starten oder weiterlaufen lassen.
            if self._pending != raw:
                self._pending = raw
                self._pending_since = now
            elif self._pending_age(now) >= self.dwell_seconds:
                self._state = raw
                self._changed_at = now
                self._pending = None
                self._pending_since = None

        return RegimeState(self._state, raw, net_gex, self._band(),
                           self._pending, self._pending_age(now),
                           self.dwell_seconds, self._changed_at)

    def _pending_age(self, now: datetime) -> float:
        if self._pending_since is None:
            return 0.0
        return max(0.0, (now - self._pending_since).total_seconds())


class SessionAnchor:
    """
    Haelt den Zustand bei Sessionstart fest und liefert Deltas dazu.

    Die Session laeuft hier von 08:00 UTC bis 08:00 UTC — das ist die Grenze,
    an der die 0DTE-Kette verfaellt und das Optionsbuch sich neu aufstellt.
    Wechselt der Trader auf eine andere Sessiondefinition, ist das der eine
    Ort, an dem es geaendert wird.
    """

    def __init__(self, boundary_hour: int = 8) -> None:
        self.boundary_hour = boundary_hour
        self._values: Dict[str, Any] = {}
        self._taken_at: Optional[datetime] = None

    def _session_start(self, now: datetime) -> datetime:
        start = now.replace(hour=self.boundary_hour, minute=0, second=0, microsecond=0)
        if start > now:
            start -= timedelta(days=1)
        return start

    def update(self, values: Dict[str, Any], now: Optional[datetime] = None) -> None:
        """Setzt den Anker, wenn noch keiner steht oder die Session gewechselt hat."""
        if now is None:
            now = datetime.now(timezone.utc)
        session_start = self._session_start(now)

        if self._taken_at is None or self._taken_at < session_start:
            self._values = {k: v for k, v in values.items() if v is not None}
            self._taken_at = now

    def deltas(self, current: Dict[str, Any]) -> Dict[str, Any]:
        """
        Differenz zum Anker je Feld. Fehlt ein Wert auf einer der beiden
        Seiten, bleibt das Delta None — es wird nichts unterstellt.
        """
        out: Dict[str, Any] = {}
        for key, now_value in current.items():
            anchor_value = self._values.get(key)
            if anchor_value is None or now_value is None:
                out[key] = None
                continue
            try:
                out[key] = now_value - anchor_value
            except TypeError:
                out[key] = None
        return out

    def to_dict(self, current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {
            "taken_at": self._taken_at.isoformat() if self._taken_at else None,
            "values": dict(self._values),
            "deltas": self.deltas(current) if current else {},
        }


class ChangeLog:
    """
    Nur materielle Aenderungen, jede genau einmal.

    Charter §6: Aenderungsliste statt Daueranimation. Die Bewertung, was
    materiell ist, trifft der Aufrufer — hier wird nur entprellt und begrenzt.
    """

    def __init__(self, maxlen: int = MAX_CHANGES) -> None:
        self._entries: Deque[Dict[str, Any]] = deque(maxlen=maxlen)
        self._last_key: Dict[str, str] = {}

    def record(self, kind: str, text: str, now: Optional[datetime] = None,
               dedupe_key: Optional[str] = None) -> bool:
        """
        Nimmt eine Aenderung auf. Gibt False zurueck, wenn sie als Wiederholung
        derselben Aussage verworfen wurde.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        key = dedupe_key or text
        if self._last_key.get(kind) == key:
            return False
        self._last_key[kind] = key
        self._entries.appendleft({"kind": kind, "text": text, "at": now.isoformat()})
        return True

    def entries(self) -> List[Dict[str, Any]]:
        return list(self._entries)
