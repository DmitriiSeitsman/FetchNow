"""Empty baseline migration.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-03 00:00:00.000000

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No schema yet; establishes Alembic version tracking."""
    pass


def downgrade() -> None:
    """No-op for empty baseline."""
    pass
