"""Django system checks for processing adapter configuration."""

from django.core.checks import Error, register

from pandect.processing.remote.cloudflare import (
    CloudflareConfig,
    CloudflareConfigurationError,
)


@register()
def cloudflare_model_adapter_check(app_configs, **_kwargs):
    """Reject model IDs that do not match their implemented adapter schemas."""
    del app_configs
    config = CloudflareConfig.from_settings()
    try:
        config.validate_model_roles()
    except CloudflareConfigurationError:
        return [
            Error(
                "A Cloudflare model is configured for an incompatible adapter role.",
                hint=(
                    "Use a model supported by the text, vision, and transcription "
                    "adapter for each corresponding PANDECT_CLOUDFLARE_*_MODEL setting."
                ),
                id="processing.E001",
            )
        ]
    return []
