"""Resolve and recheck the case owner's Cloudflare investigation grant."""

from core.analysis.errors import AnalysisError
from core.analysis.execution import (
    case_grant_revision,
    inference_configured,
    snapshot_execution,
    validate_case_execution_grant,
)
from core.analysis.gateway import GatewayClient

OPERATION = "evidence_chat"


def turn_execution(case):
    from .hosted_agent import agent_config

    if not case.external_ai_enabled:
        raise AnalysisError("external_processing_not_approved")
    if case.external_analysis_approval.get("agent_url") != agent_config()["url"]:
        raise AnalysisError("execution_changed")
    execution = snapshot_execution(OPERATION, {"backend": "gateway"}, case=case)
    if execution.get("location") != "external" or execution.get("url"):
        raise AnalysisError("gateway_not_configured")
    revision = case_grant_revision(case, OPERATION, execution)
    validate_case_execution_grant(case, OPERATION, execution, revision)
    if not inference_configured(execution):
        raise AnalysisError("gateway_not_configured")
    return execution, revision


def check_execution(turn, case):
    if turn_execution(case) != (turn.execution, turn.approval_revision):
        raise AnalysisError("execution_changed")


def infer(turn, authorize, prompt, schema):
    return GatewayClient(None, authorize, chat_turn=turn).infer(prompt, schema)
