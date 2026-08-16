"""合并 spd 与附件两分支为单 head

Revision ID: 07029964f9bf
Revises: d2b3c4d5e6f7, e0f1a2b3c4d5
Create Date: 2026-08-16 02:23:27.891836

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '07029964f9bf'
down_revision: Union[str, Sequence[str], None] = ('d2b3c4d5e6f7', 'e0f1a2b3c4d5')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
