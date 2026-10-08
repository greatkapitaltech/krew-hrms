"""
recruitment/services/

Business-logic layer for the Krew Recruitment module.

Views, API endpoints and background jobs all call into these services rather
than mutating models directly, so permission checks, company scoping,
lifecycle validation and business auditing happen in exactly one place
regardless of entry point. Mirrors the package layout already used by
krew_company_onboarding/services/.
"""
