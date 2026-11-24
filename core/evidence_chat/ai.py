"""Use the selected case AI route for each request in a chat review."""

from core.analysis.errors import AnalysisError
from core.analysis.execution import (
    case_grant_revision,
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
    revision = case_grant_revision(case, OPERATION, execution)
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
        # Check the frozen evidence before contacting the runtime, then use
        # lightweight case and route checks for model metadata requests.
        authorize()
        client = LocalClient(
            authorize,
            turn.execution,
            authorize_metadata=lambda: authorize(metadata=True),
        )
    return client.infer(prompt, schema)
