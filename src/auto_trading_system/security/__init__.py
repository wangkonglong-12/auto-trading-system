"""Operating-system security controls for the Windows trading node."""

from auto_trading_system.security.operator_password import (
    OperatorPasswordHasher,
    parse_operator_password_verifier,
    validate_operator_password_verifier,
)
from auto_trading_system.security.operator_session import (
    AuthenticatedOperatorSession,
    IssuedOperatorSession,
    LocalOperatorSessionManager,
    OperatorSessionError,
    OperatorSessionFailureKind,
    OperatorSessionPolicy,
)
from auto_trading_system.security.storage import (
    SecureStorageError,
    SecureStorageResult,
    initialize_secure_storage,
)
from auto_trading_system.security.windows_acl import (
    AclHardeningError,
    DirectoryAclResult,
    WindowsDirectoryAclHardener,
)

__all__ = [
    "AclHardeningError",
    "AuthenticatedOperatorSession",
    "DirectoryAclResult",
    "IssuedOperatorSession",
    "LocalOperatorSessionManager",
    "OperatorPasswordHasher",
    "OperatorSessionError",
    "OperatorSessionFailureKind",
    "OperatorSessionPolicy",
    "SecureStorageError",
    "SecureStorageResult",
    "WindowsDirectoryAclHardener",
    "initialize_secure_storage",
    "parse_operator_password_verifier",
    "validate_operator_password_verifier",
]
