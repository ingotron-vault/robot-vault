from processor.extractors import extract_xyz


def test_extract_json_flat_xyz():
    data = b'{"x":1.5,"y":-2,"z":3}'
    x, y, z = extract_xyz("custom", "/t", "json", data)
    assert (x, y, z) == (1.5, -2.0, 3.0)


def test_extract_image_metadata():
    data = b'{"width":640,"height":480}'
    x, y, z = extract_xyz("sensor_msgs/msg/Image", "/camera", "json", data)
    assert (x, y) == (640.0, 480.0)
    assert z is None


def test_extract_pointcloud2_metadata():
    data = b'{"width":1024,"height":64}'
    x, y, z = extract_xyz("sensor_msgs/msg/PointCloud2", "/points", "json", data)
    assert (x, y) == (1024.0, 64.0)
    assert z is None




# Non-finite values. Ways this can fail: a NaN or infinite field passes
# through as a float, so a QA ratio built from it becomes NaN, the score
# ramp reads NaN as "good", and the stored report holds JSON that is not
# valid (`NaN`). The extractor must drop non-finite values, as
# `typed_extractors._float` does.


def test_non_finite_twist_values_become_none():
    data = b'{"linear":{"x":NaN,"y":Infinity,"z":-Infinity}}'
    assert extract_xyz("geometry_msgs/msg/Twist", "/cmd_vel", "json", data) == (
        None,
        None,
        None,
    )


def test_non_finite_flat_xyz_becomes_none():
    data = b'{"x":NaN,"y":1,"z":2}'
    assert extract_xyz("custom", "/t", "json", data) == (None, 1.0, 2.0)
