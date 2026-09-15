"""The playbook: rules that turn a ticker's latest verdicts into candidates.

Input is the ledger's latest verdict per (ticker, school), restricted per
ticker to the rows that reason on its most recent filing, so every school in
a combination saw the same numbers. Each rule is a small pure function that
returns a reason string or None. The output is a list for the user to review;
it places no orders and sizes nothing.

resilience_confirmed uses the dalio_resilience school as what it is — a
balance-sheet-durability lens — and says so in its reason string.
"""

from __future__ import annotations

import csv
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

HEADER = "Candidates for review — not orders, not advice. Rules: {config}."
FORENSIC_SCHOOLS = ("chanos", "earnings_quality_skeptic")


@dataclass(frozen=True)
class PlaybookConfig:
    min_schools: int = 3
    min_conf: float = 55.0
    follow: str = "dreman"
    follow_conf: float = 65.0
    warning_conf: float = 60.0

    def describe(self) -> str:
        return f"min_schools={self.min_schools}, min_conf={self.min_conf:g}, follow={self.follow}, follow_conf={self.follow_conf:g}, warning_conf={self.warning_conf:g}"


@dataclass
class Candidate:
    ticker: str
    direction: str  # "long" | "short"
    rules_fired: list[str]
    schools_bullish: list[str]
    schools_bearish: list[str]
    schools_neutral: list[str]
    mean_conf_of_agreeing: float | None
    filing_date: str | None
    warnings: list[str] = field(default_factory=list)


Verdicts = Mapping[str, dict]  # school -> ledger row (same ticker, same filing)


def _with(verdicts: Verdicts, signal: str, min_conf: float) -> list[str]:
    return sorted(s for s, r in verdicts.items() if r["signal"] == signal and (r.get("confidence") or 0) >= min_conf)


def consensus_long(v: Verdicts, cfg: PlaybookConfig) -> str | None:
    bulls = _with(v, "bullish", cfg.min_conf)
    if len(bulls) >= cfg.min_schools and not _with(v, "bearish", 0):
        return f"{len(bulls)} schools bullish at >= {cfg.min_conf:g}, none bearish"
    return None


def consensus_short(v: Verdicts, cfg: PlaybookConfig) -> str | None:
    bears = _with(v, "bearish", cfg.min_conf)
    if len(bears) >= cfg.min_schools and not _with(v, "bullish", 0):
        return f"{len(bears)} schools bearish at >= {cfg.min_conf:g}, none bullish"
    return None


def contrarian(v: Verdicts, cfg: PlaybookConfig) -> str | None:
    row = v.get(cfg.follow)
    if row and row["signal"] in ("bullish", "bearish") and (row.get("confidence") or 0) >= cfg.follow_conf:
        return f"{cfg.follow} {row['signal']} at {row['confidence']:g}"
    return None


def resilience_confirmed(v: Verdicts, cfg: PlaybookConfig) -> str | None:
    lens = v.get("dalio_resilience")
    if not lens or lens["signal"] != "bullish" or (lens.get("confidence") or 0) < cfg.min_conf:
        return None
    others = [s for s in _with(v, "bullish", cfg.min_conf) if s != "dalio_resilience"]
    if len(others) >= 2 and not _with(v, "bearish", 0):
        return f"resilient balance sheet + {len(others)} schools bullish"
    return None


def forensic_warning(v: Verdicts, cfg: PlaybookConfig) -> str | None:
    flags = [f"{s} bearish at {v[s]['confidence']:g}" for s in FORENSIC_SCHOOLS if s in v and v[s]["signal"] == "bearish" and (v[s].get("confidence") or 0) >= cfg.warning_conf]
    return "; ".join(flags) or None


LONG_RULES: tuple[tuple[str, Callable[[Verdicts, PlaybookConfig], str | None]], ...] = (("consensus_long", consensus_long), ("resilience_confirmed", resilience_confirmed))
SHORT_RULES: tuple[tuple[str, Callable[[Verdicts, PlaybookConfig], str | None]], ...] = (("consensus_short", consensus_short),)


def same_filing_verdicts(latest_rows: Mapping[tuple[str, str], dict]) -> dict[str, dict[str, dict]]:
    """ticker -> {school: row} using only rows on the ticker's most recent filing."""
    by_ticker: dict[str, dict[str, dict]] = {}
    for (ticker, school), row in latest_rows.items():
        by_ticker.setdefault(ticker, {})[school] = row
    out: dict[str, dict[str, dict]] = {}
    for ticker, verdicts in by_ticker.items():
        newest = max((r.get("filing_date") or "") for r in verdicts.values())
        out[ticker] = {s: r for s, r in verdicts.items() if (r.get("filing_date") or "") == newest}
    return out


def playbook(latest_rows: Mapping[tuple[str, str], dict], config: PlaybookConfig | None = None) -> list[Candidate]:
    cfg = config or PlaybookConfig()
    candidates: list[Candidate] = []
    for ticker, v in sorted(same_filing_verdicts(latest_rows).items()):
        fired: dict[str, list[str]] = {"long": [], "short": []}
        for name, rule in LONG_RULES:
            if rule(v, cfg):
                fired["long"].append(name)
        for name, rule in SHORT_RULES:
            if rule(v, cfg):
                fired["short"].append(name)
        reason = contrarian(v, cfg)
        if reason:
            fired["long" if v[cfg.follow]["signal"] == "bullish" else "short"].append("contrarian")
        if not fired["long"] and not fired["short"]:
            continue
        direction = "long" if len(fired["long"]) >= len(fired["short"]) else "short"
        agreeing = "bullish" if direction == "long" else "bearish"
        confs = [r["confidence"] for r in v.values() if r["signal"] == agreeing and r.get("confidence") is not None]
        warning = forensic_warning(v, cfg)
        candidates.append(
            Candidate(
                ticker=ticker,
                direction=direction,
                rules_fired=fired[direction],
                schools_bullish=sorted(s for s, r in v.items() if r["signal"] == "bullish"),
                schools_bearish=sorted(s for s, r in v.items() if r["signal"] == "bearish"),
                schools_neutral=sorted(s for s, r in v.items() if r["signal"] == "neutral"),
                mean_conf_of_agreeing=(sum(confs) / len(confs)) if confs else None,
                filing_date=next(iter(v.values())).get("filing_date"),
                warnings=[warning] if warning else [],
            )
        )
    candidates.sort(key=lambda c: (-len(c.rules_fired), -(c.mean_conf_of_agreeing or 0), c.ticker))
    return candidates


def render_candidates(candidates: list[Candidate], config: PlaybookConfig | None = None) -> str:
    cfg = config or PlaybookConfig()
    lines = [HEADER.format(config=cfg.describe())]
    if not candidates:
        lines.append("(no rule fired)")
        return "\n".join(lines)
    head = f"{'ticker':7}| {'dir':5} | {'rules':36} | {'conf':>5} | {'bull':>4} {'bear':>4} {'neut':>4} | {'filing':10} | warnings"
    lines += [head, "-" * len(head)]
    for c in candidates:
        conf = "-" if c.mean_conf_of_agreeing is None else f"{c.mean_conf_of_agreeing:.0f}"
        lines.append(f"{c.ticker:7}| {c.direction:5} | {', '.join(c.rules_fired):36} | {conf:>5} | {len(c.schools_bullish):>4} {len(c.schools_bearish):>4} {len(c.schools_neutral):>4} | {c.filing_date or '-':10} | {'; '.join(c.warnings)}")
    return "\n".join(lines)


def write_candidates_csv(candidates: list[Candidate], path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(Candidate.__dataclass_fields__))
        writer.writeheader()
        for c in candidates:
            row = asdict(c)
            for k in ("rules_fired", "schools_bullish", "schools_bearish", "schools_neutral", "warnings"):
                row[k] = ";".join(row[k])
            writer.writerow(row)
    return path
