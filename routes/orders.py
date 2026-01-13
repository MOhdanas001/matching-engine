from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from core.order import Order, OrderSubmission, D
from core.orderbook import OrderBook
from core.manager import manager
from core.storage import books, stop_orders, get_or_create_book, on_trade_callback
from utils.logger import logger
from pydantic import BaseModel
from typing import Optional


router = APIRouter()

@router.post("/orders")
async def submit_order(payload: OrderSubmission):
    # validate & create Order
    print("order received")
    symbol = payload.symbol
    order_type = payload.order_type.lower()
    side = payload.side.lower()
    # quantity and price as Decimal
    try:
        qty = D(payload.quantity)
        if qty <= D("0"):
            raise ValueError("quantity must be positive")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid quantity: {e}")

    price = None
    if payload.price is not None:
        try:
            price = D(payload.price)
            if price <= D("0"):
                raise ValueError("price must be positive")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid price: {e}")
    
    if order_type == "stoploss":
            if price is None:
                raise HTTPException(status_code=400, detail="Stop-loss orders require a trigger price")
            order = Order.create(symbol=symbol, side=side, order_type=order_type, quantity=qty, price=price)
            stop_orders.setdefault(symbol, []).append(order)
            return {"order_id": order.id, "status": "stop_placed", "trigger_price": str(price)}
    

    if order_type == "limit" and price is None:
        raise HTTPException(status_code=400, detail="Limit orders require a price")
    
    if order_type == "ioc" and price is None:
        raise HTTPException(status_code=400, detail="ioc orders require a price, as its a limit ioc")

    if order_type == "fok" and price is None:
        raise HTTPException(status_code=400, detail="fok orders require a price, as its a limit fok")

    order = Order.create(symbol=symbol, side=side, order_type=order_type, quantity=qty, price=price)
    book = get_or_create_book(symbol)

    # process
    result = await book.submit_order(order, on_trade_callback=on_trade_callback)

    # broadcast BBO/depth even if no trades (e.g., resting order)
    snapshot = book.get_depth(10)
    await manager.broadcast_market(symbol, snapshot)

    return {"order_id": order.id, "status": result["status"], "trades": result["trades"]}



@router.delete("/order/{order_id}")
async def cancel_order(order_id: str):
    # find order in all books
    for book in books.values():
        order = book.orders.get(order_id)
        if order:
            async with book.lock:
                # remove from price level
                container = book._same_book(order.side)
                level = container.get(order.price)
                if level:
                    # remove specific order from level queue
                    for idx, o in enumerate(level.queue):
                        if o.id == order_id:
                            level.total -= o.remaining
                            level.queue.remove(o)
                            break
                    book._remove_price_if_empty(container, order.price)
                # remove from global map
                del book.orders[order_id]

                # broadcast updated book
                snapshot = book.get_depth(10)
                await manager.broadcast_market(order.symbol, snapshot)
                return {"order_id": order_id, "status": "canceled"}
    raise HTTPException(status_code=404, detail="Order not found")

@router.delete("/stoporder/{order_id}")
async def cancel_stop_order(order_id: str):
    found = False
    for symbol, orders in stop_orders.items():
        for o in list(orders):  # iterate over a copy to safely remove
            if o.id == order_id:
                orders.remove(o)
                found = True
                logger.info(f"Stop-loss order {order_id} canceled")
                break
        if found:
            break

    if not found:
        raise HTTPException(status_code=404, detail="Stop-loss order not found")

    return {"order_id": order_id, "status": "canceled"}


class ModifyOrder(BaseModel):
    quantity: Optional[str] = None
    price: Optional[str] = None

@router.put("/orders/{order_id}")
async def modify_order(order_id: str, payload: ModifyOrder):
    for book in books.values():
        order = book.orders.get(order_id)
        if order:
            async with book.lock:
                # remove old order
                container = book._same_book(order.side)
                level = container.get(order.price)
                if level:
                    for idx, o in enumerate(level.queue):
                        if o.id == order_id:
                            level.total -= o.remaining
                            level.queue.remove(o)
                            break
                    book._remove_price_if_empty(container, order.price)
                # update fields
                if payload.quantity:
                    order.quantity = D(payload.quantity)
                    order.remaining = D(payload.quantity)
                if payload.price:
                    order.price = D(payload.price)
                # reinsert as a new resting order
                level = book._ensure_level(container, order.price)
                level.add(order)
                book.orders[order_id] = order

                # broadcast updated book
                snapshot = book.get_depth(10)
                await manager.broadcast_market(order.symbol, snapshot)
                return {"order_id": order_id, "status": "modified", "new_price": str(order.price), "new_quantity": str(order.remaining)}
    raise HTTPException(status_code=404, detail="Order not found")

class ModifyStopOrder(BaseModel):
    quantity: Optional[str] = None
    price: Optional[str] = None  # the trigger price

@router.put("/stoporder/{order_id}")
async def modify_stop_order(order_id: str, payload: ModifyStopOrder):
    found = False
    for symbol, orders in stop_orders.items():
        for o in orders:
            if o.id == order_id:
                # update fields
                if payload.quantity:
                    o.quantity = D(payload.quantity)
                    o.remaining = D(payload.quantity)
                if payload.price:
                    o.price = D(payload.price)
                found = True
                logger.info(f"Stop-loss order {order_id} modified")
                return {
                    "order_id": o.id,
                    "status": "modified",
                    "new_trigger_price": str(o.price),
                    "new_quantity": str(o.remaining)
                }

    if not found:
        raise HTTPException(status_code=404, detail="Stop-loss order not found")



# ----------------------------
# WebSocket endpoints
# ----------------------------
@router.websocket("/ws/marketdata/{symbol}")
async def ws_marketdata(websocket: WebSocket, symbol: str):
    await manager.connect_market(symbol, websocket)
    try:
        while True:
            # keep connection alive; client may send ping messages
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


@router.websocket("/ws/trades/{symbol}")
async def ws_trades(websocket: WebSocket, symbol: str):
    await manager.connect_trades(symbol, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
