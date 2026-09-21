"""Alpha models — view-forming components of the quant stack.

See hedge_fund/signals/base.py for the AlphaModel / QuantModel interface.
Concrete models register here as they are implemented. Two flavors, one
interface: LLM investor agents (persona system prompts on LLMAgent) and
quant models (pure math).
"""

from __future__ import annotations

from hedge_fund.models import AnalystVerdict
from hedge_fund.signals.akre import AkreAgent
from hedge_fund.signals.base import AlphaModel, QuantModel
from hedge_fund.signals.buffett import BuffettAgent
from hedge_fund.signals.chanos import ChanosAgent
from hedge_fund.signals.dalio_resilience import DalioResilienceAgent
from hedge_fund.signals.damodaran import DamodaranAgent
from hedge_fund.signals.dreman import DremanAgent
from hedge_fund.signals.druckenmiller import DruckenmillerAgent
from hedge_fund.signals.earnings_quality_skeptic import EarningsQualitySkepticAgent
from hedge_fund.signals.fisher import FisherAgent
from hedge_fund.signals.fundsmith import FundsmithAgent
from hedge_fund.signals.graham import GrahamAgent
from hedge_fund.signals.greenblatt import GreenblattAgent
from hedge_fund.signals.klarman import KlarmanAgent
from hedge_fund.signals.llm_agent import LLMAgent
from hedge_fund.signals.lynch import LynchAgent
from hedge_fund.signals.munger import MungerAgent
from hedge_fund.signals.pabrai import PabraiAgent
from hedge_fund.signals.pead import PEADModel
from hedge_fund.signals.quality_compounder import QualityCompounderAgent
from hedge_fund.signals.schloss import SchlossAgent

ALPHA_MODEL_REGISTRY: dict[str, type[AlphaModel]] = {
    # Quant models
    "pead": PEADModel,
    # LLM investor agents
    "buffett": BuffettAgent,
    "munger": MungerAgent,
    "graham": GrahamAgent,
    "lynch": LynchAgent,
    "druckenmiller": DruckenmillerAgent,
    # Investor schools — "an analyst applying X's framework", never an
    # impersonation; each prompt carries a scope note (see SCHOOLS in README).
    "akre": AkreAgent,
    "chanos": ChanosAgent,
    "dalio_resilience": DalioResilienceAgent,
    "damodaran": DamodaranAgent,
    "dreman": DremanAgent,
    "earnings_quality_skeptic": EarningsQualitySkepticAgent,
    "fisher": FisherAgent,
    "fundsmith": FundsmithAgent,
    "greenblatt": GreenblattAgent,
    "klarman": KlarmanAgent,
    "pabrai": PabraiAgent,
    "quality_compounder": QualityCompounderAgent,
    "schloss": SchlossAgent,
}

__all__ = [
    "AlphaModel",
    "AnalystVerdict",
    "QuantModel",
    "LLMAgent",
    "BuffettAgent",
    "MungerAgent",
    "GrahamAgent",
    "LynchAgent",
    "DruckenmillerAgent",
    "AkreAgent",
    "ChanosAgent",
    "DalioResilienceAgent",
    "DamodaranAgent",
    "DremanAgent",
    "EarningsQualitySkepticAgent",
    "FisherAgent",
    "FundsmithAgent",
    "GreenblattAgent",
    "KlarmanAgent",
    "PabraiAgent",
    "QualityCompounderAgent",
    "SchlossAgent",
    "PEADModel",
    "ALPHA_MODEL_REGISTRY",
]
