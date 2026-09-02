"""Drop person.jira_account_id — Jira collection uses email only."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002_drop_jira_account_id"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("person_jira_account_id_key", "person", type_="unique")
    op.drop_column("person", "jira_account_id")


def downgrade() -> None:
    op.add_column("person", sa.Column("jira_account_id", sa.Text(), nullable=True))
    op.create_unique_constraint("person_jira_account_id_key", "person", ["jira_account_id"])
