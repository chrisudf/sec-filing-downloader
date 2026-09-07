# -*- coding: utf-8 -*-
"""VALUATION_JUDGMENT_CMD 感知 + 超时可配置（0016）。

回归对象：判断层走自定义命令时，本机 claude CLI 的一份陈旧 .credentials.json
会让 _pipeline 预检拦死所有任务、/api/valuation/auth 亮红灯——而 CLI 全程没被
调用。超时硬编码 600s 则让慢网关模式没有出路。
"""
import asyncio

import pytest

from app import valuation_service as vs


# ---------- _claude_timeout：环境覆盖与垃圾值防线 ----------

def test_timeout_default_when_unset(monkeypatch):
    monkeypatch.delenv("VALUATION_CLAUDE_TIMEOUT", raising=False)
    assert vs._claude_timeout() == 600


def test_timeout_env_override(monkeypatch):
    monkeypatch.setenv("VALUATION_CLAUDE_TIMEOUT", "900")
    assert vs._claude_timeout() == 900


@pytest.mark.parametrize("bad", ["nan", "inf", "abc", "-5", "0", "12.5", "  "])
def test_timeout_garbage_falls_back_with_warning(monkeypatch, capsys, bad):
    # 姊妹仓 boot-loop 教训：float('nan') 能过 float() 校验——这里 int() + 正数闸，
    # 垃圾值必须回默认值而不是把 NaN/负数塞进 wait_for
    monkeypatch.setenv("VALUATION_CLAUDE_TIMEOUT", bad)
    assert vs._claude_timeout() == 600
    if bad.strip():  # 全空白等价于未设置，不告警
        assert "VALUATION_CLAUDE_TIMEOUT" in capsys.readouterr().err


# ---------- 预检：自定义命令时跳过本机 CLI 凭证探测 ----------

def _boom():
    raise AssertionError("VALUATION_JUDGMENT_CMD 下不应触碰 _auth_state")


async def _run_sentinel(*a, **k):
    raise RuntimeError("SENTINEL-facts")


def test_pipeline_preflight_skipped_with_custom_cmd(monkeypatch, tmp_path):
    """env 设置时预检整体跳过：_auth_state 哪怕会爆炸也不影响，任务走到 facts 步。"""
    monkeypatch.setenv("VALUATION_JUDGMENT_CMD", "cat")
    monkeypatch.setattr(vs, "_auth_state", _boom)
    monkeypatch.setattr(vs, "_run", _run_sentinel)
    job = {"dir": str(tmp_path)}
    with pytest.raises(RuntimeError, match="SENTINEL-facts"):
        asyncio.run(vs._pipeline(job, "TEST", "a@b.c"))
    assert job["step"] == "facts"  # 预检已通过，死在我们注入的下一步


def test_pipeline_preflight_still_blocks_without_custom_cmd(monkeypatch, tmp_path):
    monkeypatch.delenv("VALUATION_JUDGMENT_CMD", raising=False)
    monkeypatch.setattr(vs, "_auth_state",
                        lambda: {"ok": False, "reason": "凭证已被清空"})
    monkeypatch.setattr(vs, "_run", _run_sentinel)
    job = {"dir": str(tmp_path)}
    with pytest.raises(vs.JudgmentAuthError, match="凭证已被清空"):
        asyncio.run(vs._pipeline(job, "TEST", "a@b.c"))


def test_auth_endpoint_reports_custom_cmd_state(monkeypatch):
    monkeypatch.setenv("VALUATION_JUDGMENT_CMD", "cat")
    monkeypatch.setattr(vs, "_auth_state", _boom)
    st = asyncio.run(vs.valuation_auth())
    assert st["ok"] is True
    assert "自定义命令" in st["reason"] and "无需本机登录" in st["reason"]
    assert st["can_popup"] is False


# ---------- _claude：自定义命令的失败不得译成「未登录」 ----------

def test_custom_cmd_failure_not_classified_as_auth(monkeypatch):
    """stderr 撞上 _AUTH_PAT（'not logged in'）也只是普通失败——前端的「登录」
    按钮修不了自定义命令，JudgmentAuthError 在这里是误导。"""
    monkeypatch.setenv("VALUATION_JUDGMENT_CMD",
                       "sh -c 'echo not logged in >&2; exit 1'")
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(vs._claude("x"))
    assert not isinstance(ei.value, vs.JudgmentAuthError)
    assert "判断层命令" in str(ei.value)
