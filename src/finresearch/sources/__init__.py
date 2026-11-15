"""Data sources. Each turns raw responses into point-in-time tables."""

from finresearch.sources import (
    cftc_cot,
    deribit,
    deribit_dvol,
    heartbeat,
    hyperliquid,
    nyfed_rates,
    philly_rtdsm,
    releases,
    synthetic,
)
from finresearch.sources.base import Adapter

ADAPTERS: dict[str, Adapter] = {
    a.id: a
    for a in (
        synthetic.ADAPTER,
        *heartbeat.ADAPTERS,
        deribit.ADAPTER,
        deribit_dvol.ADAPTER,
        hyperliquid.ADAPTER,
        nyfed_rates.ADAPTER,
        philly_rtdsm.ADAPTER,
        releases.BEA_ADAPTER,
        releases.BLS_ADAPTER,
        cftc_cot.ADAPTER,
    )
}
