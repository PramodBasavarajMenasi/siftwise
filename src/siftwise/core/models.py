"""SQLAlchemy ORM models.

``runs``, ``changed_files`` and ``test_results`` are append-only raw ingest data. ``tests`` holds
each test's identity once per repo, so the ~29M-row ``test_results`` stores a 4-byte key
instead of the test ID text. ``test_stats`` is a rollup recomputed from them and may be rebuilt
at any time.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, column_property, mapped_column

from siftwise.core.junit import Status

# Deterministic constraint names so Alembic autogenerate and downgrades are stable.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


RUN_UNIQUE_INDEX = "uq_runs_repo_ci_run_attempt_variant"

# test_results.status is a smallint. Raw SQL uses these codes (see FAILED_CODES_SQL).
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

MESSAGE_MAX_BYTES = 4096
TRUNCATION_MARKER = "…[truncated]"


def truncate_utf8(value: str, max_bytes: int) -> str:
    """Fit ``value`` into ``max_bytes`` of UTF-8, ending with ``TRUNCATION_MARKER`` if cut.

    Never splits a multi-byte character.
    """
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    marker = TRUNCATION_MARKER.encode("utf-8")
    head = encoded[: max(max_bytes - len(marker), 0)].decode("utf-8", errors="ignore")
    return head + TRUNCATION_MARKER


class TruncatedText(TypeDecorator[str]):
    """TEXT that truncates on write. Applies to ORM and Core (incl. bulk) inserts."""

    impl = Text
    cache_ok = True

    def __init__(self, max_bytes: int) -> None:
        super().__init__()
        self.max_bytes = max_bytes

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        return None if value is None else truncate_utf8(value, self.max_bytes)


class StatusCode(TypeDecorator[Status]):
    """``Status`` stored as a smallint code (``STATUS_CODES``)."""

    impl = SmallInteger
    cache_ok = True

    def process_bind_param(self, value: Status | str | None, dialect: Dialect) -> int | None:
        return None if value is None else STATUS_CODES[Status(value)]

    def process_result_value(self, value: int | None, dialect: Dialect) -> Status | None:
        return None if value is None else STATUS_BY_CODE[value]


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Repo(Base):
    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index(None, "repo_id", "commit_sha"),
        Index(None, "repo_id", "is_main", "created_at"),
        # Idempotent ingest key, one row per matrix variant. NULL variants dedupe with each
        # other via COALESCE; a NULL ci_run_id never conflicts (Postgres NULLs are distinct).
        Index(
            RUN_UNIQUE_INDEX,
            "repo_id",
            "ci_run_id",
            "run_attempt",
            text("COALESCE(variant, '')"),
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    commit_sha: Mapped[str] = mapped_column(String(64))
    branch: Mapped[str] = mapped_column(String(255))
    is_main: Mapped[bool] = mapped_column(Boolean)
    ci_run_id: Mapped[str | None] = mapped_column(String(255))
    run_attempt: Mapped[int] = mapped_column(Integer, server_default="1")
    # Matrix leg of the CI run (e.g. "ubuntu-py3.12"); one row per variant. NULL = no matrix.
    variant: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # False when the uploader could not determine the diff (so changed_files is incomplete or
    # empty). Selection must then fall back to the full suite.
    changed_files_known: Mapped[bool] = mapped_column(Boolean, server_default=true())
    # What the uploader linked the change to (``schemas.ChangeGraph``: import graphs, declared
    # rules), for triage's "was this failure related to the run's change". NULL = not sent
    # (backfill, older uploads, a failed graph): relatedness unknown.
    affected: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))

    # When the run happened. Use this (never created_at alone) wherever "latest" matters:
    # backfill ingests old runs late, so created_at is ingest time, not run time.
    occurred_at: Mapped[datetime] = column_property(func.coalesce(started_at, created_at))


class ChangedFile(Base):
    __tablename__ = "changed_files"

    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    path: Mapped[str] = mapped_column(Text, primary_key=True)


class Test(Base):
    """A test's identity in a repo: ``test_results`` refer to it by ``id`` (the test key)."""

    __tablename__ = "tests"
    __test__ = False  # not a pytest test class
    __table_args__ = (UniqueConstraint("repo_id", "test_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    test_id: Mapped[str] = mapped_column(Text)
    # The latest file path reported for the test, by run time: ``file_path_at`` is that run's
    # time, so a backfilled older run never overwrites a newer path. Results don't keep their
    # own path.
    file_path: Mapped[str | None] = mapped_column(Text)
    file_path_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TestResult(Base):
    __tablename__ = "test_results"
    __test__ = False
    __table_args__ = (
        CheckConstraint(f"status BETWEEN 0 AND {max(STATUS_CODES.values())}", name="status"),
        # Failures only (a tiny fraction of rows): the selector's co-change and recently-failed
        # queries read failures through this, never the passing results around them.
        Index(
            "ix_test_results_failed",
            "run_id",
            "test_key",
            postgresql_where=text(f"status IN {FAILED_CODES_SQL}"),
        ),
        # The same, by test: triage looks up a few hundred tests' failures at once.
        Index(
            "ix_test_results_failed_by_test",
            "test_key",
            "run_id",
            postgresql_where=text(f"status IN {FAILED_CODES_SQL}"),
        ),
    )

    # Columns in storage order (8-byte, 4-byte, then 2-byte fields): 20 bytes plus the message.
    # The primary key's run_id prefix serves per-run lookups; test_key has its own index.
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    test_key: Mapped[int] = mapped_column(
        ForeignKey("tests.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    attempt: Mapped[int] = mapped_column(SmallInteger, primary_key=True, server_default="1")
    status: Mapped[Status] = mapped_column(StatusCode())
    message: Mapped[str | None] = mapped_column(TruncatedText(MESSAGE_MAX_BYTES))


class TestStats(Base):
    __tablename__ = "test_stats"
    __test__ = False
    __table_args__ = (Index(None, "repo_id", "last_seen_run_id"),)

    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), primary_key=True
    )
    test_id: Mapped[str] = mapped_column(Text, primary_key=True)
    runs: Mapped[int] = mapped_column(Integer, server_default="0")
    failures: Mapped[int] = mapped_column(Integer, server_default="0")
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    flaky_score: Mapped[float] = mapped_column(Float, server_default="0")
    # Earliest start of a current failing streak on main, across variants.
    broken_on_main_since_sha: Mapped[str | None] = mapped_column(String(64))
    avg_duration_ms: Mapped[float | None] = mapped_column(Float)
    # Which variants are broken on main: [{"variant": str | None, "since_sha": str}], oldest
    # streak first. NULL when not broken on any variant.
    broken_on_main_variants: Mapped[list[dict[str, str | None]] | None] = mapped_column(JSONB)
    # The test's most recent appearance (any status) in the window: the run's time, and one
    # run (variant) of the latest CI run it appeared in, by the selector's CI-run order. The
    # selector's "known tests" are those whose last-seen run is in the repo's latest CI runs.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_run_id: Mapped[int | None] = mapped_column(BigInteger)
    # Test file as reported in the window (latest non-null), for the selector's commands.
    file_path: Mapped[str | None] = mapped_column(Text)


# --- shadow mode ----------------------------------------------------------------------------

# Why a stored shadow selection was or wasn't compared (``shadow_selections.status``).
SHADOW_PENDING = "pending"  # no compare yet (in progress, or it never ran: see core/shadow.py)
SHADOW_COMPARED = "compared"
SHADOW_NO_UPLOAD = "no_upload"  # compared, but nothing was recorded for its variant
SHADOW_COMMIT_MISMATCH = "commit_mismatch"  # the recorded run tested another commit
SHADOW_COMPARE_ERROR = "compare_error"  # the server failed while comparing
SHADOW_STATUSES = (
    SHADOW_PENDING,
    SHADOW_COMPARED,
    SHADOW_NO_UPLOAD,
    SHADOW_COMMIT_MISMATCH,
    SHADOW_COMPARE_ERROR,
)
SHADOW_UNIQUE_INDEX = "uq_shadow_selections_repo_ci_run_attempt_variant"
# Why a failure was missed (core/shadow.py CAUSES, most specific first).
MISS_CAUSES = ("new_test", "flaky_history", "config_gap", "no_link", "unknown")


class ShadowSelection(Base):
    """What siftwise would have selected for a shadow run (one that ran the full suite), as
    ``selector.SelectionScope``: enough for ``WouldRun`` to tell whether a test would have run.
    One per CI run attempt and variant; a NULL variant applies to every leg."""

    __tablename__ = "shadow_selections"
    __table_args__ = (
        Index(
            SHADOW_UNIQUE_INDEX,
            "repo_id",
            "ci_run_id",
            "run_attempt",
            text("COALESCE(variant, '')"),
            unique=True,
        ),
        Index(None, "repo_id", "created_at"),
        CheckConstraint("mode IN ('full', 'selective')", name="mode"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in SHADOW_STATUSES) + ")", name="status"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    ci_run_id: Mapped[str] = mapped_column(String(255))
    run_attempt: Mapped[int] = mapped_column(Integer)
    variant: Mapped[str | None] = mapped_column(String(255))
    commit_sha: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    mode: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    # tests.id of the selected tests (all come from history), and the Go-runner subset of them.
    selected_keys: Mapped[list[int]] = mapped_column(ARRAY(Integer))
    go_selected_keys: Mapped[list[int]] = mapped_column(ARRAY(Integer))
    go_packages: Mapped[list[str]] = mapped_column(ARRAY(Text))
    python_files: Mapped[list[str]] = mapped_column(ARRAY(Text))
    selected_count: Mapped[int] = mapped_column(Integer)
    total_known: Mapped[int] = mapped_column(Integer)
    estimated_ms_total: Mapped[float] = mapped_column(Float)
    estimated_ms_would_run: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), server_default=SHADOW_PENDING)
    compared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ShadowResult(Base):
    """One recorded run (variant) of a shadow CI run attempt, compared with its selection.
    Flaky failures are counted apart: neither caught nor missed."""

    __tablename__ = "shadow_results"

    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    selection_id: Mapped[int] = mapped_column(
        ForeignKey("shadow_selections.id", ondelete="CASCADE"), index=True
    )
    compared_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    failing: Mapped[int] = mapped_column(Integer)
    caught: Mapped[int] = mapped_column(Integer)
    missed: Mapped[int] = mapped_column(Integer)
    flaky: Mapped[int] = mapped_column(Integer)
    # Measured in this (full-suite) run: final attempts' durations, Go subtests left out.
    tests_total: Mapped[int] = mapped_column(Integer)
    tests_would_run: Mapped[int] = mapped_column(Integer)
    ms_total: Mapped[float] = mapped_column(Float)
    ms_would_run: Mapped[float] = mapped_column(Float)


class ShadowFailure(Base):
    """Every failing test of a compared shadow run: whether the selection would have run it,
    and triage's verdict (flaky ones are not misses)."""

    __tablename__ = "shadow_failures"
    __table_args__ = (
        CheckConstraint(
            "cause IN (" + ", ".join(f"'{c}'" for c in MISS_CAUSES) + ")", name="cause"
        ),
    )

    run_id: Mapped[int] = mapped_column(
        ForeignKey("shadow_results.run_id", ondelete="CASCADE"), primary_key=True
    )
    test_key: Mapped[int] = mapped_column(
        ForeignKey("tests.id", ondelete="CASCADE"), primary_key=True
    )
    would_run: Mapped[bool] = mapped_column(Boolean)
    verdict: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[str] = mapped_column(String(16))
    # Misses only (NULL otherwise, and for misses compared before migration 0009): why it was
    # missed, the evidence in a line, and a [[depends]] rule to consider (config gaps).
    cause: Mapped[str | None] = mapped_column(String(32))
    cause_detail: Mapped[str | None] = mapped_column(Text)
    suggestion: Mapped[str | None] = mapped_column(Text)
