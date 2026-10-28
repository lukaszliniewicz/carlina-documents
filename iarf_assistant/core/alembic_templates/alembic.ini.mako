# A generic, single-database configuration for a Carlina add-on.
[alembic]
# path to migration scripts
script_location = .

# Template for version table name. The addon_id will be substituted.
# This ensures each addon has its own migration history table.
version_table = alembic_version_${addon_id}

# Other options like sqlalchemy.url are passed programmatically
# by the AddonManager, so they are not needed here.
