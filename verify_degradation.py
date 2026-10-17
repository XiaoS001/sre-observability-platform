"""四类降级的可复现验证脚本（阶段 6-4）。

用法：
    venv\\Scripts\\python.exe verify_degradation.py

四类降级：
    1. 无 API key          → 规则路径出答案
    2. 模型超时            → 规则路径 + degradation_reasons 记录超时
    3. HTTP 错误           → 规则路径 + degradation_reasons 记录 HTTP 状态
    4. 返回体异常          → 规则路径 + degradation_reasons 记录异常类型

四类都保证：
    · 接口返回 200（不是 500）
    · degraded=True
    · answer 有实质内容（不是「分析失败」了事）
    · answer 里没有 Python 字面量 None
"""
import sys

sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")

import aiops.graph as graph_module
from aiops.graph import analyze_incident


def _incident():
    """真实告警对应的 incident（借用 app 的构造逻辑）。"""

    from aiops_service import build_incident_payload
    from models import Alert, db

    from app import app
    with app.app_context():
        return build_incident_payload(db.session.get(Alert, 1))


def _check(name, result):
    """校验降级结果是否符合四要求。"""

    answer = result.get("answer", "")
    checks = {
        "HTTP 200（ok=True，不抛）": result.get("ok") is True,
        "degraded=True": result.get("degraded") is True,
        "answer 有内容": len(answer) > 50,
        "answer 含处置建议": "处置建议" in answer,
        "answer 无 None": "None" not in answer,
        "有降级原因": bool(result.get("degradation_reasons")),
    }
    print(f"  {'✅' if all(checks.values()) else '❌'} {name}")
    for label, ok in checks.items():
        print(f"       {'✓' if ok else '✗'} {label}")
    return all(checks.values())


def scenario_no_key(incident):
    print("\n=== 降级场景 1 · 无 API key ===")
    orig_key, orig_enabled = graph_module.config.DEEPSEEK_API_KEY, graph_module.config.AI_ENABLED
    graph_module.config.DEEPSEEK_API_KEY = ""
    graph_module.config.AI_ENABLED = True
    try:
        result = analyze_incident(incident)
        ok = _check("无 key 降级", result)
        print(f"  降级原因: {result['degradation_reasons']}")
        print("  --- 答案全文 ---")
        for line in result["answer"].splitlines():
            print(f"    {line}")
        return ok
    finally:
        graph_module.config.DEEPSEEK_API_KEY = orig_key
        graph_module.config.AI_ENABLED = orig_enabled


def _with_fake_key_and_broken_call(incident, exc, label):
    """有 key 但模型调用抛异常时的降级。"""

    print(f"\n=== 降级场景 · {label} ===")
    orig_key, orig_enabled = graph_module.config.DEEPSEEK_API_KEY, graph_module.config.AI_ENABLED
    orig_call = graph_module._call_model
    graph_module.config.DEEPSEEK_API_KEY = "fake-key-for-degradation-test"
    graph_module.config.AI_ENABLED = True

    def boom(_prompt, _vars, _key):
        raise exc

    graph_module._call_model = boom
    try:
        result = analyze_incident(incident)
        ok = _check(label, result)
        print(f"  降级原因: {result['degradation_reasons']}")
        print("  --- 答案全文 ---")
        for line in result["answer"].splitlines():
            print(f"    {line}")
        return ok
    finally:
        graph_module.config.DEEPSEEK_API_KEY = orig_key
        graph_module.config.AI_ENABLED = orig_enabled
        graph_module._call_model = orig_call


def main():
    print("=" * 70)
    print("RMB03 · 四类降级验证（全部 mock，无真实 API 调用）")
    print("=" * 70)

    incident = _incident()
    print(f"\nincident: target={incident.get('target_name')} "
          f"reason={incident.get('failure_reason')} id={incident.get('id')}")

    results = [
        scenario_no_key(incident),
        _with_fake_key_and_broken_call(
            incident, TimeoutError("Read timed out. (read timeout=60)"), "模型超时"),
        _with_fake_key_and_broken_call(
            incident, RuntimeError("HTTP 429 Too Many Requests"), "HTTP 错误"),
        _with_fake_key_and_broken_call(
            incident, ValueError("模型返回内容为空"), "返回体异常"),
    ]

    print("\n" + "=" * 70)
    if all(results):
        print("✅ 四类降级全部通过：无 500、有内容、无 None、有原因")
    else:
        print(f"❌ 有降级场景不合格：{results}")
    print("=" * 70)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())