"""Legacy import path for plugin descriptor contracts."""
if not __package__:
    import _contract_path

from edge_contracts.descriptor import (
    FIELDS, GIT, KEYWORDS, MAX_JSON, NAME, NUM, PHYSICAL, PRE, REF, SCHEMA_TYPES, SEMVER, SHA,
    DescriptorError, _read, binding, canonical, compute_content_digest, load_descriptor,
    normalize_legacy_service, normalize_legacy_tool, resolve_schema, trusted_code,
    validate_descriptor, validate_instance,
)
