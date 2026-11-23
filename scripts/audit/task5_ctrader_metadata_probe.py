"""
Task 5 — Read-Only cTrader Open API Account, Symbol & Deal Metadata Probe.

Reads existing environment configuration without printing or writing credentials/secrets.
Sends ONLY read-only metadata requests:
  - ProtoOAAssetListReq
  - ProtoOATraderReq
  - ProtoOASymbolsListReq + ProtoOASymbolByIdReq (XAUUSD)
  - ProtoOADealListReq (last 180 days, up to 500 deals)
  - ProtoOAReconcileReq (verify 0 orders / 0 open positions)

Never places, modifies, or cancels orders.
Saves redacted evidence snapshot to backtest_results/audit/task5_account_specifications.json.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

from twisted.internet import reactor
from ctrader_open_api import Client, EndPoints, Protobuf, TcpProtocol
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoErrorRes
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq,
    ProtoOAAccountAuthRes,
    ProtoOAApplicationAuthReq,
    ProtoOAApplicationAuthRes,
    ProtoOAAssetListReq,
    ProtoOAAssetListRes,
    ProtoOADealListReq,
    ProtoOADealListRes,
    ProtoOAErrorRes,
    ProtoOAReconcileReq,
    ProtoOAReconcileRes,
    ProtoOASymbolByIdReq,
    ProtoOASymbolByIdRes,
    ProtoOASymbolsListReq,
    ProtoOASymbolsListRes,
    ProtoOATraderReq,
    ProtoOATraderRes,
)
from ctrader_open_api.messages import OpenApiModelMessages_pb2 as mm

OUT_PATH = Path("backtest_results/audit/task5_account_specifications.json")


def _enum_name(enum_cls: Any, val: int) -> str:
    try:
        return enum_cls.Name(val)
    except Exception:
        return str(val)


class ReadOnlyProbe:
    def __init__(self) -> None:
        self.client_id = os.environ.get("CTRADER_CLIENT_ID", "")
        self.client_secret = os.environ.get("CTRADER_CLIENT_SECRET", "")
        self.access_token = os.environ.get("CTRADER_ACCESS_TOKEN", "")
        acct_raw = os.environ.get("CTRADER_ACCOUNT_ID", "")
        self.account_id = int(acct_raw) if acct_raw.isdigit() else 0
        self.trading_mode = os.environ.get("TRADING_MODE", "demo").lower()
        self.host = (
            EndPoints.PROTOBUF_LIVE_HOST
            if self.trading_mode == "live"
            else EndPoints.PROTOBUF_DEMO_HOST
        )
        self.client = Client(self.host, EndPoints.PROTOBUF_PORT, TcpProtocol)

        self.result: Dict[str, Any] = {
            "queried_at_utc": datetime.now(timezone.utc).isoformat(),
            "broker": "Fusion Markets",
            "platform": "cTrader",
            "host": self.host,
            "trading_environment": self.trading_mode,
            "ctid_trader_account_id": self.account_id,
            "errors": [],
        }
        self.assets_by_id: Dict[int, Dict[str, Any]] = {}
        self.xau_symbol_id: int = 0
        self.pending_steps: set[str] = {"trader", "assets", "symbol_list", "deals", "reconcile"}
        self.finished = False

    def _finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(self.result, indent=2), encoding="utf-8")
        try:
            self.client.stopService()
        except Exception:
            pass
        if reactor.running:
            reactor.callLater(0.2, reactor.stop)

    def _maybe_done(self) -> None:
        if not self.pending_steps:
            self._finish()

    def start(self) -> None:
        if not (self.client_id and self.client_secret and self.access_token and self.account_id):
            self.result["errors"].append("Missing required cTrader env vars (details redacted).")
            OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
            OUT_PATH.write_text(json.dumps(self.result, indent=2), encoding="utf-8")
            print(json.dumps({"ok": False, "error": "Missing cTrader env vars"}))
            return
        self.client.setConnectedCallback(self.on_connected)
        self.client.setDisconnectedCallback(self.on_disconnected)
        self.client.setMessageReceivedCallback(self.on_message)
        reactor.callLater(25.0, self._on_timeout)
        self.client.startService()
        reactor.run()

    def _on_timeout(self) -> None:
        if not self.finished:
            self.result["errors"].append(f"Probe timed out with pending steps: {sorted(self.pending_steps)}")
            self._finish()

    def on_connected(self, client: Client) -> None:
        req = ProtoOAApplicationAuthReq()
        req.clientId = self.client_id
        req.clientSecret = self.client_secret
        client.send(req)

    def on_disconnected(self, client: Client, reason: Any) -> None:
        if not self.finished:
            self.result["errors"].append(f"Disconnected: {reason}")
            self._finish()

    def on_message(self, client: Client, message: Any) -> None:
        pt = message.payloadType
        if pt == ProtoOAApplicationAuthRes().payloadType:
            req = ProtoOAAccountAuthReq()
            req.ctidTraderAccountId = self.account_id
            req.accessToken = self.access_token
            client.send(req)
            return

        if pt == ProtoOAAccountAuthRes().payloadType:
            r_trader = ProtoOATraderReq()
            r_trader.ctidTraderAccountId = self.account_id
            client.send(r_trader)

            r_assets = ProtoOAAssetListReq()
            r_assets.ctidTraderAccountId = self.account_id
            client.send(r_assets)

            r_syms = ProtoOASymbolsListReq()
            r_syms.ctidTraderAccountId = self.account_id
            client.send(r_syms)

            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            from_ms = int((datetime.now(timezone.utc) - timedelta(days=180)).timestamp() * 1000)
            r_deals = ProtoOADealListReq()
            r_deals.ctidTraderAccountId = self.account_id
            r_deals.fromTimestamp = from_ms
            r_deals.toTimestamp = now_ms
            r_deals.maxRows = 500
            client.send(r_deals)

            r_rec = ProtoOAReconcileReq()
            r_rec.ctidTraderAccountId = self.account_id
            client.send(r_rec)
            return

        if pt == ProtoOATraderRes().payloadType:
            res = Protobuf.extract(message)
            t = res.trader
            md = int(getattr(t, "moneyDigits", 2) or 2)
            div = float(10 ** md)
            dep_id = int(getattr(t, "depositAssetId", 0) or 0)
            self.result["account"] = {
                "ctidTraderAccountId": int(t.ctidTraderAccountId),
                "brokerName": str(getattr(t, "brokerName", "")),
                "accountType": _enum_name(mm.ProtoOAAccountType, int(getattr(t, "accountType", 0))),
                "accessRights": _enum_name(mm.ProtoOAAccessRights, int(getattr(t, "accessRights", 0))),
                "depositAssetId": dep_id,
                "depositCurrency": self.assets_by_id.get(dep_id, {}).get("name"),
                "swapFree": bool(getattr(t, "swapFree", False)),
                "leverageInCents": int(getattr(t, "leverageInCents", 0) or 0),
                "leverageRatio": (int(getattr(t, "leverageInCents", 0) or 0) / 100.0),
                "maxLeverage": int(getattr(t, "maxLeverage", 0) or 0),
                "balance": float(getattr(t, "balance", 0) or 0) / div,
                "moneyDigits": md,
                "isLimitedRisk": bool(getattr(t, "isLimitedRisk", False)),
                "frenchRisk": bool(getattr(t, "frenchRisk", False)),
                "registrationTimestamp_utc": (
                    datetime.fromtimestamp(t.registrationTimestamp / 1000.0, tz=timezone.utc).isoformat()
                    if getattr(t, "registrationTimestamp", 0)
                    else None
                ),
            }
            self.pending_steps.discard("trader")
            self._maybe_done()
            return

        if pt == ProtoOAAssetListRes().payloadType:
            res = Protobuf.extract(message)
            for a in res.asset:
                self.assets_by_id[int(a.assetId)] = {
                    "assetId": int(a.assetId),
                    "name": str(a.name),
                    "displayName": str(getattr(a, "displayName", "")),
                    "digits": int(getattr(a, "digits", 2) or 2),
                }
            if "account" in self.result:
                dep_id = self.result["account"]["depositAssetId"]
                self.result["account"]["depositCurrency"] = self.assets_by_id.get(dep_id, {}).get("name")
            if "xauusd_spec" in self.result:
                mca = self.result["xauusd_spec"].get("minCommissionAssetId")
                if mca in self.assets_by_id:
                    self.result["xauusd_spec"]["minCommissionAsset"] = self.assets_by_id[mca]["name"]
            self.pending_steps.discard("assets")
            self._maybe_done()
            return

        if pt == ProtoOASymbolsListRes().payloadType:
            res = Protobuf.extract(message)
            found = None
            for s in res.symbol:
                if s.symbolName.upper() == "XAUUSD":
                    found = s
                    break
            if found is not None:
                self.xau_symbol_id = int(found.symbolId)
                self.result["xauusd_light_symbol"] = {
                    "symbolId": self.xau_symbol_id,
                    "symbolName": str(found.symbolName),
                    "enabled": bool(found.enabled),
                    "description": str(getattr(found, "description", "")),
                    "baseAssetId": int(getattr(found, "baseAssetId", 0) or 0),
                    "quoteAssetId": int(getattr(found, "quoteAssetId", 0) or 0),
                }
                self.pending_steps.add("symbol_by_id")
                req = ProtoOASymbolByIdReq()
                req.ctidTraderAccountId = self.account_id
                req.symbolId.append(self.xau_symbol_id)
                client.send(req)
            else:
                self.result["errors"].append("XAUUSD not found in ProtoOASymbolsListRes")
            self.pending_steps.discard("symbol_list")
            self._maybe_done()
            return

        if pt == ProtoOASymbolByIdRes().payloadType:
            res = Protobuf.extract(message)
            if res.symbol:
                s = res.symbol[0]
                lot_size_raw = int(getattr(s, "lotSize", 0) or 0)
                min_vol_raw = int(getattr(s, "minVolume", 0) or 0)
                step_vol_raw = int(getattr(s, "stepVolume", 0) or 0)
                max_vol_raw = int(getattr(s, "maxVolume", 0) or 0)
                lot_div = float(lot_size_raw) if lot_size_raw > 0 else 1.0
                mca_str = str(getattr(s, "minCommissionAsset", ""))
                schedule_list = [
                    {
                        "startSecond": int(iv.startSecond),
                        "endSecond": int(iv.endSecond),
                    }
                    for iv in getattr(s, "schedule", [])
                ]
                holidays = [
                    {
                        "name": str(h.name),
                        "holidayDate": int(getattr(h, "holidayDate", 0) or 0),
                        "startSecond": int(getattr(h, "startSecond", 0) or 0),
                        "endSecond": int(getattr(h, "endSecond", 0) or 0),
                        "scheduleTimeZone": str(getattr(h, "scheduleTimeZone", "")),
                    }
                    for h in getattr(s, "holiday", [])
                ]
                self.result["xauusd_spec"] = {
                    "symbolId": int(s.symbolId),
                    "digits": int(s.digits),
                    "pipPosition": int(s.pipPosition),
                    "point_size": 10 ** (-int(s.digits)),
                    "pip_size": 10 ** (-int(s.pipPosition)),
                    "enableShortSelling": bool(getattr(s, "enableShortSelling", True)),
                    "guaranteedStopLoss": bool(getattr(s, "guaranteedStopLoss", False)),
                    "lotSize_raw_cents": lot_size_raw,
                    "contract_size_units": lot_size_raw / 100.0,
                    "minVolume_raw": min_vol_raw,
                    "min_lots": min_vol_raw / lot_div,
                    "stepVolume_raw": step_vol_raw,
                    "step_lots": step_vol_raw / lot_div,
                    "maxVolume_raw": max_vol_raw,
                    "max_lots": max_vol_raw / lot_div,
                    "commission_raw": int(getattr(s, "commission", 0) or 0),
                    "preciseTradingCommissionRate": int(getattr(s, "preciseTradingCommissionRate", 0) or 0),
                    "commissionType": _enum_name(mm.ProtoOACommissionType, int(getattr(s, "commissionType", 0))),
                    "minCommission_raw": int(getattr(s, "minCommission", 0) or 0),
                    "preciseMinCommission": int(getattr(s, "preciseMinCommission", 0) or 0),
                    "minCommissionType": _enum_name(mm.ProtoOAMinCommissionType, int(getattr(s, "minCommissionType", 0))),
                    "minCommissionAsset": mca_str,
                    "slDistance": int(getattr(s, "slDistance", 0) or 0),
                    "tpDistance": int(getattr(s, "tpDistance", 0) or 0),
                    "distanceSetIn": _enum_name(mm.ProtoOASymbolDistanceType, int(getattr(s, "distanceSetIn", 0))),
                    "swapCalculationType": _enum_name(mm.ProtoOASwapCalculationType, int(getattr(s, "swapCalculationType", 0))),
                    "swapLong": float(getattr(s, "swapLong", 0.0) or 0.0),
                    "swapShort": float(getattr(s, "swapShort", 0.0) or 0.0),
                    "swapRollover3Days": _enum_name(mm.ProtoOADayOfWeek, int(getattr(s, "swapRollover3Days", 0))),
                    "swapPeriod_hours": int(getattr(s, "swapPeriod", 0) or 0),
                    "swapTime_minutes_from_midnight": int(getattr(s, "swapTime", 0) or 0),
                    "skipSWAPPeriods": int(getattr(s, "skipSWAPPeriods", 0) or 0),
                    "chargeSwapAtWeekends": bool(getattr(s, "chargeSwapAtWeekends", False)),
                    "rolloverCommission": float(getattr(s, "rolloverCommission", 0.0) or 0.0),
                    "rolloverCommission3Days": _enum_name(mm.ProtoOADayOfWeek, int(getattr(s, "rolloverCommission3Days", 0))),
                    "tradingMode": _enum_name(mm.ProtoOATradingMode, int(getattr(s, "tradingMode", 0))),
                    "scheduleTimeZone": str(getattr(s, "scheduleTimeZone", "")),
                    "schedule_intervals": schedule_list,
                    "holidays_count": len(holidays),
                    "holidays_sample": holidays[:10],
                }
            self.pending_steps.discard("symbol_by_id")
            self._maybe_done()
            return

        if pt == ProtoOADealListRes().payloadType:
            res = Protobuf.extract(message)
            deals: List[Dict[str, Any]] = []
            for d in res.deal:
                md = int(getattr(d, "moneyDigits", 2) or 2)
                div = float(10 ** md)
                close_det = None
                if d.HasField("closePositionDetail"):
                    cd = d.closePositionDetail
                    cd_md = int(getattr(cd, "moneyDigits", md) or md)
                    cd_div = float(10 ** cd_md)
                    close_det = {
                        "entryPrice": float(cd.entryPrice),
                        "grossProfit": float(cd.grossProfit) / cd_div,
                        "swap": float(cd.swap) / cd_div,
                        "commission": float(cd.commission) / cd_div,
                        "balance": float(cd.balance) / cd_div,
                        "closedVolume_raw": int(cd.closedVolume),
                        "quoteToDepositConversionRate": float(getattr(cd, "quoteToDepositConversionRate", 0.0) or 0.0),
                        "pnlConversionFee": float(getattr(cd, "pnlConversionFee", 0) or 0) / cd_div,
                    }
                deals.append(
                    {
                        "dealId": int(d.dealId),
                        "orderId": int(d.orderId),
                        "positionId": int(d.positionId),
                        "symbolId": int(d.symbolId),
                        "volume_raw": int(d.volume),
                        "filledVolume_raw": int(d.filledVolume),
                        "executionPrice": float(d.executionPrice),
                        "tradeSide": _enum_name(mm.ProtoOATradeSide, int(d.tradeSide)),
                        "dealStatus": _enum_name(mm.ProtoOADealStatus, int(d.dealStatus)),
                        "executionTimestamp_utc": datetime.fromtimestamp(d.executionTimestamp / 1000.0, tz=timezone.utc).isoformat(),
                        "commission_usd": float(getattr(d, "commission", 0) or 0) / div,
                        "marginRate": float(getattr(d, "marginRate", 0.0) or 0.0),
                        "baseToUsdConversionRate": float(getattr(d, "baseToUsdConversionRate", 0.0) or 0.0),
                        "closePositionDetail": close_det,
                    }
                )
            self.result["deal_history_180d"] = {
                "count": len(deals),
                "hasMore": bool(getattr(res, "hasMore", False)),
                "deals": deals,
            }
            self.pending_steps.discard("deals")
            self._maybe_done()
            return

        if pt == ProtoOAReconcileRes().payloadType:
            res = Protobuf.extract(message)
            self.result["open_broker_state"] = {
                "open_positions": len(res.position),
                "pending_orders": len(res.order),
            }
            self.pending_steps.discard("reconcile")
            self._maybe_done()
            return

        if pt in (ProtoOAErrorRes().payloadType, ProtoErrorRes().payloadType):
            err = Protobuf.extract(message)
            self.result["errors"].append(
                {
                    "payloadType": int(pt),
                    "errorCode": str(getattr(err, "errorCode", "")),
                    "description": str(getattr(err, "description", "")),
                }
            )
            self._finish()


def main() -> None:
    probe = ReadOnlyProbe()
    probe.start()
    print(json.dumps(probe.result, indent=2))


if __name__ == "__main__":
    main()
