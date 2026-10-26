"""Resume a case invitation after allauth sign-in or email verification."""

import re

from allauth.account.adapter import DefaultAccountAdapter


class PandectAccountAdapter(DefaultAccountAdapter):
    def pending_invitation(self, request):
        path = request.session.get("pending_invitation_path", "")
        if isinstance(path, str) and re.fullmatch(
            r"/invitations/[A-Za-z0-9_-]{1,128}/", path
        ):
            return path
        return None

    def get_login_redirect_url(self, request):
        return self.pending_invitation(request) or super().get_login_redirect_url(
            request
        )

    def get_email_verification_redirect_url(self, email_address):
        return self.pending_invitation(
            self.request
        ) or super().get_email_verification_redirect_url(email_address)
