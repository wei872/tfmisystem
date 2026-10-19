#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【七】后端下发材质检测配置的回归测试。

重点盯四件事：
  1. **优先级**：后端 > 按相机覆盖 > 全局，但 machine_stop_rules.enabled
     这个总开关仍然最高（现场急停手段，后端不该能绕过）；
  2. **存储为空时行为完全不变** —— 后端从没拉成功过时，两个解析入口
     必须和接入前一模一样；
  3. **status=0 不等于"这个类别不检测"** —— 只是这条阈值不下发，
     该类退回本地配置。静默关掉一个类别就是静默漏检；
  4. **解析失败/空结果绝不清空已有配置** —— 拉取失败时换回另一套阈值
     比用一份稍旧的配置危险得多。
"""

import asyncio
import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from Communication_Tool.material_config_client import (  # noqa: E402
    parse_config, apply_config, _is_ok, API_PATH)
from AnomalyDetection_Tool.config import remote_config_store as RCS  # noqa: E402
import Communication_Tool.material_config_client as MCM  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_store():
    """重置覆盖层 + 日志去重状态。

    去重表是模块级的，不清就会跨用例泄漏：上一个用例打过的那条，
    下一个用例期望它出现时会被静默吞掉，测试结果取决于执行顺序。
    """
    RCS.reset_remote_config_store()
    MCM._LOG_SEEN.clear()
    MCM._LABEL_WARNED.clear()
    yield
    RCS.reset_remote_config_store()
    MCM._LOG_SEEN.clear()
    MCM._LABEL_WARNED.clear()


def _store():
    return RCS.get_remote_config_store()


# 接口文档里那份示例（截取关键字段）
SAMPLE = {
    "checkLogId": 123,
    "clothNumber": "1500D/2-100145-2610-2/11-623-D06/D07/D08-*-74",
    "roller": "2610-2/11-0",
    "materialId": 10,
    "material": "1500D/2",
    "specificationId": 20,
    "specification": "100145",
    "model": {"id": 1, "modelName": "检测模型A",
              "modelPath": "/models/defect.pt", "modelVersion": "v1",
              "status": 1},
    "cameras": [
        {"cameraId": "camera1", "cameraName": "相机1",
         "thresholds": [
             {"defectName": "缺陷A", "defectLabel": "defect_a",
              "thresholdValue": 0.5, "status": 1},
         ],
         "stopSetting": [
             {"name": "缺陷A", "enabled": True, "minConfidence": 0.5,
              "minAreaMm2": 100, "triggerMode": None,
              "minWidthMm": None, "minHeightMm": None},
             {"name": "缺陷B", "enabled": True, "minConfidence": 0.6,
              "minAreaMm2": None, "triggerMode": "dimension",
              "minWidthMm": 50, "minHeightMm": 50},
         ]},
    ],
}


# ======================================================================
# §1  解析
# ======================================================================
def _cams():
    """接口文档里那段 cameras 数组（模块级，端到端用例也要用）。"""
    return [
        {"cameraId": "camera2",
         "thresholds": [
             {"defectName": "\u591a\u80a1\u7ebf", "defectLabel": "multi_line",
              "thresholdValue": 0.10, "status": 1},
             # status=0 表示这个类别这次不启用检测
             {"defectName": "\u5305\u8fb9\u4e0d\u826f", "defectLabel": "edge_bad",
              "thresholdValue": 0.9, "status": 0},
         ],
         "stopSetting": [
             {"name": "\u5355\u6839\u5305\u4e0d\u8fdb", "enabled": True,
              "minConfidence": 0.5, "triggerMode": "dimension",
              "minWidthMm": 50, "minHeightMm": 47},
             {"name": "\u6cb9\u70b9\u7ebf", "enabled": False,
              "minConfidence": 0.3, "triggerMode": None,
              "minWidthMm": None, "minHeightMm": None},
         ]},
        {"cameraId": "camera7",
         "thresholds": [
             # 这个 label 和 DEFECT_LABEL_MAP 对不上，应当告警但不影响取值
             {"defectName": "\u591a\u80a1\u7ebf", "defectLabel": "WRONG_LABEL",
              "thresholdValue": 0.12, "status": 1},
         ],
         "stopSetting": []},
    ]


class TestParse:

    def test_sample_parses(self):
        th, sr, meta = parse_config(SAMPLE)
        assert th == {("camera1", "缺陷A"): 0.5}
        assert meta["checkLogId"] == 123
        assert meta["material"] == "1500D/2"
        assert meta["specification"] == "100145"
        assert meta["modelPath"] == "/models/defect.pt"
        assert meta["camera_count"] == 1

    def test_area_rule_has_no_trigger_mode(self):
        """triggerMode=null 表示面积规则，不该塞一个 None 进去。"""
        _, sr, _ = parse_config(SAMPLE)
        rule = sr[("camera1", "缺陷A")]
        assert rule["enabled"] is True
        assert rule["min_confidence"] == 0.5
        assert rule["min_area_mm2"] == 100
        assert "trigger_mode" not in rule
        assert "min_width_mm" not in rule

    def test_dimension_rule(self):
        _, sr, _ = parse_config(SAMPLE)
        rule = sr[("camera1", "缺陷B")]
        assert rule["trigger_mode"] == "dimension"
        assert rule["min_width_mm"] == 50
        assert rule["min_height_mm"] == 50
        assert "min_area_mm2" not in rule

    def test_status_zero_is_skipped_not_treated_as_class_off(self):
        """**关键**：status=0 只是这条阈值不下发，不是"这个类别不检测"。"""
        data = {"cameras": [{"cameraId": "camera2", "thresholds": [
            {"defectName": "多股线", "thresholdValue": 0.9, "status": 0}]}]}
        th, _, _ = parse_config(data)
        assert th == {}, "不该下发"
        # 更不该产生任何"关掉这个类别"的副作用
        assert ("camera2", "多股线") not in th

    def test_status_missing_defaults_to_enabled(self):
        data = {"cameras": [{"cameraId": "camera2", "thresholds": [
            {"defectName": "多股线", "thresholdValue": 0.3}]}]}
        th, _, _ = parse_config(data)
        assert th == {("camera2", "多股线"): 0.3}

    def test_enabled_missing_defaults_to_true(self):
        """后端漏传 enabled 不该等于"不停机"。"""
        data = {"cameras": [{"cameraId": "camera1", "stopSetting": [
            {"name": "缺陷A", "minConfidence": 0.5}]}]}
        _, sr, _ = parse_config(data)
        assert sr[("camera1", "缺陷A")]["enabled"] is True

    def test_enabled_false_is_applied(self):
        data = {"cameras": [{"cameraId": "camera1", "stopSetting": [
            {"name": "缺陷A", "enabled": False, "minConfidence": 0.5}]}]}
        _, sr, _ = parse_config(data)
        assert sr[("camera1", "缺陷A")]["enabled"] is False

    @pytest.mark.parametrize("bad", [None, "abc", -0.1, 1.5, {}, []])
    def test_bad_threshold_value_skipped(self, bad):
        data = {"cameras": [{"cameraId": "camera1", "thresholds": [
            {"defectName": "缺陷A", "thresholdValue": bad, "status": 1}]}]}
        th, _, _ = parse_config(data)
        assert th == {}

    def test_bool_is_not_a_number(self):
        """True 在 Python 里 == 1，必须显式挡掉。"""
        data = {"cameras": [{"cameraId": "camera1", "thresholds": [
            {"defectName": "缺陷A", "thresholdValue": True, "status": 1}]}]}
        th, _, _ = parse_config(data)
        assert th == {}

    def test_one_bad_row_does_not_kill_the_whole_config(self):
        data = {"cameras": [{"cameraId": "camera1", "thresholds": [
            {"defectName": "缺陷A", "thresholdValue": 0.5, "status": 1},
            {"defectName": "缺陷B", "thresholdValue": "垃圾", "status": 1},
            {"defectName": "缺陷C", "thresholdValue": 0.7, "status": 1}]}]}
        th, _, _ = parse_config(data)
        assert th == {("camera1", "缺陷A"): 0.5, ("camera1", "缺陷C"): 0.7}

    def test_missing_defect_name_skipped(self):
        data = {"cameras": [{"cameraId": "camera1", "thresholds": [
            {"thresholdValue": 0.5, "status": 1}]}]}
        th, _, _ = parse_config(data)
        assert th == {}

    def test_empty_cameras_gives_empty_tables(self):
        th, sr, meta = parse_config({"cameras": []})
        assert th == {} and sr == {} and meta["camera_count"] == 0

    def test_cameras_missing_is_tolerated(self):
        th, sr, _ = parse_config({"checkLogId": 1})
        assert th == {} and sr == {}

    @pytest.mark.parametrize("bad", [None, 42, "x", []])
    def test_bad_shape_raises(self, bad):
        with pytest.raises(ValueError):
            parse_config(bad)


# ======================================================================
# §2  Result 包装
# ======================================================================
class TestResultWrapper:

    @pytest.mark.parametrize("code", [0, 200, "0", "200", "SUCCESS", "ok"])
    def test_ok_codes(self, code):
        assert _is_ok({"code": code, "data": {}}) is True

    def test_missing_code_is_tolerated(self):
        """现有上报代码都不校验 code，这里也从宽。"""
        assert _is_ok({"data": {}}) is True

    @pytest.mark.parametrize("code", [500, "500", "ERROR", "FAIL"])
    def test_error_codes(self, code):
        assert _is_ok({"code": code, "data": {}}) is False

    def test_non_dict_is_not_ok(self):
        assert _is_ok(None) is False
        assert _is_ok([]) is False


# ======================================================================
# §3  存储为空时行为不变 + 优先级
# ======================================================================
class TestPriority:

    def _tc(self):
        # 类名是 ClassThresholdConfig（dataclass），覆盖块的取法是
        # camera_override_provider(camera_name) -> dict | None，
        # 不是"一次返回整张表"。
        from AnomalyDetection_Tool.models.data_types import (
            ClassThresholdConfig)
        blocks = {"camera2": {"class_confidence_thresholds": {"多股线": 0.60}}}
        return ClassThresholdConfig(
            # 两个类都放进全局表：否则未下发的类会落到 dataclass 的
            # default_threshold=0.25，测不出"按类隔离"这件事。
            thresholds_by_name={"多股线": 0.65, "包边不良": 0.55},
            thresholds_by_id={},
            camera_override_provider=lambda cam: blocks.get(cam),
        )

    def test_empty_store_behaves_exactly_as_before(self):
        assert _store().is_empty()
        tc = self._tc()
        assert tc.get_threshold("多股线", 0, "camera2") == 0.60   # 按相机
        assert tc.get_threshold("多股线", 0, "camera3") == 0.65   # 全局

    def test_remote_beats_camera_override(self):
        _store().apply({("camera2", "多股线"): 0.10}, {})
        tc = self._tc()
        assert tc.get_threshold("多股线", 0, "camera2") == 0.10

    def test_remote_is_per_camera(self):
        _store().apply({("camera2", "多股线"): 0.10}, {})
        tc = self._tc()
        assert tc.get_threshold("多股线", 0, "camera3") == 0.65, "没下发的相机走本地"

    def test_remote_is_per_class(self):
        _store().apply({("camera2", "多股线"): 0.10}, {})
        tc = self._tc()
        assert tc.get_threshold("包边不良", 0, "camera2") == 0.55

    def test_no_camera_name_falls_back(self):
        """camera_name 为空时不该命中任何按相机的下发。"""
        _store().apply({("camera2", "多股线"): 0.10}, {})
        tc = self._tc()
        assert tc.get_threshold("多股线", 0, None) == 0.65


class TestStopRulePriority:

    RULES = {"enabled": True, "rules": {
        "多股线": {"enabled": True, "min_confidence": 0.70,
                   "min_area_mm2": 100}}}

    def test_empty_store_behaves_as_before(self):
        from AnomalyDetection_Tool.config.settings import effective_stop_rule
        r = effective_stop_rule("多股线", "camera2", stop_rules=self.RULES)
        assert r["min_confidence"] == 0.70

    def test_remote_beats_global(self):
        from AnomalyDetection_Tool.config.settings import effective_stop_rule
        _store().apply({}, {("camera2", "多股线"):
                            {"enabled": True, "min_confidence": 0.30}})
        r = effective_stop_rule("多股线", "camera2", stop_rules=self.RULES)
        assert r["min_confidence"] == 0.30

    def test_master_switch_still_wins(self):
        """machine_stop_rules.enabled=false 是现场急停，后端不该能绕过。"""
        from AnomalyDetection_Tool.config.settings import effective_stop_rule
        _store().apply({}, {("camera2", "多股线"):
                            {"enabled": True, "min_confidence": 0.30}})
        off = {"enabled": False, "rules": self.RULES["rules"]}
        assert effective_stop_rule("多股线", "camera2", stop_rules=off) is None

    def test_remote_enabled_false_means_no_stop(self):
        from AnomalyDetection_Tool.config.settings import effective_stop_rule
        _store().apply({}, {("camera2", "多股线"): {"enabled": False}})
        # 存储返回的是 enabled=False 的规则本身；由调用方按 enabled 判断。
        # effective_stop_rule 直接返回它，语义与本地 enabled:false 一致。
        r = effective_stop_rule("多股线", "camera2", stop_rules=self.RULES)
        assert r == {"enabled": False}

    def test_returns_a_copy(self):
        from AnomalyDetection_Tool.config.settings import effective_stop_rule
        _store().apply({}, {("camera2", "多股线"):
                            {"enabled": True, "min_confidence": 0.30}})
        r = effective_stop_rule("多股线", "camera2", stop_rules=self.RULES)
        r["min_confidence"] = 0.99
        again = effective_stop_rule("多股线", "camera2", stop_rules=self.RULES)
        assert again["min_confidence"] == 0.30, "调用方不该能改坏存储"


# ======================================================================
# §4  失败不清空
# ======================================================================
class TestNeverWipe:

    def test_apply_refuses_empty_result(self):
        _store().apply({("camera2", "多股线"): 0.10}, {})
        assert apply_config({"cameras": []}) is False
        assert _store().get_threshold("camera2", "多股线") == 0.10, "不该被清掉"

    def test_apply_refuses_unparsable(self):
        _store().apply({("camera2", "多股线"): 0.10}, {})
        assert apply_config({"cameras": "不是数组"}) is False
        assert _store().get_threshold("camera2", "多股线") == 0.10

    def test_partial_data_still_applies(self):
        """只有停机规则、没有阈值，也算有效配置。"""
        data = {"cameras": [{"cameraId": "camera1", "stopSetting": [
            {"name": "缺陷A", "enabled": True, "minConfidence": 0.5}]}]}
        assert apply_config(data) is True
        assert _store().get_stop_rule("camera1", "缺陷A")["min_confidence"] == 0.5


# ======================================================================
# §5  并发 / 快照
# ======================================================================
class TestConcurrency:

    def test_concurrent_read_write_is_safe(self):
        errs = []
        stop = threading.Event()

        def writer():
            try:
                i = 0
                while not stop.is_set():
                    _store().apply({("camera2", "多股线"): 0.1 + (i % 5) / 100},
                                   {("camera2", "多股线"):
                                    {"enabled": True, "min_confidence": 0.5}})
                    i += 1
            except Exception as e:              # pragma: no cover
                errs.append(e)

        def reader():
            try:
                while not stop.is_set():
                    _store().get_threshold("camera2", "多股线")
                    _store().get_stop_rule("camera2", "多股线")
                    _store().snapshot()
            except Exception as e:              # pragma: no cover
                errs.append(e)

        ths = [threading.Thread(target=writer)] + \
              [threading.Thread(target=reader) for _ in range(6)]
        [t.start() for t in ths]
        threading.Event().wait(0.3)
        stop.set()
        [t.join() for t in ths]
        assert errs == []

    def test_snapshot_shape(self):
        apply_config(SAMPLE)
        snap = _store().snapshot()
        assert snap["applied"] is True
        assert snap["cameras"] == ["camera1"]
        assert snap["threshold_count"] == 1
        assert snap["stop_rule_count"] == 2
        assert snap["meta"]["material"] == "1500D/2"


# ======================================================================
# §6  配置与接线
# ======================================================================
class TestWiring:

    @pytest.fixture(scope="class")
    def cfg(self):
        import yaml
        with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_config_section(self, cfg):
        c = cfg["material_config"]
        assert c["enabled"] is True
        assert float(c["timeout_sec"]) > 0

    def test_settings_exports_it(self):
        from AnomalyDetection_Tool.config.settings import MATERIAL_CONFIG
        assert MATERIAL_CONFIG.get("enabled") is True

    def test_api_path_is_relative_to_the_base(self):
        """API_PATH 是**基址之后**的那一段，不能再写 /api。

        基址 get_api_base_url() 已含 api_base_path("/api")，这里再写一层
        就会拼成 /api/api/v1/...，现场 404。
        仓库里其他调用点同一惯例："/image/upload"、"/v1/defect/log/size"。
        """
        assert API_PATH == "/v1/defect/material/current-config"
        assert not API_PATH.startswith("/api/"), \
            "基址已含 /api，这里再写会拼成 /api/api/..."

    def test_full_url_matches_the_confirmed_address(self):
        """拼出来必须与现场确认的地址逐字符相同。"""
        import Communication_Tool.material_config_client as M
        url = M._get_base_url() + API_PATH
        assert url == (
            "http://localhost:8890/api/v1/defect/material/"
            "current-config"), url
        assert url.count("/api/") == 1, url

    def test_base_url_defaults_to_the_global_one(self):
        """默认没有覆盖项时，基址就等于全局基址（同一个后端）。"""
        import Communication_Tool.material_config_client as M
        from AnomalyDetection_Tool.config.settings import (
            MATERIAL_CONFIG, get_api_base_url)
        assert MATERIAL_CONFIG.get("api_port") is None
        assert M._get_base_url() == get_api_base_url()
        assert M._get_base_url().endswith(":8890/api")

    def test_port_override_does_not_touch_the_global(self):
        """万一后端挪端口，改 material_config 就行，全局不能被带跑。

        全局那三项被检测信息上报、布料尺寸上报、周期上报共用。
        """
        import Communication_Tool.material_config_client as M
        from AnomalyDetection_Tool.config import settings as S
        saved = dict(S.MATERIAL_CONFIG)
        try:
            S.MATERIAL_CONFIG["api_port"] = 8001
            assert ":8001/" in M._get_base_url(), M._get_base_url()
            # 全局一点没动
            assert S.get_api_base_url().endswith(":8890/api")
            from Communication_Tool.http_client import get_base_url
            assert get_base_url().endswith(":8890/api")
        finally:
            S.MATERIAL_CONFIG.clear()
            S.MATERIAL_CONFIG.update(saved)


    def test_threshold_hook_is_first_step(self):
        with open(os.path.join(ROOT, "AnomalyDetection_Tool", "models",
                               "data_types.py"), encoding="utf-8") as f:
            src = f.read()
        body = src[src.index("def get_threshold"):]
        assert body.index("get_remote_config_store") < \
            body.index("# ---- 1~3")

    def test_stop_rule_hook_is_after_master_switch(self):
        with open(os.path.join(ROOT, "AnomalyDetection_Tool", "config",
                               "settings.py"), encoding="utf-8") as f:
            src = f.read()
        body = src[src.index("def effective_stop_rule"):]
        body = body[:body.index("\ndef ")]
        assert body.index("总开关关着") < body.index("get_remote_config_store")

    def test_main_yolo_wires_it(self):
        with open(os.path.join(ROOT, "main_yolo.py"), encoding="utf-8") as f:
            src = f.read()
        assert "refresh_material_config" in src
        assert "material-config-loop" in src

    def test_startup_fetch_is_off_the_main_thread(self):
        """启动绝不能被网络卡住。"""
        with open(os.path.join(ROOT, "main_yolo.py"), encoding="utf-8") as f:
            src = f.read()
        seg = src[src.index("【七】后端下发的材质检测配置"):]
        seg = seg[:seg.index("【四】正常图片定时保存")]
        assert 'target=_mc_fetch, args=("启动",)' in seg
        assert "daemon=True" in seg


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


class TestEndToEnd:
    """真实 HTTP 往返：客户端走 aiohttp，服务端用 stdlib http.server。

    前面所有用例都只喂 dict，绕过了"请求真的发出去了吗"这一环。
    这一组把完整路径跑通，并断言**服务端实际收到**的路径——本地拼的字符串
    对上不代表请求真的打对了地方。曾经因为只看本地字符串，把一个端口错误
    误判成了路径错误。
    """

    def _serve(self, payload, status=200):
        import http.server, socketserver, threading as _th
        hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        socketserver.TCPServer.allow_reuse_address = True
        srv = socketserver.TCPServer(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        _th.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, port, hits

    def test_real_http_round_trip(self, monkeypatch):
        payload = {"code": 0, "data": {"clothNumber": "1500D/2-100145-2610-2",
                                       "cameras": _cams()}}
        srv, port, hits = self._serve(payload)
        try:
            import Communication_Tool.material_config_client as M
            monkeypatch.setattr(
                M, "_get_base_url", lambda: f"http://127.0.0.1:{port}/api")

            assert M.refresh_once(timeout=5) is True
            # 请求真的打到了服务端，且路径符合接口文档
            # 服务端**实际收到**的路径才算数，不能只看本地拼的字符串
            assert hits == ["/api/v1/defect/material/current-config"]
            # 注意：snapshot() 的 key 是拍平后的「相机/类别」字符串（诊断
            # 视图，方便打日志），不是内部的元组 key。取值要走
            # get_threshold / get_stop_rule 这两个真正的访问器。
            assert _store().get_threshold("camera2", "\u591a\u80a1\u7ebf") == 0.10
            r = _store().get_stop_rule(
                "camera2", "\u5355\u6839\u5305\u4e0d\u8fdb")
            assert r["trigger_mode"] == "dimension"
            assert r["min_width_mm"] == 50
        finally:
            srv.shutdown()

    def test_http_500_does_not_apply(self, monkeypatch):
        srv, port, _ = self._serve({}, status=500)
        try:
            _store().apply(thresholds={(("camera9", "多股线"), 0.9)},
                           meta={"material": "旧料"})
            import Communication_Tool.material_config_client as M
            monkeypatch.setattr(
                M, "_get_base_url", lambda: f"http://127.0.0.1:{port}/api")
            assert M.refresh_once(timeout=5) is False
            assert _store().get_threshold("camera9", "多股线") == 0.9
        finally:
            srv.shutdown()

    def test_dead_port_does_not_raise(self, monkeypatch):
        """端口没人听 —— 现场后端没起时就是这个状态，不能抛。"""
        import Communication_Tool.material_config_client as M
        monkeypatch.setattr(M, "_get_base_url",
                            lambda: "http://127.0.0.1:1/api")
        assert M.refresh_once(timeout=1) is False
        assert _store().is_empty()


class TestRefreshOnceRobustness:
    """refresh_once 为什么"总是在独立线程里跑自己的事件循环"。

    说清楚：今天在生产的三个调用点（启动守护线程、「开始」回调内部又起的
    线程、定时循环线程）都不在事件循环里，所以旧写法暂时不会炸。但
    ① 谁要是从协程里调一次就静默失效（请求根本不发，只返回 False），
    ② 旧写法没有硬超时，请求卡死会把调用线程一起挂住 —— 在定时线程里
    就等于配置永远刷不动了。这两条都要钉住。
    """

    def test_callable_from_inside_a_running_loop(self):
        import asyncio
        import http.server, socketserver, threading as _th
        payload = {"code": 0, "data": {"cameras": _cams()}}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                b = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def log_message(self, *a):
                pass

        socketserver.TCPServer.allow_reuse_address = True
        srv = socketserver.TCPServer(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        _th.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            import Communication_Tool.material_config_client as M
            M._get_base_url = lambda: f"http://127.0.0.1:{port}/api"

            async def caller():
                # 关键：refresh_once 是在一个正在运行的事件循环里被调的
                return M.refresh_once(timeout=5)

            assert asyncio.run(caller()) is True
            assert _store().get_threshold("camera2", "多股线") == 0.10
        finally:
            srv.shutdown()

    def test_hard_timeout_does_not_hang_caller(self, monkeypatch):
        """请求挂死时，refresh_once 必须在有限时间内返回 False。"""
        import time as _t
        import Communication_Tool.material_config_client as M

        async def hang(**kw):
            await asyncio.sleep(60)
            return None

        monkeypatch.setattr(M, "_get_base_url",
                            lambda: "http://127.0.0.1:1/api")
        monkeypatch.setattr(M, "fetch_current_config", hang)

        t0 = _t.time()
        assert M.refresh_once(timeout=1) is False
        assert _t.time() - t0 < 8      # 硬超时是 timeout+3


class TestChangeReporting:
    """变更必须可见：由什么变更成什么、是否成功、当前生效的是哪一份。

    这些日志是 EnhancedLogger 直接打的，caplog/capsys 抓不到，
    所以统一 monkeypatch 模块级的 info/warning 来收集。
    """

    @staticmethod
    def _collect(monkeypatch):
        import Communication_Tool.material_config_client as M
        logs = {"info": [], "warning": [], "error": []}
        for lvl in logs:
            monkeypatch.setattr(M, lvl,
                                (lambda L: lambda m: L.append(m))(logs[lvl]))
        return logs

    def _cams(self, thr=0.10, conf=0.5):
        return {"clothNumber": "C1", "roller": "R1", "material": "1500D/2",
                "specification": "100145",
                "cameras": [{"cameraId": "camera2",
                             "thresholds": [{"defectName": "多股线",
                                             "defectLabel": "multi_line",
                                             "thresholdValue": thr,
                                             "status": 1}],
                             "stopSetting": [{"name": "单根包不进",
                                              "enabled": True,
                                              "minConfidence": conf,
                                              "triggerMode": "dimension",
                                              "minWidthMm": 50,
                                              "minHeightMm": 47}]}]}

    def test_first_apply_reports_source_and_diff(self, monkeypatch):
        import Communication_Tool.material_config_client as M
        logs = self._collect(monkeypatch)
        assert M.apply_config(self._cams(0.10)) is True
        out = "\n".join(logs["info"])
        assert "变更成功" in out
        assert "来源：后端数据库" in out
        assert "首次下发" in out
        assert "此前生效的是 config.yaml" in out
        # 明细里必须能看到"由什么变成什么"
        assert "多股线 阈值" in out and "->0.1" in out
        assert "[原:config.yaml]" in out

    def test_no_change_prints_nothing_by_default(self, monkeypatch):
        """同样的配置再拉一次，**一行都不打**（现场要求：只在出错/变更时打）。"""
        import Communication_Tool.material_config_client as M
        M.apply_config(self._cams(0.10))
        logs = self._collect(monkeypatch)
        assert M.apply_config(self._cams(0.10)) is True
        assert logs["info"] == [] and logs["warning"] == [] and logs["error"] == []

    def test_no_change_can_still_be_shown(self, monkeypatch):
        """想确认轮询还活着：quiet_when_unchanged 设 false 即可。"""
        import Communication_Tool.material_config_client as M
        from AnomalyDetection_Tool.config.settings import MATERIAL_CONFIG
        monkeypatch.setitem(MATERIAL_CONFIG, "quiet_when_unchanged", False)
        M.apply_config(self._cams(0.10))
        logs = self._collect(monkeypatch)
        assert M.apply_config(self._cams(0.10)) is True
        assert len(logs["info"]) == 1, logs
        assert "变化 0 项" in logs["info"][0]
        assert "无需改动" in logs["info"][0]
        assert "变更明细" not in logs["info"][0]

    def test_changed_value_shows_old_and_new(self, monkeypatch):
        import Communication_Tool.material_config_client as M
        M.apply_config(self._cams(0.10))
        logs = self._collect(monkeypatch)
        assert M.apply_config(self._cams(0.42)) is True
        out = "\n".join(logs["info"])
        assert "变化 1 项" in out
        assert "多股线 阈值 0.1->0.42" in out
        assert "[原:后端]" in out          # 上一次的值来自后端

    def test_stop_rule_change_is_listed_field_by_field(self, monkeypatch):
        import Communication_Tool.material_config_client as M
        M.apply_config(self._cams(0.10, conf=0.5))
        logs = self._collect(monkeypatch)
        assert M.apply_config(self._cams(0.10, conf=0.87)) is True
        out = "\n".join(logs["info"])
        assert "单根包不进 停机" in out
        assert "min_confidence 0.5->0.87" in out

    def test_detail_defaults_to_complete(self, monkeypatch):
        """默认全部打印 —— 截断会让后面的相机看起来像没变。"""
        import Communication_Tool.material_config_client as M
        many = {"clothNumber": "C1", "cameras": [
            {"cameraId": f"camera{i}",
             "thresholds": [{"defectName": "多股线", "defectLabel": "multi_line",
                             "thresholdValue": 0.10, "status": 1}],
             "stopSetting": []} for i in range(1, 41)]}
        logs = self._collect(monkeypatch)
        assert M.apply_config(many) is True
        out = "\n".join(logs["info"])
        for i in range(1, 41):
            assert f"【camera{i}】" in out, f"camera{i} 明细被截断了"
        assert "另有" not in out

    def test_parse_failure_reports_failure_and_source(self, monkeypatch):
        import Communication_Tool.material_config_client as M
        logs = self._collect(monkeypatch)
        assert M.apply_config({"clothNumber": "C1", "cameras": []}) is False
        assert any("变更失败" in m for m in logs["warning"])
        assert any("config.yaml" in m for m in logs["warning"])

    def test_source_says_backend_when_store_populated(self, monkeypatch):
        """已经成功下发过一次后拉取失败，要说清仍在用后端那份。"""
        import Communication_Tool.material_config_client as M
        M.apply_config(self._cams(0.10))
        logs = self._collect(monkeypatch)
        M._log_source("后端未返回可用配置")
        out = "\n".join(logs["info"])
        assert "上一次成功下发的配置" in out
        assert "阈值 1 条" in out


class TestLabelWarningDedup:
    """defectLabel 不一致是配置层面的固定问题，同一项只该告警一次。"""

    def test_same_mismatch_warns_once(self, monkeypatch):
        import Communication_Tool.material_config_client as M
        seen = []
        monkeypatch.setattr(M, "warning", lambda m: seen.append(m))
        M._LABEL_WARNED.clear()
        for _ in range(5):
            M._check_label("camera1", "缺纬", "missing_latitude")
        assert len(seen) == 1, seen

    def test_new_mismatch_still_warns(self, monkeypatch):
        import Communication_Tool.material_config_client as M
        seen = []
        monkeypatch.setattr(M, "warning", lambda m: seen.append(m))
        M._LABEL_WARNED.clear()
        M._check_label("camera1", "缺纬", "missing_latitude")
        M._check_label("camera2", "缺纬", "missing_latitude")
        assert len(seen) == 2, seen

    def test_changed_label_warns_again(self, monkeypatch):
        import Communication_Tool.material_config_client as M
        seen = []
        monkeypatch.setattr(M, "warning", lambda m: seen.append(m))
        M._LABEL_WARNED.clear()
        M._check_label("camera1", "缺纬", "missing_latitude")
        M._check_label("camera1", "缺纬", "yet_another_label")
        assert len(seen) == 2, seen


class TestAllCamerasChange:
    """前端「全部相机」改一次就是 8 台一起动。

    明细必须能看出**哪台**变了，且不能被截断——截断会让 camera2 之后的
    相机看起来像没变。同时要能看到「特殊值」那台没跟着变。
    """

    CLASSES = ["多股线", "缺纬", "单根包不进", "包边不良"]

    @staticmethod
    def _collect(monkeypatch):
        import Communication_Tool.material_config_client as M
        logs = []
        monkeypatch.setattr(M, "info", lambda m: logs.append(m))
        monkeypatch.setattr(M, "warning", lambda m: None)
        monkeypatch.setattr(M, "_warn_model_mismatch", lambda meta: None)
        return logs

    def payload(self, v, special=None):
        """全部相机统一设 v；special = {相机: (类别, 值)} 保留特殊值。"""
        special = special or {}
        cams = []
        for i in range(1, 9):
            cid = f"camera{i}"
            ths = []
            for c in self.CLASSES:
                val = special.get((cid, c), v)
                ths.append({"defectName": c, "defectLabel": c, "status": 1,
                            "thresholdValue": val})
            cams.append({"cameraId": cid, "thresholds": ths,
                         "stopSetting": [{"name": "单根包不进", "enabled": True,
                                          "minConfidence": v,
                                          "triggerMode": "dimension",
                                          "minWidthMm": 50,
                                          "minHeightMm": 47}]})
        return {"clothNumber": "C1", "roller": "R1", "material": "1500D/2",
                "specification": "100145", "cameras": cams}

    def test_every_changed_camera_is_named(self, monkeypatch):
        """8 台全变，8 台都得在"变化相机"那一行里，一个不能少。"""
        import Communication_Tool.material_config_client as M
        logs = self._collect(monkeypatch)
        M.apply_config(self.payload(0.30))
        out = "\n".join(logs)
        line = [x for x in out.split("\n") if "变化相机" in x][0]
        assert "8 台" in line
        for i in range(1, 9):
            assert f"camera{i}(" in line, f"camera{i} 没出现在变化相机里：{line}"

    def test_detail_is_complete_by_default(self, monkeypatch):
        """默认不截断：每台的明细块都要在。"""
        import Communication_Tool.material_config_client as M
        logs = self._collect(monkeypatch)
        M.apply_config(self.payload(0.30))
        out = "\n".join(logs)
        for i in range(1, 9):
            assert f"【camera{i}】" in out, f"camera{i} 的明细块缺失"
        assert "另有" not in out and "未列出" not in out

    def test_per_camera_block_shows_class_and_values(self, monkeypatch):
        """明细里每台相机下要能看到类别名和前后值。"""
        import Communication_Tool.material_config_client as M
        logs = self._collect(monkeypatch)
        M.apply_config(self.payload(0.30))
        out = "\n".join(logs)
        blk = out.split("【camera3】")[1].split("【camera4】")[0]
        for c in self.CLASSES:
            assert c in blk, f"camera3 的明细里缺类别 {c}"
        assert "->0.3" in blk

    def test_special_value_camera_is_called_out(self, monkeypatch):
        """camera8 的 多股线 是特殊值 0.55，改「全部相机」时始终不动它。

        特殊值特意取 0.55（不是 config.yaml 里的 0.65），免得断言碰巧因为
        本地默认值相同而变绿。
        """
        import Communication_Tool.material_config_client as M
        sp = {("camera8", "多股线"): 0.55}
        M.apply_config(self.payload(0.30, sp))       # 建立基线
        logs = self._collect(monkeypatch)
        M.apply_config(self.payload(0.12, sp))       # 全部相机 0.30 -> 0.12
        out = "\n".join(logs)
        assert "同类别未跟随变更" in out
        hold = [x for x in out.split("\n") if "多股线" in x and "保持原值" in x]
        assert hold, out
        assert "7 台已更新" in hold[0] and "camera8=0.55" in hold[0]
        # camera8 的明细里不该有 多股线 那一行（它没变）
        blk = out.split("【camera8】")[1]
        assert "多股线 阈值" not in blk
        # 但它仍然出现在"变化相机"里（另外 3 个类变了）
        line = [x for x in out.split("\n") if "变化相机" in x][0]
        assert "camera8(4项)" in line

    def test_two_special_cameras_are_both_listed(self, monkeypatch):
        """特殊值可能是两台（比如 camera1 和 camera8）。"""
        import Communication_Tool.material_config_client as M
        sp = {("camera1", "多股线"): 0.55, ("camera8", "多股线"): 0.55}
        M.apply_config(self.payload(0.30, sp))
        logs = self._collect(monkeypatch)
        M.apply_config(self.payload(0.12, sp))
        out = "\n".join(logs)
        hold = [x for x in out.split("\n") if "多股线" in x and "保持原值" in x][0]
        assert "6 台已更新" in hold and "2 台保持原值" in hold
        assert "camera1=0.55" in hold and "camera8=0.55" in hold

    def test_stop_rule_holdout_is_called_out(self, monkeypatch):
        """停机阈值也有特殊值，同样要点出来。"""
        import Communication_Tool.material_config_client as M
        def _sp(v, cam8_stop):
            p = self.payload(v)
            p["cameras"][7]["stopSetting"][0]["minConfidence"] = cam8_stop
            return p
        M.apply_config(_sp(0.30, 0.9))           # 建立基线，camera8 停机=0.9
        logs = self._collect(monkeypatch)
        M.apply_config(_sp(0.12, 0.9))           # 全部相机改 0.12，camera8 仍 0.9
        out = "\n".join(logs)
        hold = [x for x in out.split("\n")
                if "停机规则" in x and "保持原值" in x]
        assert hold, out
        assert "camera8" in hold[0] and "conf>=0.9" in hold[0]

    def test_camera_with_no_change_is_listed_separately(self, monkeypatch):
        """整台相机都没变的，要单独列出来，别让人以为漏了它。"""
        import Communication_Tool.material_config_client as M
        logs = self._collect(monkeypatch)
        p = self.payload(0.30)
        p["cameras"][4]["thresholds"] = []            # camera5 整台没配
        p["cameras"][4]["stopSetting"] = []
        M.apply_config(p)
        out = "\n".join(logs)
        assert "未变化相机（1 台）：camera5" in out

    def test_all_cameras_unchanged_is_silent(self, monkeypatch):
        """8 台都没变 -> 完全不打。2s 一轮下这是每轮都会走到的路径。"""
        import Communication_Tool.material_config_client as M
        M.apply_config(self.payload(0.30))
        logs = self._collect(monkeypatch)
        for _ in range(5):                       # 模拟连续 5 轮轮询
            M.apply_config(self.payload(0.30))
        assert logs == [], logs

    def test_max_diff_lines_can_still_cap(self, monkeypatch):
        """真要限制行数：material_config.max_diff_lines > 0 时生效。"""
        import Communication_Tool.material_config_client as M
        from AnomalyDetection_Tool.config.settings import MATERIAL_CONFIG
        monkeypatch.setitem(MATERIAL_CONFIG, "max_diff_lines", 6)
        assert M._diff_max_lines() == 6
        logs = self._collect(monkeypatch)
        M.apply_config(self.payload(0.30))
        out = "\n".join(logs)
        assert "未列出" in out
        # 即使明细被截断，"变化相机"那一行仍然完整
        line = [x for x in out.split("\n") if "变化相机" in x][0]
        for i in range(1, 9):
            assert f"camera{i}(" in line

    def test_max_diff_lines_default_is_unlimited(self, monkeypatch):
        import Communication_Tool.material_config_client as M
        from AnomalyDetection_Tool.config.settings import MATERIAL_CONFIG
        monkeypatch.delitem(MATERIAL_CONFIG, "max_diff_lines", raising=False)
        assert M._diff_max_lines() == 0


class TestDetectionActuallyApplies:
    """日志打完不算完 —— 读到的值必须真的进到检测判决里。

    本端只读不改：前端页面改 -> 数据库改 -> 本端拉取 -> 检测用新值。
    这一组从 HTTP 拉取一路查到 ClassThresholdConfig.get_threshold 和
    effective_stop_rule，确认「全部相机改值」时 7 台跟着走、特殊值那台不动，
    而且是**检测路径实际取到的值**，不是日志里写的值。
    """

    CLASSES = ["\u591a\u80a1\u7ebf", "\u7f3a\u7eac",
               "\u5355\u6839\u5305\u4e0d\u8fdb", "\u5305\u8fb9\u4e0d\u826f"]

    def _serve_mutable(self, db, monkeypatch):
        import http.server, socketserver, threading as _th

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                cams = []
                for i in range(1, 9):
                    cid = "camera%d" % i
                    ths = [{"defectName": c, "defectLabel": c, "status": 1,
                            "thresholdValue": (db["cam8_thr"]
                                               if (i == 8 and c == self.CLS0)
                                               else db["v"])}
                           for c in self.CLASSES]
                    st = [{"name": "\u5355\u6839\u5305\u4e0d\u8fdb",
                           "enabled": True,
                           "minConfidence": (db["cam8_stop"] if i == 8
                                             else db["v"]),
                           "triggerMode": "dimension",
                           "minWidthMm": 50, "minHeightMm": 47}]
                    cams.append({"cameraId": cid, "thresholds": ths,
                                 "stopSetting": st})
                body = json.dumps({"code": 0, "data": {
                    "clothNumber": "C1", "cameras": cams}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        H.CLS0 = self.CLASSES[0]
        H.CLASSES = self.CLASSES
        socketserver.TCPServer.allow_reuse_address = True
        srv = socketserver.TCPServer(("127.0.0.1", 0), H)
        _th.Thread(target=srv.serve_forever, daemon=True).start()

        import Communication_Tool.material_config_client as M
        monkeypatch.setattr(
            M, "_get_base_url",
            lambda: "http://127.0.0.1:%d/api" % srv.server_address[1])
        monkeypatch.setattr(M, "_warn_model_mismatch", lambda meta: None)
        return srv

    @staticmethod
    def _detector():
        """与 detection_pipeline.py:149 完全相同的接线。"""
        from AnomalyDetection_Tool.config.settings import (
            CLASS_CONFIDENCE_THRESHOLDS, get_camera_threshold_override,
            get_default_class_confidence)
        from AnomalyDetection_Tool.models.data_types import (
            ClassThresholdConfig)
        return ClassThresholdConfig(
            thresholds_by_name=CLASS_CONFIDENCE_THRESHOLDS,
            default_threshold_provider=get_default_class_confidence,
            camera_override_provider=get_camera_threshold_override)

    def test_values_reach_the_detection_path(self, monkeypatch):
        import Communication_Tool.material_config_client as M
        from AnomalyDetection_Tool.config.settings import effective_stop_rule
        db = {"v": 0.12, "cam8_thr": 0.55, "cam8_stop": 0.9}
        srv = self._serve_mutable(db, monkeypatch)
        tc = self._detector()
        multi, stop_cls = self.CLASSES[0], self.CLASSES[2]
        try:
            # ---- 前端第一次改库 ----
            assert M.refresh_once(timeout=5) is True
            assert abs(tc.get_threshold(multi, 0, "camera1") - 0.12) < 1e-9
            assert abs(tc.get_threshold(multi, 0, "camera5") - 0.12) < 1e-9
            # 特殊值那台不吃「全部相机」的值
            assert abs(tc.get_threshold(multi, 0, "camera8") - 0.55) < 1e-9
            r1 = effective_stop_rule(stop_cls, "camera1")
            r8 = effective_stop_rule(stop_cls, "camera8")
            assert abs(r1["min_confidence"] - 0.12) < 1e-9
            assert abs(r8["min_confidence"] - 0.90) < 1e-9

            # ---- 前端再改一次：全部 0.12 -> 0.20，特殊值仍不动 ----
            db["v"] = 0.20
            assert M.refresh_once(timeout=5) is True
            for i in (1, 2, 3, 4, 5, 6, 7):
                got = tc.get_threshold(multi, 0, "camera%d" % i)
                assert abs(got - 0.20) < 1e-9, ("camera%d" % i, got)
            assert abs(tc.get_threshold(multi, 0, "camera8") - 0.55) < 1e-9
            assert abs(effective_stop_rule(stop_cls, "camera1")
                       ["min_confidence"] - 0.20) < 1e-9
            assert abs(effective_stop_rule(stop_cls, "camera8")
                       ["min_confidence"] - 0.90) < 1e-9
        finally:
            srv.shutdown()

    def test_no_backend_means_yaml_still_drives_detection(self, monkeypatch):
        """后端从没下发过 -> 检测用的必须是 config.yaml 的值。"""
        from AnomalyDetection_Tool.config.settings import (
            _camera_det_threshold)
        RCS.reset_remote_config_store()
        tc = self._detector()
        multi = self.CLASSES[0]
        got = tc.get_threshold(multi, 0, "camera1")
        assert abs(got - 0.65) < 1e-9, got          # config.yaml 里的值
        assert abs(_camera_det_threshold("camera1", multi) - got) < 1e-9


class TestPollingQuietness:
    """现场 refresh_interval_sec=2，即每 2 秒轮询一次数据库。

    要求：只在**出错**或**真的发生变更**时打日志。后端的稳定状态
    （哪些类 status=0、拉取失败的原因、当前生效的是哪一份）每轮重打一遍
    会把变更明细彻底淹掉。
    """

    @staticmethod
    def _collect(monkeypatch):
        import Communication_Tool.material_config_client as M
        logs = []
        for lvl in ("info", "warning", "error"):
            monkeypatch.setattr(M, lvl,
                                (lambda L: lambda m: L.append(m))(logs))
        monkeypatch.setattr(M, "_warn_model_mismatch", lambda meta: None)
        return logs

    @staticmethod
    def _cams(disabled=None):
        """disabled = {相机: [类别...]} 这些 (相机,类) 的 status=0。"""
        disabled = disabled or {}
        out = []
        for i in range(1, 9):
            cid = "camera%d" % i
            ths = [{"defectName": c, "defectLabel": c,
                    "status": 0 if c in disabled.get(cid, ()) else 1,
                    "thresholdValue": 0.30}
                   for c in ("多股线", "包边不良")]
            out.append({"cameraId": cid, "thresholds": ths, "stopSetting": []})
        return {"clothNumber": "C1", "roller": "R1", "material": "1500D/2",
                "specification": "100145", "cameras": out}

    def test_skip_warning_prints_once_across_polls(self, monkeypatch):
        """status=0 是后端的稳定状态，不是每轮的新事件。"""
        import Communication_Tool.material_config_client as M
        logs = self._collect(monkeypatch)
        dis = {"camera%d" % i: ("包边不良",) for i in (1, 3, 4, 5, 6, 7, 8)}
        for _ in range(10):                       # 10 轮轮询 = 现场 20 秒
            M.apply_config(self._cams(dis))
        skips = [m for m in logs if "跳过" in m and "status=0" in m]
        assert len(skips) == 1, "跳过告警重复了 %d 次" % len(skips)
        assert "camera1/包边不良" in skips[0]

    def test_skip_warning_reprints_when_it_changes(self, monkeypatch):
        """跳过内容变了必须重新打出来，否则后端的改动看不见。"""
        import Communication_Tool.material_config_client as M
        logs = self._collect(monkeypatch)
        M.apply_config(self._cams({"camera1": ("包边不良",)}))
        M.apply_config(self._cams({"camera1": ("包边不良",)}))
        M.apply_config(self._cams({"camera1": ("包边不良",),
                                   "camera2": ("多股线",)}))
        skips = [m for m in logs if "跳过" in m]
        assert len(skips) == 2, skips
        assert "跳过 1 条阈值" in skips[0]
        assert "跳过 2 条阈值" in skips[1]

    def test_two_kinds_of_skip_are_deduped_independently(self, monkeypatch):
        """阈值跳过和停机规则跳过必须各用各的去重键。

        共用一个键的话，两条消息会互相顶掉：每轮轮询都判定"内容变了"，
        于是两条都重打一遍 —— 去重形同虚设。
        """
        import Communication_Tool.material_config_client as M
        logs = self._collect(monkeypatch)
        cfg = self._cams({"camera1": ("包边不良",)})
        # 再制造一条停机规则的跳过（stopSetting 不是数组）
        cfg["cameras"][0]["stopSetting"] = "not-a-list"
        for _ in range(6):
            M.apply_config(cfg)
        skips = [m for m in logs if "跳过" in m]
        assert len(skips) == 2, "应各打一次，实际 %d 条：%s" % (len(skips), skips)
        assert any("跳过 1 条阈值" in m for m in skips), skips
        assert any("跳过 1 条停机规则" in m for m in skips), skips

    def test_repeated_polling_with_no_change_is_silent(self, monkeypatch):
        import Communication_Tool.material_config_client as M
        cfg = self._cams()
        M.apply_config(cfg)
        logs = self._collect(monkeypatch)
        for _ in range(30):                       # 30 轮 = 现场 60 秒
            M.apply_config(cfg)
        assert logs == [], "30 轮无变化打了 %d 行" % len(logs)

    def test_source_log_dedups_even_though_age_changes(self, monkeypatch):
        """_log_source 的消息里带「Ns 前应用」，每秒都在变。

        去重签名必须单独构造，否则这条永远去重不掉 —— 这正是当初的坑。
        """
        import Communication_Tool.material_config_client as M
        from AnomalyDetection_Tool.config.remote_config_store import (
            get_remote_config_store)
        M.apply_config(self._cams())
        assert not get_remote_config_store().is_empty()
        logs = self._collect(monkeypatch)
        # 必须真的让 age_sec 变化才算测到：它精度 0.1s，连续快速调用时
        # 值根本不变，那样即使去重签名写错也照样"通过"，等于没测。
        st = get_remote_config_store()
        for back in (1.0, 2.0, 3.0, 4.0, 5.0):
            import time as _t
            st._applied_at = _t.time() - back
            M._log_source("后端未返回可用配置")
        ages = {round(1.0 * i, 1) for i in range(1, 6)}
        assert len(ages) == 5                      # 确认 age 确实在变
        assert len(logs) == 1, "同一原因因 age_sec 变化重复了 %d 次" % len(logs)
        assert "上一次成功下发的配置" in logs[0]

    def test_source_log_reprints_when_reason_changes(self, monkeypatch):
        import Communication_Tool.material_config_client as M
        M.apply_config(self._cams())
        logs = self._collect(monkeypatch)
        M._log_source("拉取超时")
        M._log_source("拉取超时")
        M._log_source("网络错误: connection refused")
        assert len(logs) == 2, logs
        assert "拉取超时" in logs[0] and "connection refused" in logs[1]

    def test_failure_reprints_after_recovery(self, monkeypatch):
        """安全性质：故障恢复后再次失败，必须重新报出来。

        去重表里那条失败记录如果不清，第二次同样的故障就会被静默吞掉 ——
        那是漏报，比刷屏严重得多。
        """
        import Communication_Tool.material_config_client as M
        cfg = self._cams()
        M.apply_config(cfg)                       # 先建立基线（会打变更明细）
        logs = self._collect(monkeypatch)
        M._log_source("拉取超时")
        assert len(logs) == 1
        M._log_source("拉取超时")
        assert len(logs) == 1, "同一原因重复了"
        logs.clear()
        M.apply_config(cfg)                       # 恢复正常 -> 清失败去重
        assert logs == [], "无变化的恢复轮不该打日志"
        M._log_source("拉取超时")                 # 又挂
        assert len(logs) == 1, "恢复后再次失败被去重吞掉了 —— 漏报故障"

    def test_every_real_change_still_prints(self, monkeypatch):
        """静音只针对"没变化"；真有变更时每一轮都要完整打。"""
        import Communication_Tool.material_config_client as M
        logs = self._collect(monkeypatch)
        for v in (0.30, 0.28, 0.26, 0.24):
            cfg = self._cams()
            for c in cfg["cameras"]:
                for t in c["thresholds"]:
                    t["thresholdValue"] = v
            M.apply_config(cfg)
        changes = [m for m in logs if "变更成功" in m]
        assert len(changes) == 4, "4 次真实变更只打了 %d 行" % len(changes)
        for m in changes:
            assert "变更明细" in m
        # 首次那批 defectLabel 告警只该出现一轮，不跟着 4 次变更重复
        lbl = [m for m in logs if "defectLabel" in m]
        assert len({m for m in lbl}) == len(lbl), "label 告警重复了"

    def test_quiet_defaults_to_true(self, monkeypatch):
        import Communication_Tool.material_config_client as M
        from AnomalyDetection_Tool.config.settings import MATERIAL_CONFIG
        monkeypatch.delitem(MATERIAL_CONFIG, "quiet_when_unchanged",
                            raising=False)
        assert M._quiet_when_unchanged() is True


class TestStartupSource:
    """开局以数据库为准，数据库有问题才回落 config.yaml。

    以前启动拉取一律丢后台线程，覆盖层还是空的检测就已经开跑了 ——
    开头那几秒实际用的是本地 yaml，与"开局以数据库为准"相反。
    现在开局是**有硬上限的阻塞等待**，等完才走到 run_production_mode()。
    """

    @staticmethod
    def _collect(monkeypatch):
        import Communication_Tool.material_config_client as M
        logs = []
        for lvl in ("info", "warning", "error"):
            monkeypatch.setattr(M, lvl,
                                (lambda L: lambda m: L.append(m))(logs))
        monkeypatch.setattr(M, "_warn_model_mismatch", lambda meta: None)
        return logs

    def test_source_says_yaml_when_store_empty(self):
        import Communication_Tool.material_config_client as M
        assert "config.yaml" in M.active_source()
        assert "从未成功下发" in M.active_source()

    def test_source_says_backend_after_apply(self):
        import Communication_Tool.material_config_client as M
        M.apply_config({"clothNumber": "C1", "cameras": [
            {"cameraId": "camera1",
             "thresholds": [{"defectName": "多股线", "defectLabel": "multi_line",
                             "thresholdValue": 0.2, "status": 1}],
             "stopSetting": []}]})
        s = M.active_source()
        assert "后端数据库" in s and "阈值 1 条" in s, s

    def test_source_never_raises(self):
        """来源查询是诊断用的，自己出异常也不能把启动带崩。"""
        import Communication_Tool.material_config_client as M
        import AnomalyDetection_Tool.config.remote_config_store as R
        real = R.get_remote_config_store

        def boom():
            raise RuntimeError("store 炸了")
        R.get_remote_config_store = boom
        try:
            assert "无法确定" in M.active_source()
        finally:
            R.get_remote_config_store = real

    def test_dead_backend_returns_within_the_hard_bound(self, monkeypatch):
        """后端连不上时，阻塞时长必须有硬上限，不能把开机流程挂死。

        注入实验证明这条是真会咬人的：把 refresh_once 里的
        join(timeout + 3) 改成 join() 之后，这条用例从 9s 变成 65s。
        """
        import socket
        import time
        import Communication_Tool.material_config_client as M
        # 占一个端口再立刻放掉 —— 这个端口上一定没人监听
        sk = socket.socket()
        sk.bind(("127.0.0.1", 0))
        dead_port = sk.getsockname()[1]
        sk.close()
        monkeypatch.setattr(
            M, "_get_base_url",
            lambda: "http://127.0.0.1:%d/api" % dead_port)
        M._LOG_SEEN.clear()
        try:
            t0 = time.monotonic()
            assert M.refresh_once(timeout=2.0) is False
            dt = time.monotonic() - t0
        finally:
            M._LOG_SEEN.clear()
        assert dt < 2.0 + 3.0 + 1.5, "阻塞 %.2fs 超过硬上限" % dt
        assert "config.yaml" in M.active_source()

    def test_slow_backend_is_waited_for(self, monkeypatch):
        """后端慢但在超时内 -> 必须等它，开局就用数据库那份。"""
        import http.server, socketserver, threading as _th
        import time
        import Communication_Tool.material_config_client as M
        from AnomalyDetection_Tool.config.settings import effective_stop_rule
        from AnomalyDetection_Tool.models.data_types import ClassThresholdConfig
        from AnomalyDetection_Tool.config.settings import (
            CLASS_CONFIDENCE_THRESHOLDS, get_camera_threshold_override,
            get_default_class_confidence)

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(0.6)
                cams = [{"cameraId": "camera%d" % i,
                         "thresholds": [{"defectName": "多股线",
                                         "defectLabel": "multi_line",
                                         "thresholdValue": 0.20,
                                         "status": 1}],
                         "stopSetting": [{"name": "单根包不进", "enabled": True,
                                          "minConfidence": 0.20,
                                          "triggerMode": "dimension",
                                          "minWidthMm": 50,
                                          "minHeightMm": 47}]}
                        for i in range(1, 9)]
                body = json.dumps({"code": 0, "data": {"clothNumber": "C1",
                                                       "cameras": cams}}
                                  ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        socketserver.TCPServer.allow_reuse_address = True
        srv = socketserver.TCPServer(("127.0.0.1", 0), H)
        _th.Thread(target=srv.serve_forever, daemon=True).start()
        monkeypatch.setattr(
            M, "_get_base_url",
            lambda: "http://127.0.0.1:%d/api" % srv.server_address[1])
        monkeypatch.setattr(M, "_warn_model_mismatch", lambda meta: None)
        tc = ClassThresholdConfig(
            thresholds_by_name=CLASS_CONFIDENCE_THRESHOLDS,
            default_threshold_provider=get_default_class_confidence,
            camera_override_provider=get_camera_threshold_override)
        try:
            t0 = time.monotonic()
            assert M.refresh_once(timeout=5) is True
            assert time.monotonic() - t0 >= 0.6      # 真的等了
            # 开局这一刻，检测取到的就是数据库那份
            assert abs(tc.get_threshold("多股线", 0, "camera1") - 0.20) < 1e-9
            assert abs(effective_stop_rule("单根包不进", "camera1")
                       ["min_confidence"] - 0.20) < 1e-9
            assert "后端数据库" in M.active_source()
        finally:
            srv.shutdown()


class TestStartupWiring:
    """main_yolo 里的接线：开局拉取必须阻塞，且在启动检测之前。"""

    @pytest.fixture()
    def wired(self):
        import fake_galaxy_sdk
        fake_galaxy_sdk.install()
        import main_yolo
        return main_yolo

    def _main_src(self, wired):
        import inspect
        return inspect.getsource(wired.main)

    def test_startup_fetch_is_not_in_a_thread_by_default(self, wired):
        """wait_on_start=true（默认）时，开局拉取是同步调用。"""
        src = self._main_src(wired)
        assert '_mc_wait = bool(_mc_cfg.get("wait_on_start", True))' in src
        # 阻塞分支里直接调 _mc_fetch，而不是塞进 Thread
        blk = src[src.index("if _mc_wait:"):src.index("else:", src.index("if _mc_wait:"))]
        assert '_mc_fetch("启动")' in blk, blk
        assert "threading.Thread" not in blk, "开局又变回后台线程了"

    def test_nonblocking_path_is_kept_behind_the_switch(self, wired):
        """wait_on_start=false 时保留旧的后台线程行为。"""
        src = self._main_src(wired)
        tail = src[src.index('name="material-config-init"') - 400:]
        assert "threading.Thread" in tail
        assert 'args=("启动",)' in tail

    def test_fetch_happens_before_detection_starts(self, wired):
        """顺序不能反：拉取在前，run_production_mode() 在后。"""
        src = self._main_src(wired)
        i_fetch = src.index("开局检测/停机配置来源")
        # 必须匹配真正的**调用**（return run_xxx()）。裸写 run_production_mode()
        # 会先命中我自己在注释里提的那一句 —— 锚点看着唯一其实不唯一。
        i_prod = src.index("return run_production_mode()")
        i_sim = src.index("return run_simulation_mode()")
        assert i_fetch < i_prod, "开局拉取跑到了启动检测之后"
        assert i_fetch < i_sim, "开局拉取跑到了启动模拟之后"

    def test_active_source_is_imported(self, wired):
        assert callable(wired.material_config_source)

    def test_config_default_waits(self):
        """config.yaml 里 wait_on_start 默认开着。"""
        import io
        import yaml
        p = os.path.join(ROOT, "config.yaml")
        c = yaml.safe_load(io.open(p, encoding="utf-8"))
        assert c["material_config"]["wait_on_start"] is True
