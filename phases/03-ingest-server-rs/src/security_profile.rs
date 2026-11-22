//! Pure ingest security profile resolution and startup validation.
//!
//! This module defines the supported postures. Keeping this boundary free of
//! environment reads and service state lets startup and tests use one truth table.

/// The token docker-compose.yml falls back to when the operator has not set
/// one. Known-weak (it is committed), so its use is warned about at startup.
pub(crate) const DEV_DEFAULT_TOKEN: &str = "dev-ingest-token-change-me";
/// Release bearer credentials need enough room for generated entropy and must
/// survive an ASCII HTTP/2 metadata field unchanged. `make secrets` emits 64
/// lowercase hex characters, comfortably above this floor.
pub(crate) const MIN_RELEASE_TOKEN_BYTES: usize = 32;

/// The only supported ingest security postures.  This is deliberately a Rust
/// enum rather than a boolean because per-robot bearer authentication (`token`)
/// and the client-certificate topology (`mtls`) have different,
/// non-interchangeable invariants. The profile is the one source for every
/// derived setting below.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum IngestSecurityProfile {
    Dev,
    Token,
    Mtls,
}

impl IngestSecurityProfile {
    pub(crate) fn parse(value: &str) -> Result<Self, String> {
        match value {
            "dev" => Ok(Self::Dev),
            "token" => Ok(Self::Token),
            "mtls" => Ok(Self::Mtls),
            _ => Err(format!(
                "INGEST_SECURITY_PROFILE must be one of dev, token, mtls (got {value:?})"
            )),
        }
    }

    pub(crate) fn default_shared_token_policy(self) -> SharedTokenPolicy {
        match self {
            Self::Dev => SharedTokenPolicy::Allow,
            Self::Token | Self::Mtls => SharedTokenPolicy::Deny,
        }
    }

    pub(crate) fn requires_robot_certificate_identity(self) -> bool {
        matches!(self, Self::Mtls)
    }

    /// `TLS_MTLS` follows from the deployed profiles, so a release has one
    /// switch. Only `dev` reads the variable. An explicit value that
    /// contradicts `token` or `mtls` stops startup instead of being ignored.
    pub(crate) fn resolve_tls_mtls(self, configured: Option<&str>) -> Result<bool, String> {
        let requested = configured.map(|value| value.trim() == "true");
        let derived = match self {
            Self::Dev => return Ok(requested.unwrap_or(false)),
            Self::Token => false,
            Self::Mtls => true,
        };
        match requested {
            Some(requested) if requested != derived => Err(format!(
                "TLS_MTLS={} contradicts INGEST_SECURITY_PROFILE={}, which sets it to {derived}; remove TLS_MTLS",
                configured.unwrap_or_default().trim(),
                profile_name(self)
            )),
            _ => Ok(derived),
        }
    }
}

/// Whether the shared fleet credential may upload on the data plane.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum SharedTokenPolicy {
    Allow,
    Deny,
}

impl SharedTokenPolicy {
    pub(crate) fn parse(value: &str) -> Result<Self, String> {
        match value {
            "allow" => Ok(Self::Allow),
            "deny" => Ok(Self::Deny),
            _ => Err(format!(
                "INGEST_SHARED_TOKEN_POLICY must be one of allow, deny (got {value:?})"
            )),
        }
    }
}

/// Resolve `INGEST_SECURITY_PROFILE`. Unset means `dev` (the release Compose
/// file sets `token` unless the operator chooses `mtls`); any other value must
/// name one of the three profiles.
pub(crate) fn resolve_ingest_security_profile(
    configured_profile: Option<&str>,
) -> Result<IngestSecurityProfile, String> {
    configured_profile
        .map(IngestSecurityProfile::parse)
        .unwrap_or(Ok(IngestSecurityProfile::Dev))
}

/// Derive and validate the policy setting.  Keeping it separate from startup
/// I/O makes every profile cell unit-testable without mutating process env.
pub(crate) fn resolve_shared_token_policy(
    profile: IngestSecurityProfile,
    configured_policy: Option<&str>,
) -> Result<SharedTokenPolicy, String> {
    configured_policy
        .map(SharedTokenPolicy::parse)
        .unwrap_or_else(|| Ok(profile.default_shared_token_policy()))
}

/// A deployment secret in `token`/`mtls` is long enough to contain the
/// generated entropy and safe to carry as an HTTP/2 metadata value.  The
/// predicate intentionally rejects spaces and control characters too.
pub(crate) fn is_valid_release_secret(token: Option<&str>) -> bool {
    token.is_some_and(|token| {
        let token = token.trim();
        token.len() >= MIN_RELEASE_TOKEN_BYTES
            && token != DEV_DEFAULT_TOKEN
            && token.bytes().all(|byte| (0x21..=0x7e).contains(&byte))
    })
}

/// Return the first startup-policy violation for a resolved security
/// profile.  This function intentionally has no environment access so the
/// full truth table stays unit-testable.
pub(crate) fn profile_configuration_error(
    profile: IngestSecurityProfile,
    tls_enabled: bool,
    tls_mtls: bool,
    shared_token_policy: SharedTokenPolicy,
    auth_token: Option<&str>,
    provisioning_token: Option<&str>,
) -> Option<String> {
    if tls_mtls && !tls_enabled {
        return Some("TLS_MTLS=true requires TLS_ENABLED=true".to_string());
    }
    if matches!(profile, IngestSecurityProfile::Dev) {
        return None;
    }
    if !tls_enabled {
        return Some(format!(
            "INGEST_SECURITY_PROFILE={} requires TLS_ENABLED=true",
            profile_name(profile)
        ));
    }
    match profile {
        IngestSecurityProfile::Token if tls_mtls => {
            return Some("INGEST_SECURITY_PROFILE=token requires TLS_MTLS=false".to_string());
        }
        IngestSecurityProfile::Mtls if !tls_mtls => {
            return Some("INGEST_SECURITY_PROFILE=mtls requires TLS_MTLS=true".to_string());
        }
        _ => {}
    }
    if shared_token_policy != SharedTokenPolicy::Deny {
        return Some(format!(
            "INGEST_SECURITY_PROFILE={} requires INGEST_SHARED_TOKEN_POLICY=deny",
            profile_name(profile)
        ));
    }
    if !is_valid_release_secret(auth_token) {
        return Some(format!(
            "INGEST_SECURITY_PROFILE={} requires INGEST_AUTH_TOKEN to contain at least 32 visible ASCII characters and not use the committed development token",
            profile_name(profile)
        ));
    }
    if !is_valid_release_secret(provisioning_token) {
        return Some(format!(
            "INGEST_SECURITY_PROFILE={} requires INGEST_PROVISIONING_TOKEN to contain at least 32 visible ASCII characters",
            profile_name(profile)
        ));
    }
    if auth_token.map(str::trim) == provisioning_token.map(str::trim) {
        return Some(format!(
            "INGEST_SECURITY_PROFILE={} requires INGEST_PROVISIONING_TOKEN to differ from INGEST_AUTH_TOKEN",
            profile_name(profile)
        ));
    }
    None
}

pub(crate) fn profile_name(profile: IngestSecurityProfile) -> &'static str {
    match profile {
        IngestSecurityProfile::Dev => "dev",
        IngestSecurityProfile::Token => "token",
        IngestSecurityProfile::Mtls => "mtls",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_dev_token_and_mtls_are_profiles() {
        assert_eq!(
            resolve_ingest_security_profile(None),
            Ok(IngestSecurityProfile::Dev)
        );
        for (name, profile) in [
            ("dev", IngestSecurityProfile::Dev),
            ("token", IngestSecurityProfile::Token),
            ("mtls", IngestSecurityProfile::Mtls),
        ] {
            assert_eq!(resolve_ingest_security_profile(Some(name)), Ok(profile));
            assert_eq!(profile_name(profile), name);
        }
        // Only the three names resolve; other words, empty and wrong case do not.
        for other in ["tls", "production", "", "TOKEN"] {
            let error = resolve_ingest_security_profile(Some(other)).unwrap_err();
            assert!(error.contains("dev, token, mtls"), "{error}");
        }
    }

    #[test]
    fn tls_mtls_is_derived_from_the_deployed_profiles() {
        use IngestSecurityProfile::*;
        // `dev` reads the variable; anything but "true" is off.
        assert_eq!(Dev.resolve_tls_mtls(None), Ok(false));
        assert_eq!(Dev.resolve_tls_mtls(Some("true")), Ok(true));
        assert_eq!(Dev.resolve_tls_mtls(Some("false")), Ok(false));
        // `token` and `mtls` derive it; an agreeing value is accepted.
        assert_eq!(Token.resolve_tls_mtls(None), Ok(false));
        assert_eq!(Token.resolve_tls_mtls(Some("false")), Ok(false));
        assert_eq!(Mtls.resolve_tls_mtls(None), Ok(true));
        assert_eq!(Mtls.resolve_tls_mtls(Some("true")), Ok(true));
        // A contradicting value stops startup.
        assert!(Token
            .resolve_tls_mtls(Some("true"))
            .unwrap_err()
            .contains("remove TLS_MTLS"));
        assert!(Mtls.resolve_tls_mtls(Some("false")).is_err());
    }

    #[test]
    fn token_and_mtls_share_the_deployed_invariants() {
        let auth = "f".repeat(MIN_RELEASE_TOKEN_BYTES);
        let provisioning = "a".repeat(MIN_RELEASE_TOKEN_BYTES);
        for (profile, mtls) in [
            (IngestSecurityProfile::Token, false),
            (IngestSecurityProfile::Mtls, true),
        ] {
            let check = |tls, mtls, policy| {
                profile_configuration_error(
                    profile,
                    tls,
                    mtls,
                    policy,
                    Some(&auth),
                    Some(&provisioning),
                )
            };
            let deny = SharedTokenPolicy::Deny;
            assert_eq!(check(true, mtls, deny), None, "{profile:?}");
            assert_eq!(profile.default_shared_token_policy(), deny);
            assert!(check(false, false, deny).is_some(), "{profile:?} no TLS");
            assert!(check(true, !mtls, deny).is_some(), "{profile:?} wrong mTLS");
            let shared = check(true, mtls, SharedTokenPolicy::Allow);
            assert!(shared.is_some(), "{profile:?} shared token");
            let no_auth =
                profile_configuration_error(profile, true, mtls, deny, None, Some(&provisioning));
            assert!(no_auth.is_some(), "{profile:?} no auth token");
        }
        assert!(!IngestSecurityProfile::Token.requires_robot_certificate_identity());
        assert!(IngestSecurityProfile::Mtls.requires_robot_certificate_identity());
    }
}
