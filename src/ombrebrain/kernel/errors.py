class OmbreError(Exception):
    code = "ombre_error"


class CapabilityLoadError(OmbreError):
    code = "capability_load_error"


class PolicyViolation(OmbreError):
    code = "policy_violation"
