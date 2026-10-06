from whale_tracker import score_whale, build_consensus, Whale, signal_grade


def test_signal_grade():
    assert signal_grade(90) == "A+"
    assert signal_grade(82) == "A"
    assert signal_grade(74) == "B"
    assert signal_grade(60) == "C"


def test_score_profitable_above_loser():
    ch = {"marginSummary": {"accountValue": "1000000"}, "assetPositions": [{"position": {"coin": "BTC", "szi": "10", "positionValue": "2000000", "unrealizedPnl": "0", "entryPx": "1", "leverage": {"value": 2}}}]}
    good = [["perpWeek", {"pnlHistory": [[1, "100000"]]}], ["perpMonth", {"pnlHistory": [[1, "300000"]]}], ["perpAllTime", {"pnlHistory": [[1, "800000"]]}]]
    bad = [["perpWeek", {"pnlHistory": [[1, "-100000"]]}], ["perpMonth", {"pnlHistory": [[1, "-300000"]]}], ["perpAllTime", {"pnlHistory": [[1, "-800000"]]}]]
    assert score_whale(ch, good)[0] > score_whale(ch, bad)[0]


def test_weighted_consensus():
    whales = [Whale("w1", "0x" + "1"*40, 90, 1), Whale("w2", "0x" + "2"*40, 80, 2)]
    pos = {
        whales[0].address: {"ETH": {"side": "LONG", "value": 2_000_000}},
        whales[1].address: {"ETH": {"side": "LONG", "value": 1_000_000}},
    }
    c = build_consensus(whales, pos)["ETH:LONG"]
    assert c["count"] == 2
    assert c["weighted_share"] == 1.0

from whale_tracker import next_signal_id, close_result_pct, duration_text


def test_signal_id_sequence_and_result():
    state = {}
    a = next_signal_id(state, "MAIN", "ETH", "LONG", 1791331200000)
    b = next_signal_id(state, "MAIN", "ETH", "LONG", 1791331200000)
    assert a != b
    assert a.startswith("MAIN-ETH-LONG-")
    assert close_result_pct("LONG", 100, 110) == 10
    assert close_result_pct("SHORT", 100, 90) == 10
    assert duration_text(0, 3_660_000) == "1h 1m"


from whale_tracker import distance_pct, build_limit_clusters


def test_distance_pct():
    assert round(distance_pct(99, 100), 2) == 1.0
    assert round(distance_pct(101, 100), 2) == 1.0


def test_limit_cluster_requires_multiple_whales():
    w1 = Whale("w1", "0x" + "1"*40, 90, 1)
    w2 = Whale("w2", "0x" + "2"*40, 80, 2)
    rows = [
        {"whale": w1, "oid": "1", "coin": "BTC", "side": "LONG", "limit_px": 99000, "market_px": 100000, "notional": 400000},
        {"whale": w2, "oid": "2", "coin": "BTC", "side": "LONG", "limit_px": 99500, "market_px": 100000, "notional": 400000},
    ]
    clusters = build_limit_clusters(rows, {"BTC": 100000})
    c = clusters["BTC:LONG"]
    assert c["count"] == 2
    assert c["notional"] == 800000
