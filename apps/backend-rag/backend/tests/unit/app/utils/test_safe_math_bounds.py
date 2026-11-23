from backend.app.utils.safe_math import SafeMathError, safe_evaluate


def test_rejects_power_operands_outside_bounds():
    for expression in (
        "2**101",
        "2**-101",
        "(10**16)**2",
        "(2**100)**2",
    ):
        try:
            safe_evaluate(expression)
        except SafeMathError:
            continue
        raise AssertionError(f"Expected SafeMathError for {expression}")


def test_rejects_expression_longer_than_200_characters():
    expression = "1+" * 100 + "1"

    try:
        safe_evaluate(expression)
    except SafeMathError:
        return
    raise AssertionError("Expected SafeMathError for an overlong expression")


def test_allows_power_operands_at_the_bounds():
    for expression, expected in (
        ("2**100", 2**100),
        ("1.05**30", 1.05**30),
        ("(10**15)**2", (10**15) ** 2),
    ):
        assert safe_evaluate(expression) == expected


def test_allows_existing_arithmetic_operators():
    for expression, expected in (
        ("1500000000 * 0.11", 1500000000 * 0.11),
        ("-2**3", -8),
        ("7 // 2 + 7 % 2", 4),
    ):
        assert safe_evaluate(expression) == expected


def test_allows_expression_of_exactly_200_characters():
    expression = "1+" * 99 + "10"

    assert len(expression) == 200
    assert safe_evaluate(expression) == 109
