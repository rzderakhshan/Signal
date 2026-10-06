import whale_tracker as wt


def test_positions_from_state():
    data = {"assetPositions": [
        {"position": {"coin":"BTC","szi":"2","entryPx":"80000","positionValue":"165000","unrealizedPnl":"5000","liquidationPx":"70000","leverage":{"value":10}}},
        {"position": {"coin":"ETH","szi":"-3","entryPx":"2500","positionValue":"7800","unrealizedPnl":"-100","liquidationPx":"3100","leverage":{"value":5}}},
    ]}
    p = wt.positions_from_state(data)
    assert p["BTC"]["side"] == "LONG"
    assert p["ETH"]["side"] == "SHORT"


def test_consensus():
    allp = {
        "A":{"BTC":{"side":"LONG","value":100}},
        "B":{"BTC":{"side":"LONG","value":200}},
        "C":{"BTC":{"side":"SHORT","value":300}},
    }
    old = wt.CONSENSUS_MIN_WHALES
    wt.CONSENSUS_MIN_WHALES = 2
    try:
        c = wt.consensus_map(allp)
        assert c["BTC:LONG"]["count"] == 2
        assert c["BTC:LONG"]["notional"] == 300
    finally:
        wt.CONSENSUS_MIN_WHALES = old
