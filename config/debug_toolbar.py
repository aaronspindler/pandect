"""Keep toolbar diagnostics within the request that produced them."""

from debug_toolbar.panels.sql import SQLPanel
from debug_toolbar.store import BaseStore


class CurrentRequestStore(BaseStore):
    """Render inline from panel stats without retaining diagnostic snapshots."""

    @classmethod
    def exists(cls, request_id):
        return False

    @classmethod
    def set(cls, request_id):
        pass

    @classmethod
    def save_panel(cls, request_id, panel_id, data=None):
        pass


class CurrentRequestSQLPanel(SQLPanel):
    @classmethod
    def get_urls(cls):
        return []

    def generate_stats(self, request, response):
        super().generate_stats(request, response)
        for query in self.get_stats()["queries"]:
            # The SQL template hides replay controls when params is None.
            query["params"] = None
