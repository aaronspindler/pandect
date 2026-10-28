"""Safe processing failures; never persist parser/provider exception strings."""


class AnalysisError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


ERROR_MESSAGES = {
    "gateway_not_configured": (
        "External AI processing is not configured. Ask the operator to "
        "configure its credentials and supported models."
    ),
    "gateway_authentication": (
        "External AI authorization failed. Ask the operator to check its "
        "credentials before retrying."
    ),
    "gateway_quota": (
        "The external AI service has no available quota. Ask the operator to "
        "review its account limits."
    ),
    "gateway_case_limit": (
        "This case reached the configured daily AI request limit. Try another "
        "day or ask the operator to review the limit."
    ),
    "gateway_rate_limit": (
        "The external AI service is busy. Wait before starting another run."
    ),
    "gateway_unavailable": (
        "The external AI service could not be reached within the allowed "
        "time. Try another run later."
    ),
    "gateway_provider_failure": (
        "The external AI service could not complete this request. Try another "
        "run after checking its availability."
    ),
    "gateway_invalid_response": (
        "The returned AI output did not pass validation. No unvalidated "
        "result was published."
    ),
    "gateway_input_limit": (
        "The selected content exceeds the configured AI request limit. Choose "
        "a smaller source selection."
    ),
    "gateway_output_limit": (
        "The AI response exceeded the allowed size. Choose a smaller source selection."
    ),
    "geocoder_not_configured": (
        "Geographic lookup is not configured. Ask the operator to configure a "
        "supported lookup service and its attribution."
    ),
    "geocoder_unavailable": (
        "The geographic lookup service is unavailable. Try another run later."
    ),
    "source_changed": (
        "A selected source differs from the retained processing input. Review "
        "the source and start a new selection."
    ),
    "source_unavailable": (
        "A selected original or exact extraction version cannot be read. "
        "Review its availability before retrying."
    ),
    "access_removed": (
        "The requesting account no longer has permission to process these "
        "sources. A current owner or editor can start a new run."
    ),
    "processing_version_unavailable": (
        "The queued method version is no longer available. Start a new run to "
        "record the current method."
    ),
    "geocode_review_required": (
        "A selected geographic estimate no longer has an accepted current "
        "review. Review that exact candidate before using it again."
    ),
}


def error_message(code):
    return ERROR_MESSAGES.get(
        code,
        "The selected inputs or processing service could not complete "
        "within the supported bounds.",
    )
