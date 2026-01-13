from fastapi import WebSocket
from typing import Dict
from utils.logger import logger


class ConnectionManager:
    def __init__(self):
        # symbol -> set of websockets
        self.market_conns: Dict[str, set] = {}
        self.trade_conns: Dict[str, set] = {}

    async def connect_market(self, symbol: str, websocket: WebSocket):
        await websocket.accept()
        self.market_conns.setdefault(symbol, set()).add(websocket)
        logger.info(f"Market ws connected: {symbol} total={len(self.market_conns[symbol])}")

    async def connect_trades(self, symbol: str, websocket: WebSocket):
        await websocket.accept()
        self.trade_conns.setdefault(symbol, set()).add(websocket)
        logger.info(f"Trades ws connected: {symbol} total={len(self.trade_conns[symbol])}")

    def disconnect(self, websocket: WebSocket):
        for d in (self.market_conns, self.trade_conns):
            for symbol, conns in list(d.items()):
                if websocket in conns:
                    conns.remove(websocket)
                    logger.info(f"Disconnected ws for {symbol}, remaining={len(conns)}")

    async def broadcast_market(self, symbol: str, message: dict):
        conns = self.market_conns.get(symbol, set()).copy()
        if not conns:
            return
        logger.debug(f"Broadcast market {symbol} to {len(conns)} clients")
        for ws in list(conns):
            try:
                await ws.send_json({"type": "l2_update", **message})
            except Exception:
                self.disconnect(ws)

    async def broadcast_trade(self, symbol: str, message: dict):
        conns = self.trade_conns.get(symbol, set()).copy()
        if not conns:
            return
        logger.debug(f"Broadcast trade {symbol} to {len(conns)} clients")
        for ws in list(conns):
            try:
                await ws.send_json({"type": "trade", **message})
            except Exception:
                self.disconnect(ws)

manager = ConnectionManager()