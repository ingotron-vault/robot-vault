from query_service.routers.query_api import _bounded_count_bucket


def test_bounded_count_bucket_uses_stable_numeric_labels():
    assert _bounded_count_bucket(0) == "<=0"
    assert _bounded_count_bucket(7) == "<=10"
    assert _bounded_count_bucket(100_001) == ">100000"
    assert "$" not in _bounded_count_bucket(7)
