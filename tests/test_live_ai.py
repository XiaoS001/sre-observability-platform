"""AI 根因分析的 live 测试（真调模型，**默认 skip**）。

🔴 为什么要单独放一个文件：
   前面所有测试都把模型调用 mock 掉了 —— pytest 全程零 API 消耗、零费用、
   不依赖网络。但那样就没验证过「真接上模型能不能跑通」。

   所以留这一条 live 用例：它会真调一次模型，验证端到端链路。

运行方式：
    # 默认跳过（CI 与日常开发都走这条）
    venv\\Scripts\\python.exe -m pytest tests -q

    # 显式跑 live（需要先设好环境变量）
    set DEEPSEEK_API_KEY=sk-xxx
    venv\\Scripts\\python.exe -m pytest tests/test_live_ai.py -m live -q

不设 key 时它会 skip，而不是失败 —— 因为「没配 key」是一个合法的部署状态。
"""
import os

import pytest


pytestmark = pytest.mark.live


@pytest.fixture()
def require_api_key():
    """没有 key 就 skip（不是 fail）—— 没配 key 是合法状态。"""

    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        pytest.skip("未配置 DEEPSEEK_API_KEY，跳过 live 测试")
    return key


@pytest.fixture()
def live_opt_in(request):
    """🔴 双保险：即使机器上配了 key，普通跑也必须跳过 live。

    为什么需要这一层：`require_api_key` 只在「没有 key」时跳过。本机开发者
    很可能在环境变量里配好了 key（我就是），那么跑一次普通的
    `pytest tests` 就会真的调模型 —— 既花钱，又可能因为网络/额度问题
    让 CI 变红。

    所以再加一道：live 测试必须显式 `-m live` 才会真正执行。
    """

    if "live" not in request.config.getoption("-m", ""):
        pytest.skip("live 测试需显式指定 -m live 才会运行")


@pytest.fixture()
def live_env(live_opt_in, require_api_key):
    """两道门都过了才给 key。"""

    return require_api_key


def test_live_end_to_end_analysis(live_env):
    """真调一次模型，验证：模型通了 → 答案带引用 → 引用全部有效。

    这是整个项目里唯一会产生真实 API 消耗的测试。
    """

    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    import aiops.graph as graph_module
    from aiops.graph import analyze_incident
    from aiops_service import build_incident_payload
    from models import Alert, Target, db

    from app import app

    with app.app_context():
        target = Target(
            name="live-测试目标", url="https://live.example.test",
            timeout=5, interval_seconds=60, enabled=True,
        )
        db.session.add(target)
        db.session.commit()

        from datetime import datetime

        from models import CheckRecord

        db.session.add(CheckRecord(
            target_id=target.id, status_code=None, duration_ms=12.0,
            is_success=False, error_msg="连接失败: live test",
            checked_at=datetime.now(),
        ))
        db.session.commit()

        from models import AlertRule

        rule = AlertRule(
            target_id=target.id, name="live 规则",
            rule_type=AlertRule.RULE_CONSECUTIVE_FAILURE, threshold=2,
        )
        db.session.add(rule)
        db.session.commit()

        alert = Alert(
            rule_id=rule.id, target_id=target.id,
            message="[live-测试目标] 连接失败",
            observed_value=3.0, threshold=2.0, reason="connect",
        )
        db.session.add(alert)
        db.session.commit()

        incident = build_incident_payload(db.session.get(Alert, alert.id))

    result = analyze_incident(incident)

    # 不管模型成功与否，接口层面都不该抛 —— 这是本项目的硬约束
    assert result["ok"] is True
    assert result["answer"], "答案不应为空"

    # 🔴 不论走模型还是降级，答案里都不该有 Python 字面量
    assert "None" not in result["answer"]
    assert "null" not in result["answer"]

    # 引用必须全部有效 —— 编造的引用说明模型在编
    assert result["citation_check"]["invalid_citations"] == [], (
        f"检出无效引用（模型可能编造了证据）："
        f"{result['citation_check']['invalid_citations']}\n"
        f"答案：{result['answer'][:500]}"
    )

    # 如果走了降级，打印原因便于排查（live 环境常见：key 无效/额度不足）
    if result["degraded"]:
        pytest.skip(f"live 测试降级了，原因：{result['degradation_reasons']}")

    print(f"\n[live] 模型输出：\n{result['answer'][:500]}")
    print(f"[live] 引用：{result['citation_check']['citations']}")