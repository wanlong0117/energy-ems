"""schemas.py 验证：点位硬校验、区间重叠与请求模型边界。"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from helpers import create_device, create_point
from schemas import (
    DeviceIn, EnergyCategoryIn, MeterIn, MeterPointIn, PointIn, PointValidator,
    TariffIn, fail, ok, validate_point,
)


def test_reg_offset_boundaries(isolated_db):
    assert PointValidator.validate_reg_offset(0) == 0
    assert PointValidator.validate_reg_offset(65535) == 65535
    for bad in (-1, 65536, "abc"):
        with pytest.raises(ValueError):
            PointValidator.validate_reg_offset(bad)


def test_reg_count_boundaries(isolated_db):
    assert PointValidator.validate_reg_count(1) == 1
    assert PointValidator.validate_reg_count(125) == 125
    for bad in (0, 126, -1, "abc"):
        with pytest.raises(ValueError):
            PointValidator.validate_reg_count(bad)


@pytest.mark.parametrize(
    "data_type,count,valid",
    [
        ("u16", 1, True), ("s16", 1, True),
        ("u32", 2, True), ("s32", 2, True), ("f32", 2, True), ("u64", 4, True),
        ("u16", 2, False), ("f32", 4, False), ("u64", 2, False), ("foo", 1, False),
    ],
)
def test_type_count_compatibility(isolated_db, data_type, count, valid):
    if valid:
        PointValidator.validate_type_count(data_type, count)
    else:
        with pytest.raises(ValueError):
            PointValidator.validate_type_count(data_type, count)


def test_check_overlap_rejects_overlap_but_accepts_adjacent(isolated_db):
    device_id = create_device()
    first = create_point(device_id, reg_offset=0, reg_count=2)
    create_point(device_id, reg_offset=10, reg_count=2, name="第二点")

    with pytest.raises(ValueError, match="重叠"):
        PointValidator.check_overlap(device_id, 1, 3, 1, 2)
    with pytest.raises(ValueError, match="重叠"):
        PointValidator.check_overlap(device_id, 1, 3, 9, 3)

    # 首尾相接不算重叠
    PointValidator.check_overlap(device_id, 1, 3, 2, 2)
    # 不同从站 / 不同功能码不构成冲突
    PointValidator.check_overlap(device_id, 2, 3, 1, 2)
    PointValidator.check_overlap(device_id, 1, 4, 1, 2)
    # 更新自身时排除自己
    PointValidator.check_overlap(device_id, 1, 3, 0, 2, exclude_id=first)


@pytest.mark.parametrize(
    "overrides",
    [
        {"reg_offset": -1}, {"reg_offset": 65536},
        {"reg_count": 0}, {"reg_count": 126},
        {"reg_count": 4, "data_type": "u16"},
        {"func_code": 5}, {"byte_order": "AB"}, {"byte_order": "XYZW"},
    ],
)
def test_validate_point_rejects_invalid_payload(isolated_db, overrides):
    device_id = create_device()
    body = {
        "device_id": device_id, "slave_id": 1, "func_code": 3,
        "reg_offset": 100, "reg_count": 2, "data_type": "u32", "byte_order": "CDAB",
    }
    body.update(overrides)
    with pytest.raises(ValueError):
        validate_point(body)


def test_validate_point_accepts_valid_payload(isolated_db):
    device_id = create_device()
    body = {
        "device_id": device_id, "slave_id": 1, "func_code": 4,
        "reg_offset": 65534, "reg_count": 2, "data_type": "f32", "byte_order": "CDAB",
    }
    validate_point(body)  # 不应抛异常


def test_request_models_enforce_field_rules(isolated_db):
    valid_point = PointIn(name="P", device_id=1, slave_id=1, reg_offset=0,
                          reg_count=2, data_type="f32", byte_order="cdab")
    assert valid_point.byte_order == "CDAB"
    assert valid_point.data_type == "f32"

    for model, payload in [
        (PointIn, dict(name="P", device_id=1, slave_id=1, reg_offset=0,
                       reg_count=2, data_type="f32", byte_order="AB")),
        (DeviceIn, dict(name="GW", host="127.0.0.1", protocol="http")),
        (DeviceIn, dict(name="GW", host="127.0.0.1", port=70000)),
        (MeterIn, dict(name="M", node_id=1, category_id=1, purpose="unknown")),
        (MeterPointIn, dict(meter_id=1, point_id=1, role="bad")),
        (TariffIn, dict(name="T", price_cent=-1)),
        (EnergyCategoryIn, dict(name="电")),
    ]:
        with pytest.raises(ValidationError):
            model(**payload)


def test_unified_response_helpers(isolated_db):
    assert ok() == {"ok": True}
    assert ok([1, 2]) == {"ok": True, "data": [1, 2]}
    response = fail("出错了", 404)
    assert response.status_code == 404
    assert json.loads(response.body) == {"ok": False, "error": "出错了"}
