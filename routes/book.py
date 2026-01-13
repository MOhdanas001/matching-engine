from fastapi import APIRouter, HTTPException
from core.order import Order, OrderSubmission, D
from core.orderbook import OrderBook
from core.manager import manager
from core.storage import books, stop_orders, get_or_create_book, on_trade_callback
from utils.logger import logger



router = APIRouter()


@router.get("/book/{symbol}")
async def get_book(symbol: str, depth: int = 10):
    book = get_or_create_book(symbol)
    depth_data=book.get_depth(depth)
    stops = stop_orders.get(symbol, [])
    stop_data = [
        {
            "order_id": o.id,
            "side": o.side,
            "quantity": str(o.remaining),
            "trigger_price": str(o.price),
            "order_type": o.order_type
        }
        for o in stops
    ]
    return {
        "symbol": symbol,
        "order_book": depth_data,
        "stop_orders": stop_data
    } 


@router.get("/bbo/{symbol}")
async def get_bbo(symbol: str):
    book = get_or_create_book(symbol)
    return book.get_bbo()




# ----------------------------
# Simple demo helper to create initial liquidity (optional)
# ----------------------------
@router.post("/demo/fill")
async def demo_fill(symbol: str, bids: int = 5, asks: int = 5):
    """
    Create some demo resting limit orders (useful for manual testing)
    """
    book = get_or_create_book(symbol)
    # create bids descending
    mid = D("30000")
    spread = D("50")
    for i in range(bids):
        p = mid - (i + 1) * D("10")
        o = Order.create(symbol, "sell" if False else "buy", "limit", D("0.1"), p)
        await book.submit_order(o, on_trade_callback=on_trade_callback)
    for i in range(asks):
        p = mid + (i + 1) * D("10")
        o = Order.create(symbol, "sell", "limit", D("0.1"), p)
        await book.submit_order(o, on_trade_callback=on_trade_callback)
    snapshot = book.get_depth(10)
    await manager.broadcast_market(symbol, snapshot)
    return {"status": "ok", "bbo": book.get_bbo()}

