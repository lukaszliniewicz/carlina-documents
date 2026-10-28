"""Test helpers for explicitly binding hand-built Flask applications."""

from flask import Flask

from carlina.runtime_profiles import (
    ContentPolicy,
    RootProfileBinding,
    RuntimeProfile,
    RuntimeProfileError,
)


def bind_content_policy(
    app: Flask,
    *,
    profile: RuntimeProfile = RuntimeProfile.WORKBENCH,
    content_policy: ContentPolicy = ContentPolicy.INSTALLATION_ENVELOPE_V2,
    legacy: bool = True,
) -> None:
    """Bind a test app to the historical Workbench content policy.

    The legacy default preserves the EncryptedType compatibility of hand-built
    legacy fixtures; it is not a production fallback for missing root metadata.
    """

    binding = RootProfileBinding(profile, content_policy, legacy)
    extension_key = "carlina_profile_binding"
    if extension_key in app.extensions and app.extensions[extension_key] != binding:
        raise RuntimeProfileError(
            "test app already has an incompatible root profile binding"
        )
    app.extensions[extension_key] = binding
