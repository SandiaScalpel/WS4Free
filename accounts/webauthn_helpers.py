from django_otp_webauthn.helpers import WebAuthnHelper
from webauthn.helpers.structs import ResidentKeyRequirement


class WS4FreeWebAuthnHelper(WebAuthnHelper):
    def get_discoverable_credentials_preference(self) -> ResidentKeyRequirement:
        # Passkeys are a second factor here, not passwordless login, so "required"
        # is unnecessarily strict. "preferred" lets Bitwarden and other browser-based
        # passkey managers register without a ConstraintError.
        return ResidentKeyRequirement.PREFERRED
