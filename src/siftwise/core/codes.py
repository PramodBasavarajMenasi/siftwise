"""Status codes of ``test_results.status`` (a smallint), for the ORM and raw SQL.

Pure, no SQLAlchemy: the light CLI imports the selector, which uses ``FAILED_CODES_SQL``.
``core.models`` re-exports these names.
"""

from siftwise.core.junit import Status

STATUS_CODES: dict[Status, int] = {
    Status.PASSED: 0,
    Status.FAILED: 1,
    Status.SKIPPED: 2,
    Status.ERROR: 3,
}
STATUS_BY_CODE = {code: status for status, code in STATUS_CODES.items()}
PASSED_CODE = STATUS_CODES[Status.PASSED]
SKIPPED_CODE = STATUS_CODES[Status.SKIPPED]
# "failed or error": both count as a failure everywhere. For raw SQL: ``status IN (1, 3)``.
FAILED_CODES_SQL = f"({STATUS_CODES[Status.FAILED]}, {STATUS_CODES[Status.ERROR]})"
