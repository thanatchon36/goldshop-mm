import pytest

from goldshop.execution.orders import (
    ClientIdGenerator,
    InvalidTransition,
    Order,
    OrderStatus,
    Side,
)


def test_happy_path_new_partial_fill():
    o = Order("x", Side.BUY, 100.0, 2.0)
    o.transition(OrderStatus.NEW)
    o.apply_fill(0.5)
    assert o.status is OrderStatus.PARTIALLY_FILLED and o.remaining == 1.5
    o.apply_fill(1.5)
    assert o.status is OrderStatus.FILLED and o.is_terminal


def test_fill_during_pending_cancel_keeps_pending_cancel_then_cancels():
    o = Order("x", Side.SELL, 100.0, 2.0, status=OrderStatus.NEW)
    o.transition(OrderStatus.PENDING_CANCEL)
    o.apply_fill(1.0)
    assert o.status is OrderStatus.PENDING_CANCEL and not o.is_working
    o.transition(OrderStatus.CANCELED)
    assert o.is_terminal


def test_fill_can_win_race_against_cancel():
    o = Order("x", Side.SELL, 100.0, 1.0, status=OrderStatus.PENDING_CANCEL)
    o.apply_fill(1.0)
    assert o.status is OrderStatus.FILLED


@pytest.mark.parametrize("terminal", [OrderStatus.FILLED, OrderStatus.CANCELED,
                                      OrderStatus.REJECTED, OrderStatus.EXPIRED])
def test_terminal_states_are_final(terminal):
    o = Order("x", Side.BUY, 100.0, 1.0, status=terminal)
    for s in OrderStatus:
        with pytest.raises(InvalidTransition):
            o.transition(s)
    with pytest.raises(InvalidTransition):
        o.apply_fill(0.1)


def test_invalid_transitions():
    o = Order("x", Side.BUY, 100.0, 1.0, status=OrderStatus.NEW)
    with pytest.raises(InvalidTransition):
        o.transition(OrderStatus.PENDING_NEW)
    with pytest.raises(InvalidTransition):
        o.transition(OrderStatus.REJECTED)


def test_overfill_rejected():
    o = Order("x", Side.BUY, 100.0, 1.0, status=OrderStatus.NEW)
    with pytest.raises(ValueError):
        o.apply_fill(1.5)


def test_client_ids_deterministic_and_binance_safe():
    a, b = ClientIdGenerator("gs1"), ClientIdGenerator("gs1")
    ids_a = [a.next(Side.BUY), a.next(Side.SELL)]
    ids_b = [b.next(Side.BUY), b.next(Side.SELL)]
    assert ids_a == ids_b == ["gs1-b-1", "gs1-s-2"]
    assert ClientIdGenerator("gs1", start=41).next(Side.BUY) == "gs1-b-42"
    with pytest.raises(ValueError):
        ClientIdGenerator("bad prefix!")
