"""Private Pandect investigation Worker. The callback origin is fixed on the Worker."""

import os

PANDECT_CLOUDFLARE_AGENT = {
    "url": os.environ.get("PANDECT_AGENT_URL", ""),
    "secret": os.environ.get("PANDECT_AGENT_SECRET", ""),
}
