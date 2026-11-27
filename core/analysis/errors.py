"""Safe processing failures; never persist parser/provider exception strings."""


class AnalysisError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


MODEL_CITATION_FAILURE = (
    "The AI suggestions did not have verifiable source citations. "
    "Try again or choose fewer sources."
)

MODEL_VALIDATION_MESSAGES = {
    "chat_budget_exhausted": (
        "This question reached its model usage limit. Ask a narrower follow-up."
    ),
    "model_content_limit": (
        "The selected text exceeds the limit for this analysis. Choose fewer "
        "or shorter sources."
    ),
    "model_result_structure": (
        "The AI response did not have the expected format. Try again or choose "
        "fewer sources."
    ),
    "model_suggestion_citations": MODEL_CITATION_FAILURE,
    "model_citation_structure": MODEL_CITATION_FAILURE,
    "model_citation_source": MODEL_CITATION_FAILURE,
    "model_quote_offset_required": MODEL_CITATION_FAILURE,
    "model_quote_mismatch": MODEL_CITATION_FAILURE,
}


ERROR_MESSAGES = {
    "chat_agent_not_configured": (
        "Ask the operator to configure the Cloudflare investigation Worker."
    ),
    "chat_agent_cutover": "Ask a new question to use the Cloudflare agent.",
    "chat_agent_unavailable": (
        "The Cloudflare investigation service is unavailable. Retry this question."
    ),
    **MODEL_VALIDATION_MESSAGES,
    "imessage_invalid_format": (
        "This file does not match a supported message CSV profile. "
        "Use an 18-column iMessage or 15-column WhatsApp UTF-8 CSV."
    ),
    "imessage_multiple_chats": (
        "This export contains more than one chat label. "
        "Export one conversation and prepare a new review."
    ),
    "imessage_sender_limit": (
        "This group has more than 100 incoming senders in the selected dates. "
        "Review a shorter date range."
    ),
    "imessage_import_limit": (
        "This CSV exceeds the CSV import size limit or the limit of "
        "131,072 characters per field. There is no message-row limit. "
        "Review the file size and individual fields before trying again."
    ),
    "grouping_scope_changed": (
        "The conversation changed while its sessions were being grouped. "
        "Open the conversation and group the current messages again."
    ),
    "direct_messages_required": (
        "This analysis uses person-to-person messages. "
        "Choose a direct message or select a text analysis for group messages."
    ),
    "imessage_source_changed": (
        "The retained CSV no longer matches the reviewed original. "
        "Retain the correct source and prepare a new review."
    ),
    "imessage_conflict": (
        "An imported row or person association conflicts with this confirmation. "
        "Review the retained messages and selected people before preparing "
        "another import."
    ),
    "imessage_invalid_confirmation": (
        "This import no longer agrees with its prepared review. "
        "Return to the retained CSV and prepare a new review."
    ),
    "import_membership_unknown": (
        "This historical import does not record which messages were newly added. "
        "Open retained messages to run local checks individually, or use case checks."
    ),
    "retry_approval_required": (
        "Ask the case owner to review external analysis in Case settings, "
        "then start another run."
    ),
    "original_route_unavailable": "The service for this run is unavailable.",
    "retry_external_route_unavailable": (
        "The external service is unavailable or needs current case approval. "
        "Review External analysis in Case settings."
    ),
    "plate_image_required": "Choose one original JPEG or PNG image for plate search.",
    "plate_detector_unavailable": (
        "Local plate detection is unavailable. Ask the operator to sync the "
        "locked dependencies or rebuild the application and worker images."
    ),
    "execution_changed": (
        "Processing settings changed after this run was queued. Ask the operator "
        "to check the selected service and model, then start another run to "
        "record the current settings."
    ),
    # Keep historical result pages readable after retiring the old runtime.
    "local_ai_not_configured": "The former local AI service was unconfigured.",
    "local_ai_model_changed": "The former local model no longer matched its pin.",
    "local_ai_authentication": "The former local service rejected authorization.",
    "local_ai_busy": "The former local service was busy.",
    "local_ai_unavailable": "The former local service was unavailable.",
    "local_ai_invalid_input": "The former local service rejected the selected input.",
    "local_ai_invalid_response": "The local model response failed validation.",
    "local_ai_input_limit": "The request exceeded the former model input limit.",
    "local_ai_output_limit": "The response exceeded the former model output limit.",
    "local_ai_context_limit": "The request exceeded the former model context limit.",
    "ocr_not_installed": "The former OCR engine was unavailable.",
    "ocr_failed": "The former OCR engine could not complete this run.",
    "ocr_invalid_output": "The former OCR engine returned invalid regions.",
    "gateway_not_configured": (
        "External AI processing is not configured. Ask the operator to "
        "configure its credentials and supported models."
    ),
    "external_processing_not_approved": (
        "The case has no current approval for this external destination. "
        "Ask the case owner to review external analysis in Case settings, "
        "then start another run."
    ),
    "gateway_transcription_not_configured": (
        "External transcription is unavailable. Ask the operator to configure "
        "Workers AI for recordings."
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
        "This run stopped at the former daily AI request limit. "
        "Start another run after the case owner has approved the destination."
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
    "gateway_incomplete_response": (
        "The external AI response was incomplete. No result was published. "
        "Choose a smaller selection before starting another run."
    ),
    "gateway_refused": (
        "The external AI service declined this analysis. No result was published."
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
    "writing_evidence_changed": (
        "Case evidence changed while this result was being verified."
    ),
    "source_material_limit": (
        "The retained material exceeds this analysis's input limit. "
        "Choose fewer or shorter sources and start a new run."
    ),
    "source_unavailable": (
        "A selected original or exact extraction version cannot be read. "
        "Review its availability before retrying."
    ),
    "source_archived": (
        "A selected source was archived. Start a new analysis with active evidence."
    ),
    "chat_archive_scope_retired": "Ask a new question to use active evidence.",
    "chat_review_scope_retired": (
        "This review did not apply the requested filters. Ask the question again."
    ),
    "chat_literal_review_retired": (
        "This review may have counted field labels. Ask the question again."
    ),
    "sighting_comparison_superseded": (
        "A newer result replaced a selected comparison. Review the current "
        "evidence and start a new sighting group."
    ),
    "sighting_proposal_superseded": "A newer run replaced this sighting proposal.",
    "access_removed": (
        "The requesting account no longer has permission to process these "
        "sources. A current owner or editor can start a new run."
    ),
    "processing_version_unavailable": (
        "The queued method version is no longer available. Start a new run to "
        "record the current method."
    ),
    "geocode_review_required": (
        "Choose Use this estimate on the address lookup result before using "
        "it for spatial analysis."
    ),
}


def error_message(code):
    return ERROR_MESSAGES.get(
        code,
        "The selected inputs or processing service could not complete "
        "within the supported bounds.",
    )
