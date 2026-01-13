import asyncio
from decimal import Decimal
import pytest

from core.orderbook import OrderBook
from core.order import Order, D


def make_order(symbol, side, otype, qty, price=None):
    """Helper to create any type of order."""
    return Order.create(
        symbol=symbol,
        side=side,
        order_type=otype,
        quantity=D(qty),
        price=(D(price) if price else None),
    )


@pytest.mark.asyncio
async def test_limit_order_matching():
    """Test that limit orders rest and match correctly."""
    book = OrderBook("LIM-USD")

    # Resting sells
    s1 = make_order("LIM-USD", "sell", "limit", "1", "101")
    s2 = make_order("LIM-USD", "sell", "limit", "1", "102")
    await book.submit_order(s1)
    await book.submit_order(s2)

    # Buy at 101 should fill exactly 1
    b1 = make_order("LIM-USD", "buy", "limit", "1", "101")
    res = await book.submit_order(b1)

    assert res["status"] in ("filled", "partial")
    assert sum(Decimal(t["quantity"]) for t in res["trades"]) == Decimal("1")


@pytest.mark.asyncio
async def test_market_order_fill():
    """Test that market orders consume best available liquidity."""
    book = OrderBook("MKT-USD")

    await book.submit_order(make_order("MKT-USD", "sell", "limit", "1", "100"))
    await book.submit_order(make_order("MKT-USD", "sell", "limit", "1", "101"))

    taker = make_order("MKT-USD", "buy", "market", "1.5")
    res = await book.submit_order(taker)

    assert res["status"] in ("filled", "partial")
    assert sum(Decimal(t["quantity"]) for t in res["trades"]) == Decimal("1.5")


@pytest.mark.asyncio
async def test_ioc_behavior():
    """Test Immediate-Or-Cancel (IOC) orders."""
    book = OrderBook("IOC-USD")

    # One resting sell
    await book.submit_order(make_order("IOC-USD", "sell", "limit", "1", "200"))

    # IOC buy for 2 @ 200 (limit price)
    ioc = make_order("IOC-USD", "buy", "ioc", "2", "200")
    res = await book.submit_order(ioc)

    # Should fill 1 and cancel remaining
    assert res["status"] in ("partial", "canceled")
    assert sum(Decimal(t["quantity"]) for t in res["trades"]) == Decimal("1")


@pytest.mark.asyncio
async def test_fok_behavior_not_fillable():
    """Test Fill-Or-Kill (FOK) order cancels if not fully matchable."""
    book = OrderBook("FOK-USD")

    await book.submit_order(make_order("FOK-USD", "sell", "limit", "1", "300"))

    # FOK buy 2 @ 300 (not enough liquidity)
    fok = make_order("FOK-USD", "buy", "fok", "2", "300")
    res = await book.submit_order(fok)

    assert res["status"] == "canceled"
    assert len(res["trades"]) == 0


@pytest.mark.asyncio
async def test_fok_behavior_fillable():
    """Test Fill-Or-Kill (FOK) order fills completely if possible."""
    book = OrderBook("FOK2-USD")

    await book.submit_order(make_order("FOK2-USD", "sell", "limit", "1", "100"))
    await book.submit_order(make_order("FOK2-USD", "sell", "limit", "1", "101"))

    fok = make_order("FOK2-USD", "buy", "fok", "2", "102")
    res = await book.submit_order(fok)

    assert res["status"] == "filled"
    assert sum(Decimal(t["quantity"]) for t in res["trades"]) == Decimal("2")


@pytest.mark.asyncio
async def test_stoploss_trigger_simulation():
    """Test stoploss converts to market when trigger price is hit."""
    book = OrderBook("STOP-USD")

    # Add resting sell at 100
    await book.submit_order(make_order("STOP-USD", "sell", "limit", "1", "100"))

    # Create a stoploss buy order (price=stop trigger)
    stop_order = make_order("STOP-USD", "buy", "stoploss", "1", "101")

    # Before trigger — just stored as pending (simulate)
    pending_stop_orders = [stop_order]
    assert len(pending_stop_orders) == 1

    # Simulate price reaching 101
    last_traded_price = Decimal("101")
    if last_traded_price >= stop_order.price:
        # Convert stoploss → market order
        stop_order.order_type = "market"
        res = await book.submit_order(stop_order)
    else:
        res = {"status": "inactive"}

    assert res["status"] in ("filled", "partial")
    assert any(t["quantity"] for t in res["trades"])
