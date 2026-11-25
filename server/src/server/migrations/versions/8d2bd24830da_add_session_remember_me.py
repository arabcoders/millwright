"""
add session remember me

Revision ID: 8d2bd24830da
Revises: 312c7c63441f
Create Date: 2026-11-25 07:13:14.158428

"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "8d2bd24830da"
down_revision: str | Sequence[str] | None = "312c7c63441f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing sessions were issued with persistent cookies.
    op.add_column("sessions", sa.Column("remember_me", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.execute("UPDATE sessions SET remember_me = true")


def downgrade() -> None:
    op.drop_column("sessions", "remember_me")
