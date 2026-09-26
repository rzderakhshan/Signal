from scanner import evaluate_fundamental_context


def profile(score, catalyst=0, available=True):
    return {"available": available, "score": score, "grade": "GOOD", "catalyst_score": catalyst}


def test_strong_fundamentals_support_buy():
    r = evaluate_fundamental_context(profile(78), "BUY")
    assert r["contribution"] == 10
    assert "fundamental_quality_supports_buy" in r["evidence"]


def test_positive_catalyst_adds_buy_confirmation():
    r = evaluate_fundamental_context(profile(78, 55), "BUY")
    assert r["contribution"] == 15
    assert "positive_catalyst_supports_buy" in r["evidence"]


def test_strong_fundamentals_conflict_with_sell():
    r = evaluate_fundamental_context(profile(80, 0), "SELL")
    assert r["contribution"] == -10
    assert "strong_fundamentals_conflict_with_sell" in r["conflicts"]


def test_weak_fundamentals_support_sell():
    r = evaluate_fundamental_context(profile(30), "SELL")
    assert r["contribution"] == 10
    assert "weak_fundamentals_support_sell" in r["evidence"]


def test_fundamentals_never_create_direction():
    r = evaluate_fundamental_context(profile(95, 90), "NEUTRAL")
    assert r["contribution"] == 0
    assert r["evidence"] == []


def test_crypto_or_missing_profile_is_neutral():
    r = evaluate_fundamental_context(None, "BUY")
    assert r["contribution"] == 0
    assert r["evidence"] == []
