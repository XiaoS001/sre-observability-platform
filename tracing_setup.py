"""OpenTelemetry tracing setup with isolated failure behavior."""

import logging
import os
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode


logger = logging.getLogger('rmb03.tracing')

_provider = None
_TRACES_SUFFIX = '/v1/traces'
_EXPORT_TIMEOUT_SECONDS = 2.0


def _trace_endpoint(endpoint):
    endpoint = endpoint.strip().rstrip('/')
    if endpoint.endswith(_TRACES_SUFFIX):
        return endpoint
    return endpoint + _TRACES_SUFFIX


def setup_tracing(app):
    """Configure tracing once for the process and instrument one Flask app.

    分两层容错：
      1) provider + exporter —— 这一层失败才算 setup 失败（返回 None）
      2) Flask 自动埋点     —— 单独处理，失败只降级记日志，**不影响 provider 可用**

    第 2 层为什么要单独处理：Flask 的 `instrument_app` 是往应用上注册 `before_request`，
    而 **Flask 拒绝在应用处理过第一个请求之后再注册**（会抛 AssertionError）。
    测试里共用 session 级 app、或运行期很晚才调用时都会遇到这种情况。
    这时自动埋点确实做不了，但**手工 span 照样能用** —— 不能因为埋点失败就把
    provider 一起丢掉。
    """

    global _provider

    endpoint = os.environ.get('RMB03_OTLP_ENDPOINT', '').strip()
    if not endpoint:
        return None

    try:
        provider = _provider
        if provider is None:
            current_provider = trace.get_tracer_provider()
            if isinstance(current_provider, TracerProvider):
                provider = current_provider
            else:
                provider = TracerProvider(
                    resource=Resource.create({'service.name': 'rmb03'}),
                )
                trace.set_tracer_provider(provider)
                current_provider = trace.get_tracer_provider()
                if isinstance(current_provider, TracerProvider):
                    provider = current_provider
            _provider = provider

        if not getattr(provider, '_rmb03_exporter_configured', False):
            exporter = OTLPSpanExporter(
                endpoint=_trace_endpoint(endpoint),
                timeout=_EXPORT_TIMEOUT_SECONDS,
            )
            processor = BatchSpanProcessor(exporter)
            provider.add_span_processor(processor)
            provider._rmb03_exporter_configured = True
            provider._rmb03_span_processor = processor
    except Exception:
        # Tracing is optional telemetry: setup failures must never stop RMB03.
        logger.exception('tracing_setup_failed', extra={
            'event': 'tracing_setup_failed',
        })
        return None

    if not getattr(app, '_is_instrumented_by_opentelemetry', False):
        try:
            FlaskInstrumentor().instrument_app(app)
        except Exception as exc:
            # 自动埋点做不了（例如 app 已经处理过请求）只是少了一层 HTTP 埋点，
            # 手工 span 仍然可用，所以这里降级为 warning，不污染 setup 结果。
            logger.warning('tracing_flask_instrument_failed', extra={
                'event': 'tracing_flask_instrument_failed',
                'error': str(exc)[:200],
            })

    return provider


@contextmanager
def safe_span(name):
    """Open a span without allowing tracing failures to escape to business code."""

    span_manager = None
    span = None
    try:
        span_manager = trace.get_tracer('rmb03').start_as_current_span(name)
        span = span_manager.__enter__()
    except Exception:
        logger.exception('tracing_span_start_failed', extra={
            'event': 'tracing_span_start_failed',
            'span_name': name,
        })
        yield None
        return

    try:
        yield span
    except BaseException as exc:
        try:
            span.set_status(Status(StatusCode.ERROR))
            span.record_exception(exc)
        except Exception:
            logger.exception('tracing_span_record_failed', extra={
                'event': 'tracing_span_record_failed',
                'span_name': name,
            })
        try:
            span_manager.__exit__(type(exc), exc, exc.__traceback__)
        except Exception:
            logger.exception('tracing_span_end_failed', extra={
                'event': 'tracing_span_end_failed',
                'span_name': name,
            })
        raise
    else:
        try:
            span_manager.__exit__(None, None, None)
        except Exception:
            logger.exception('tracing_span_end_failed', extra={
                'event': 'tracing_span_end_failed',
                'span_name': name,
            })


def set_span_attributes(span, attributes):
    """Set span attributes without letting telemetry errors affect business."""

    if span is None:
        return
    try:
        span.set_attributes(attributes)
    except Exception:
        logger.exception('tracing_attributes_failed', extra={
            'event': 'tracing_attributes_failed',
        })


def mark_span_error(span, reason):
    """Mark a failed business check as an error without raising to the caller."""

    if span is None:
        return
    try:
        span.set_status(Status(StatusCode.ERROR, reason))
    except Exception:
        logger.exception('tracing_status_failed', extra={
            'event': 'tracing_status_failed',
            'reason': reason,
        })
