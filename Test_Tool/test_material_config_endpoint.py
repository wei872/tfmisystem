# -*- coding: utf-8 -*-
"""变更日志里必须带上「这份配置是从哪个接口拉的」。

对账时只看到"阈值变了"是不够的，还得知道是哪台服务、哪个地址给的 ——
后端挪过端口之后，日志继续报旧地址就是在撒谎。所以地址一律现算。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import Communication_Tool.material_config_client as M  # noqa: E402
from AnomalyDetection_Tool.config import remote_config_store as RCS  # noqa: E402

FULL = "GET http://localhost:8890/api/v1/defect/material/current-config"


@pytest.fixture(autouse=True)
def _clean():
    RCS.reset_remote_config_store()
    M._LOG_SEEN.clear()
    M._LABEL_WARNED.clear()
    yield
    RCS.reset_remote_config_store()
    M._LOG_SEEN.clear()
    M._LABEL_WARNED.clear()


def _collect(monkeypatch):
    logs = []
    for lvl in ("info", "warning", "error"):
        monkeypatch.setattr(M, lvl, (lambda L: lambda m: L.append(m))(logs))
    monkeypatch.setattr(M, "_warn_model_mismatch", lambda meta: None)
    return logs


def _cfg(v=0.12, ncam=2):
    return {"clothNumber": "C1", "roller": "R1", "material": "1500D/2",
            "specification": "100145",
            "cameras": [{"cameraId": "camera%d" % i,
                         "thresholds": [{"defectName": "多股线",
                                         "defectLabel": "multi_line",
                                         "thresholdValue": v, "status": 1}],
                         "stopSetting": [
                             {"name": "单根包不进", "enabled": True,
                              "minConfidence": v, "triggerMode": "dimension",
                              "minWidthMm": 50, "minHeightMm": 47}]}
                        for i in range(1, ncam + 1)]}


class TestEndpointString:
    def test_matches_the_confirmed_address(self):
        """现场确认过的完整地址，一个字都不能差。"""
        assert M._endpoint() == FULL

    def test_method_is_get_not_post(self):
        """这个接口是 GET。POST 是**另一个**接口 /api/image/upload 的方法。"""
        assert M.HTTP_METHOD == "GET"
        assert M._endpoint().startswith("GET ")

    def test_endpoint_is_base_plus_api_path(self):
        """地址必须等于基址 + API_PATH，不能是另一处写死的字符串。"""
        assert M._endpoint() == "GET " + M._get_base_url() + M.API_PATH

    def test_endpoint_follows_config(self, monkeypatch):
        """后端挪了端口，日志要跟着走 —— 写死就会继续报旧地址。"""
        from AnomalyDetection_Tool.config.settings import MATERIAL_CONFIG
        monkeypatch.setitem(MATERIAL_CONFIG, "api_port", 9999)
        assert M._endpoint() == (
            "GET http://localhost:9999/api/v1/defect/material/current-config")

    def test_endpoint_never_raises(self, monkeypatch):
        """地址解析失败也不能把变更日志带崩。"""
        def boom():
            raise RuntimeError("配置读不出来")
        monkeypatch.setattr(M, "_get_base_url", boom)
        out = M._endpoint()
        assert out.startswith("GET ") and "地址解析失败" in out


class TestChangeLogCarriesEndpoint:
    def test_change_log_includes_it(self, monkeypatch):
        logs = _collect(monkeypatch)
        assert M.apply_config(_cfg(0.12)) is True
        out = "\n".join(logs)
        assert "来源接口：" + FULL in out, out

    def test_endpoint_line_sits_right_after_the_headline(self, monkeypatch):
        """紧跟标题行，别埋在明细里 —— 明细可能有几十行。"""
        logs = _collect(monkeypatch)
        M.apply_config(_cfg(0.12))
        lines = logs[0].split("\n")
        assert lines[0].startswith("[材质配置] 变更成功")
        assert lines[1].strip() == "来源接口：" + FULL

    def test_no_change_log_stays_silent(self, monkeypatch):
        """没变更时一行都不打，自然也不打接口地址。"""
        M.apply_config(_cfg(0.12))
        logs = _collect(monkeypatch)
        M.apply_config(_cfg(0.12))
        assert logs == [], logs

    def test_each_change_reports_it_again(self, monkeypatch):
        """每次变更都要带，不能只带第一次。"""
        logs = _collect(monkeypatch)
        for v in (0.12, 0.15, 0.18):
            M.apply_config(_cfg(v))
        hits = [m for m in logs if "来源接口：" + FULL in m]
        assert len(hits) == 3, len(hits)


class TestFailureLogsCarryEndpoint:
    def test_empty_payload_warning(self, monkeypatch):
        logs = _collect(monkeypatch)
        assert M.apply_config({"clothNumber": "C1", "cameras": []}) is False
        assert any("变更失败" in m and FULL in m for m in logs), logs

    def test_parse_error(self, monkeypatch):
        logs = _collect(monkeypatch)
        assert M.apply_config("not a dict") is False
        assert any("解析异常" in m and FULL in m for m in logs), logs

    def test_source_log_when_store_empty(self, monkeypatch):
        logs = _collect(monkeypatch)
        M._log_source("后端未返回可用配置")
        assert any("config.yaml 本地配置" in m and FULL in m for m in logs), logs

    def test_source_log_when_store_populated(self, monkeypatch):
        M.apply_config(_cfg(0.12))
        logs = _collect(monkeypatch)
        M._log_source("拉取超时")
        assert any("上一次成功下发的配置" in m and FULL in m for m in logs), logs
