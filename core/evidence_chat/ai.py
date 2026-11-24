"""Use the selected case AI route for each request in a chat review."""

from core.analysis.errors import AnalysisError
from core.analysis.execution import (
    inference_configured,
    snapshot_execution,
    validate_case_execution_grant,
)
from core.analysis.gateway import GatewayClient
from core.analysis.local import LocalClient

OPERATION = "evidence_chat"


def turn_execution(case):
    execution = snapshot_execution(OPERATION, {"backend": case.ai_backend}, case=case)
    validate_route = execution["location"] == "external"
    revision = (
        case.external_analysis_approval.get("revision", "") if validate_route else ""
    )
    if not inference_configured(execution):
        raise AnalysisError(
            "gateway_not_configured" if validate_route else "local_ai_not_configured"
        )
    if validate_route:
        validate_case_execution_grant(case, OPERATION, execution, revision)
    return execution, revision


def check_execution(turn, case):
    # A new case backend choice applies to subsequent questions. The approved
    # destination, installation settings and grant for this turn remain pinned.
    current = snapshot_execution(
        OPERATION, {"backend": turn.execution["backend"]}, case=case
    )
    if current != turn.execution:
        raise AnalysisError("execution_changed")
    if current["location"] == "external":
        validate_case_execution_grant(case, OPERATION, current, turn.approval_revision)


def infer(turn, authorize, prompt, schema):
    if turn.execution["location"] == "external":
        client = GatewayClient(None, authorize, chat_turn=turn)
    else:
        client = LocalClient(authorize, turn.execution)
    return client.infer(prompt, schema)
