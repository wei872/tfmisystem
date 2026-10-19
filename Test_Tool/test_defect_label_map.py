# -*- coding: utf-8 -*-
"""defectType 标识的跨路径一致性。

疵点上报有**两条独立路径**，各自维护一份「中文类名 -> 英文标识」的映射：

  YOLO 检测路径   AnomalyDetection_Tool/output/detection_info_formatter.py
                  DEFECT_LABEL_MAP  ->  map_defect_type(class_name)
  经纬线分析路径   AnomalyDetection_Tool/services/warp_weft_stop_handler.py
                  STOP_TYPE_TO_DEFECT（stop_type -> 标识）

同一个疵点在两条路径上必须上报**同一个 defectType**，否则后端会把同一种
缺陷统计成两种。这份测试就是钉住这件事的。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from AnomalyDetection_Tool.output.detection_info_formatter import (  # noqa: E402
    DEFECT_LABEL_MAP, map_defect_type)
from AnomalyDetection_Tool.services.warp_weft_stop_handler import (  # noqa: E402
    STOP_TYPE_TO_DEFECT)

# 前端/模型那份 19 类疵点清单
CLASSES_19 = [
    "纬线杂质", "断纬", "多股线", "纬线不直", "包边不重叠", "捻接器接头",
    "小尾巴", "经线毛球", "包边不良", "手工接头", "纬斜", "短废纱线",
    "单根包不进", "棉团", "蚊虫小黑点", "小布头", "稀缝", "油点线", "飘絮",
]

# 中文类名 -> 经纬线路径的 stop_type（只列两边都覆盖的疵点）
SHARED = {
    "断纬": "broken_weft",
    "稀缝": "warp",
    "纬斜": "lat_slope",
}


class TestCrossPathConsistency:
    """两条路径对同一个疵点必须给出同一个标识。"""

    @pytest.mark.parametrize("cls", sorted(SHARED))
    def test_shared_defect_agrees(self, cls):
        yolo = DEFECT_LABEL_MAP[cls]
        ww = STOP_TYPE_TO_DEFECT[SHARED[cls]]
        assert yolo == ww, (
            "%s 在两条路径上标识不一致：YOLO=%r 经纬线=%r" % (cls, yolo, ww))

    def test_duanwei_is_weft_broken(self):
        """断纬 -> weft_broken。

        曾经差点被当成「缺纬 改了名」而映射成 lack_lat。那是错的：
        断纬（纬线断裂成根）和 缺纬（纬线间距过大）是两种不同的疵点，
        由 warp_weft_analysis_tasks.py 各自独立产生。
        """
        assert DEFECT_LABEL_MAP["断纬"] == "weft_broken"
        assert DEFECT_LABEL_MAP["断纬"] == STOP_TYPE_TO_DEFECT["broken_weft"]

    def test_duanwei_and_quewei_are_distinct(self):
        """两者必须是不同的标识 —— 合并就等于把两种疵点混成一种。"""
        assert "缺纬" in DEFECT_LABEL_MAP
        assert DEFECT_LABEL_MAP["断纬"] != DEFECT_LABEL_MAP["缺纬"]
        assert (STOP_TYPE_TO_DEFECT["broken_weft"]
                != STOP_TYPE_TO_DEFECT["weft"])

    def test_warp_weft_map_is_unchanged(self):
        """经纬线那份是后端已在用的契约，不能被顺手改动。"""
        assert STOP_TYPE_TO_DEFECT == {
            "weft": "missing_latitude",
            "broken_weft": "weft_broken",
            "warp": "thin_seam",
            "lat_slope": "lat_slope",
        }


class TestCoverage:
    """19 类里哪些有英文标识、哪些没有 —— 没有的会原样上报中文名。"""

    def test_known_gap_is_exactly_two(self):
        """手工接头 / 小布头 仍缺映射（现场确认前不动）。

        map_defect_type() 查不到时**原样返回中文名**，所以这两类上报的
        defectType 是中文。后端若不接受，补上映射即可。
        """
        missing = [c for c in CLASSES_19 if c not in DEFECT_LABEL_MAP]
        assert missing == ["手工接头", "小布头"], missing

    def test_other_17_are_mapped(self):
        mapped = [c for c in CLASSES_19 if c in DEFECT_LABEL_MAP]
        assert len(mapped) == 17, mapped

    def test_fallback_returns_chinese(self):
        assert map_defect_type("手工接头") == "手工接头"
        assert map_defect_type("小布头") == "小布头"

    def test_known_divergence_quewei(self):
        """缺纬 在两条路径上标识不同，是**已知且刻意保留**的。

        YOLO 路径 lack_lat，经纬线路径 missing_latitude（后端实际下发的
        是后者）。缺纬 不在这 19 类里、YOLO 路径大概率走不到，现场确认前
        先不动。这里把它钉住，免得有人"顺手统一"时改错方向。
        """
        assert DEFECT_LABEL_MAP["缺纬"] == "lack_lat"
        assert STOP_TYPE_TO_DEFECT["weft"] == "missing_latitude"

    def test_all_identifiers_are_unique(self):
        """一个标识不能同时代表两种疵点。"""
        vals = list(DEFECT_LABEL_MAP.values())
        dup = {v for v in vals if vals.count(v) > 1}
        assert not dup, dup
        assert len(set(vals)) == len(vals)

    def test_identifiers_are_ascii_snake_case(self):
        """上传字段是英文标识，不能混进中文或空格。"""
        for k, v in DEFECT_LABEL_MAP.items():
            assert v.isascii(), (k, v)
            assert " " not in v, (k, v)
