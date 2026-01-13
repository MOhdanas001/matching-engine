
from typing import Dict, List
from core.order import D, Order
from core.orderbook import OrderBook
import asyncio
from core.manager import manager


books: Dict[str, OrderBook] = {}
stop_orders: Dict[str, List[Order]] = {}




def get_or_create_book(symbol: str) -> OrderBook:
    if symbol not in books:
        books[symbol] = OrderBook(symbol)
    return books[symbol]


# callback for trades to broadcast
async def on_trade_callback(trade: dict):
    book = books.get(trade["symbol"])
    if book:
        # send to trade subscribers
        await manager.broadcast_trade(trade["symbol"], trade)
        # also send updated market snapshot
        snapshot = book.get_depth(10)
        await manager.broadcast_market(trade["symbol"], snapshot)


        triggered_orders = []
        if trade["symbol"] in stop_orders:
            for stop_order in list(stop_orders[trade["symbol"]]):
                trigger_price = stop_order.price
                trade_price = D(trade["price"])
                # Buy stop triggers if trade price >= stop price
                # Sell stop triggers if trade price <= stop price
                if (stop_order.side == "buy" and trade_price >= trigger_price) or \
                   (stop_order.side == "sell" and trade_price <= trigger_price):
                    triggered_orders.append(stop_order)

            for o in triggered_orders:
                stop_orders[trade["symbol"]].remove(o)
                o.order_type = "market"
                book = get_or_create_book(o.symbol)
                asyncio.create_task(book.submit_order(o, on_trade_callback=on_trade_callback))
