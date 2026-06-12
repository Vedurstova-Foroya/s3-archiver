"""Domain errors for s3-archiver."""


class S3ArchiverError(Exception):
    """Base error for the application."""


class ConfigError(S3ArchiverError):
    """Raised when configuration is missing or invalid."""


class LoggingError(S3ArchiverError):
    """Raised when log handlers cannot be configured."""


class HealthCheckError(S3ArchiverError):
    """Raised when the S3 health check fails."""


class ArchiveRunError(S3ArchiverError):
    """Raised when an archive run completes with failed phases."""


class CleanupError(S3ArchiverError):
    """Raised when a cleanup run fails to delete and verify every object."""


class CleanupManifestError(S3ArchiverError):
    """Raised when a cleanup manifest is malformed, corrupted, or mangled."""
