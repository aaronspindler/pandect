"""Safe processing failures; never persist parser/provider exception strings."""


class AnalysisError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


ERROR_MESSAGES = {
    "execution_changed": (
        "Processing settings changed after this run was queued. Ask the operator "
        "to check the selected service and model, then start another run to "
        "record the current settings."
    ),
    "local_ai_not_configured": (
        "Local AI processing is unavailable. Ask the operator to configure "
        "the private runtime and its verified model, then start another run."
    ),
    "local_ai_model_changed": (
        "The local runtime or model differs from this run's retained settings. "
        "No evidence was submitted to a replacement model. Ask the operator "
        "to check its configuration, then start another run."
    ),
    "local_ai_authentication": (
        "Local runtime authorization failed. Ask the operator to check "
        "its access token before starting another run."
    ),
    "local_ai_busy": (
        "The local runtime is busy or not ready. Wait before starting another run."
    ),
    "local_ai_unavailable": (
        "The local runtime could not finish within the allowed time. "
        "Ask the operator to check its availability and resources, then "
        "start another run. Evidence was not sent to a cloud service."
    ),
    "local_ai_invalid_input": (
        "The selected content is unsupported by the local runtime. "
        "Review the supported input profile before starting another run."
    ),
    "local_ai_invalid_response": (
        "The local AI output did not pass validation. No unvalidated "
        "result was published."
    ),
    "local_ai_input_limit": (
        "The selected content exceeds the local runtime request limit. "
        "Choose a smaller source selection."
    ),
    "local_ai_output_limit": (
        "The local runtime response exceeded the allowed size. "
        "Choose a smaller source selection."
    ),
    "local_ai_context_limit": (
        "The complete request and output allowance exceed the local model's "
        "context window. Choose fewer or shorter sources; content was not truncated."
    ),
    "ocr_not_installed": (
        "Local text extraction is unavailable. Ask the operator to install "
        "Tesseract with its English language data, then start another run."
    ),
    "ocr_failed": (
        "Local text extraction failed. Ask the operator to check Tesseract "
        "and its English language data, then start another run."
    ),
    "ocr_invalid_output": (
        "Local OCR returned invalid regions. No result was published."
    ),
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
    "map_context_not_configured": (
        "Map context is not configured. Ask the operator to configure its "
        "reference service and attribution. Local grouping remains available."
    ),
    "map_context_history_unavailable": (
        "The configured map source does not support the requested historical "
        "snapshot. Choose a supported date; current data was not substituted."
    ),
    "map_context_invalid_reference_time": (
        "Choose a valid map snapshot with a timezone, no later than now."
    ),
    "map_context_unavailable": (
        "The map reference service is unavailable. Completed reference lookups "
        "remain retained; start another run later for the unavailable areas."
    ),
    "map_context_rate_limited": (
        "The map reference service is busy. Wait before starting another run."
    ),
    "map_context_authentication": (
        "Map reference authorization failed. Ask the operator to check its service."
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
