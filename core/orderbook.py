import asyncio
from collections import deque
from decimal import Decimal
import uuid
from sortedcontainers import SortedDict
from typing import Deque, Dict, Optional
from core.order import Order, D, now_iso
from utils.logger import logger

class PriceLevel:
    def __init__(self):
        self.queue: Deque[Order] = deque()
        self.total: Decimal = D("0")

    def add(self, order: Order):
        self.queue.append(order)
        self.total += order.remaining

    def pop_oldest(self) -> Order:
        order = self.queue[0]
        return order

    def remove_oldest(self) -> Order:
        order = self.queue.popleft()
        self.total -= order.remaining
        return order

    def decrease_oldest(self, amount: Decimal):
        # decrease remaining and total by amount
        assert self.queue
        oldest = self.queue[0]
        if amount >= oldest.remaining:
            self.total -= oldest.remaining
            oldest.remaining = D("0")
            self.queue.popleft()
        else:
            oldest.remaining -= amount
            self.total -= amount

class OrderBook:
    def __init__(self, symbol: str):
        self.symbol = symbol
        # SortedDict sorts ascending by key
        self.asks: SortedDict = SortedDict()  # price -> PriceLevel (lowest first)
        self.bids: SortedDict = SortedDict()  # price -> PriceLevel (lowest first) -> iterate in reverse for best bid
        self.orders: Dict[str, Order] = {}  # id -> Order
        self.lock = asyncio.Lock()
        self.trade_seq = 0

    # helper to ensure price level exists
    def _ensure_level(self, container: SortedDict, price: Decimal) -> PriceLevel:
        if price not in container:
            container[price] = PriceLevel()
        return container[price]

    def _remove_price_if_empty(self, container: SortedDict, price: Decimal):
        lvl = container.get(price)
        if not lvl or len(lvl.queue) == 0:
            if price in container:
                del container[price]

    def get_bbo(self) -> Dict:
        best_bid = None
        best_ask = None
        if len(self.bids) > 0:
            # best bid is last key (highest price)
            bprice, blevel = self.bids.peekitem(-1)
            best_bid = (str(bprice), str(blevel.total))
        if len(self.asks) > 0:
            aprice, alevel = self.asks.peekitem(0)
            best_ask = (str(aprice), str(alevel.total))
        return {"symbol": self.symbol, "best_bid": best_bid, "best_ask": best_ask, "timestamp": now_iso()}

    def get_depth(self, depth: int = 10) -> Dict:
        asks = []
        bids = []
        # asks: lowest -> higher
        for i, (price, lvl) in enumerate(self.asks.items()):
            if i >= depth:
                break
            asks.append([str(price), str(lvl.total)])
        # bids: highest -> lower
        # iterate reversed
        n = 0
        for price in reversed(self.bids.keys()):
            if n >= depth:
                break
            lvl = self.bids[price]
            bids.append([str(price), str(lvl.total)])
            n += 1
        return {"symbol": self.symbol, "asks": asks, "bids": bids, "timestamp": now_iso()}

    def _opposite_book(self, side: str) -> SortedDict:
        return self.asks if side == "buy" else self.bids

    def _same_book(self, side: str) -> SortedDict:
        return self.bids if side == "buy" else self.asks

    def _best_price_for_side(self, side: str) -> Optional[Decimal]:
        if side == "buy":
            if len(self.bids) == 0:
                return None
            return self.bids.peekitem(-1)[0]
        else:
            if len(self.asks) == 0:
                return None
            return self.asks.peekitem(0)[0]
        
    async def submit_order(self, order: Order, on_trade_callback=None) -> Dict:
        """
        Submit an order. Returns dict describing order status and trades list (if any).
        on_trade_callback(trade_dict) will be called for each executed trade (async or sync).
        """
        
        
        async with self.lock:
            logger.info(f"Submitting order {order.id} {order.side} {order.order_type} {order.quantity}@{order.price}")
            trades = []

            MAKER_FEE_RATE = D("-0.0002")  # -0.02% rebate
            TAKER_FEE_RATE = D("0.0010")   # 0.1% fee

            # helpers
            def make_trade(price: Decimal, qty: Decimal, maker: Order, taker: Order, aggressor_side: str):
                self.trade_seq += 1
                trade_value=price*qty
                maker_fee = trade_value * MAKER_FEE_RATE
                taker_fee = trade_value * TAKER_FEE_RATE
                trade = {
                    "timestamp": now_iso(),
                    "symbol": self.symbol,
                    "trade_id": f"{self.symbol}-{self.trade_seq}-{uuid.uuid4()}",
                    "price": str(price),
                    "quantity": str(qty),
                    "trade_value": str(trade_value),
                    "aggressor_side": aggressor_side,
                    "maker_order_id": maker.id,
                    "taker_order_id": taker.id,
                    "maker_fee": str(maker_fee),
                    "taker_fee": str(taker_fee),
                }
                return trade

            is_market = order.order_type == "market"

            # ----------------------------
            # FOK pre-check: ensure fillable at acceptable prices only
            # ----------------------------
            if order.order_type == "fok":
                total = D("0")
                opp = self._opposite_book(order.side)

                if len(opp) == 0:
                    logger.info(f"FOK order {order.id} cannot be filled (no liquidity) -> canceled")
                    return {"order_id": order.id, "status": "canceled", "reason": "fok_not_fillable", "trades": []}

                if is_market:
                    # market FOK: sum entire opposite book
                    for p, lvl in opp.items():
                        total += lvl.total
                else:
                    # limit FOK: sum only levels that are acceptable to the taker price
                    if order.side == "buy":
                        # accept asks with price <= order.price
                        for p, lvl in opp.items():
                            if p > order.price:
                                break
                            total += lvl.total
                    else:
                        # sell: accept bids with price >= order.price
                        for p in reversed(opp.keys()):
                            if p < order.price:
                                break
                            total += opp[p].total

                logger.debug(f"FOK pre-check total available={total} for required {order.quantity}")
                if total < order.quantity:
                    logger.info(f"FOK order {order.id} cannot be filled fully -> canceled")
                    return {"order_id": order.id, "status": "canceled", "reason": "fok_not_fillable", "trades": []}

            # ----------------------------
            # Matching loop (price-time priority)
            # ----------------------------
            opp = self._opposite_book(order.side)
            same = self._same_book(order.side)

            while order.remaining > D("0"):
                if len(opp) == 0:
                    # no liquidity
                    break

                # determine best opposite price & level
                if order.side == "buy":
                    # best ask is lowest price
                    best_price, best_level = opp.peekitem(0)
                    price_acceptable = True if is_market else (best_price <= order.price)
                else:
                    # best bid is highest price (peekitem(-1))
                    best_price, best_level = opp.peekitem(-1)
                    price_acceptable = True if is_market else (best_price >= order.price)

                if not price_acceptable:
                    # best price not acceptable -> cannot match further
                    break

                # match against resting orders at this price level (FIFO)
                while order.remaining > D("0") and len(best_level.queue) > 0:
                    resting = best_level.queue[0]  # oldest resting order at this level
                    trade_qty = min(order.remaining, resting.remaining)
                    exec_price = resting.price if resting.price is not None else best_price
                    trade = make_trade(exec_price, trade_qty, resting, order, aggressor_side=order.side)

                    # update quantities
                    order.remaining -= trade_qty
                    resting.remaining -= trade_qty
                    best_level.total -= trade_qty

                    # if resting fully filled, remove it
                    if resting.remaining == D("0"):
                        best_level.queue.popleft()
                        if resting.id in self.orders:
                            del self.orders[resting.id]

                    # record and send trade
                    trades.append(trade)
                    logger.info(f"Trade executed: {trade}")
                    if on_trade_callback:
                        if asyncio.iscoroutinefunction(on_trade_callback):
                            await on_trade_callback(trade)
                        else:
                            on_trade_callback(trade)

                # if level empty remove price level
                if len(best_level.queue) == 0:
                    try:
                        del opp[best_price]
                    except Exception:
                        pass

                # continue while loop to attempt next price level if still remaining

            # ----------------------------
            # Post-match handling for IOC / FOK / market / limit
            # ----------------------------

            # IOC: do not rest any remainder; any unfilled portion is canceled
            if order.order_type == "ioc":
                if order.remaining > D("0"):
                    status = "partial" if len(trades) > 0 else "canceled"
                    logger.info(f"IOC order {order.id} completed with status={status}, remaining canceled")
                    return {"order_id": order.id, "status": status, "trades": trades}
                else:
                    # fully filled
                    logger.info(f"IOC order {order.id} fully filled")
                    return {"order_id": order.id, "status": "filled", "trades": trades}

            # FOK: should have been pre-checked; if any remainder exists here it's an unexpected condition.
            if order.order_type == "fok":
                if order.remaining > D("0"):
                    # Defensive: if we somehow couldn't fill after pre-check, rollback (not implemented)
                    logger.info(f"FOK order {order.id} unexpectedly not fully filled -> cancel (no partial fills permitted)")
                    # In this implementation we will cancel and return trades (but ideally would rollback trades)
                    return {"order_id": order.id, "status": "canceled", "trades": []}
                else:
                    logger.info(f"FOK order {order.id} fully filled")
                    return {"order_id": order.id, "status": "filled", "trades": trades}

            # If remaining > 0 and it is limit order -> place remaining on the book
            if order.remaining > D("0") and order.order_type == "limit":
                price = order.price
                level = self._ensure_level(same, price)
                level.add(order)
                self.orders[order.id] = order
                status = "resting" if len(trades) == 0 else "partial"
                logger.info(f"Limit order {order.id} resting on book {order.side} {order.remaining}@{price}")
                return {"order_id": order.id, "status": status, "trades": trades}

            # Market orders: any remaining quantity after consuming book is canceled (market cannot rest)
            if order.remaining > D("0"):
                status = "partial" if len(trades) > 0 else "canceled"
                logger.info(f"Market order {order.id} leftover -> status {status}")
                return {"order_id": order.id, "status": status, "trades": trades}

            # If we reach here, order fully filled
            logger.info(f"Order {order.id} fully filled")
            return {"order_id": order.id, "status": "filled", "trades": trades}

