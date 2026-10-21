"""How grid-scale batteries in Australia's National Electricity Market actually operate, and how much energy-arbitrage
revenue they leave on the table compared with a perfect-foresight optimum (AEMO open data)."""

from nem_battery.optimizer import BatteryParams, DispatchResult, optimize_day

__all__ = ["BatteryParams", "DispatchResult", "optimize_day"]
__version__ = "0.1.0"
