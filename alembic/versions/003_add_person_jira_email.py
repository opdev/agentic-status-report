"""Add person.jira_email for per-person Jira collection."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "003_add_person_jira_email"
down_revision: Union[str, None] = "002_drop_jira_account_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("person", sa.Column("jira_email", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("person", "jira_email")
