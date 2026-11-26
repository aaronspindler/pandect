"""Server-owned destination and credentials for private case search instances."""

import os

PANDECT_CLOUDFLARE_SEARCH = {
    "account": os.environ.get("PANDECT_CLOUDFLARE_ACCOUNT_ID")
    or os.environ.get("PANDECT_AI_ACCOUNT_ID", ""),
    "namespace": os.environ.get("PANDECT_CLOUDFLARE_SEARCH_NAMESPACE", "production"),
    "token": os.environ.get("PANDECT_CLOUDFLARE_SEARCH_TOKEN", ""),
}
