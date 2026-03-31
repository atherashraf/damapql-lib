import sys
from sqlalchemy import inspect, text


class TableSchemaUpdater:
    def __init__(self, engine, logger=None):
        self.engine = engine
        self.logger = logger
        self.dialect = engine.dialect.name

    def log(self, msg, level="info"):
        if self.logger:
            fn = getattr(self.logger, level, None)
            if callable(fn):
                fn(msg)
                return
        print(f"[{level.upper()}] {msg}")

    def ask_user(self, question):
        """
        Strictly requires 'Y' to proceed.
        Anything else (y, yes, Y + space) is treated as 'No'.
        """
        if not sys.stdin.isatty():
            self.log("Non-interactive terminal; skipping restricted action.", level="warning")
            return False

        print(f"\n❓ {question}", end=" ")
        confirm = input("(Y/n): ")  # No .strip() or .lower() here
        return confirm == "Y"

    def refresh_inspector(self):
        self.inspector = inspect(self.engine)

    def get_table_names(self):
        self.refresh_inspector()
        return self.inspector.get_table_names()

    def get_columns_info(self, table_name):
        self.refresh_inspector()
        return {c["name"]: c for c in self.inspector.get_columns(table_name)}

    def column_type_sql(self, column):
        return column.type.compile(dialect=self.engine.dialect)

    def server_default_sql(self, column):
        if not column.server_default:
            return ""
        arg = column.server_default.arg
        if hasattr(arg, "text"):
            return f"DEFAULT {arg.text}"
        return f"DEFAULT {str(arg)}"

    def column_def_sql(self, column, include_name=False):
        parts = []
        if include_name:
            parts.append(f'"{column.name}"')
        parts.append(self.column_type_sql(column))
        parts.append("NULL" if column.nullable else "NOT NULL")
        default_sql = self.server_default_sql(column)
        if default_sql:
            parts.append(default_sql)
        return " ".join(parts)

    def create_table(self, table_obj):
        table_obj.create(self.engine)
        self.log(f"📦 Created new table: {table_obj.name}")

    def add_column(self, table_name, column):
        col_def = self.column_def_sql(column, include_name=False)
        sql = f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" {col_def}'
        with self.engine.begin() as conn:
            conn.execute(text(sql))
        self.log(f"✨ Added column [{column.name}] to {table_name}")

    def rename_column(self, table_name, old_name, new_name):
        if not self.ask_user(f"RENAME '{table_name}.{old_name}' to '{new_name}'?"):
            self.log(f"⏭️  Skipped rename for {old_name}")
            return False

        if self.dialect in ("postgresql", "sqlite"):
            sql = f'ALTER TABLE "{table_name}" RENAME COLUMN "{old_name}" TO "{new_name}"'
        elif self.dialect in ("mysql", "mariadb"):
            sql = f'ALTER TABLE `{table_name}` RENAME COLUMN `{old_name}` TO `{new_name}`'
        else:
            raise NotImplementedError(f"Rename not supported for {self.dialect}")

        with self.engine.begin() as conn:
            conn.execute(text(sql))
        self.log(f"🔁 Renamed column {table_name}.{old_name} -> {new_name}")
        return True

    def drop_column(self, table_name, column_name):
        if not self.ask_user(f"DROP column '{column_name}' from '{table_name}'?"):
            self.log(f"⏭️  Skipped drop for {column_name}")
            return False

        if self.dialect in ("mysql", "mariadb"):
            sql = f'ALTER TABLE `{table_name}` DROP COLUMN `{column_name}`'
        else:
            sql = f'ALTER TABLE "{table_name}" DROP COLUMN "{column_name}"'

        with self.engine.begin() as conn:
            conn.execute(text(sql))
        self.log(f"🔥 Dropped column {table_name}.{column_name}")
        return True

    def alter_nullable(self, table_name, column):
        if self.dialect == "postgresql":
            action = "DROP NOT NULL" if column.nullable else "SET NOT NULL"
            sql = f'ALTER TABLE "{table_name}" ALTER COLUMN "{column.name}" {action}'
        elif self.dialect in ("mysql", "mariadb"):
            null_stmt = "NULL" if column.nullable else "NOT NULL"
            sql = f'ALTER TABLE `{table_name}` MODIFY COLUMN `{column.name}` {self.column_type_sql(column)} {null_stmt}'
        else:
            return

        with self.engine.begin() as conn:
            conn.execute(text(sql))
        self.log(f"🛠 Updated nullable for {table_name}.{column.name}")

    def sync_table(self, table_obj, rename_map=None):
        rename_map = rename_map or {}
        inverse_rename_map = {v: k for k, v in rename_map.items()}
        table_name = table_obj.name

        existing_cols = self.get_columns_info(table_name)
        if not existing_cols:
            self.create_table(table_obj)
            return

        # 1. RENAMES (MUST BE FIRST)
        for column in table_obj.columns:
            new_name = column.name
            old_name = inverse_rename_map.get(new_name)

            # If the DB has the OLD name but NOT the NEW name
            if old_name and (old_name in existing_cols) and (new_name not in existing_cols):
                if self.rename_column(table_name, old_name, new_name):
                    # Refresh state immediately so 'Add Missing' knows it's done
                    existing_cols = self.get_columns_info(table_name)

        # 2. ADD MISSING
        for column in table_obj.columns:
            if column.name not in existing_cols:
                self.add_column(table_name, column)
                existing_cols = self.get_columns_info(table_name)

        # 3. NULLABLE
        for column in table_obj.columns:
            if column.name in existing_cols:
                db_null = existing_cols[column.name].get("nullable", True)
                if db_null != column.nullable:
                    self.alter_nullable(table_name, column)

        # 4. CLEANUP ORPHANS
        model_col_names = {c.name for c in table_obj.columns}
        for db_col in list(existing_cols.keys()):
            if db_col not in model_col_names:
                # This will ask to drop 'area_acers' if it wasn't renamed
                self.drop_column(table_name, db_col)