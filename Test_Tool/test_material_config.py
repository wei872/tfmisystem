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


@pytest.fixture(autouse=True)
def _clean_store():
    RCS.reset_remote_config_store()
    yield
    RCS.reset_remote_config_store()


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

    def test_no_change_does_not_repeat_details(self, monkeypatch):
        """同样的配置再拉一次，不能再打一遍明细（否则 2s 一轮会刷屏）。"""
        import Communication_Tool.material_config_client as M
        M.apply_config(self._cams(0.10))
        logs = self._collect(monkeypatch)
        assert M.apply_config(self._cams(0.10)) is True
        out = "\n".join(logs["info"])
        assert "变化 0 项" in out
        assert "无需改动" in out
        assert "变更明细" not in out

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

    def test_detail_lines_are_capped(self, monkeypatch):
        """144 条阈值不能全打出来。"""
        import Communication_Tool.material_config_client as M
        many = {"clothNumber": "C1", "cameras": [
            {"cameraId": f"camera{i}",
             "thresholds": [{"defectName": "多股线", "defectLabel": "multi_line",
                             "thresholdValue": 0.10, "status": 1}],
             "stopSetting": []} for i in range(1, 41)]}
        logs = self._collect(monkeypatch)
        assert M.apply_config(many) is True
        out = "\n".join(logs["info"])
        assert "另有" in out and "项变化未列出" in out

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
